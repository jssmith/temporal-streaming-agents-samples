"""Temporal activities for the analytics agent."""

import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import openai
from temporalio import activity

from .database import get_connection, get_db_path
from .event_batcher import EventBatcher
from .types import (
    ModelCallInput,
    ModelCallResult,
    ToolCallInfo,
    ToolInput,
    ToolResult,
)

logger = logging.getLogger(__name__)

FORBIDDEN_PREFIXES = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE")
ROW_LIMIT = 500
TIMEOUT_SECONDS = 30

TOOL_DEFINITIONS = [
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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _execute_sql(query: str) -> dict:
    """Execute a read-only SQL query against the Chinook database."""
    stripped = query.strip().upper()
    for prefix in FORBIDDEN_PREFIXES:
        if stripped.startswith(prefix):
            return {"error": f"Write operations not allowed: {prefix}"}

    if "LIMIT" not in stripped:
        query = query.rstrip().rstrip(";") + f" LIMIT {ROW_LIMIT}"

    conn = get_connection(readonly=True)
    try:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"rows": rows, "row_count": len(rows)}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()


async def _execute_python(code: str, working_dir: Path) -> dict:
    """Execute Python code in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            cwd=str(working_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )

        result: dict = {}
        if stdout:
            result["output"] = stdout.decode()
        if stderr:
            result["error"] = stderr.decode()
        if not stdout and not stderr:
            result["output"] = "(no output)"
        return result

    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}


async def _execute_bash(command: str, working_dir: Path) -> dict:
    """Execute a shell command in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(working_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )

        output = (stdout.decode() if stdout else "") + (stderr.decode() if stderr else "")
        return {"output": output, "exit_code": proc.returncode}

    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_event(event_type: str, **data) -> dict:
    return {
        "type": event_type,
        "timestamp": _now_iso(),
        "data": data,
    }


async def _get_batcher(signal_name: str = "receive_events", interval: float = 2.0) -> EventBatcher:
    """Create an EventBatcher connected to the current workflow."""
    info = activity.info()
    client = activity.client()
    handle = client.get_workflow_handle(info.workflow_id)
    return EventBatcher(handle, signal_name, interval)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    """Stream a model call via the OpenAI Responses API.

    Signals streaming events (THINKING_DELTA, TEXT_DELTA, etc.) back to the
    workflow via batched signals. Returns structural data (response_id,
    tool_calls, final_text).
    """
    batcher = await _get_batcher()
    info = activity.info()

    # Retry detection
    if info.attempt > 1:
        batcher.add(_make_event(
            "RETRY",
            operation_id=input.operation_id,
            attempt=info.attempt,
            message="Retrying model call...",
        ))
        await batcher.flush()

    client = openai.AsyncOpenAI()

    kwargs: dict = {
        "model": input.model,
        "tools": input.tools,
        "input": input.input_messages,
        "store": True,
    }
    if input.previous_response_id:
        kwargs["previous_response_id"] = input.previous_response_id

    tool_calls: dict[str, dict] = {}
    text_buffer = ""
    thinking_buffer = ""
    thinking_active = False
    response_id = ""

    async def read_stream():
        nonlocal text_buffer, thinking_buffer, thinking_active, response_id

        async with client.responses.stream(**kwargs) as stream:
            async for event in stream:
                activity.heartbeat()
                event_type = getattr(event, "type", None)

                # Thinking/reasoning events
                if event_type == "response.reasoning_summary_text.delta":
                    delta = event.delta
                    if not thinking_active:
                        thinking_active = True
                        batcher.add(_make_event("THINKING_START"))
                    thinking_buffer += delta
                    batcher.add(_make_event("THINKING_DELTA", delta=delta))

                elif event_type == "response.reasoning_summary_text.done":
                    if thinking_active:
                        batcher.add(_make_event("THINKING_COMPLETE", content=thinking_buffer))
                        await batcher.flush()
                        thinking_buffer = ""
                        thinking_active = False

                # Text output — buffer, classify after stream
                elif event_type == "response.output_text.delta":
                    text_buffer += event.delta

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

    async def timer_flush():
        await batcher.run_flusher()

    # Run both tasks concurrently, cancel timer when stream completes
    completed, pending = await asyncio.wait(
        [asyncio.create_task(read_stream()), asyncio.create_task(timer_flush())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    # Re-raise any exceptions from the stream task
    for t in completed:
        t.result()

    # Close thinking if still open
    if thinking_active:
        batcher.add(_make_event("THINKING_COMPLETE", content=thinking_buffer))

    # Classify buffered text
    if tool_calls and text_buffer:
        # Text before tool calls = planning/explanation -> emit as thinking
        batcher.add(_make_event("THINKING_START"))
        batcher.add(_make_event("THINKING_DELTA", delta=text_buffer))
        batcher.add(_make_event("THINKING_COMPLETE", content=text_buffer))
        text_buffer = ""

    if not tool_calls and text_buffer:
        # Final answer text
        batcher.add(_make_event("TEXT_DELTA", delta=text_buffer))
        batcher.add(_make_event("TEXT_COMPLETE", text=text_buffer))

    # Final flush
    await batcher.flush()

    # Build tool call info
    parsed_tool_calls = []
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
        final_text=text_buffer if not tool_calls else None,
    )


@activity.defn
async def execute_tool(input: ToolInput) -> ToolResult:
    """Execute a tool and return its result.

    For retry scenarios, signals a RETRY event before re-executing.
    """
    batcher = await _get_batcher()
    info = activity.info()

    # Retry detection
    if info.attempt > 1:
        batcher.add(_make_event(
            "RETRY",
            operation_id=input.operation_id,
            attempt=info.attempt,
            message=f"Retrying {input.tool_name}...",
        ))
        await batcher.flush()

    working_dir = Path(input.working_dir)
    result = await _run_tool(input.tool_name, input.arguments, working_dir)

    return ToolResult(
        call_id=input.call_id,
        tool_name=input.tool_name,
        result=result,
    )
