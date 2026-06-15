"""Temporal activities for the analytics agent."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import openai
from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError

from analytics_shared.constants import EVENTS_TOPIC
from analytics_shared.database import get_db_path, load_schema as _load_schema
from analytics_shared.events import make_event
from analytics_shared.sql_tool import execute_sql as _execute_sql
from analytics_shared.types import ToolCallInfo

from .types import (
    ModelCallInput,
    ModelCallResult,
    TokenUsage,
    ToolInput,
    ToolResult,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30

# How often the activity heartbeats while a model call or tool runs. Cancellation
# is delivered to an activity only on a heartbeat, so this interval sets the
# floor on interrupt latency; it must stay well under HEARTBEAT_TIMEOUT (set on
# the workflow side) so a quiet model doesn't trip the timeout.
HEARTBEAT_INTERVAL_SECONDS = 1.0


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the subprocess's whole process group.

    The subprocess is started with start_new_session=True so it leads its own
    group; killing the group (not just the direct child) also takes down any
    grandchildren a shell pipeline spawned, which would otherwise hold the
    pipes open and hang proc.wait().
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group and reap it so pipes close and no zombie remains."""
    _kill_group(proc)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


async def _execute_python(code: str, working_dir: Path) -> dict:
    """Execute Python code in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        cwd=str(working_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _terminate(proc)
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}
    except asyncio.CancelledError:
        # Workflow interrupt: kill the subprocess before propagating so an
        # interrupted turn doesn't leave a Python process running.
        await _terminate(proc)
        raise

    result: dict = {}
    if stdout:
        result["output"] = stdout.decode()
    if stderr:
        result["error"] = stderr.decode()
    if not stdout and not stderr:
        result["output"] = "(no output)"
    return result


async def _execute_bash(command: str, working_dir: Path) -> dict:
    """Execute a shell command in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(working_dir),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _terminate(proc)
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}
    except asyncio.CancelledError:
        await _terminate(proc)
        raise

    output = (stdout.decode() if stdout else "") + (stderr.decode() if stderr else "")
    return {"output": output, "exit_code": proc.returncode}


