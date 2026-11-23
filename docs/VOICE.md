# Bookly agent: voice mode

Optional. The text agent works without it.


Add `ELEVENLABS_API_KEY` to `.env` and a mic button appears next to Send (Chrome, Edge or Safari). Tap to talk, and your words appear in the message box as you speak. Tap again to send. Speech goes through the same pipeline as typed text:

```
audio ─► live transcript (ElevenLabs Scribe realtime) ─► normalize_transcript ─► /api/chat/stream (every guard, unchanged) ─► speakable ─► speech (ElevenLabs)
```

- `normalize_transcript` turns "forty-eight oh one" into `4801` and "ana dot ruiz at example dot com" into `ana.ruiz@example.com`, so the router's order-number rule and verification work on speech. It leaves ordinary words like "one or two" and "I'm at home" alone.
- Live transcription streams microphone audio from the browser straight to ElevenLabs. It uses a single-use token from `/api/voice/live`, so the API key never reaches the browser. If the live connection fails, the mic falls back to recording the whole message and transcribing it in one go.
- `/api/voice/speak` takes only a session id and reads that session's last guarded reply. It never accepts text from the caller, so nothing gets spoken that didn't pass the output guards.
- `speakable` only changes formatting. It reads tracking numbers in groups of four and masked emails as "the address starting with a". A test checks it never adds, drops or reorders a digit.

Limits:
- Spoken email verification depends on how accurately the speech is transcribed.
- Replies play only after the whole audio clip is generated. There's no streaming playback and no filler while tools run.
- Latency per stage hasn't been measured yet.
