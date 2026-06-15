"""Workflow-level interrupt test (requires a Temporal dev server on :7233).

Validates the end-to-end interrupt path with a fake, interruptible model call:
the workflow cancels the in-flight model activity, the activity catches the
cancellation and returns its partial text, and the workflow persists that
partial (marked interrupted) and emits INTERRUPTED instead of AGENT_COMPLETE.

This is the test that pins down the load-bearing design choice: with
WAIT_CANCELLATION_COMPLETED, a model_call that catches CancelledError and
*returns* a partial result delivers that partial back to the workflow.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import (
    WorkflowStreamClient,
    WorkflowStreamItem,
)
from temporalio.worker import Worker

from analytics_shared.constants import EVENTS_TOPIC
from analytics_shared.types import ToolCallInfo

from src.types import (
    ModelCallInput,
    ModelCallResult,
    SessionInfo,
    StartTurnInput,
    ToolResult,
    WorkflowState,
)
from src.workflows import AnalyticsWorkflow

PARTIAL_TEXT = "Here is the start of an answer"


@activity.defn(name="load_schema")
async def fake_load_schema() -> str:
    return "CREATE TABLE Artist (ArtistId INTEGER PRIMARY KEY, Name TEXT);"


@activity.defn(name="model_call")
async def fake_model_call_interruptible(input: ModelCallInput) -> ModelCallResult:
    """Stream a little text, then stay in flight until cancelled.

    Mirrors the real activity's interrupt contract: on CancelledError, return
    the partial text with interrupted=True rather than re-raising.
    """
    stream = WorkflowStreamClient.from_within_activity(
        batch_interval=timedelta(seconds=0.05)
    )
    async with stream:
        events = stream.topic(EVENTS_TOPIC, type=dict)
        events.publish({"type": "TEXT_DELTA", "data": {"delta": PARTIAL_TEXT}})
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return ModelCallResult(
                response_id="",
                tool_calls=[],
                final_text=PARTIAL_TEXT,
                interrupted=True,
            )


@activity.defn(name="execute_tool")
async def fake_execute_tool(input) -> dict:  # pragma: no cover - not reached here
    return {}


async def _collect_until(
    client: Client,
    workflow_id: str,
    event_type: str,
    timeout: float = 25.0,
) -> list[WorkflowStreamItem[dict]]:
    """Subscribe from offset 0 and collect events until `event_type` is seen."""
    stream = WorkflowStreamClient.create(client, workflow_id)
    items: list[WorkflowStreamItem[dict]] = []
    async with asyncio.timeout(timeout):
        async for item in stream.subscribe(
            topics=[EVENTS_TOPIC], from_offset=0, result_type=dict,
            poll_cooldown=timedelta(0),
        ):
            items.append(item)
            if item.data.get("type") == event_type:
                break
    return items


def _types(items: list[WorkflowStreamItem[dict]]) -> list[str]:
    return [i.data["type"] for i in items]


@pytest.fixture
async def client() -> Client:
    return await Client.connect(
        "localhost:7233", data_converter=pydantic_data_converter
    )


@pytest.mark.asyncio
async def test_interrupt_persists_partial_and_emits_interrupted(client: Client, tmp_path):
    task_queue = f"interrupt-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[AnalyticsWorkflow],
        activities=[fake_load_schema, fake_model_call_interruptible, fake_execute_tool],
        max_cached_workflows=0,
    ):
        workflow_id = f"interrupt-test-{uuid.uuid4().hex[:8]}"
        handle = await client.start_workflow(
            AnalyticsWorkflow.run,
            WorkflowState(working_dir=str(tmp_path), db_schema="schema"),
            id=workflow_id,
            task_queue=task_queue,
        )

        await handle.signal(
            AnalyticsWorkflow.start_turn, StartTurnInput(message="hi")
        )

        # Wait until the turn is live (partial text streamed), then interrupt.
        await _collect_until(client, workflow_id, "TEXT_DELTA")
        await handle.signal(AnalyticsWorkflow.interrupt)

        items = await _collect_until(client, workflow_id, "INTERRUPTED")
        types = _types(items)
        assert "INTERRUPTED" in types
        assert "AGENT_COMPLETE" not in types

        # The partial is persisted, marked interrupted, and the turn is over.
        info: SessionInfo = await handle.query(AnalyticsWorkflow.get_session)
        assert info.turn_in_progress is False
        assistant = [m for m in info.messages if m.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == PARTIAL_TEXT
        assert assistant[0]["interrupted"] is True

        await handle.signal(AnalyticsWorkflow.close_session)


# Records the previous_response_id seen by each model_call invocation, so the
# tool-phase test can assert the response chain is reset after an interrupt.
_TOOL_TEST_MODEL_CALLS: list[str | None] = []


@activity.defn(name="model_call")
async def fake_model_call_tool_then_text(input: ModelCallInput) -> ModelCallResult:
    _TOOL_TEST_MODEL_CALLS.append(input.previous_response_id)
    if len(_TOOL_TEST_MODEL_CALLS) == 1:
        # Turn 1: request a tool (the fake tool below blocks until cancelled).
        return ModelCallResult(
            response_id="resp_1",
            tool_calls=[ToolCallInfo(
                item_id="i1", call_id="c1", name="execute_sql",
                arguments={"query": "SELECT 1"},
            )],
            final_text=None,
        )
    # Turn 2 (after the interrupt): just answer.
    return ModelCallResult(response_id="resp_2", tool_calls=[], final_text="done")


@activity.defn(name="execute_tool")
async def fake_blocking_tool(input) -> ToolResult:
    """Stay in flight (heartbeating) until the workflow cancels us."""
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_interrupt_during_tool_phase_resets_response_chain(client: Client, tmp_path):
    _TOOL_TEST_MODEL_CALLS.clear()
    task_queue = f"interrupt-tool-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[AnalyticsWorkflow],
        activities=[fake_load_schema, fake_model_call_tool_then_text, fake_blocking_tool],
        max_cached_workflows=0,
    ):
        workflow_id = f"interrupt-tool-{uuid.uuid4().hex[:8]}"
        handle = await client.start_workflow(
            AnalyticsWorkflow.run,
            WorkflowState(working_dir=str(tmp_path), db_schema="schema"),
            id=workflow_id,
            task_queue=task_queue,
        )

        await handle.signal(
            AnalyticsWorkflow.start_turn, StartTurnInput(message="run a query")
        )

        # Interrupt once we're in the tool phase (tool task in flight).
        await _collect_until(client, workflow_id, "TOOL_CALL_START")
        await handle.signal(AnalyticsWorkflow.interrupt)

        items = await _collect_until(client, workflow_id, "INTERRUPTED")
        types = _types(items)
        assert "INTERRUPTED" in types
        assert "AGENT_COMPLETE" not in types
        assert "TOOL_CALL_COMPLETE" not in types  # the tool was cancelled, not run

        info: SessionInfo = await handle.query(AnalyticsWorkflow.get_session)
        assert info.turn_in_progress is False

        # Turn 2 must complete cleanly. If the response chain weren't reset
        # (C2), the next model call would chain off resp_1 — whose tool call was
        # never answered. We assert the chain was dropped to None.
        await handle.signal(
            AnalyticsWorkflow.start_turn, StartTurnInput(message="hello")
        )
        await _collect_until(client, workflow_id, "AGENT_COMPLETE")
        assert _TOOL_TEST_MODEL_CALLS[1] is None

        await handle.signal(AnalyticsWorkflow.close_session)
