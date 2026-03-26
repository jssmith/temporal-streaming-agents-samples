"""Integration test: measure actual Temporal actions under different poll intervals.

Runs a real workflow in Temporal's test server, simulates the BFF polling
loop, and counts events in the workflow history to quantify action savings.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporalio.api.enums.v1 import EventType

from src.types import (
    ActivityEventsInput,
    PollEventsInput,
    PollEventsResult,
)


@dataclass
class EmitEventsActivity:
    client: Client

    @activity.defn
    async def emit_events(self, handle_id: str) -> None:
        """Simulate streaming: emit events back to workflow via signal."""
        handle = self.client.get_workflow_handle(handle_id)
        for i in range(10):
            await handle.signal(
                "receive_events",
                ActivityEventsInput(events=[
                    {"type": "TEXT_DELTA", "data": {"text": f"chunk-{i * 2}"}},
                    {"type": "TEXT_DELTA", "data": {"text": f"chunk-{i * 2 + 1}"}},
                ]),
            )
            await asyncio.sleep(0.1)


@workflow.defn(name="PollThrottleTestWorkflow")
class PollThrottleTestWorkflow:
    def __init__(self) -> None:
        self._event_list: list[dict] = []
        self._turn_complete: bool = True
        self._start_requested: bool = False
        self._poll_done: bool = False

    @workflow.signal
    def start_turn(self) -> None:
        self._start_requested = True

    @workflow.signal
    def receive_events(self, input: ActivityEventsInput) -> None:
        self._event_list.extend(input.events)

    @workflow.signal
    def poll_complete(self) -> None:
        self._poll_done = True

    @workflow.update
    async def poll_events(self, input: PollEventsInput) -> PollEventsResult:
        await workflow.wait_condition(
            lambda: len(self._event_list) > input.last_seen_index
            or self._turn_complete,
            timeout=300,
        )
        new_events = self._event_list[input.last_seen_index:]
        return PollEventsResult(
            events=new_events,
            turn_complete=self._turn_complete,
        )

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._start_requested)
        self._turn_complete = False

        wf_id = workflow.info().workflow_id
        await workflow.execute_activity(
            "emit_events",
            wf_id,
            start_to_close_timeout=timedelta(seconds=30),
        )

        self._turn_complete = True
        # Wait for the polling client to signal it's done
        await workflow.wait_condition(lambda: self._poll_done)


async def run_bff_poll_loop(handle, poll_interval: float) -> int:
    """Simulate the BFF polling loop. Returns total events received."""
    last_index = 0
    total_events = 0

    while True:
        result = await handle.execute_update(
            PollThrottleTestWorkflow.poll_events,
            PollEventsInput(last_seen_index=last_index),
        )
        total_events += len(result.events)
        last_index += len(result.events)

        if result.turn_complete:
            break
        await asyncio.sleep(poll_interval)

    return total_events


def count_history_events(history: WorkflowHistory) -> dict[str, int]:
    """Count event types from workflow history."""
    counts: dict[str, int] = {}
    for event in history.events:
        event_type = EventType.Name(event.event_type)
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def sum_billable_actions(counts: dict[str, int]) -> int:
    """Sum events that map to billable Temporal Cloud actions."""
    billable = 0
    for event_type, count in counts.items():
        if "UPDATE" in event_type and "ACCEPTED" in event_type:
            billable += count
        if event_type == "EVENT_TYPE_TIMER_STARTED":
            billable += count
        if event_type == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            billable += count
        if event_type == "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED":
            billable += count
    return billable


TASK_QUEUE = "test-poll-throttle"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_action_count_comparison():
    """Run the same workflow with 0s and 0.2s poll intervals, compare actions."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
    ) as env:
        results = {}

        for label, interval in [("no_throttle", 0.0), ("200ms", 0.2)]:
            workflow_id = f"test-throttle-{label}"
            activities = EmitEventsActivity(client=env.client)

            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[PollThrottleTestWorkflow],
                activities=[activities.emit_events],
            ):
                handle = await env.client.start_workflow(
                    PollThrottleTestWorkflow.run,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                )

                await handle.signal(PollThrottleTestWorkflow.start_turn)
                await asyncio.sleep(0.3)

                total_events = await run_bff_poll_loop(handle, interval)

                # Signal workflow that we're done polling so it can exit
                await handle.signal(PollThrottleTestWorkflow.poll_complete)
                await handle.result()

                history = await handle.fetch_history()
                counts = count_history_events(history)
                billable = sum_billable_actions(counts)

                results[label] = {
                    "total_events": total_events,
                    "billable_actions": billable,
                    "history_counts": counts,
                }

        print("\n" + "=" * 70)
        print("TEMPORAL ACTION COUNT FROM ACTUAL EVENT HISTORY")
        print("=" * 70)

        for label, data in results.items():
            print(f"\n--- {label} ---")
            print(f"  Events delivered to client: {data['total_events']}")
            print(f"  Billable actions: {data['billable_actions']}")
            print(f"  History breakdown:")
            for event_type, count in sorted(data["history_counts"].items()):
                marker = ""
                if any(k in event_type for k in ["UPDATE", "TIMER_STARTED", "ACTIVITY_TASK_SCHEDULED", "SIGNALED"]):
                    marker = " *"
                print(f"    {event_type}: {count}{marker}")

        no_throttle = results["no_throttle"]["billable_actions"]
        throttled = results["200ms"]["billable_actions"]
        print(f"\n--- Summary ---")
        print(f"  No throttle: {no_throttle} billable actions")
        print(f"  200ms throttle: {throttled} billable actions")
        if throttled > 0:
            print(f"  Reduction: {no_throttle - throttled} fewer actions ({no_throttle / throttled:.1f}x)")
        print(f"  (* = billable action type)")

        assert results["no_throttle"]["total_events"] == 20
        assert results["200ms"]["total_events"] == 20
        assert throttled < no_throttle
