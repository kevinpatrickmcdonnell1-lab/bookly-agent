"""Speech-to-text and text-to-speech. ElevenLabs for both directions (Scribe in, streamed TTS out),
called over plain REST with httpx2 (already installed by the anthropic SDK). FakeVoice backs the
no-key tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from urllib.parse import urlencode

from bookly.config import Settings

ELEVENLABS = "https://api.elevenlabs.io/v1"
REALTIME_WS = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


class Voice(Protocol):
    async def transcribe(self, audio: bytes, mime: str) -> str: ...
    async def realtime_url(self, max_seconds: int = 120) -> str: ...
    def synthesize(self, text: str) -> AsyncIterator[bytes]: ...


def _filename(mime: str) -> str:
    ext = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/ogg": "ogg",
           "audio/wav": "wav", "audio/x-m4a": "m4a"}.get(mime.split(";")[0].strip(), "webm")
    return f"speech.{ext}"


class ElevenLabsVoice:
    def __init__(self, settings: Settings, transport=None):
        import httpx2
        self.s = settings
        self.client = httpx2.AsyncClient(base_url=ELEVENLABS, timeout=30.0, transport=transport,
                                         headers={"xi-api-key": settings.elevenlabs_api_key})

    async def transcribe(self, audio: bytes, mime: str) -> str:
        # tag_audio_events off: we want the words, not "(laughs)" or "(background noise)".
        r = await self.client.post("/speech-to-text",
                                   files={"file": (_filename(mime), audio, mime.split(";")[0])},
                                   data={"model_id": self.s.elevenlabs_stt_model, "language_code": "en",
                                         "tag_audio_events": "false"})
        r.raise_for_status()
        return r.json().get("text", "")

    async def realtime_url(self, max_seconds: int = 120) -> str:
        """A WebSocket URL the browser can stream microphone audio to for live transcription. It
        carries a single-use token (expires in 15 minutes, consumed on connect), never the API key."""
        r = await self.client.post("/single-use-token/realtime_scribe",
                                   params={"max_session_duration_seconds": max_seconds})
        r.raise_for_status()
        query = urlencode({"model_id": self.s.elevenlabs_realtime_model, "token": r.json()["token"],
                           "audio_format": "pcm_16000", "language_code": "en", "commit_strategy": "manual"})
        return f"{REALTIME_WS}?{query}"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        async with self.client.stream("POST", f"/text-to-speech/{self.s.elevenlabs_voice_id}/stream",
                                      params={"output_format": "mp3_44100_128"},
                                      json={"text": text, "model_id": self.s.elevenlabs_tts_model}) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                yield chunk


class FakeVoice:
    """Deterministic stand-in: transcribe returns the queued transcripts in order."""

    def __init__(self, transcripts: list[str] | None = None):
        self.transcripts = list(transcripts or [])
        self.spoken: list[str] = []

    async def transcribe(self, audio: bytes, mime: str) -> str:
        return self.transcripts.pop(0) if self.transcripts else ""

    async def realtime_url(self, max_seconds: int = 120) -> str:
        return f"{REALTIME_WS}?token=fake"

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        yield b"ID3"
        yield text.encode()


def build_voice(settings: Settings) -> Voice | None:
    return ElevenLabsVoice(settings) if settings.voice_enabled else None