async def _run_tool(tool_name: str, arguments: dict, working_dir: Path) -> dict:
    """Dispatch a tool call to the appropriate implementation."""
    if tool_name == "execute_sql":
        return await _execute_sql(arguments["query"])
    elif tool_name == "execute_python":
        return await _execute_python(arguments["code"], working_dir)
    elif tool_name == "bash":
        return await _execute_bash(arguments["command"], working_dir)
    else:
        return {"error": f"Unknown tool: {tool_name}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_type: str, **data) -> dict:
    """Build a streaming event stamped with wall-clock time.

    Activities run non-deterministically, so they use the real clock (unlike
    the workflow, which stamps events with workflow.now()). The stream client
    handles JSON serialization via the data converter at flush time.
    """
    return make_event(event_type, datetime.now(timezone.utc).isoformat(), **data)


@contextlib.asynccontextmanager
async def _heartbeating(interval: float = HEARTBEAT_INTERVAL_SECONDS):
    """Heartbeat on a timer for the duration of the block.

    Cancellation is delivered to an activity on its heartbeat, and work inside
    can go quiet (a reasoning model before its first token, a long subprocess),
    so a steady timer keeps interrupts prompt and avoids tripping the heartbeat
    timeout during a lull. The task is cancelled and awaited on exit.
    """
    async def _loop() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(interval)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn
async def load_schema() -> str:
    """Load the database schema. Runs as an activity to keep I/O out of workflows."""
    return await asyncio.to_thread(_load_schema)


@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    """Stream a model call via the OpenAI Responses API.

    Publishes streaming events (THINKING_DELTA, TEXT_DELTA, etc.) to the
    workflow via WorkflowStreamClient. Returns structural data
    (response_id, tool_calls, final_text).
    """
    batch_interval = timedelta(
        seconds=float(os.environ.get("WORKFLOW_STREAM_BATCH_INTERVAL", "2.0"))
    )
    stream = WorkflowStreamClient.from_within_activity(batch_interval=batch_interval)
    info = activity.info()

    async with stream:
        events = stream.topic(EVENTS_TOPIC, type=dict)
        # Retry detection
        if info.attempt > 1:
            events.publish(_make_event(
                "RETRY",
                operation_id=input.operation_id,
                attempt=info.attempt,
                message="Retrying model call...",
            ), force_flush=True)

        oai_client = openai.AsyncOpenAI(max_retries=0)

        kwargs: dict = {
            "model": input.model,
            "tools": input.tools,
            "input": input.input_messages,
            "store": True,
        }
        if input.reasoning_effort:
            kwargs["reasoning"] = {"effort": input.reasoning_effort}
        if input.previous_response_id:
            kwargs["previous_response_id"] = input.previous_response_id

        tool_calls: dict[str, dict] = {}
        text_buffer = ""
        thinking_buffer = ""
        thinking_active = False
        response_id = ""
        token_usage: TokenUsage | None = None
        interrupted = False

        try:
            # This Responses-API event dispatch mirrors the one in the ephemeral
            # backend (backend-ephemeral/src/agent.py). They are deliberately not
            # shared: this one publishes to a workflow stream and is
            # interruptible; that one yields SSE inline and reclassifies pre-tool
            # text as thinking. Keep them in sync by hand.
            async with _heartbeating(), oai_client.responses.stream(**kwargs) as oai_stream:
                async for event in oai_stream:
                    event_type = getattr(event, "type", None)

                    # Thinking/reasoning events
                    if event_type == "response.reasoning_summary_text.delta":
                        delta = event.delta
                        if not thinking_active:
                            thinking_active = True
                            events.publish(_make_event("THINKING_START"))
                        thinking_buffer += delta
                        events.publish(_make_event("THINKING_DELTA", delta=delta))

                    elif event_type == "response.reasoning_summary_text.done":
                        if thinking_active:
                            events.publish(_make_event(
                                "THINKING_COMPLETE", content=thinking_buffer,
                            ), force_flush=True)
                            thinking_buffer = ""
                            thinking_active = False

                    # Text output — stream incrementally
                    elif event_type == "response.output_text.delta":
                        text_buffer += event.delta
                        events.publish(_make_event("TEXT_DELTA", delta=event.delta))

                    # Function call argument streaming
                    elif event_type == "response.function_call_arguments.delta":
                        item_id = event.item_id
                        if item_id not in tool_calls:
                            tool_calls[item_id] = {"name": None, "arguments_str": ""}
                        tool_calls[item_id]["arguments_str"] += event.delta

                    elif event_type == "response.function_call_arguments.done":
                        item_id = event.item_id
                        if item_id in tool_calls:
                            tool_calls[item_id]["arguments_str"] = event.arguments

                    # Output item added — captures function name and call_id
                    elif event_type == "response.output_item.added":
                        item = event.item
                        if getattr(item, "type", None) == "function_call":
                            item_id = getattr(item, "id", None)
                            call_id = getattr(item, "call_id", None)
                            name = item.name
                            if item_id:
                                tool_calls[item_id] = {
                                    "name": name,
                                    "call_id": call_id,
                                    "arguments_str": tool_calls.get(item_id, {}).get("arguments_str", ""),
                                }

                    # Response completed — capture response_id
                    elif event_type == "response.completed":
                        response = event.response
                        response_id = response.id
                        if response.usage:
                            u = response.usage
                            token_usage = TokenUsage(
                                input_tokens=u.input_tokens,
                                output_tokens=u.output_tokens,
                                reasoning_tokens=getattr(u.output_tokens_details, "reasoning_tokens", 0) or 0,
                                cached_tokens=getattr(u.input_tokens_details, "cached_tokens", 0) or 0,
                            )

        except asyncio.CancelledError:
            # Workflow interrupt. Stop streaming and return whatever text has
            # accumulated so far so the workflow can persist the partial turn.
            # We deliberately do NOT re-raise: returning lets the caller (which
            # uses WAIT_CANCELLATION_COMPLETED) receive the partial result
            # instead of a bare cancellation. Exiting the stream context above
            # aborts the in-flight OpenAI request.
            interrupted = True
        except openai.AuthenticationError as e:
            raise ApplicationError(
                f"Invalid API key: {e}",
                type="AuthenticationError",
                non_retryable=True,
            ) from e
        except openai.RateLimitError as e:
            raise ApplicationError(
                f"Rate limited: {e}",
                type="RateLimitError",
            ) from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise ApplicationError(
                    f"OpenAI server error ({e.status_code}): {e}",
                    type="ServerError",
                ) from e
            raise ApplicationError(
                f"OpenAI client error ({e.status_code}): {e}",
                type="ClientError",
                non_retryable=True,
            ) from e
        except openai.APIConnectionError as e:
            raise ApplicationError(
                f"Connection error: {e}",
                type="ConnectionError",
            ) from e

        # On the interrupt path we skip these extra publishes: the deltas have
        # already streamed, and we want minimal, non-blocking work before the
        # stream client flushes on context exit (a force_flush as the last
        # publish before __aexit__ can hang the activity on 1.27.0).
        if not interrupted:
            # Close thinking if still open
            if thinking_active:
                events.publish(_make_event("THINKING_COMPLETE", content=thinking_buffer))

            # Text was streamed incrementally as TEXT_DELTA. Emit completion.
            if text_buffer:
                events.publish(_make_event("TEXT_COMPLETE", text=text_buffer))

        # Context manager exit flushes remaining buffer

    # A non-interrupted call must have seen response.completed and captured a
    # response_id; the workflow chains the next turn off it via
    # previous_response_id. An empty id here means the stream ended without that
    # event, so fail rather than corrupt the chain with "". Retryable: a fresh
    # attempt usually reaches completion.
    if not interrupted and not response_id:
        raise ApplicationError(
            "Model stream ended without a response.completed event; no response_id captured",
            type="MissingResponseId",
        )

    # An interrupted call returns its partial text and no tool calls — the
    # workflow persists the partial and ends the turn rather than acting on
    # half-streamed tool arguments.
    parsed_tool_calls = []
    if not interrupted:
        for item_id, tc in tool_calls.items():
            try:
                arguments = json.loads(tc["arguments_str"])
            except json.JSONDecodeError:
                arguments = {}
            parsed_tool_calls.append(ToolCallInfo(
                item_id=item_id,
                call_id=tc.get("call_id", item_id),
                name=tc["name"],
                arguments=arguments,
            ))

    return ModelCallResult(
        response_id=response_id,
        tool_calls=parsed_tool_calls,
        final_text=text_buffer if (interrupted or not tool_calls) else None,
        usage=token_usage,
        interrupted=interrupted,
    )


@activity.defn
async def execute_tool(input: ToolInput) -> ToolResult:
    """Execute a tool and return its result.

    For retry scenarios, publishes a RETRY event before re-executing.
    """
    info = activity.info()

    # Retry detection
    if info.attempt > 1:
        stream = WorkflowStreamClient.from_within_activity()
        async with stream:
            events = stream.topic(EVENTS_TOPIC, type=dict)
            events.publish(_make_event(
                "RETRY",
                operation_id=input.operation_id,
                attempt=info.attempt,
                message=f"Retrying {input.tool_name}...",
            ), force_flush=True)

    working_dir = Path(input.working_dir)

    # Heartbeat on a timer while the tool runs so a cancellation can be
    # delivered (it only arrives on a heartbeat). For the subprocess tools
    # (python/bash) the CancelledError kills the process group. execute_sql
    # runs in a worker thread that can't be interrupted, so its cancellation
    # only takes effect once the (short, read-only) query returns.
    async with _heartbeating():
        result = await _run_tool(input.tool_name, input.arguments, working_dir)

    return ToolResult(
        call_id=input.call_id,
        tool_name=input.tool_name,
        result=result,
    )
