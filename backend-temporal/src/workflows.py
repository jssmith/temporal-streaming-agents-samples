"""Analytics agent workflow — durable agent loop with event streaming."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.pubsub import PubSubMixin
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
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

MODEL = "gpt-4.1"
EVENTS_TOPIC = "events"

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


TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "name": "execute_sql",
        "description": "Run a read-only SQL query against the Chinook SQLite database. Returns rows as a list of objects.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The SQL query to execute",
                }
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "execute_python",
        "description": "Run Python code in a subprocess. pandas, matplotlib, sqlite3, json, math, statistics, collections, itertools are available. DB_PATH env var points to the SQLite file. Save matplotlib figures to files in the current directory. Print output to stdout.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute",
                }
            },
            "required": ["code"],
        },
    },
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command. DB_PATH env var is available. Working directory is the session directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        },
    },
]


@workflow.defn
class AnalyticsWorkflow(PubSubMixin):

    @workflow.init
    def __init__(self, state: WorkflowState) -> None:
        self.init_pubsub(prior_state=state.pubsub_state)
        self._messages: list[dict] = state.messages
        self._pending_message: str | None = None
        self._turn_complete: bool = True
        self._interrupted: bool = False
        self._closed: bool = False
        self._response_id: str | None = state.response_id
        self._working_dir: str = state.working_dir
        self._schema: str | None = state.db_schema

    # -- helpers --

    def _emit(self, event_type: str, **data) -> None:
        event = {
            "type": event_type,
            "timestamp": workflow.now().isoformat(),
            "data": data,
        }
        self.publish(EVENTS_TOPIC, json.dumps(event).encode())

    def _build_system_prompt(self) -> str:
        session_id = workflow.info().workflow_id
        return SYSTEM_PROMPT_TEMPLATE.format(schema=self._schema).replace(
            "SESSION_ID", session_id
        )

    # -- signals --

    @workflow.signal
    def start_turn(self, input: StartTurnInput) -> None:
        self._pending_message = input.message

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
                lambda: self._pending_message is not None or self._closed
            )
            if self._closed:
                return
            message: str = self._pending_message  # type: ignore[assignment]
            self._pending_message = None
            self._turn_complete = False
            self._interrupted = False

            await self._run_turn(message)

            self._turn_complete = True

            if workflow.info().is_continue_as_new_suggested():
                self.drain_pubsub()
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(args=[WorkflowState(
                    working_dir=self._working_dir,
                    messages=self._messages,
                    response_id=self._response_id,
                    db_schema=self._schema,
                    pubsub_state=self.get_pubsub_state(),
                )])

    async def _run_turn(self, message: str) -> None:
        self._messages.append({
            "role": "user",
            "content": message,
            "timestamp": workflow.now().isoformat(),
        })

        self._emit("USER_MESSAGE", content=message)
        self._emit("AGENT_START", agent_name="analyst")

        system_prompt = self._build_system_prompt()
        input_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        for msg in self._messages[:-1]:  # all previous messages
            input_messages.append({"role": msg["role"], "content": msg["content"]})
        input_messages.append({"role": "user", "content": message})

        tool_outputs_for_next_call: list[dict] | None = None
        retry_policy = RetryPolicy(maximum_attempts=3)

        while not self._interrupted:
            operation_id = str(workflow.uuid4())

            if tool_outputs_for_next_call is not None:
                call_input = ModelCallInput(
                    input_messages=tool_outputs_for_next_call,
                    previous_response_id=self._response_id,
                    tools=TOOL_DEFINITIONS,
                    model=MODEL,
                    operation_id=operation_id,
                )
            else:
                call_input = ModelCallInput(
                    input_messages=input_messages,
                    previous_response_id=self._response_id,
                    tools=TOOL_DEFINITIONS,
                    model=MODEL,
                    operation_id=operation_id,
                )

            model_task = asyncio.create_task(
                workflow.execute_activity(
                    "model_call",
                    call_input,
                    start_to_close_timeout=timedelta(seconds=180),
                    retry_policy=retry_policy,
                    heartbeat_timeout=timedelta(seconds=30),
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
                    await model_task
                except (asyncio.CancelledError, ActivityError):
                    pass
                break

            model_result: ModelCallResult = model_task.result()

            self._response_id = model_result.response_id

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

            # Execute tools in parallel
            tool_tasks = [
                workflow.execute_activity(
                    "execute_tool",
                    ToolInput(
                        tool_name=tc.name,
                        arguments=tc.arguments,
                        working_dir=self._working_dir,
                        call_id=tc.call_id,
                        operation_id=str(workflow.uuid4()),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                    result_type=ToolResult,
                )
                for tc in model_result.tool_calls
            ]

            tool_outputs_for_next_call = []
            for coro in workflow.as_completed(tool_tasks):
                result: ToolResult = await coro
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

        self._emit("AGENT_COMPLETE")
