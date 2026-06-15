"""Analytics agent workflow — durable agent loop with event streaming."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from analytics_shared.constants import EVENTS_TOPIC
    from analytics_shared.events import make_event
    from analytics_shared.tools import TOOL_DEFINITIONS

    from .types import (
        ModelCallInput,
        ModelCallResult,
        SessionInfo,
        StartTurnInput,
        ToolInput,
        ToolResult,
        WorkflowState,
    )

logger = workflow.logger

# The model/tool activities heartbeat on a timer so an interrupt can be
# delivered (cancellation reaches an activity only on a heartbeat). A 3s
# heartbeat timeout encodes the cancellation latency we accept: the worker
# throttles heartbeats to ~80% of the timeout, so the workflow's cancel lands
# within roughly this window. The 180s/60s start-to-close timeouts bound a
# single model call / tool run.
HEARTBEAT_TIMEOUT = timedelta(seconds=3)
MODEL_CALL_TIMEOUT = timedelta(seconds=180)
TOOL_CALL_TIMEOUT = timedelta(seconds=60)

SYSTEM_PROMPT_TEMPLATE = """You are an analytics assistant with access to a Chinook music store database (SQLite).

You have three tools:
- execute_sql: Run read-only SQL queries against the database
- execute_python: Run Python code with pandas, matplotlib, etc.
- bash: Run shell commands in the session working directory

## How to work

Before executing any tools, briefly explain your approach: what you're going to do and why.
After getting results, interpret them before presenting to the user.

When a task requires multiple steps, work through them methodically:
1. Explain what you need to find out
2. Run the necessary queries or code
3. If something fails or returns unexpected results, explain what went wrong and try a different approach
4. Synthesize the results into a clear answer

Use SQL for direct data retrieval and simple aggregations. Use Python when you need
pandas operations (pivot tables, crosstabs, statistical analysis, complex transformations).
Use bash for file operations or running scripts you've written.

When you can run independent analyses in parallel, do so — request multiple tool calls
at once rather than sequentially.

Present results in well-formatted markdown with tables where appropriate.

When you save files (charts, exports, etc.) they are accessible via URL. To display a chart
you saved as `chart.png`, include it in your response as:
![description](/api/sessions/SESSION_ID/files/chart.png)

