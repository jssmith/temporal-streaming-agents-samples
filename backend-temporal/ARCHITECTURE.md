# Analytics Agent: Temporal Backend Architecture

Implementation details for the Temporal-backed analytics agent. For the general
streaming architecture, see the [top-level README](../README.md).

## Project Structure

```
backend-temporal/
└── src/
    ├── main.py              # FastAPI BFF (stateless proxy)
    ├── workflows.py         # Temporal workflow (agent loop + state)
    ├── activities.py        # LLM calls + tool execution
    ├── constants.py         # Shared constants (topic names)
    ├── types.py             # Pydantic models (workflow contract)
    ├── temporal_client.py   # Temporal connection config
    ├── worker.py            # Temporal worker entry point
    └── database.py          # SQLite connection
```

## Workflow Contract

The workflow exposes a small API via Temporal primitives:

| Primitive | Name | Purpose |
|---|---|---|
| Signal | `start_turn` | Enqueue a user message |
| Signal | `interrupt` | Cancel the current turn |
| Signal | `close_session` | Graceful workflow exit |
| Signal | `__pubsub_publish` | Activity→Workflow event delivery (PubSubMixin) |
| Update | `__pubsub_poll` | Long-poll for new events (PubSubMixin) |
| Query | `__pubsub_offset` | Current pub/sub offset (PubSubMixin) |
| Query | `get_session` | Session metadata and messages |

## Event Types

Events are plain dicts with a `type`, `timestamp`, and `data` payload:

| Event Type | Source | Description |
|---|---|---|
| `USER_MESSAGE` | Workflow | User message echoed to stream |
| `AGENT_START` | Workflow | Turn begins |
| `THINKING_START` | Activity (Signal) | Model reasoning begins |
| `THINKING_DELTA` | Activity (Signal) | Incremental reasoning text |
| `THINKING_COMPLETE` | Activity (Signal) | Reasoning block complete |
| `TEXT_DELTA` | Activity (Signal) | Incremental response text |
| `TEXT_COMPLETE` | Activity (Signal) | Full response text |
| `TOOL_CALL_START` | Workflow | Tool execution begins |
| `TOOL_CALL_COMPLETE` | Workflow | Tool execution result |
| `TOKEN_USAGE` | Workflow | Token usage stats for the model call |
| `RETRY` | Activity (Signal) | Activity retry detected |
| `AGENT_COMPLETE` | Workflow | Turn complete |

Events from the activity (thinking, text deltas) are published via
`PubSubClient` and arrive as batched Signals. Events from the workflow (tool
calls, agent lifecycle) are published directly via `self.publish()`.

## Failure Modes and Recovery

| Failure | Impact | Recovery |
|---|---|---|
| **Browser/UI** | SSE connection drops | Reload the page. Events are durable in the workflow — a fresh SSE stream from the current event index resumes seamlessly. |
| **BFF** | SSE stream and subscribe loop terminate | Restart the server. It is stateless. The frontend reconnects and resumes subscribing from its last known offset. No events are lost. |
| **BFF during a turn** | Mid-turn SSE stream breaks | The workflow continues running. When the BFF restarts and the frontend reconnects, it can subscribe for events it missed. The turn completes regardless of whether anyone is subscribing. |
| **Worker** | Activity and workflow tasks stop | Temporal reassigns tasks to another worker (or the same worker after restart). Workflow state is rebuilt from event history. The main loop resumes from the last committed state. |
| **LLM activity** | Model call fails mid-stream | Temporal retries per the RetryPolicy. The activity detects retries via `activity.info().attempt` and injects a `RETRY` event so the UI can notify the user. Partial streaming events from the failed attempt may have already been signaled to the workflow — they remain in the event list as-is. |
| **LLM API (rate limit)** | 429 from provider | Raised as retryable `ApplicationError`. Temporal backs off per the RetryPolicy. |
| **LLM API (auth error)** | 401 from provider | Raised as non-retryable `ApplicationError`. The activity fails immediately and the workflow propagates the error. |
| **LLM API (server error)** | 500+ from provider | Raised as retryable `ApplicationError`. Temporal retries with backoff. |
| **Temporal server** | Signals and Updates cannot be delivered | Activities and workflows pause. When the Temporal server recovers, everything resumes. Streaming events buffered in the activity are delivered once signals succeed. If the outage outlasts the activity attempt, buffered events from that attempt may be lost; the activity retries from scratch with a `RETRY` event. |

## Retry Detection

Activities detect retries by checking `activity.info().attempt`. On retry, a
`RETRY` event is injected into the stream before re-executing:

```python
@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    pubsub = PubSubClient.create(batch_interval=2.0)
    info = activity.info()

    async with pubsub:
        if info.attempt > 1:
            pubsub.publish(EVENTS_TOPIC, make_event(
                "RETRY",
                attempt=info.attempt,
                message="Retrying model call...",
            ), priority=True)
        # ... proceed with model call
```

## Interrupt Handling

The workflow supports cancelling a running turn via an `interrupt` Signal.
The main loop monitors both the model activity and the interrupt flag:

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

The pub/sub event buffer grows across turns. For long conversations, the
workflow checks whether Temporal suggests continuing as new, drains pending
pub/sub deliveries, and carries forward all state including the pub/sub state:

```python
if workflow.info().is_continue_as_new_suggested():
    self.drain_pubsub()
    await workflow.wait_condition(workflow.all_handlers_finished)
    workflow.continue_as_new(args=[WorkflowState(
        messages=self._messages,
        pubsub_state=self.get_pubsub_state(),
        ...
    )])
```

## Heartbeats

The model call activity heartbeats on every event received from the OpenAI
stream. Combined with a 30-second `heartbeat_timeout`, this detects stalled
streams early — if the LLM connection hangs, Temporal times out the activity
after 30 seconds rather than waiting for the full 180-second
`start_to_close_timeout`. This is important because LLM streaming connections
can hang indefinitely on network issues without raising an exception.

```python
async for event in stream:
    activity.heartbeat()
    pubsub.publish(EVENTS_TOPIC, translate(event))
```
