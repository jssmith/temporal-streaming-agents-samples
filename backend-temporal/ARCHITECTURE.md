# Analytics Agent: Temporal Backend Architecture

Implementation details for the Temporal-backed analytics agent. For the general
streaming architecture, see the [top-level README](../README.md).

## Project Structure

```
backend-temporal/
└── src/
    ├── main.py            # FastAPI BFF (stateless proxy)
    ├── workflows.py       # Temporal workflow (agent loop + state)
    ├── activities.py      # LLM calls + tool execution
    ├── event_batcher.py   # Nagle-like signal batching
    ├── types.py           # Pydantic models (workflow contract)
    ├── worker.py          # Temporal worker entry point
    └── database.py        # SQLite connection
```

## Workflow Contract

The workflow exposes a small API via Temporal primitives:

| Primitive | Name | Purpose |
|---|---|---|
| Signal | `start_turn` | Enqueue a user message |
| Signal | `interrupt` | Cancel the current turn |
| Signal | `receive_events` | Activity→Workflow event delivery |
| Update | `poll_events` | Long-poll for new events |
| Query | `get_event_count` | Current event list length |
| Query | `get_session` | Session metadata and messages |

## Event Types

Events are plain dicts with a `type`, `timestamp`, and `data` payload:

| Event Type | Source | Description |
|---|---|---|
| `AGENT_START` | Workflow | Turn begins |
| `THINKING_START` | Activity (Signal) | Model reasoning begins |
| `THINKING_DELTA` | Activity (Signal) | Incremental reasoning text |
| `THINKING_COMPLETE` | Activity (Signal) | Reasoning block complete |
| `TEXT_DELTA` | Activity (Signal) | Incremental response text |
| `TEXT_COMPLETE` | Activity (Signal) | Full response text |
| `TOOL_CALL_START` | Workflow | Tool execution begins |
| `TOOL_CALL_COMPLETE` | Workflow | Tool execution result |
| `RETRY` | Activity (Signal) | Activity retry detected |
| `AGENT_COMPLETE` | Workflow | Turn complete |
| `ERROR` | BFF | Unrecoverable error |

Events from the activity (thinking, text deltas) arrive via batched Signals.
Events from the workflow (tool calls, agent lifecycle) are emitted directly.
The BFF adds error events if the poll mechanism itself fails.

## Failure Modes and Recovery

| Failure | Impact | Recovery |
|---|---|---|
| **Browser/UI** | SSE connection drops | Reload the page. Events are durable in the workflow — a fresh SSE stream from the current event index resumes seamlessly. |
| **BFF** | SSE stream and poll loop terminate | Restart the server. It is stateless. The frontend reconnects and resumes polling from its last known index. No events are lost. |
| **BFF during a turn** | Mid-turn SSE stream breaks | The workflow continues running. When the BFF restarts and the frontend reconnects, it can poll for events it missed. The turn completes regardless of whether anyone is polling. |
| **Worker** | Activity and workflow tasks stop | Temporal reassigns tasks to another worker (or the same worker after restart). Workflow state is rebuilt from event history. The main loop resumes from the last committed state. |
| **LLM activity** | Model call fails mid-stream | Temporal retries per the RetryPolicy. The activity detects retries via `activity.info().attempt` and injects a `RETRY` event so the UI can notify the user. Partial streaming events from the failed attempt may have already been signaled to the workflow — they remain in the event list as-is. |
| **LLM API (rate limit)** | 429 from provider | Raised as retryable `ApplicationError`. Temporal backs off per the RetryPolicy. |
| **LLM API (auth error)** | 401 from provider | Raised as non-retryable `ApplicationError`. Surfaces immediately as an error event. |
| **LLM API (server error)** | 500+ from provider | Raised as retryable `ApplicationError`. Temporal retries with backoff. |
| **Temporal server** | Signals and Updates cannot be delivered | Activities and workflows pause. When the Temporal server recovers, everything resumes. No events are lost — they were buffered in the activity until the Signal could be delivered. |

## Retry Detection

Activities detect retries by checking `activity.info().attempt`. On retry, a
`RETRY` event is injected into the stream before re-executing:

```python
@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    info = activity.info()
    if info.attempt > 1:
        batcher.add(make_event(
            "RETRY",
            attempt=info.attempt,
            message="Retrying model call...",
        ))
        await batcher.flush()
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

The event list grows across turns. For long conversations, the workflow checks
whether Temporal suggests continuing as new and carries forward all state:

```python
if workflow.info().is_continue_as_new_suggested():
    workflow.continue_as_new(args=[WorkflowState(
        messages=self._messages,
        event_list=self._event_list,
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
    batcher.add(translate(event))
```
