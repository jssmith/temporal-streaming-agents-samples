# Voice Analytics Agent

Terminal-based voice agent that queries the Chinook music database via spoken questions.

The interaction is half-duplex: speak a question, then listen for the answer.
Mid-playback barge-in is intentionally not implemented — it requires duplex
audio with feedback suppression, which is out of scope for this demo.

## Setup

```bash
cd apps/voice-terminal
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

Press Ctrl+C to exit.

## Run (Temporal)

Requires a running Temporal dev server:

```bash
temporal server start-dev
```

Terminal 1 — start the worker:

```bash
uv run python -m src.worker
```

Terminal 2 — start the voice client:

```bash
uv run python -m src.main_temporal
```

Same voice interaction as the non-Temporal version, but each step (transcribe, model call, SQL, TTS) runs as a Temporal activity with automatic retries. Conversation state is durable — survives worker restarts.

## Sample Conversation

Try these to see the agent in action, building from simple to complex:

**Quick fact**
> "How many tracks do we have by Led Zeppelin?"

**Simple query**
> "Who is our biggest spending customer?"

**Follow-up question**
> "What genres of music do they listen to?"

**Multi-step analysis**
> "Compare sales between 2009 and 2010."

In the Temporal version, each step of this conversation is visible in the
Temporal UI as activities (transcribe, model_call, execute_sql). Audio
chunks and lifecycle events stream to the client via
`temporalio.contrib.workflow_streams` — the activity publishes TTS audio
to the workflow's stream log, and the client subscribes for real-time
playback.
