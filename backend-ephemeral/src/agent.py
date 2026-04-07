"""Agent loop using the OpenAI Responses API directly."""

import asyncio
import json
import logging
from typing import AsyncGenerator

import openai

from .database import load_schema
from .events import SSEEvent
from .sessions import Session
from .tools import TOOL_DEFINITIONS, run_tool

logger = logging.getLogger(__name__)

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


def _build_system_prompt(session_id: str) -> str:
    schema = load_schema()
    return SYSTEM_PROMPT_TEMPLATE.format(schema=schema).replace("SESSION_ID", session_id)


async def run_agent_turn(
    session: Session,
    message: str,
) -> AsyncGenerator[SSEEvent, None]:
    """Run one turn of the agent loop, yielding SSE events.

    The loop:
    1. Send conversation to Responses API (streamed)
    2. Stream the response, emitting SSE events
    3. If tool calls requested, execute them and loop with tool outputs
    4. Continue until the model produces a final text response
    """
    client = openai.AsyncOpenAI()

    yield SSEEvent.user_message(message)
    yield SSEEvent.agent_start()

    system_prompt = _build_system_prompt(session.session_id)

    # First call: full conversation history
    input_messages = [{"role": "system", "content": system_prompt}]
    for msg in session.messages:
        input_messages.append(msg)
    input_messages.append({"role": "user", "content": message})

    # After tool calls, we only send tool outputs with previous_response_id
    tool_outputs_for_next_call: list[dict] | None = None

    while True:
        kwargs: dict = {
            "model": "gpt-5.4-mini",
            "tools": TOOL_DEFINITIONS,
            "store": True,
        }

        if tool_outputs_for_next_call is not None:
            # Continuation after tool calls: use previous_response_id + tool outputs only
            kwargs["input"] = tool_outputs_for_next_call
            kwargs["previous_response_id"] = session.response_id
        else:
            # First call of this turn
            kwargs["input"] = input_messages
            if session.response_id:
                kwargs["previous_response_id"] = session.response_id

        # Stream the response
        # Keyed by item_id (fc_...) -> {name, call_id (call_...), arguments_str}
        tool_calls: dict[str, dict] = {}
        text_buffer = ""
        thinking_buffer = ""
        thinking_active = False

        async with client.responses.stream(**kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                # Track sequence numbers for resume
                seq = getattr(event, "sequence_number", None)
                if seq is not None:
                    session.sequence_number = seq

                # Thinking/reasoning events (from reasoning models)
                if event_type == "response.reasoning_summary_text.delta":
                    delta = event.delta
                    if not thinking_active:
                        thinking_active = True
                        yield SSEEvent.thinking_start()
                    thinking_buffer += delta
                    yield SSEEvent.thinking_delta(delta)

                elif event_type == "response.reasoning_summary_text.done":
                    if thinking_active:
                        yield SSEEvent.thinking_complete(thinking_buffer)
                        thinking_buffer = ""
                        thinking_active = False

                # Text output events — buffer only, classify after stream completes
                elif event_type == "response.output_text.delta":
                    text_buffer += event.delta

                elif event_type == "response.output_text.done":
                    pass  # Will emit after stream completes based on context

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

                # Output item added — captures the function name and call_id
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
                    session.response_id = response.id

        # Close thinking if still open
        if thinking_active:
            yield SSEEvent.thinking_complete(thinking_buffer)
            thinking_active = False

        # Classify buffered text based on whether tool calls follow
        if tool_calls and text_buffer:
            # Text before tool calls = planning/explanation → emit as thinking
            yield SSEEvent.thinking_start()
            yield SSEEvent.thinking_delta(text_buffer)
            yield SSEEvent.thinking_complete(text_buffer)
            text_buffer = ""

        # If no tool calls, this is the final answer
        if not tool_calls:
            if text_buffer:
                # Emit buffered text as TEXT events
                yield SSEEvent.text_delta(text_buffer)
                yield SSEEvent.text_complete(text_buffer)
                session.messages.append({"role": "user", "content": message})
                session.messages.append({"role": "assistant", "content": text_buffer})
            break

        # Execute tool calls (parallel if multiple)
        # parsed_calls: list of (item_id, call_id, tool_name, arguments)
        parsed_calls = []
        for item_id, tc in tool_calls.items():
            try:
                arguments = json.loads(tc["arguments_str"])
            except json.JSONDecodeError:
                arguments = {}
            parsed_calls.append((item_id, tc["call_id"], tc["name"], arguments))

        # Emit all TOOL_CALL_START events immediately (use call_id for frontend)
        for item_id, call_id, tool_name, arguments in parsed_calls:
            yield SSEEvent.tool_call_start(call_id or item_id, tool_name, arguments)

        # Execute tools and emit completions as each finishes (not batched)
        async def _execute(item_id: str, call_id: str, tool_name: str, arguments: dict):
            result = await run_tool(tool_name, arguments, session.working_dir)
            return item_id, call_id, tool_name, result

        tasks = [
            asyncio.create_task(_execute(iid, cid, tname, args))
            for iid, cid, tname, args in parsed_calls
        ]

        # Stream completions as each task finishes
        tool_results = []
        for coro in asyncio.as_completed(tasks):
            item_id, call_id, tool_name, result = await coro
            tool_results.append((item_id, call_id, tool_name, result))
            error = result.get("error")
            frontend_id = call_id or item_id
            if error:
                yield SSEEvent.tool_call_complete(frontend_id, tool_name, error=error)
            else:
                yield SSEEvent.tool_call_complete(frontend_id, tool_name, result=result)

        # Build tool outputs for the next API call (must use call_id)
        tool_outputs_for_next_call = []
        for item_id, call_id, tool_name, result in tool_results:
            tool_outputs_for_next_call.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            })

        # Reset for next iteration
        tool_calls = {}
        text_buffer = ""

    yield SSEEvent.agent_complete()
