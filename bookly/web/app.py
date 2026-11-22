"""FastAPI app: one-page chat with a live trace panel, scenario cards and the Agent Inbox."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from bookly.agent.loop import Agent
from bookly.agent.state import Session
from bookly.config import get_settings
from bookly.data import seed
from bookly.data.repo import Repo

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Warm-up: build the agent and open the API connection at startup, so the first
    # customer doesn't wait for SDK import, client setup and TLS (measured ~10 s cold).
    # models.retrieve is a free metadata call.
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            a = agent()
            await a.llm.client.models.retrieve(_settings.router_model)
        except Exception as exc:  # noqa: BLE001 - warm-up is best effort
            print(f"warm-up skipped: {exc!r}")
    yield


app = FastAPI(title="Bookly support agent", lifespan=lifespan)
INDEX = Path(__file__).with_name("index.html")

_settings = get_settings()
_repo = Repo(_settings.db_path)
_repo.ensure()
_agent: Agent | None = None
_sessions: dict[str, Session] = {}


def agent() -> Agent:
    global _agent
    if _agent is None:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise HTTPException(500, "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")
        from bookly.runtime import build_agent
        _agent = build_agent(_settings, _repo)
    return _agent


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX.read_text()


@app.get("/api/scenarios")
def scenarios() -> dict:
    return {"scenarios": seed.SCENARIOS, "executor": _settings.executor_preset,
            "models": {"guard": _settings.guard_model, "router": _settings.router_model,
                       "executor_returns": _settings.executor_model_for("returns"),
                       "executor_other": _settings.executor_model_for("order_status")}}


@app.post("/api/session")
def new_session() -> dict:
    s = Session()
    _sessions[s.id] = s
    return {"session_id": s.id}


@app.post("/api/chat")
async def chat(body: ChatIn) -> dict:
    s = _sessions.get(body.session_id)
    if s is None:
        raise HTTPException(404, "Unknown session; start a new conversation.")
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message.")
    if len(text) > 2000:
        raise HTTPException(400, "Message too long.")
    result = await agent().run_turn(s, text)
    return {"reply": result.reply, "route": result.route, "turn": s.turn, "events": result.events,
            "ticket_id": result.ticket_id, "state": s.snapshot()}


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn) -> StreamingResponse:
    """Same turn as /api/chat, streamed as NDJSON: one line per trace event as it
    happens (so the UI can show progress), then a final "done" line with the reply."""
    s = _sessions.get(body.session_id)
    if s is None:
        raise HTTPException(404, "Unknown session; start a new conversation.")
    text = body.message.strip()
    if not text or len(text) > 2000:
        raise HTTPException(400, "Message must be 1-2000 characters.")
    a = agent()
    queue: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            result = await a.run_turn(s, text, on_event=lambda e: queue.put_nowait({"kind": "event", "event": e}))
            queue.put_nowait({"kind": "done", "reply": result.reply, "route": result.route, "turn": s.turn,
                              "ticket_id": result.ticket_id, "state": s.snapshot()})
        except Exception as exc:  # noqa: BLE001
            queue.put_nowait({"kind": "error", "detail": f"{type(exc).__name__}: {exc}"})

    async def lines():
        task = asyncio.create_task(run())
        while True:
            item = await queue.get()
            yield json.dumps(item, default=str) + "\n"
            if item["kind"] in ("done", "error"):
                break
        await task

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/api/tickets")
def tickets() -> dict:
    return {"tickets": _repo.tickets()}


@app.get("/api/returns")
def returns() -> dict:
    return {"returns": _repo.returns()}


@app.post("/api/reset")
def reset() -> dict:
    _repo.reset()
    _sessions.clear()
    return {"ok": True}
