"""All tunables in one place. Every value can be overridden from the environment or .env.

Model names are config, not code: a reviewer can rerun the whole system on another
model by changing one line in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines). Real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"

# Executor presets compared by `make compare-models`.
# Keys are AOP names; "default" covers any AOP not listed.
EXECUTOR_PRESETS: dict[str, dict[str, str]] = {
    "sonnet": {"default": SONNET},
    "haiku": {"default": HAIKU},
    "hybrid": {
        "default": HAIKU,
        "returns": SONNET,
    },
}

# USD per million tokens: (input, output, cache_read, cache_write_5m).
# Source: Anthropic pricing as of 2026-11. Update here if prices change.
PRICING: dict[str, tuple[float, float, float, float]] = {
    HAIKU: (0.10, 0.50, 0.01, 0.125),
    SONNET: (2.00, 10.00, 0.10, 2.50),
    OPUS: (4.00, 20.00, 0.20, 5.00),
}


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("BOOKLY_DB", str(ROOT / "bookly.db"))))

    guard_model: str = field(default_factory=lambda: _env("GUARD_MODEL", HAIKU))
    router_model: str = field(default_factory=lambda: _env("ROUTER_MODEL", HAIKU))
    supervisor_model: str = field(default_factory=lambda: _env("SUPERVISOR_MODEL", HAIKU))
    judge_model: str = field(default_factory=lambda: _env("JUDGE_MODEL", OPUS))

    # Executor: a preset, optionally overridden for every route by EXECUTOR_MODEL.
    executor_preset: str = field(default_factory=lambda: _env("EXECUTOR_PRESET", "sonnet"))
    executor_model_override: str = field(default_factory=lambda: os.environ.get("EXECUTOR_MODEL", "").strip())
    executor_effort: str = field(default_factory=lambda: _env("EXECUTOR_EFFORT", "low"))
    # Server-side refusal fallback for Sonnet 5.5 executors ("default" or "off").
    executor_fallbacks: str = field(default_factory=lambda: _env("EXECUTOR_FALLBACKS", "default"))

    # Loop limits
    max_tool_rounds: int = field(default_factory=lambda: int(_env("MAX_TOOL_ROUNDS", "6")))
    max_gate_denials: int = field(default_factory=lambda: int(_env("MAX_GATE_DENIALS", "3")))
    max_tool_errors: int = field(default_factory=lambda: int(_env("MAX_TOOL_ERRORS", "2")))
    max_verify_attempts: int = 3

    # Router thresholds
    clarify_below: float = 0.6
    switch_aop_at: float = 0.8

    # Action limits (also used by the policy engine)
    approval_cap_cents: int = field(default_factory=lambda: int(_env("APPROVAL_CAP_CENTS", "7500")))
    max_returns_90d_without_approval: int = 3

    # Escalation
    reply_time_hours: int = 4
    business_open_hour_et: int = 9
    business_close_hour_et: int = 18

    # Voice (optional). Speech-to-text and text-to-speech are ElevenLabs; set the key to show the mic.
    elevenlabs_api_key: str = field(default_factory=lambda: os.environ.get("ELEVENLABS_API_KEY", "").strip(),
                                    repr=False)
    # Default voice is "George", one of ElevenLabs' premade voices; any Voice Library id works.
    elevenlabs_voice_id: str = field(default_factory=lambda: _env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"))
    elevenlabs_tts_model: str = field(default_factory=lambda: _env("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"))
    elevenlabs_stt_model: str = field(default_factory=lambda: _env("ELEVENLABS_STT_MODEL", "scribe_v1"))
    # Live transcription while the customer talks (browser streams straight to ElevenLabs with a
    # single-use token; the API key never leaves the server).
    elevenlabs_realtime_model: str = field(default_factory=lambda: _env("ELEVENLABS_REALTIME_MODEL",
                                                                        "scribe_v2_realtime"))
    max_audio_bytes: int = 5 * 1024 * 1024

    @property
    def voice_enabled(self) -> bool:
        return bool(self.elevenlabs_api_key)

    def executor_model_for(self, aop: str) -> str:
        if self.executor_model_override:
            return self.executor_model_override
        preset = EXECUTOR_PRESETS.get(self.executor_preset, EXECUTOR_PRESETS["sonnet"])
        return preset.get(aop, preset["default"])


def get_settings() -> Settings:
    return Settings()


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    price = PRICING.get(model)
    if not price:
        return 0.0
    p_in, p_out, p_read, p_write = price
    return (input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_write) / 1_000_000
