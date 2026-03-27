"""Streaming agent loop with SQL tool calling.

Streams GPT-4.1 response, detects sentence boundaries, and fires callbacks
for incremental TTS. Executes SQL tool calls inline (fast, synchronous).
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import openai

from .database import load_schema
from .sql_tool import TOOL_DEFINITION, execute_sql

logger = logging.getLogger(__name__)

MODEL = "gpt-4.1"

SYSTEM_PROMPT_TEMPLATE = """You are a voice analytics assistant with access to a Chinook music store database (SQLite).

You have one tool:
- execute_sql: Run read-only SQL queries against the database

Keep your spoken responses concise and conversational. When presenting data, summarize the key
findings rather than reading out entire tables. For example, say "The top 3 artists by sales are
AC/DC, Metallica, and U2" rather than listing every column.

When a question requires data, run the SQL query first, then speak the answer naturally.

Database schema:
{schema}"""

# Sentence boundary: punctuation followed by space or end of string
_SENTENCE_END = re.compile(r'(?<=[.!?:])(?:\s|$)')
_MIN_FLUSH_LEN = 30  # minimum chars before flushing a sentence


@dataclass
class ToolCallLog:
    name: str
    arguments: dict
    result: dict


@dataclass
class TurnResult:
    response_text: str
    response_id: str | None
    tool_calls: list[ToolCallLog] = field(default_factory=list)


async def run_turn(
    message: str,
    conversation: list[dict],
    previous_response_id: str | None = None,
    on_sentence: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call: Callable[[str, dict, dict], None] | None = None,
) -> TurnResult:
    """Run one agent turn: stream GPT-4.1, execute SQL tools, return result.

    Args:
        message: User's transcribed message
        conversation: Conversation history (mutated — new messages appended)
        previous_response_id: OpenAI response ID for conversation continuation
        on_sentence: Async callback fired for each complete sentence (for TTS)
        on_tool_call: Sync callback fired after each tool execution
    """
    schema = load_schema()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(schema=schema)

    client = openai.AsyncOpenAI()

    conversation.append({"role": "user", "content": message})

    # Build input messages
    input_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for msg in conversation:
        input_messages.append({"role": msg["role"], "content": msg["content"]})

    tool_calls_log: list[ToolCallLog] = []
    response_id = previous_response_id
    tool_outputs: list[dict] | None = None

    while True:
        kwargs: dict = {
            "model": MODEL,
            "tools": [TOOL_DEFINITION],
            "store": True,
        }
        if tool_outputs is not None:
            kwargs["input"] = tool_outputs
            kwargs["previous_response_id"] = response_id
        else:
            kwargs["input"] = input_messages
            if response_id:
                kwargs["previous_response_id"] = response_id

        text_buffer = ""
        full_text = ""  # accumulates all text for the final result
        pending_tool_calls: dict[str, dict] = {}

        async with client.responses.stream(**kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "response.output_text.delta":
                    text_buffer += event.delta
                    full_text += event.delta

                    # Check for sentence boundary to fire TTS
                    if on_sentence and len(text_buffer) >= _MIN_FLUSH_LEN:
                        match = _SENTENCE_END.search(text_buffer)
                        if match:
                            sentence = text_buffer[:match.end()].strip()
                            text_buffer = text_buffer[match.end():]
                            if sentence:
                                await on_sentence(sentence)

                elif event_type == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", None) == "function_call":
                        item_id = getattr(item, "id", None)
                        if item_id:
                            pending_tool_calls[item_id] = {
                                "name": item.name,
                                "call_id": getattr(item, "call_id", item_id),
                                "arguments_str": "",
                            }

                elif event_type == "response.function_call_arguments.delta":
                    item_id = event.item_id
                    if item_id in pending_tool_calls:
                        pending_tool_calls[item_id]["arguments_str"] += event.delta

                elif event_type == "response.function_call_arguments.done":
                    item_id = event.item_id
                    if item_id in pending_tool_calls:
                        pending_tool_calls[item_id]["arguments_str"] = event.arguments

                elif event_type == "response.completed":
                    response_id = event.response.id

        # Flush remaining text buffer to TTS
        if text_buffer.strip() and on_sentence:
            await on_sentence(text_buffer.strip())

        # If no tool calls, we're done
        if not pending_tool_calls:
            conversation.append({"role": "assistant", "content": full_text.strip()})
            return TurnResult(
                response_text=full_text.strip(),
                response_id=response_id,
                tool_calls=tool_calls_log,
            )

        # Execute tool calls
        tool_outputs = []
        for item_id, tc in pending_tool_calls.items():
            try:
                arguments = json.loads(tc["arguments_str"])
            except json.JSONDecodeError:
                arguments = {}

            result = execute_sql(arguments.get("query", ""))

            tool_calls_log.append(ToolCallLog(
                name=tc["name"],
                arguments=arguments,
                result=result,
            ))

            if on_tool_call:
                on_tool_call(tc["name"], arguments, result)

            tool_outputs.append({
                "type": "function_call_output",
                "call_id": tc["call_id"],
                "output": json.dumps(result),
            })