Database schema:
{schema}"""


# Tool schemas (execute_sql, execute_python, bash) are defined once in
# analytics_shared.tools and imported above. Pure data dicts pass through the
# workflow sandbox fine.


@workflow.defn
class AnalyticsWorkflow:

    @workflow.init
    def __init__(self, state: WorkflowState) -> None:
        self.stream = WorkflowStream(prior_state=state.stream_state)
        self.events = self.stream.topic(EVENTS_TOPIC, type=dict)
        self._messages: list[dict] = state.messages
        self._pending_messages: list[str] = []
        self._turn_complete: bool = True
        self._interrupted: bool = False
        self._closed: bool = False
        self._response_id: str | None = state.response_id
        self._working_dir: str = state.working_dir
        self._schema: str | None = state.db_schema
        self._model: str = state.model
        self._reasoning_effort: str | None = state.reasoning_effort

    # -- helpers --

    def _emit(self, event_type: str, **data) -> None:
        # Stamp with workflow.now() so the timestamp is deterministic on replay
        # (activities use wall-clock instead).
        self.events.publish(
            make_event(event_type, workflow.now().isoformat(), **data)
        )

    def _build_system_prompt(self) -> str:
        session_id = workflow.info().workflow_id
        return SYSTEM_PROMPT_TEMPLATE.format(schema=self._schema).replace(
            "SESSION_ID", session_id
        )

    def _persist_partial(self, result: ModelCallResult | None) -> None:
        """Persist the partial assistant text from an interrupted model call.

        Marked interrupted so a reloaded client renders the same partial turn
        it saw live instead of a user message with no reply.
        """
        if result is not None and result.final_text:
            self._messages.append({
                "role": "assistant",
                "content": result.final_text,
                "timestamp": workflow.now().isoformat(),
                "interrupted": True,
            })

    # -- signals --

    @workflow.signal
    def start_turn(self, input: StartTurnInput) -> None:
        # Queue rather than overwrite: a second start_turn arriving while a turn
        # runs must not clobber the first. The run loop drains these in order.
        self._pending_messages.append(input.message)

    @workflow.signal
    def interrupt(self) -> None:
        self._interrupted = True

    @workflow.signal
    def close_session(self) -> None:
        self._closed = True

    # -- queries --

    @workflow.query
    def get_session(self) -> SessionInfo:
        return SessionInfo(
            session_id=workflow.info().workflow_id,
            messages=self._messages,
            turn_in_progress=not self._turn_complete,
        )

    # -- main loop --

    @workflow.run
    async def run(self, state: WorkflowState) -> None:
        # Load schema via activity on first run (or after continue-as-new
        # if it wasn't carried forward)
        if self._schema is None:
            self._schema = await workflow.execute_activity(
                "load_schema",
                start_to_close_timeout=timedelta(seconds=10),
                result_type=str,
            )

        while True:
            await workflow.wait_condition(
                lambda: bool(self._pending_messages) or self._closed
            )
            if self._closed:
                return
            message = self._pending_messages.pop(0)
            self._turn_complete = False
            self._interrupted = False

            await self._run_turn(message)

            self._turn_complete = True

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

    async def _run_turn(self, message: str) -> None:
        """Run one turn of the durable agent loop.

        This is the canonical loop: append the user message, then repeatedly
        call the model and execute any tool calls it requests until it returns
        a final answer (or an interrupt ends the turn). The model call and each
        tool run are activities; the workflow itself stays deterministic.

        Context is carried server-side. With the Responses API store=True, each
        response holds the prior turn's context, so once we have a response_id
        we send only the new input (the user message on a fresh call, or the
        tool outputs after a tool phase) and chain off previous_response_id. We
        send the full system + history only when there is no response_id to
        chain from (first turn, or after an interrupt reset the chain to None).
        """
        self._messages.append({
            "role": "user",
            "content": message,
            "timestamp": workflow.now().isoformat(),
        })

        self._emit("USER_MESSAGE", content=message)
        self._emit("AGENT_START", agent_name="analyst")

        if self._response_id is not None:
            # Chain off the stored response: send only the new user message.
            first_call_messages: list[dict] = [{"role": "user", "content": message}]
        else:
            # No chain to resume: send system + full history + the new message.
            system_prompt = self._build_system_prompt()
            first_call_messages = [{"role": "system", "content": system_prompt}]
            for msg in self._messages[:-1]:  # all previous messages
                first_call_messages.append(
                    {"role": msg["role"], "content": msg["content"]}
                )
            first_call_messages.append({"role": "user", "content": message})

        tool_outputs_for_next_call: list[dict] | None = None
        retry_policy = RetryPolicy(maximum_attempts=3)

        while not self._interrupted:
            operation_id = str(workflow.uuid4())

            # After a tool phase send only the tool outputs; otherwise send the
            # first-call messages. Both chain off previous_response_id.
            messages = (
                tool_outputs_for_next_call
                if tool_outputs_for_next_call is not None
                else first_call_messages
            )
            call_input = ModelCallInput(
                input_messages=messages,
                previous_response_id=self._response_id,
                tools=TOOL_DEFINITIONS,
                model=self._model,
                operation_id=operation_id,
                reasoning_effort=self._reasoning_effort,
            )

            model_task = asyncio.create_task(
                workflow.execute_activity(
                    "model_call",
                    call_input,
                    start_to_close_timeout=MODEL_CALL_TIMEOUT,
                    retry_policy=retry_policy,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    # Wait for the activity to wind down on cancel so we receive
                    # the partial result it returns (the model call catches the
                    # cancellation and returns whatever text streamed so far).
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                    result_type=ModelCallResult,
                )
            )

            # Wait for either completion or interrupt
            await workflow.wait_condition(
                lambda: model_task.done() or self._interrupted
            )

            if self._interrupted and not model_task.done():
                model_task.cancel()
                try:
                    interrupted_result: ModelCallResult | None = await model_task
                except (asyncio.CancelledError, ActivityError):
                    interrupted_result = None
                self._persist_partial(interrupted_result)
                break

            model_result: ModelCallResult = model_task.result()

            # An interrupt that landed just as the call finished: persist the
            # text and end the turn without acting on any tool calls.
            if self._interrupted or model_result.interrupted:
                self._persist_partial(model_result)
                break

            self._response_id = model_result.response_id

            if model_result.usage:
                self._emit(
                    "TOKEN_USAGE",
                    input_tokens=model_result.usage.input_tokens,
                    output_tokens=model_result.usage.output_tokens,
                    reasoning_tokens=model_result.usage.reasoning_tokens,
                    cached_tokens=model_result.usage.cached_tokens,
                )

            if not model_result.tool_calls:
                if model_result.final_text:
                    self._messages.append({
                        "role": "assistant",
                        "content": model_result.final_text,
                        "timestamp": workflow.now().isoformat(),
                    })
                break

            # Emit TOOL_CALL_START for each tool call
            for tc in model_result.tool_calls:
                self._emit(
                    "TOOL_CALL_START",
                    call_id=tc.call_id,
                    tool_name=tc.name,
                    arguments=tc.arguments,
                )

            # Execute tools in parallel, interruptibly. Each runs as a task so
            # we can cancel it mid-execution. WAIT_CANCELLATION_COMPLETED makes
            # the workflow wait for the activity to actually finish cancelling
            # (kill its subprocess, report cancelled) before the turn ends. With
            # TRY_CANCEL the workflow would resolve the activity and move on while
            # it was still running; its next heartbeat would then fail (the
            # server no longer knows it), the SDK would report a failure, and the
            # retry policy would re-run the tool — an orphaned-execution storm.
            tool_tasks = [
                asyncio.create_task(
                    workflow.execute_activity(
                        "execute_tool",
                        ToolInput(
                            tool_name=tc.name,
                            arguments=tc.arguments,
                            working_dir=self._working_dir,
                            call_id=tc.call_id,
                            operation_id=str(workflow.uuid4()),
                        ),
                        start_to_close_timeout=TOOL_CALL_TIMEOUT,
                        retry_policy=retry_policy,
                        heartbeat_timeout=HEARTBEAT_TIMEOUT,
                        cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                        result_type=ToolResult,
                    )
                )
                for tc in model_result.tool_calls
            ]

            # Drain results as each tool finishes (so the UI sees completions
            # incrementally), but bail out the moment an interrupt arrives.
            tool_outputs_for_next_call = []
            processed: set[int] = set()
            while len(processed) < len(tool_tasks):
                await workflow.wait_condition(
                    lambda: self._interrupted
                    or any(
                        t.done() for i, t in enumerate(tool_tasks) if i not in processed
                    )
                )
                if self._interrupted:
                    break
                for i, t in enumerate(tool_tasks):
                    if i in processed or not t.done():
                        continue
                    processed.add(i)
                    result: ToolResult = t.result()
                    error = result.result.get("error")
                    if error:
                        self._emit(
                            "TOOL_CALL_COMPLETE",
                            call_id=result.call_id,
                            tool_name=result.tool_name,
                            error=error,
                        )
                    else:
                        self._emit(
                            "TOOL_CALL_COMPLETE",
                            call_id=result.call_id,
                            tool_name=result.tool_name,
                            result=result.result,
                        )
                    tool_outputs_for_next_call.append({
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": json.dumps(result.result),
                    })

            if self._interrupted:
                for t in tool_tasks:
                    if not t.done():
                        t.cancel()
                # Let the cancellations resolve so no tasks dangle into the
                # next turn; the activities kill their subprocesses on the way.
                await asyncio.gather(*tool_tasks, return_exceptions=True)
                # These tool calls were never answered with function_call_output
                # (any that did finish are intentionally discarded along with
                # tool_outputs_for_next_call). Drop the response chain so the
                # next turn starts fresh from full history rather than chaining
                # off a response with outstanding tool calls, which the
                # Responses API rejects.
                self._response_id = None
                break

        if self._interrupted:
            self._emit("INTERRUPTED")
        else:
            self._emit("AGENT_COMPLETE")
