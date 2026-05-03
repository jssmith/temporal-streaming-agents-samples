# Temporal Streaming Agents Samples

Sample applications demonstrating how to stream AI agent progress to users
through Temporal workflows. The samples use the OpenAI Responses API directly
(not an agent SDK) and show how to use Temporal's existing primitives —
Signals, Updates, and Queries — to deliver real-time streaming from durable
workflows, without additional infrastructure like Redis. The streaming patterns
generalize to any LLM provider with a streaming API.

## The Streaming Problem

Streaming means rendering agent progress as it happens rather than only when
the agent completes. AI agent streams commonly include:

- **LLM tokens**: Responses rendered incrementally as the model generates them.
- **Reasoning outputs**: Internal chain-of-thought exposed separately from the
  response.
- **Application messages**: Tool calls, status updates, agent handoffs, and
  other progress indicators originating from the application or from behind the
  model API (e.g., web search results).

Streaming keeps users engaged, builds trust through transparency, and enables
agent steering — cancelling unproductive work or interrupting to add context.
This is particularly important for long-running agents that do significant work
between interactions.

### Streaming and Durable Execution

A key question is the level of durability that is desirable — or achievable —
in streaming agentic applications.

Making state durable introduces latency and consumes system resources. If
failures are rare or consequences are low, durable streaming might not be
justified. But for production agents that run expensive multi-step workflows,
losing progress to a server restart or transient failure is costly.

The degree to which LLM calls are resumable mid-stream varies by provider.
OpenAI supports a fully resumable background mode. Google Interactions provides
access to end results of an interrupted stream once the call completes.
Anthropic's API accepts a response prefix that can resume a streaming response.
Some providers have no streaming recovery at all.

These samples demonstrate patterns that work regardless of whether the
underlying LLM API supports resumption.

## Architecture

### Without Temporal

```mermaid
flowchart LR
    B[Browser] -->|SSE| BFF[BFF]
    BFF -->|stream| LLM[LLM]
```

The BFF (backend-for-frontend) runs the agent loop, buffers events in memory,
and streams them to the browser via SSE. If the server restarts, all in-flight
work and session state is lost.

### With Temporal

```mermaid
flowchart LR
    B[Browser] -->|SSE| BFF[BFF]
    BFF -->|"subscribe"| Topic[(Stream topic)]
    Y[LLM Activity] -->|"publish"| Topic
    W[Workflow] -->|"publish"| Topic
    W -->|Activity| Y
    Y -->|stream| LLM[LLM]
```

The BFF becomes a stateless proxy. Session state, conversation history, and
the event stream all live in the workflow. The BFF can be restarted at any
time without losing work.

There are two streaming transport problems to solve:

1. **Activity → Workflow**: how does the LLM activity send streaming events
   (tokens, thinking, tool calls) back to the workflow while the activity is
   still running?
2. **Workflow → BFF**: how does the BFF receive those events from the workflow
   to forward as SSE to the browser?

Both are solved by the same primitive: a Workflow Stream topic from
`temporalio.contrib.workflow_streams`. The workflow holds a `WorkflowStream`,
opens a topic, and publishes lifecycle events directly. The activity opens a
client to the same topic and publishes streaming deltas. The BFF opens a
client and subscribes for new items. Offsets, batching, and durable history
are handled by the contrib module.

### Transport: Activity → Topic (Batched Publish)

```mermaid
flowchart LR
    Y[LLM Activity] -->|"publish (batched)"| Topic[(Stream topic)]
    W[Workflow] -->|Invoke| Y
```

The LLM activity streams the model response using the OpenAI Responses API
(`openai.responses.stream()`). The pattern generalizes to any LLM provider
with a streaming API. As events arrive, the activity translates them into
application events and publishes them. The contrib module batches publishes
and flushes them to the workflow as a single Signal at a configurable
interval (default 2.0s, override via `WORKFLOW_STREAM_BATCH_INTERVAL`).

This is a Nagle-like batching strategy: buffer events, flush on a timer.
Callers can also force a flush immediately for significant events (e.g.,
end of a thinking block).

```python
@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    stream = WorkflowStreamClient.from_within_activity(
        batch_interval=timedelta(seconds=2.0),
    )
    async with stream:
        events = stream.topic(EVENTS_TOPIC, type=dict)

        async with openai_client.responses.stream(**kwargs) as oai_stream:
            async for event in oai_stream:
                activity.heartbeat()
                events.publish(translate(event))
```

The workflow holds the same topic by name and writes lifecycle events to it
directly:

