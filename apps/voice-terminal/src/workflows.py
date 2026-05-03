"""Voice analytics workflow — durable voice-to-SQL agent loop."""

from __future__ import annotations

import json
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from analytics_shared.constants import EVENTS_TOPIC
    from analytics_shared.sql_tool import TOOL_DEFINITION  # schema only

    from .types import (
        ModelCallInput,
        ModelCallResult,
        StartTurnInput,
        TranscribeInput,
        VoiceWorkflowState,
    )

logger = workflow.logger

MODEL = "gpt-4.1"

END_SESSION_TOOL = {
    "type": "function",
    "name": "end_session",
    "description": (
        "End the conversation and shut down the workflow. Call this only when "
        "the user clearly indicates they are done — saying goodbye, 'we're done', "
        "'that's all', etc. Speak a brief farewell to the user before calling it."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SYSTEM_PROMPT_TEMPLATE = """You are a voice analytics assistant with access to a Chinook music store database (SQLite).

You have two tools:
- execute_sql: Run read-only SQL queries against the database.
- end_session: End the conversation. Call this when the user says goodbye or otherwise indicates they're done. Speak a brief farewell first.

Keep your spoken responses concise and conversational. When presenting data, summarize the key
findings rather than reading out entire tables.

When a question requires data, run the SQL query first, then speak the answer naturally.

Database schema:
{schema}"""


@workflow.defn
class VoiceAnalyticsWorkflow:

    @workflow.init
    def __init__(self, state: VoiceWorkflowState) -> None:
        self.stream = WorkflowStream(prior_state=state.stream_state)
        self.events = self.stream.topic(EVENTS_TOPIC, type=dict)
        self._messages: list[dict] = state.messages
        self._response_id: str | None = state.response_id
        self._schema: str | None = state.db_schema
        self._closed: bool = state.closed
        self._turn_active: bool = False
        self._pending_audio: str | None = state.pending_audio
        # When the agent itself ends the session via the end_session tool,
        # we hold the workflow open until the client has drained the final
        # SESSION_CLOSED / TURN_COMPLETE events via truncate. Without this,
        # the workflow completes before subscribe can deliver them and the
        # client tries to start another turn against a closed workflow.
        self._end_session_requested: bool = False

    # -- helpers --

    def _emit(self, event_type: str, **data) -> None:
        self.events.publish({
            "type": event_type,
            "timestamp": workflow.now().isoformat(),
            "data": data,
        })

    # -- signals --

    @workflow.signal
    def start_turn(self, input: StartTurnInput) -> None:
        self._pending_audio = input.audio_base64

    @workflow.signal
    def close_session(self) -> None:
        self._closed = True

    @workflow.signal
    def truncate(self, up_to_offset: int) -> None:
        """Client signals that it has consumed events up to this offset."""
        self.stream.truncate(up_to_offset)

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
                # If the agent itself ended the session, give the client a
                # chance to consume the final events before we exit.
                # Truncation by the client clears _log; once it's empty the
                # subscriber has acknowledged everything we published.
                if self._end_session_requested:
                    try:
                        await workflow.wait_condition(
                            lambda: len(self.stream._log) == 0,
                            timeout=timedelta(seconds=10),
                        )
                    except TimeoutError:
                        # Best-effort drain; client may have died.
                        pass
                return

            audio_b64: str = self._pending_audio  # type: ignore[assignment]
            self._pending_audio = None
            self._turn_active = True

            await self._run_turn(audio_b64)

            self._turn_active = False

            if workflow.info().is_continue_as_new_suggested() and not self._closed:
                # Wait for the client's per-turn truncate to catch up before
                # snapshotting. Without this, the un-acked audio for the just-
                # finished turn rides into the CAN args and can exceed the
                # ~4 MB Temporal payload limit (max_output_tokens=700 allows
                # ~11 MB on the wire). When the in-memory log is empty,
                # truncation has caught up to whatever's been published.
                await workflow.wait_condition(
                    lambda: len(self.stream._log) == 0 or self._closed
                )
                if self._closed:
                    return
                # closed/pending_audio reflect any signals processed during
                # the wait above — they're carried into the new run so a
                # close_session or start_turn arriving in the handoff window
                # isn't dropped.
                await self.stream.continue_as_new(lambda state: [VoiceWorkflowState(
                    messages=self._messages,
                    response_id=self._response_id,
                    db_schema=self._schema,
                    stream_state=state,
                    pending_audio=self._pending_audio,
                    closed=self._closed,
                )])

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

        self._messages.append({"role": "user", "content": transcript})

        # 2. Agent loop (model call + tool execution)
        self._emit("STATUS", text="Thinking...")
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(schema=self._schema)
        input_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in self._messages:
            input_messages.append({"role": msg["role"], "content": msg["content"]})

        tool_outputs: list[dict] | None = None
        end_session_called = False

        while not end_session_called:
            if tool_outputs is not None:
                self._emit("STATUS", text="Processing results...")
                call_input = ModelCallInput(
                    input_messages=tool_outputs,
                    previous_response_id=self._response_id,
                    tools=[TOOL_DEFINITION, END_SESSION_TOOL],
                    model=MODEL,
                )
            else:
                call_input = ModelCallInput(
                    input_messages=input_messages,
                    previous_response_id=self._response_id,
                    tools=[TOOL_DEFINITION, END_SESSION_TOOL],
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
                # model_call activity via WorkflowStreamClient.
                if model_result.final_text:
                    self._messages.append({
                        "role": "assistant",
                        "content": model_result.final_text,
                    })
                    self._emit("RESPONSE_TEXT", text=model_result.final_text)
                break

            # Dispatch each tool call. end_session is handled in-workflow
            # (no activity); other tools route to execute_sql.
            tool_outputs = []
            for tc in model_result.tool_calls:
                if tc.name == "end_session":
                    self._emit("TOOL_CALL", name=tc.name, arguments=tc.arguments, result={})
                    end_session_called = True
                    continue

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

            # If every tool in this batch was end_session, there's nothing
            # to feed back to the model — break out of the agent loop.
            if not tool_outputs:
                break

        if end_session_called:
            self._emit("SESSION_CLOSED")
        self._emit("TURN_COMPLETE")
        if end_session_called:
            self._end_session_requested = True
            self._closed = True
