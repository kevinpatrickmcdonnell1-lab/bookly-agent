"""LLMClient: a provider-agnostic interface, plus the Anthropic implementation.

Only two shapes of call exist in the system:
  * structured(): one-shot classification returning JSON that matches a schema
    (input guard, router, output supervisor, eval judge)
  * chat(): the executor's multi-turn tool-use call
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from bookly.config import SONNET, cost_usd


@dataclass
class LLMCall:
    model: str
    content: list[dict[str, Any]]           # response blocks as API params, ready to append to history
    stop_reason: str | None
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.input_tokens, self.output_tokens, self.cache_read, self.cache_write)

    def trace_fields(self) -> dict[str, Any]:
        return {"model": self.model, "latency_ms": self.latency_ms, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": round(self.cost_usd, 6)}


class LLMClient(Protocol):
    async def structured(self, *, model: str, system: str, user: str, schema: dict,
                         max_tokens: int = 1024, effort: str = "low") -> tuple[dict, LLMCall]: ...

    async def chat(self, *, model: str, system: str, tools: list[dict], messages: list[dict],
                   effort: str = "low", max_tokens: int = 4096) -> LLMCall: ...


def _usage(resp: Any) -> dict[str, int]:
    u = resp.usage
    return {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}


class AnthropicLLM:
    """Raw Messages API. No agent framework: the loop lives in agent/loop.py."""

    def __init__(self, fallbacks: str = "default"):
        import anthropic  # imported here so the no-key test suite never needs it configured

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=2, timeout=60.0)
        self.fallbacks = fallbacks

    async def structured(self, *, model: str, system: str, user: str, schema: dict,
                         max_tokens: int = 1024, effort: str = "low") -> tuple[dict, LLMCall]:
        kwargs: dict[str, Any] = {}
        if model.startswith("claude-haiku"):
            # Classification is narrow and runs every turn: no thinking on Haiku.
            # (Opus 5.5 and Sonnet 5.5 reject disabled thinking; they run adaptive.)
            kwargs["thinking"] = {"type": "disabled"}
        t0 = time.perf_counter()
        resp = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            # Sampling params like temperature are rejected on the 5.5 models; the
            # JSON schema is what makes the output deterministic in shape.
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        )
        call = LLMCall(model=model, content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
                       stop_reason=resp.stop_reason, latency_ms=int((time.perf_counter() - t0) * 1000),
                       **_usage(resp))
        if resp.stop_reason == "refusal":
            raise RuntimeError("classifier refused")
        return json.loads(call.text), call

    async def chat(self, *, model: str, system: str, tools: list[dict], messages: list[dict],
                   effort: str = "low", max_tokens: int = 4096) -> LLMCall:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            # Brand voice + tools are frozen for the whole session: cached, and
            # thinking blocks in history stay valid. Per-turn state arrives as
            # role:"system" messages appended to `messages`.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            cache_control={"type": "ephemeral"},     # also cache the growing conversation
            output_config={"effort": effort},
        )
        if model == SONNET and self.fallbacks == "default":
            # Server-side refusal fallback: a declined request is re-run on a
            # fallback model inside the same call.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        t0 = time.perf_counter()
        resp = await self.client.beta.messages.create(**kwargs)
        return LLMCall(model=model, content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
                       stop_reason=resp.stop_reason, latency_ms=int((time.perf_counter() - t0) * 1000),
                       extra={"served_by": resp.model}, **_usage(resp))