```python
class AnalyticsWorkflow:
    @workflow.init
    def __init__(self, state: WorkflowState) -> None:
        self.stream = WorkflowStream(prior_state=state.stream_state)
        self.events = self.stream.topic(EVENTS_TOPIC, type=dict)
        # ...

    def _emit(self, event_type: str, **data) -> None:
        self.events.publish({"type": event_type, "data": data})
```

**Why batched publishes?** Each flush is a single Temporal Signal carrying
the buffered events, instead of one Signal per token. At 2-second intervals
a typical model call produces 3–5 batches rather than hundreds of one-event
signals.

### Transport: Topic → BFF (Long-Poll Subscribe)

```mermaid
flowchart LR
    B[Browser] -->|SSE| BFF[BFF]
    BFF -->|"subscribe (long-poll)"| Topic[(Stream topic)]
```

The BFF builds a `WorkflowStreamClient` against the workflow ID and calls
`stream.subscribe(topics=[EVENTS_TOPIC], from_offset=...)`, which long-polls
the workflow until new items are available. Each item carries its offset, so
clients can reconnect from where they left off.

```python
async def event_stream(session_id: str, from_offset: int):
    stream = WorkflowStreamClient.create(client, session_id)
    async for item in stream.subscribe(
        topics=[EVENTS_TOPIC], from_offset=from_offset, result_type=dict,
    ):
        yield f"data: {json.dumps(item.value)}\n\n"

return StreamingResponse(event_stream(...), media_type="text/event-stream")
```

**Why subscribe instead of push?** The contrib module already implements the
long-poll mechanics — turning workflow Updates into a streaming subscription
under the hood. The workflow doesn't need to track which clients are
listening or manage connection state. Reconnection is just "pass the last
offset you saw."

**Why not WebSockets?** A push-based approach would avoid repeated polls but
require the workflow to manage connection state. The subscribe approach is
simpler and works well for AI agent latencies (seconds, not milliseconds).

### Per-Turn Resumption

Stream items have monotonically increasing offsets across the workflow's
lifetime. To stream only the current turn, the BFF queries the current
offset before signaling `start_turn`, and subscribes from that offset:

```python
stream = WorkflowStreamClient.create(client, session_id)
start_offset = await stream.get_offset()
await handle.signal(AnalyticsWorkflow.start_turn, StartTurnInput(message=text))
# Subscribe from start_offset onward...
```

On reconnect (even after server restart), the client resumes from its last
known offset. Continue-as-new preserves offsets via `stream.continue_as_new`,
so subscriptions span workflow chains transparently.

## Analytics Agent

Chat-based analytics agent that queries a Chinook music store database
(SQLite). The agent writes and executes SQL queries, Python code, and shell
commands, reasons about results, recovers from errors, and presents formatted
analysis.

See [apps/backend-temporal/ARCHITECTURE.md](apps/backend-temporal/ARCHITECTURE.md) for
implementation details including event types, failure modes, and recovery
behavior.

### Testing

Unit tests cover pure logic (reducers, event serialization, tool guards, Pydantic
types). E2E tests hit the real OpenAI API through Playwright.

```bash
# Frontend unit tests (Vitest)
cd apps/frontend && npx vitest run

# Backend-ephemeral unit tests (pytest)
cd apps/backend-ephemeral && uv run python -m pytest tests/ --timeout=30

# Backend-temporal unit tests (pytest)
cd apps/backend-temporal && uv run python -m pytest tests/ --timeout=30

# E2E tests (Playwright, requires OPENAI_API_KEY)
npx playwright test
```

E2E tests auto-start the ephemeral backend and frontend via Playwright's
`webServer` config. They skip if `OPENAI_API_KEY` is not set.

### Prerequisites

