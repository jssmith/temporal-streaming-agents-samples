"""E2E tests for the voice analytics workflow with mocked activities.

Tests the pub/sub integration: event delivery, truncation, offset tracking,
and continue-as-new — all with realistic-sized audio payloads but no real
OpenAI calls or microphone input.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pubsub import PubSubClient, PubSubItem, PubSubMixin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from src.types import (
    AUDIO_TOPIC,
    EVENTS_TOPIC,
    ModelCallInput,
    ModelCallResult,
    StartTurnInput,
    ToolCallInfo,
    TranscribeInput,
    VoiceWorkflowState,
)
from src.workflows import VoiceAnalyticsWorkflow


# ---------------------------------------------------------------------------
# Test workflow: force CAN after every turn
# ---------------------------------------------------------------------------


@workflow.defn(name="VoiceWorkflowForceCAN")
class VoiceWorkflowForceCAN(PubSubMixin):
    """Test workflow that continues-as-new on explicit signal."""

    @workflow.init
    def __init__(self, state: VoiceWorkflowState) -> None:
        self.init_pubsub(prior_state=state.pubsub_state)
        self._messages: list[dict] = state.messages
        self._schema: str | None = state.db_schema
        self._closed = False
        self._should_can = False
        self._pending_audio: str | None = None

    def _emit(self, event_type: str, **data) -> None:
        event = {"type": event_type, "data": data}
        self.publish(EVENTS_TOPIC, json.dumps(event).encode())

    @workflow.signal
    def start_turn(self, input: StartTurnInput) -> None:
        self._pending_audio = input.audio_base64

    @workflow.signal
    def close_session(self) -> None:
        self._closed = True

    @workflow.signal
    def truncate(self, up_to_offset: int) -> None:
        self.truncate_pubsub(up_to_offset)

    @workflow.signal
    def trigger_can(self) -> None:
        self._should_can = True

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
                lambda: self._pending_audio is not None
                or self._closed
                or self._should_can
            )
            if self._closed:
                return

            if self._should_can:
                self._should_can = False
                self.drain_pubsub()
                await workflow.wait_condition(workflow.all_handlers_finished)
                end_offset = self._pubsub_base_offset + len(self._pubsub_log)
                self.truncate_pubsub(end_offset)
                workflow.continue_as_new(args=[VoiceWorkflowState(
                    messages=self._messages,
                    db_schema=self._schema,
                    pubsub_state=self.get_pubsub_state(),
                )])

            audio_b64 = self._pending_audio
            self._pending_audio = None

            self._emit("STATUS", text="Transcribing...")
            transcript = await workflow.execute_activity(
                "transcribe",
                TranscribeInput(audio_base64=audio_b64),
                start_to_close_timeout=timedelta(seconds=10),
                result_type=str,
            )
            self._emit("TRANSCRIPT", text=transcript)

            result = await workflow.execute_activity(
                "model_call_large",
                ModelCallInput(
                    input_messages=[
                        {"type": "function_call_output", "output": "{}"}
                    ],
                    previous_response_id=None,
                    tools=[],
                    model="test",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=10),
                result_type=ModelCallResult,
            )
            if result.final_text:
                self._emit("RESPONSE_TEXT", text=result.final_text)
            self._emit("TURN_COMPLETE")


# ---------------------------------------------------------------------------
# Fake activities — deterministic, no network
# ---------------------------------------------------------------------------


def _fake_pcm(size_bytes: int) -> str:
    """Generate a base64-encoded fake PCM payload of approximately size_bytes."""
    return base64.b64encode(os.urandom(size_bytes)).decode()


@activity.defn(name="load_schema")
async def fake_load_schema() -> str:
    return "CREATE TABLE Artist (ArtistId INTEGER PRIMARY KEY, Name TEXT);"


@activity.defn(name="transcribe")
async def fake_transcribe(input: TranscribeInput) -> str:
    return "How many artists are in the database?"


@activity.defn(name="model_call")
async def fake_model_call(input: ModelCallInput) -> ModelCallResult:
    """Simulate a model call that publishes TTS audio via pub/sub."""
    pubsub = PubSubClient.create(batch_interval=0.05)

    has_tool_outputs = any(
        m.get("type") == "function_call_output" for m in input.input_messages
    )

    if not has_tool_outputs:
        # First call: request a tool call
        return ModelCallResult(
            response_id="resp_fake_1",
            tool_calls=[ToolCallInfo(
                item_id="item_1",
                call_id="call_1",
                name="execute_sql",
                arguments={"query": "SELECT COUNT(*) FROM Artist"},
            )],
            final_text=None,
        )

    # Second call: generate response with TTS audio
    async with pubsub:
        # Publish multiple audio chunks of varying sizes
        for i in range(3):
            chunk_size = 50_000 + i * 25_000  # 50KB, 75KB, 100KB
            audio_b64 = _fake_pcm(chunk_size)
            pubsub.publish(
                AUDIO_TOPIC,
                json.dumps({"audio_base64": audio_b64}).encode(),
                priority=True,
            )
            activity.heartbeat()

    return ModelCallResult(
        response_id="resp_fake_2",
        tool_calls=[],
        final_text="There are 275 artists in the database.",
    )


@activity.defn(name="model_call_large")
async def fake_model_call_large(input: ModelCallInput) -> ModelCallResult:
    """Model call that produces many large audio chunks (stress test)."""
    pubsub = PubSubClient.create(batch_interval=0.05)

    async with pubsub:
        # 10 chunks of ~100KB each ≈ 1MB total
        for i in range(10):
            audio_b64 = _fake_pcm(100_000)
            pubsub.publish(
                AUDIO_TOPIC,
                json.dumps({"audio_base64": audio_b64}).encode(),
                priority=True,
            )
            activity.heartbeat()

    return ModelCallResult(
        response_id="resp_large",
        tool_calls=[],
        final_text="Here is a very detailed answer with lots of audio.",
    )


@activity.defn(name="execute_sql")
async def fake_execute_sql(query: str) -> dict:
    return {"rows": [{"count": 275}], "row_count": 1}


FAKE_ACTIVITIES = [
    fake_load_schema,
    fake_transcribe,
    fake_model_call,
    fake_execute_sql,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_events(
    handle,
    client: Client,
    from_offset: int,
    expected_count: int,
    timeout: float = 15.0,
) -> tuple[list[PubSubItem], int]:
    """Subscribe and collect expected_count items. Returns (items, last_offset)."""
    pubsub = PubSubClient.create(client, workflow_id=handle.id)
    items: list[PubSubItem] = []
    last_offset = from_offset
    try:
        async with asyncio.timeout(timeout):
            async for item in pubsub.subscribe(
                topics=[AUDIO_TOPIC, EVENTS_TOPIC],
                from_offset=from_offset,
                poll_cooldown=0,
            ):
                items.append(item)
                last_offset = item.offset + 1
                if len(items) >= expected_count:
                    break
    except asyncio.TimeoutError:
        pass
    return items, last_offset


async def _collect_until_turn_complete(
    handle,
    client: Client,
    from_offset: int,
    timeout: float = 15.0,
) -> tuple[list[PubSubItem], int]:
    """Subscribe until TURN_COMPLETE. Returns (items, offset_past_turn_complete)."""
    pubsub = PubSubClient.create(client, workflow_id=handle.id)
    items: list[PubSubItem] = []
    last_offset = from_offset
    try:
        async with asyncio.timeout(timeout):
            async for item in pubsub.subscribe(
                topics=[AUDIO_TOPIC, EVENTS_TOPIC],
                from_offset=from_offset,
                poll_cooldown=0,
            ):
                items.append(item)
                last_offset = item.offset + 1
                if item.topic == EVENTS_TOPIC:
                    event = json.loads(item.data)
                    if event.get("type") == "TURN_COMPLETE":
                        break
    except asyncio.TimeoutError:
        pass
    return items, last_offset


def _parse_event(item: PubSubItem) -> dict | None:
    if item.topic == EVENTS_TOPIC:
        return json.loads(item.data)
    return None


def _event_types(items: list[PubSubItem]) -> list[str]:
    """Extract event types from a list of items (skipping audio)."""
    types = []
    for item in items:
        evt = _parse_event(item)
        if evt:
            types.append(evt["type"])
    return types


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client() -> Client:
    return await Client.connect(
        "localhost:7233",
        data_converter=pydantic_data_converter,
    )


@pytest.fixture
def task_queue() -> str:
    return f"voice-test-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSingleTurn:
    """Single turn with tool call, audio chunks, and response."""

    @pytest.mark.asyncio
    async def test_full_turn_event_sequence(self, client: Client, task_queue: str):
        """A complete turn emits: STATUS, TRANSCRIPT, STATUS, TOOL_CALL,
        STATUS, RESPONSE_TEXT, TURN_COMPLETE — plus audio chunks."""
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceAnalyticsWorkflow],
            activities=FAKE_ACTIVITIES,
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceAnalyticsWorkflow.run,
                VoiceWorkflowState(),
                id=f"voice-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            # Send a turn
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )

            items, last_offset = await _collect_until_turn_complete(
                handle, client, 0
            )

            event_types = _event_types(items)
            assert "STATUS" in event_types
            assert "TRANSCRIPT" in event_types
            assert "TOOL_CALL" in event_types
            assert "RESPONSE_TEXT" in event_types
            assert event_types[-1] == "TURN_COMPLETE"

            # Should have audio chunks from the model_call activity
            audio_items = [i for i in items if i.topic == AUDIO_TOPIC]
            assert len(audio_items) == 3

            # All items should have valid offsets
            for i, item in enumerate(items):
                assert item.offset >= 0
                if i > 0:
                    assert item.offset > items[i - 1].offset

            await handle.signal(VoiceAnalyticsWorkflow.close_session)

    @pytest.mark.asyncio
    async def test_offsets_are_monotonic_with_audio(
        self, client: Client, task_queue: str
    ):
        """Per-item offsets are correct even with interleaved audio and events."""
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceAnalyticsWorkflow],
            activities=FAKE_ACTIVITIES,
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceAnalyticsWorkflow.run,
                VoiceWorkflowState(),
                id=f"voice-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )

            items, _ = await _collect_until_turn_complete(handle, client, 0)

            # Offsets must be strictly monotonically increasing
            offsets = [item.offset for item in items]
            for i in range(1, len(offsets)):
                assert offsets[i] > offsets[i - 1], (
                    f"Offset not increasing at position {i}: {offsets}"
                )

            await handle.signal(VoiceAnalyticsWorkflow.close_session)


class TestTruncation:
    """Truncation safety: only truncate what was actually consumed."""

    @pytest.mark.asyncio
    async def test_truncate_and_resume(self, client: Client, task_queue: str):
        """After truncating consumed events, a new subscription from the
        truncated offset succeeds and sees new events."""
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceAnalyticsWorkflow],
            activities=FAKE_ACTIVITIES,
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceAnalyticsWorkflow.run,
                VoiceWorkflowState(),
                id=f"voice-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            # Turn 1
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )
            items1, offset1 = await _collect_until_turn_complete(
                handle, client, 0
            )
            assert len(items1) > 0

            # Truncate up to consumed offset
            await handle.signal(VoiceAnalyticsWorkflow.truncate, offset1)

            # Turn 2 — subscribe from offset1
            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )
            items2, offset2 = await _collect_until_turn_complete(
                handle, client, offset1
            )
            assert len(items2) > 0
            assert offset2 > offset1

            # All turn 2 offsets are >= offset1
            for item in items2:
                assert item.offset >= offset1

            await handle.signal(VoiceAnalyticsWorkflow.close_session)

class TestMultipleTurns:
    """Multi-turn conversations with truncation between turns."""

    @pytest.mark.asyncio
    async def test_three_turns_with_truncation(
        self, client: Client, task_queue: str
    ):
        """Three consecutive turns, truncating after each. Offsets advance
        correctly and no stale events leak between turns."""
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceAnalyticsWorkflow],
            activities=FAKE_ACTIVITIES,
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceAnalyticsWorkflow.run,
                VoiceWorkflowState(),
                id=f"voice-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            offset = 0
            for turn in range(3):
                await handle.signal(
                    VoiceAnalyticsWorkflow.start_turn,
                    StartTurnInput(audio_base64=_fake_pcm(1000)),
                )
                items, offset = await _collect_until_turn_complete(
                    handle, client, offset
                )
                event_types = _event_types(items)
                assert event_types[-1] == "TURN_COMPLETE", (
                    f"Turn {turn}: last event is {event_types[-1]}"
                )
                # Truncate after each turn
                await handle.signal(VoiceAnalyticsWorkflow.truncate, offset)

            await handle.signal(VoiceAnalyticsWorkflow.close_session)


class TestContinueAsNew:
    """Continue-as-new with pub/sub state and truncation."""

    @pytest.mark.asyncio
    async def test_continue_as_new_with_truncation(
        self, client: Client, task_queue: str
    ):
        """Force continue-as-new after a turn with large audio payloads.
        The subscription follows the CAN chain and the next turn works.

        Uses an explicit trigger_can signal so the test controls when CAN
        happens — after the subscriber has finished consuming the turn.
        This avoids the in-flight poll race that occurs when the workflow
        CAN's immediately after emitting TURN_COMPLETE.
        """
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceWorkflowForceCAN],
            activities=[
                fake_load_schema,
                fake_transcribe,
                fake_model_call_large,
                fake_execute_sql,
            ],
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceWorkflowForceCAN.run,
                VoiceWorkflowState(),
                id=f"voice-test-can-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            # Turn 1 — large audio payloads
            await handle.signal(
                VoiceWorkflowForceCAN.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )

            items, consumed_offset = await _collect_until_turn_complete(
                handle, client, 0
            )
            event_types = _event_types(items)
            assert "TURN_COMPLETE" in event_types
            audio_items = [i for i in items if i.topic == AUDIO_TOPIC]
            assert len(audio_items) == 10  # fake_model_call_large produces 10

            # Truncate consumed items, then trigger CAN
            await handle.signal(VoiceWorkflowForceCAN.truncate, consumed_offset)
            await handle.signal(VoiceWorkflowForceCAN.trigger_can)

            # Wait for CAN to complete
            await asyncio.sleep(1.0)

            # Turn 2 — subscription follows the CAN chain.
            handle = client.get_workflow_handle(handle.id)
            await handle.signal(
                VoiceWorkflowForceCAN.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )

            # After CAN, offsets reset to the truncated base_offset.
            # The new run starts with an empty log at base_offset = consumed_offset.
            items2, _ = await _collect_until_turn_complete(
                handle, client, consumed_offset
            )
            event_types2 = _event_types(items2)
            assert "TURN_COMPLETE" in event_types2

            await handle.signal(VoiceWorkflowForceCAN.close_session)


class TestLargePayloads:
    """Stress tests with large audio payloads."""

    @pytest.mark.asyncio
    async def test_large_audio_chunks_delivered(
        self, client: Client, task_queue: str
    ):
        """Audio chunks of ~50-100KB each are all delivered correctly."""
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[VoiceAnalyticsWorkflow],
            activities=FAKE_ACTIVITIES,
            max_cached_workflows=0,
        ):
            handle = await client.start_workflow(
                VoiceAnalyticsWorkflow.run,
                VoiceWorkflowState(),
                id=f"voice-test-large-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

            await handle.signal(
                VoiceAnalyticsWorkflow.start_turn,
                StartTurnInput(audio_base64=_fake_pcm(1000)),
            )

            items, _ = await _collect_until_turn_complete(handle, client, 0)

            audio_items = [i for i in items if i.topic == AUDIO_TOPIC]
            assert len(audio_items) == 3  # fake_model_call produces 3 chunks

            # Each audio chunk should decode to valid base64 PCM
            for item in audio_items:
                payload = json.loads(item.data)
                pcm = base64.b64decode(payload["audio_base64"])
                assert len(pcm) >= 50_000

            await handle.signal(VoiceAnalyticsWorkflow.close_session)
