"""Voice around the unchanged text pipeline: speech -> transcript -> normalise -> Agent.run_turn ->
guarded reply -> speakable() -> speech. Nothing here decides anything; it only changes how text is
written so the existing parsers and guards see what they expect."""

from bookly.voice.normalize import normalize_transcript
from bookly.voice.speakable import speakable

__all__ = ["normalize_transcript", "speakable"]
