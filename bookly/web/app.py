"""FastAPI app: one-page chat with a live trace panel, scenario cards, the Agent Inbox and the eval dashboard."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from bookly.agent.loop import Agent
from bookly.agent.state import Session
from bookly.config import get_settings
from bookly.data import seed
from bookly.data.repo import Repo
from bookly.voice import normalize_transcript, speakable
from bookly.voice.providers import Voice, build_voice
from bookly.web import evals_data

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
    return {"scenarios": seed.SCENARIOS, "executor": _settings.executor_preset, "voice": _settings.voice_enabled,
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


# ---- voice ------------------------------------------------------------------------
# Speech in -> normalised transcript -> the client sends it to /api/chat/stream like typed text, so
# every guard runs unchanged -> /api/voice/speak reads back the session's last approved reply.

_voice: Voice | None = None


def voice() -> Voice:
    global _voice
    if _voice is None:
        _voice = build_voice(_settings)
        if _voice is None:
            raise HTTPException(503, "Voice is off. Set ELEVENLABS_API_KEY in .env to turn it on.")
    return _voice


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request, session_id: str) -> dict:
    """Raw audio body (the browser's MediaRecorder blob) -> {"text": what to send as the message}."""
    if session_id not in _sessions:
        raise HTTPException(404, "Unknown session; start a new conversation.")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "No audio received.")
    if len(audio) > _settings.max_audio_bytes:
        raise HTTPException(413, "Recording too long; keep it under a minute.")
    try:
        raw = await voice().transcribe(audio, request.headers.get("content-type", "audio/webm"))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Transcription failed: {type(exc).__name__}") from exc
    return {"text": normalize_transcript(raw)[:2000], "raw": raw}


class SessionIn(BaseModel):
    session_id: str


@app.post("/api/voice/live")
async def voice_live(body: SessionIn) -> dict:
    """WebSocket URL (with a single-use token) for live transcription in the browser."""
    if body.session_id not in _sessions:
        raise HTTPException(404, "Unknown session; start a new conversation.")
    try:
        return {"url": await voice().realtime_url()}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Live transcription unavailable: {type(exc).__name__}") from exc


class TextIn(BaseModel):
    text: str


@app.post("/api/voice/normalize")
def voice_normalize(body: TextIn) -> dict:
    """The same spoken-identifier clean-up the batch path does, for a live transcript."""
    return {"text": normalize_transcript(body.text)[:2000]}




@app.post("/api/voice/speak")
async def voice_speak(body: SessionIn) -> StreamingResponse:
    s = _sessions.get(body.session_id)
    if s is None:
        raise HTTPException(404, "Unknown session; start a new conversation.")
    if not s.last_reply:
        raise HTTPException(409, "Nothing to say yet.")
    chunks = voice().synthesize(speakable(s.last_reply)).__aiter__()
    try:  # Pull the first chunk here so an upstream failure is a 502, not a broken stream.
        first = await chunks.__anext__()
    except StopAsyncIteration:
        first = b""
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Speech failed: {type(exc).__name__}") from exc

    async def audio():
        yield first
        async for c in chunks:
            yield c

    return StreamingResponse(audio(), media_type="audio/mpeg")


@app.get("/api/tickets")
def tickets() -> dict:
    return {"tickets": _repo.tickets()}


@app.get("/api/returns")
def returns() -> dict:
    return {"returns": _repo.returns()}


@app.get("/api/evals")
def evals() -> dict:
    return {"runs": evals_data.list_runs()}


@app.get("/api/evals/{run_id}")
def eval_detail(run_id: str) -> dict:
    detail = evals_data.run_detail(run_id)
    if detail is None:
        raise HTTPException(404, "No such eval run.")
    return detail


@app.post("/api/reset")
def reset() -> dict:
    _repo.reset()
    _sessions.clear()
    return {"ok": True}
