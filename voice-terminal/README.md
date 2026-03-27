# Voice Analytics Agent

Terminal-based voice agent that queries the Chinook music database via spoken questions.

## Setup

```bash
cd voice-terminal
uv sync
```

Requires `OPENAI_API_KEY` in your environment:

```bash
source ~/api_keys.sh
```

## Run (Non-Temporal)

```bash
uv run python -m src.main_simple
```

Speak your question when you see "Listening...". The agent will:
1. Transcribe your speech (Whisper)
2. Query the database if needed (GPT-4.1 + SQL)
3. Speak the answer back (TTS)

Press Ctrl+C to exit. Speak during playback to interrupt.
