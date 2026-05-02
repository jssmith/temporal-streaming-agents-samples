# Analytics Agent: Temporal Backend Architecture

Implementation details for the Temporal-backed analytics agent. For the general
streaming architecture, see the [top-level README](../../README.md).

## Project Structure

```
apps/backend-temporal/
└── src/
    ├── main.py            # FastAPI BFF (stateless proxy)
    ├── workflows.py       # Temporal workflow (agent loop + state)
    ├── activities.py      # LLM calls + tool execution
    ├── types.py           # Pydantic models (workflow contract)
    ├── worker.py          # Temporal worker entry point
    └── temporal_client.py # Temporal Cloud / dev-server connection
```

Shared library: `packages/shared/` (`analytics_shared.database`,
`analytics_shared.sql_tool`, `analytics_shared.constants`,
`analytics_shared.types`).

## Workflow Contract

The workflow exposes a small API via Temporal primitives:

| Primitive | Name | Purpose |
|---|---|---|
| Signal | `start_turn` | Enqueue a user message |
| Signal | `interrupt` | Cancel the current turn |
| Signal | `close_session` | Drain and exit the workflow |
| Query | `get_session` | Session metadata and messages |

Streaming events are not part of this surface — they ride a
`temporalio.contrib.workflow_streams` topic, not Signals or Updates.

## Streaming Transport

Events flow through one Workflow Stream topic:

```
self.stream  = WorkflowStream(prior_state=state.stream_state)
self.events  = self.stream.topic(EVENTS_TOPIC, type=dict)
```

- **Workflow → topic**: lifecycle events (turn start/complete, tool calls,
  errors) are published directly with `self.events.publish({...})`.
- **Activity → topic**: the model_call activity opens a
  `WorkflowStreamClient.from_within_activity(batch_interval=...)`, gets
  the same topic by name, and publishes streaming text and thinking deltas.
  Events are batched and flushed in a single Signal at the configured
  interval (default 2.0s, override with `WORKFLOW_STREAM_BATCH_INTERVAL`).
- **BFF → topic**: the FastAPI server creates a `WorkflowStreamClient` and
  long-polls `stream.subscribe(topics=[EVENTS_TOPIC], from_offset=...)` to
  pull new events. It writes them to the SSE response.

State (offsets, durable log) lives in the workflow. The BFF is stateless;
restarting it does not lose anything because subscribers resume from a
client-supplied offset.

## Event Types

Events are plain dicts with a `type`, `timestamp`, and `data` payload:

| Event Type | Source | Description |
|---|---|---|
| `AGENT_START` | Workflow | Turn begins |
| `THINKING_START` | Activity | Model reasoning begins |
| `THINKING_DELTA` | Activity | Incremental reasoning text |
| `THINKING_COMPLETE` | Activity | Reasoning block complete |
| `TEXT_DELTA` | Activity | Incremental response text |
| `TEXT_COMPLETE` | Activity | Full response text |
| `TOOL_CALL_START` | Workflow | Tool execution begins |
| `TOOL_CALL_COMPLETE` | Workflow | Tool execution result |
| `RETRY` | Activity | Activity retry detected |
| `AGENT_COMPLETE` | Workflow | Turn complete |
| `ERROR` | BFF | Unrecoverable error |

## Failure Modes and Recovery

| Failure | Impact | Recovery |
|---|---|---|
| **Browser/UI** | SSE connection drops | Reload the page. Events are durable in the workflow stream — a fresh subscription resumes from the client's last known offset. |
| **BFF** | SSE stream and subscribe loop terminate | Restart the server. It is stateless. The frontend reconnects and the new BFF re-subscribes from the last offset. No events are lost. |
| **BFF during a turn** | Mid-turn SSE stream breaks | The workflow keeps running. When the BFF restarts and the frontend reconnects, the new subscription pulls events that were published while no one was listening. |
| **Worker** | Activity and workflow tasks stop | Temporal reassigns tasks to another worker (or the same worker after restart). Workflow state, including the stream log, is rebuilt from event history. |
| **LLM activity** | Model call fails mid-stream | Temporal retries per the RetryPolicy. The activity detects retries via `activity.info().attempt` and publishes a `RETRY` event so the UI can notify the user. Events from a failed attempt remain in the stream as-is. |
| **LLM API (rate limit)** | 429 from provider | Raised as retryable `ApplicationError`. Temporal backs off per the RetryPolicy. |
| **LLM API (auth error)** | 401 from provider | Raised as non-retryable `ApplicationError`. Surfaces immediately as an error event. |
| **LLM API (server error)** | 500+ from provider | Raised as retryable `ApplicationError`. Temporal retries with backoff. |
| **Temporal server** | Signals, queries, and stream Updates cannot be delivered | Activities and workflows pause. When Temporal recovers, everything resumes. Events buffered in the activity flush on reconnect. |

## Retry Detection

Activities detect retries by checking `activity.info().attempt` and publishing
a `RETRY` event before the next attempt:

```python
@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    info = activity.info()
    if info.attempt > 1:
        stream = WorkflowStreamClient.from_within_activity()
        async with stream:
            events = stream.topic(EVENTS_TOPIC, type=dict)
            events.publish(_make_event(
                "RETRY",
                attempt=info.attempt,
                message="Retrying model call...",
            ))
    # ... proceed with model call
```

## Interrupt Handling

The workflow supports cancelling a running turn via an `interrupt` Signal.
The main loop races the model activity task against the interrupt flag and
cancels the activity if interrupt wins:

```python
@workflow.signal
def interrupt(self) -> None:
    self._interrupted = True

# In the main loop:
model_task = asyncio.create_task(
    workflow.execute_activity("model_call", ...)
)
await workflow.wait_condition(
    lambda: model_task.done() or self._interrupted
)
if self._interrupted and not model_task.done():
    model_task.cancel()
```

The BFF exposes an interrupt endpoint. The frontend calls it on Esc:

```
POST /api/sessions/{id}/interrupt  →  Signal(interrupt)
```

## Continue-As-New

The stream log grows across turns. For long conversations the workflow
hands off to a fresh execution, carrying the stream state forward so
subscribers' offsets stay valid:

```python
if workflow.info().is_continue_as_new_suggested():
    await self.stream.continue_as_new(lambda state: [WorkflowState(
        working_dir=self._working_dir,
        model=self._model,
        reasoning_effort=self._reasoning_effort,
        messages=self._messages,
        response_id=self._response_id,
        db_schema=self._schema,
        stream_state=state,
    )])
```

`stream.continue_as_new` drains pending publishes, waits for handlers to
finish, and packs a `WorkflowStreamState` snapshot into the next run.

## Heartbeats

The model call activity heartbeats on every event received from the OpenAI
stream. Combined with a 30-second `heartbeat_timeout`, this detects stalled
streams early — if the LLM connection hangs, Temporal times out the activity
after 30 seconds rather than waiting for the full 180-second
`start_to_close_timeout`. LLM streaming connections can hang indefinitely on
network issues without raising an exception.

```python
async for event in oai_stream:
    activity.heartbeat()
    events.publish(translate(event))
```
