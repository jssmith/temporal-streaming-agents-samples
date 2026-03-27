"""Voice analytics workflow — durable voice-to-SQL agent loop."""

from __future__ import annotations

import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .sql_tool import TOOL_DEFINITION  # just the schema, no execution
    from .types import (
        AckAudioInput,
        ActivityEventsInput,
        ModelCallInput,
        ModelCallResult,
        PollAudioInput,
        PollAudioResult,
        StartTurnInput,
        ToolCallSummary,
        TranscribeInput,
        VoiceWorkflowState,
    )

logger = workflow.logger

MODEL = "gpt-4.1"

SYSTEM_PROMPT_TEMPLATE = """You are a voice analytics assistant with access to a Chinook music store database (SQLite).

You have one tool:
- execute_sql: Run read-only SQL queries against the database

Keep your spoken responses concise and conversational. When presenting data, summarize the key
findings rather than reading out entire tables.

When a question requires data, run the SQL query first, then speak the answer naturally.

Database schema:
{schema}"""


@workflow.defn
class VoiceAnalyticsWorkflow:

    @workflow.init
    def __init__(self, state: VoiceWorkflowState) -> None:
        self._messages: list[dict] = state.messages
        self._response_id: str | None = state.response_id
        self._schema: str | None = state.db_schema
        self._closed: bool = False
        self._interrupted: bool = False
        # Turn state
        self._turn_active: bool = False
        self._turn_complete: bool = True
        self._pending_audio: str | None = None
        # Audio chunks + metadata for client polling.
        # _audio_base_index tracks how many chunks have been discarded,
        # so absolute index = _audio_base_index + position in _audio_chunks.
        self._audio_chunks: list[str] = []
        self._audio_base_index: int = 0
        self._transcript: str | None = None
        self._response_text: str | None = None
        self._tool_calls: list[dict] = []

    # -- signals --

    @workflow.signal
    def start_turn(self, input: StartTurnInput) -> None:
        self._pending_audio = input.audio_base64

    @workflow.signal
    def interrupt(self) -> None:
        self._interrupted = True

    @workflow.signal
    def close_session(self) -> None:
        self._closed = True

    @workflow.signal
    def receive_events(self, input: ActivityEventsInput) -> None:
        """Receive batched events from activities (audio chunks, etc.)."""
        for event in input.events:
            if event.get("type") == "AUDIO_CHUNK":
                audio_b64 = event.get("audio_base64")
                if audio_b64:
                    self._audio_chunks.append(audio_b64)

    @workflow.signal
    def ack_audio(self, input: AckAudioInput) -> None:
        """Client acknowledges consumed audio chunks. Discard them to free memory."""
        discard_count = input.through_index - self._audio_base_index
        if discard_count > 0:
            self._audio_chunks = self._audio_chunks[discard_count:]
            self._audio_base_index = input.through_index

    # -- update (long-poll for audio chunks) --

    # Cap poll response to ~1MB of base64 audio data
    _MAX_POLL_BYTES = 1_000_000

    def _abs_len(self) -> int:
        """Absolute index of the end of the audio chunk list."""
        return self._audio_base_index + len(self._audio_chunks)

    @workflow.update
    async def poll_audio(self, input: PollAudioInput) -> PollAudioResult:
        await workflow.wait_condition(
            lambda: self._abs_len() > input.last_seen_index
            or self._turn_complete,
            timeout=300,
        )
        # Convert absolute index to position in current list
        start_pos = max(0, input.last_seen_index - self._audio_base_index)

        # Return chunks up to the size cap
        chunks_to_return: list[str] = []
        total_bytes = 0
        pos = start_pos
        while pos < len(self._audio_chunks):
            chunk = self._audio_chunks[pos]
            chunk_size = len(chunk)
            if total_bytes + chunk_size > self._MAX_POLL_BYTES and chunks_to_return:
                break  # would exceed cap, stop (but always return at least 1)
            chunks_to_return.append(chunk)
            total_bytes += chunk_size
            pos += 1

        next_abs_index = self._audio_base_index + pos
        return PollAudioResult(
            audio_chunks=chunks_to_return,
            next_index=next_abs_index,
            transcript=self._transcript,
            response_text=self._response_text if self._turn_complete else None,
            tool_calls=self._tool_calls,
            turn_complete=self._turn_complete and pos >= len(self._audio_chunks),
        )

    # -- query --

    @workflow.query
    def get_state(self) -> dict:
        return {
            "messages": self._messages,
            "turn_active": self._turn_active,
        }

    # -- main loop --

    @workflow.run
    async def run(self, state: VoiceWorkflowState) -> None:
        if self._schema is None:
            self._schema = await workflow.execute_activity(
                "load_schema",
                start_to_close_timeout=timedelta(seconds=10),
                result_type=str,
            )

        while True:
            await workflow.wait_condition(
                lambda: self._pending_audio is not None or self._closed
            )
            if self._closed:
                return

            audio_b64: str = self._pending_audio  # type: ignore[assignment]
            self._pending_audio = None
            self._turn_complete = False
            self._turn_active = True
            self._interrupted = False
            self._audio_chunks = []
            self._audio_base_index = 0
            self._transcript = None
            self._response_text = None
            self._tool_calls = []

            await self._run_turn(audio_b64)

            self._turn_complete = True
            self._turn_active = False

            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[VoiceWorkflowState(
                    messages=self._messages,
                    response_id=self._response_id,
                    db_schema=self._schema,
                )])

    async def _run_turn(self, audio_b64: str) -> None:
        retry_policy = RetryPolicy(maximum_attempts=3)

        # 1. Transcribe
        transcript: str = await workflow.execute_activity(
            "transcribe",
            TranscribeInput(audio_base64=audio_b64),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
            result_type=str,
        )
        self._transcript = transcript

        if self._interrupted:
            return

        self._messages.append({"role": "user", "content": transcript})

        # 2. Agent loop (model call + tool execution)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(schema=self._schema)
        input_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in self._messages:
            input_messages.append({"role": msg["role"], "content": msg["content"]})

        tool_outputs: list[dict] | None = None

        while not self._interrupted:
            if tool_outputs is not None:
                call_input = ModelCallInput(
                    input_messages=tool_outputs,
                    previous_response_id=self._response_id,
                    tools=[TOOL_DEFINITION],
                    model=MODEL,
                )
            else:
                call_input = ModelCallInput(
                    input_messages=input_messages,
                    previous_response_id=self._response_id,
                    tools=[TOOL_DEFINITION],
                    model=MODEL,
                )

            model_result: ModelCallResult = await workflow.execute_activity(
                "model_call",
                call_input,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
                heartbeat_timeout=timedelta(seconds=30),
                result_type=ModelCallResult,
            )

            self._response_id = model_result.response_id

            if not model_result.tool_calls:
                # TTS audio chunks were already signaled back by the
                # model_call activity as sentences completed.
                if model_result.final_text:
                    self._messages.append({
                        "role": "assistant",
                        "content": model_result.final_text,
                    })
                    self._response_text = model_result.final_text
                break

            # Execute SQL tool calls via activity
            tool_outputs = []
            for tc in model_result.tool_calls:
                result: dict = await workflow.execute_activity(
                    "execute_sql",
                    tc.arguments.get("query", ""),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                    result_type=dict,
                )

                self._tool_calls.append(ToolCallSummary(
                    name=tc.name,
                    arguments=tc.arguments,
                    result=result,
                ).model_dump())

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": tc.call_id,
                    "output": json.dumps(result),
                })
