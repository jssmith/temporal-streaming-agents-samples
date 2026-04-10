"""Temporal activities for the voice analytics agent.

The model_call activity streams the GPT-4.1 response, detects sentence
boundaries, generates TTS for each sentence, and publishes audio chunks
back to the workflow via PubSubClient.
"""

import asyncio
import base64
import json
import logging
import re

import openai
from temporalio import activity
from temporalio.contrib.pubsub import PubSubClient
from temporalio.exceptions import ApplicationError

from .database import load_schema as _load_schema
from .sql_tool import execute_sql as _execute_sql
from .types import (
    AUDIO_TOPIC,
    ModelCallInput,
    ModelCallResult,
    ToolCallInfo,
    TranscribeInput,
)

logger = logging.getLogger(__name__)

# Sentence boundary detection (same as agent.py)
_SENTENCE_END = re.compile(r'(?<=[.!?:])(?:\s|$)')
_MIN_FLUSH_LEN = 30


async def _generate_tts(text: str) -> str:
    """Generate TTS audio for text. Returns base64-encoded PCM."""
    client = openai.AsyncOpenAI()
    response = await client.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=text,
        response_format="pcm",
    )
    return base64.b64encode(response.content).decode()


@activity.defn
async def load_schema() -> str:
    """Load the database schema."""
    return await asyncio.to_thread(_load_schema)


@activity.defn
async def transcribe(input: TranscribeInput) -> str:
    """Transcribe audio using Whisper API."""
    audio_bytes = base64.b64decode(input.audio_base64)
    client = openai.AsyncOpenAI()
    transcript = await client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.wav", audio_bytes, "audio/wav"),
    )
    return transcript.text


@activity.defn
async def model_call(input: ModelCallInput) -> ModelCallResult:
    """Stream a model call via the OpenAI Responses API.

    Detects sentence boundaries in the streamed text, generates TTS for
    each sentence, and publishes audio chunks to the workflow's pub/sub
    log via PubSubClient. Returns structural data (response_id, tool_calls,
    final_text).
    """
    pubsub = PubSubClient.create(batch_interval=0.1)
    client = openai.AsyncOpenAI(max_retries=0)

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
    full_text = ""
    response_id = ""

    async def send_sentence_audio(sentence: str) -> None:
        """Generate TTS for a sentence and publish it to the pub/sub log."""
        audio_b64 = await _generate_tts(sentence)
        pubsub.publish(
            AUDIO_TOPIC,
            json.dumps({"audio_base64": audio_b64}).encode(),
            priority=True,
        )

    try:
        async with pubsub:
            async with client.responses.stream(**kwargs) as stream:
                async for event in stream:
                    activity.heartbeat()
                    event_type = getattr(event, "type", None)

                    # Text output — buffer and detect sentence boundaries
                    if event_type == "response.output_text.delta":
                        text_buffer += event.delta
                        full_text += event.delta

                        # Check for sentence boundary to fire TTS
                        if len(text_buffer) >= _MIN_FLUSH_LEN:
                            match = _SENTENCE_END.search(text_buffer)
                            if match:
                                sentence = text_buffer[:match.end()].strip()
                                text_buffer = text_buffer[match.end():]
                                if sentence:
                                    await send_sentence_audio(sentence)

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
                        response_id = event.response.id

            # Flush remaining text as final sentence (after stream closes, still inside pubsub context)
            if text_buffer.strip():
                await send_sentence_audio(text_buffer.strip())

        # pubsub context manager flushes remaining buffer on exit

    except openai.AuthenticationError as e:
        raise ApplicationError(
            f"Invalid API key: {e}",
            type="AuthenticationError",
            non_retryable=True,
        )
    except openai.RateLimitError as e:
        raise ApplicationError(f"Rate limited: {e}", type="RateLimitError")
    except openai.APIStatusError as e:
        if e.status_code >= 500:
            raise ApplicationError(
                f"OpenAI server error ({e.status_code}): {e}",
                type="ServerError",
            )
        raise ApplicationError(
            f"OpenAI client error ({e.status_code}): {e}",
            type="ClientError",
            non_retryable=True,
        )
    except openai.APIConnectionError as e:
        raise ApplicationError(f"Connection error: {e}", type="ConnectionError")

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
        final_text=full_text.strip() if full_text and not tool_calls else None,
    )


@activity.defn
async def execute_sql(query: str) -> dict:
    """Execute a read-only SQL query against the Chinook database."""
    return _execute_sql(query)
