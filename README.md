# Temporal Streaming Agents Samples

Full-stack AI agent demos built on
[`temporalio.contrib.workflow_streams`](https://docs.temporal.io/develop/python/libraries/workflow-streams)
(ships in `temporalio>=1.27.0`, public preview). Two end-to-end apps —
one web, one terminal — plus a non-Temporal comparison backend.

This repo sits between two related resources:

- **API reference + semantics**: the [Workflow Streams docs page](https://docs.temporal.io/develop/python/libraries/workflow-streams).
- **Minimal feature-focused scenarios**: [samples-python/workflow_streams](https://github.com/temporalio/samples-python/tree/main/workflow_streams) (basic publish/subscribe, reconnecting subscriber, external publisher, bounded log via truncate, LLM with retry).
- **This repo**: those primitives put together at production shape — a full backend-for-frontend (BFF, the HTTP service the browser talks to) plus frontend and worker, multi-turn conversations, durability tested in anger, real audio over Temporal.

## Demos

### `apps/backend-temporal` — Analytics chat agent (web)

A chat UI over the Chinook music store database. The agent has SQL,
Python, and bash tools and writes results back as a streaming response
with markdown tables and embedded charts. Sessions are durable workflows;
the FastAPI BFF is a stateless SSE proxy that subscribes to the workflow
stream and resumes from the client's last-seen offset on reconnect. The
React frontend keeps a per-session runtime with background SSE streams,
so a turn fired on tab A keeps producing tokens into A's cached state
while you read tab B.

See [`apps/backend-temporal/ARCHITECTURE.md`](apps/backend-temporal/ARCHITECTURE.md).

### `apps/voice-terminal` — Voice agent (terminal)

Spoken queries against the same database. Half-duplex: speak, then listen.
Each turn is a Temporal workflow; transcribe, model_call, and execute_sql
are activities. TTS audio is streamed sentence-by-sentence over the
workflow stream. Continue-as-new paired with client-driven per-turn
truncation keeps the durable history bounded across long conversations.
The agent has an `end_session` tool it calls when the user says goodbye.

See [`apps/voice-terminal/ARCHITECTURE.md`](apps/voice-terminal/ARCHITECTURE.md).

### `apps/backend-ephemeral` — Same agent, no Temporal

A drop-in non-Temporal backend for the analytics frontend. Same agent
loop, in-memory sessions, no durability. Useful for seeing what running
on Temporal actually buys you.

## Running

The fast path:

```bash
export OPENAI_API_KEY=sk-...
scripts/run-demo.sh analytics    # backend-temporal worker + BFF + frontend
scripts/run-demo.sh voice        # voice-terminal worker; client in another terminal
```

The script is idempotent against a `temporal server start-dev` you've
already started, prints which cluster won (local or Temporal Cloud per
`apps/backend-temporal/.env`), and tears down only what it spawned on
Ctrl+C.

For the analytics demo, browse to <http://localhost:3001>. For the voice
demo, the script prints the client command to run in a second terminal.

## What these demos exercise that the focused scenarios don't

- **Continue-as-new in a hot loop with active subscribers**, on both
  apps.
- **Truncation paired with CAN** to bound durable history for
  long-running voice conversations (per-turn ack from the client; CAN
  waits for `_log` to drain before snapshotting).
- **Stateless BFF resumption** from offset on reconnect.
- **Per-tab frontend cache** (LRU 5) with persistent background streams,
  so switching tabs is a synchronous restore.
- **Real audio over Temporal**: payload sizing, `max_output_tokens` as a
  per-turn budget, drain-before-close on agent-initiated shutdown.

## Setup

Prerequisites: Python 3.12+, Node.js 18+, [`uv`](https://docs.astral.sh/uv/),
[Temporal CLI](https://docs.temporal.io/cli) (`brew install temporal` on
macOS), and an OpenAI API key with Write access to `/v1/responses`.

```bash
./setup.sh                      # downloads the Chinook SQLite database
uv sync                         # installs all apps + packages/shared
(cd apps/frontend && npm install)
```

To run against Temporal Cloud, drop `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, and `TEMPORAL_API_KEY` into
`apps/backend-temporal/.env`. The run-demo script and `temporal_client.py`
both honor it; otherwise everything points at `localhost:7233`.

## Layout

```
apps/
  backend-temporal/      Analytics agent: workflow + activities + FastAPI BFF
  backend-ephemeral/     Same agent without Temporal (in-memory)
  voice-terminal/        Voice agent: workflow + activities + terminal client
  frontend/              Next.js app shared by both backends
packages/shared/         Chinook DB access, SQL tool, common types
scripts/                 run-demo.sh and friends
```

## Running by hand

If you'd rather run pieces individually:

```bash
# Terminal 1
temporal server start-dev

# Terminal 2 — worker
cd apps/backend-temporal
export OPENAI_API_KEY=sk-...
uv run python -m src.worker

# Terminal 3 — BFF
cd apps/backend-temporal
uv run uvicorn src.main:app --reload --port 8001

# Terminal 4 — frontend
cd apps/frontend
npm run dev
```

For the ephemeral comparison, replace terminals 2 and 3 with a single
`(cd apps/backend-ephemeral && uv run uvicorn src.main:app --reload --port 8001)`.

## Tests

```bash
(cd apps/voice-terminal     && uv run python -m pytest tests/ --timeout=60)
(cd apps/backend-temporal   && uv run python -m pytest tests/ --timeout=30)
(cd apps/backend-ephemeral  && uv run python -m pytest tests/ --timeout=30)
(cd apps/frontend           && npx vitest run)
```

End-to-end Playwright suites under `tests/e2e/` and
`playwright.temporal.config.ts` need a running Temporal cluster and an
`OPENAI_API_KEY`.
