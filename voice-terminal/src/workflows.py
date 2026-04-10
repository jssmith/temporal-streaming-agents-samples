"""Voice analytics workflow — durable voice-to-SQL agent loop."""

from __future__ import annotations

import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.pubsub import PubSubMixin

with workflow.unsafe.imports_passed_through():
    from .sql_tool import TOOL_DEFINITION  # just the schema, no execution
    from .types import (
        EVENTS_TOPIC,
        ModelCallInput,
        ModelCallResult,
        StartTurnInput,
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
class VoiceAnalyticsWorkflow(PubSubMixin):

    @workflow.init
    def __init__(self, state: VoiceWorkflowState) -> None:
        self.init_pubsub(prior_state=state.pubsub_state)
        self._messages: list[dict] = state.messages
        self._response_id: str | None = state.response_id
        self._schema: str | None = state.db_schema
        self._closed: bool = False
        self._interrupted: bool = False
        self._turn_active: bool = False
        self._pending_audio: str | None = None

    # -- helpers --

    def _emit(self, event_type: str, **data) -> None:
        event = {
            "type": event_type,
            "timestamp": workflow.now().isoformat(),
            "data": data,
        }
        self.publish(EVENTS_TOPIC, json.dumps(event).encode())

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
    def truncate(self, up_to_offset: int) -> None:
        """Client signals that it has consumed events up to this offset."""
        self.truncate_pubsub(up_to_offset)

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
            self._turn_active = True
            self._interrupted = False

            await self._run_turn(audio_b64)

            self._turn_active = False

            # NOTE: continue-as-new is disabled for now. The pub/sub
            # subscription has in-flight polls that race with truncation
            # during CAN, causing "offset before base offset" crashes.
            # Voice sessions are short-lived; CAN can be re-enabled once
            # the pub/sub mixin handles truncated offsets gracefully.

    async def _run_turn(self, audio_b64: str) -> None:
        retry_policy = RetryPolicy(maximum_attempts=3)

        # 1. Transcribe
        self._emit("STATUS", text="Transcribing...")
        transcript: str = await workflow.execute_activity(
            "transcribe",
            TranscribeInput(audio_base64=audio_b64),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
            result_type=str,
        )
        self._emit("TRANSCRIPT", text=transcript)

        if self._interrupted:
            self._emit("TURN_COMPLETE")
            return

        self._messages.append({"role": "user", "content": transcript})

        # 2. Agent loop (model call + tool execution)
        self._emit("STATUS", text="Thinking...")
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(schema=self._schema)
        input_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in self._messages:
            input_messages.append({"role": msg["role"], "content": msg["content"]})

        tool_outputs: list[dict] | None = None

        while not self._interrupted:
            if tool_outputs is not None:
                self._emit("STATUS", text="Processing results...")
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
                # TTS audio chunks were already published by the
                # model_call activity via PubSubClient.
                if model_result.final_text:
                    self._messages.append({
                        "role": "assistant",
                        "content": model_result.final_text,
                    })
                    self._emit("RESPONSE_TEXT", text=model_result.final_text)
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

                self._emit(
                    "TOOL_CALL",
                    name=tc.name,
                    arguments=tc.arguments,
                    result=result,
                )

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": tc.call_id,
                    "output": json.dumps(result),
                })

        self._emit("TURN_COMPLETE")