- Python 3.12+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Temporal CLI](https://docs.temporal.io/cli) (`brew install temporal` on macOS)
- OpenAI API key (full-access, or a restricted key with Write access to `/v1/responses`)

### Setup

```bash
# Download the Chinook SQLite database
./setup.sh

# Install backend dependencies
uv sync  # workspace at the repo root installs all apps + packages/shared

# Install frontend dependencies
(cd apps/frontend && npm install)
```

### Running

The fastest path is `scripts/run-demo.sh`, which boots everything and
attaches to an already-running Temporal dev server if one is up:

```bash
export OPENAI_API_KEY=sk-...
scripts/run-demo.sh analytics    # backend-temporal worker + BFF + frontend
# or
scripts/run-demo.sh voice        # voice-terminal worker; client runs in another terminal
```

For the analytics demo, browse to <http://localhost:3001>. For the voice
demo, the script starts the worker and prints the client command to run
in a second terminal.

If you'd rather run the components by hand:

```bash
# Terminal 1: Temporal dev server
temporal server start-dev

# Terminal 2: Worker
# Note: this sample uses the OpenAI Responses API with gpt-4.1. A full-access
# project key works, or a restricted key with Write permission for /v1/responses.
# Read-only keys will fail. No permissions for Assistants or OpenAI-hosted tools
# are required.
export OPENAI_API_KEY=sk-...
cd apps/backend-temporal
uv run python -m src.worker

# Terminal 3: FastAPI proxy (port 8001)
cd apps/backend-temporal
uv run uvicorn src.main:app --reload --port 8001

# Terminal 4: Frontend (port 3001)
cd apps/frontend
npm run dev
```

Open http://localhost:3001

### Running (Temporal Cloud)

To run against Temporal Cloud instead of a local dev server, create a
`apps/backend-temporal/.env` file:

```bash
TEMPORAL_ADDRESS=your-namespace.abcd.tmprl.cloud:7233
TEMPORAL_NAMESPACE=your-namespace.abcd
TEMPORAL_API_KEY=your-api-key
OPENAI_API_KEY=sk-...
```

Then start the worker, API server, and frontend as above — but skip the
`temporal server start-dev` step. The worker and API server read connection
settings from the `.env` file automatically.

To create an API key, go to [cloud.temporal.io](https://cloud.temporal.io) →
profile → **API Keys** → **Create API Key**.

### Running (Ephemeral Backend)

An ephemeral (non-Temporal) backend is included for comparison. It runs the
same agent with the same frontend but keeps all state in memory.

```bash
# Terminal 1: Backend (port 8001)
# Note: this sample uses the OpenAI Responses API with gpt-4.1. A full-access
# project key works, or a restricted key with Write permission for /v1/responses.
# Read-only keys will fail. No permissions for Assistants or OpenAI-hosted tools
# are required.
export OPENAI_API_KEY=sk-...
cd apps/backend-ephemeral
uv sync
uv run uvicorn src.main:app --reload --port 8001

# Terminal 2: Frontend (port 3001)
cd apps/frontend
npm run dev
```

### Demo Script

#### 1. Basic SQL Query

Click **"Show me the top 10 customers by total spending"** from the suggested prompts.

**Watch for:**
- User message appears right-aligned in purple
- "Running SQL..." step appears with timer, then completes as "Executed SQL"
- Click the SQL step to expand it — shows the query with syntax highlighting and the raw result
- Markdown table streams in with 10 customer rows
- Summary text follows the table

#### 2. Cross-Tabulation (Multi-Step)

Type: **"Create a cross-tabulation of genres vs countries — which countries prefer which genres? Show the top 5 genres and top 5 countries by purchase volume."**

**Watch for:**
- Multiple SQL execution steps (the agent may query data in stages)
- Final output: a crosstab table with genres as columns, countries as rows
- Insights about the data below the table

#### 3. Parallel SQL Queries

Type: **"I want three things: (1) the top 5 artists by total revenue, (2) the top 5 genres by track count, and (3) the average invoice total by country. Get all three."**

**Watch for:**
- Multiple "Running SQL..." steps appear simultaneously (parallel execution)
- Steps complete independently
- Final output synthesizes all three results into separate tables

#### 4. Multi-Turn Conversation

After the previous query, type: **"Now show me the top 3 albums for Iron Maiden specifically"**

**Watch for:**
- Agent uses context from the previous turn ("Iron Maiden" appeared in the results)
- Returns specific album data for that artist

#### 5. Bash Tool (Write + Run Script)

Start a new session (click **+ New chat** in the sidebar), then type: **"Write a Python script that generates a summary report of the database and save it to report.py, then run it"**

**Watch for:**
- "Running bash..." steps for writing and executing the script
- If the script errors (e.g., wrong DB path), the agent reasons about the error and retries
- Tell it: **"Use the DB_PATH environment variable to find the database"**
- Final output shows the report with table summaries

#### 6. Session Management

- Click **+ New chat** to create a new session
- Each session has its own conversation history and backend state
- Click any session in the sidebar to switch back — the full conversation is preserved
- Session previews update to show the first message

#### 7. Interrupt

Type a broad query like: **"Give me a detailed breakdown of every customer's purchase history including all invoices and line items"**

While the agent is working, press **Esc**.

**Watch for:**
- Stream stops immediately
- Any partial output remains visible
- Input returns to idle ("Ask anything...")
- You can send a new message

#### 8. Queue Follow-Up

Send any query. While the agent is running, type a follow-up and press Enter.

**Watch for:**
- Input placeholder says "Type to steer the agent or queue a follow-up"
- First query completes normally
- Queued message is sent automatically as the next turn
