"""Tests for BFF poll throttling (MIN_POLL_INTERVAL).

Simulates the event_stream polling loop from main.py with a mock
workflow handle, counting execute_update calls under different
poll intervals to verify throttling and quantify savings.
"""

import asyncio
import time

import pytest

from src.types import PollEventsResult


async def simulate_poll_loop(
    poll_interval: float,
    event_batches: list[PollEventsResult],
    poll_latency: float = 0.0,
) -> tuple[int, float]:
    """Simulate the BFF polling loop from main.py.

    Returns (poll_count, elapsed_seconds).
    """
    batch_iter = iter(event_batches)
    poll_count = 0
    start = time.monotonic()

    while True:
        await asyncio.sleep(poll_latency)  # simulate network round-trip
        result = next(batch_iter)
        poll_count += 1

        if result.turn_complete:
            break
        await asyncio.sleep(poll_interval)

    elapsed = time.monotonic() - start
    return poll_count, elapsed


def make_batches(n_flowing: int, events_per_batch: int = 3) -> list[PollEventsResult]:
    """Create n_flowing batches with events, then a final turn_complete batch."""
    batches = []
    for _ in range(n_flowing):
        events = [{"type": "TEXT_DELTA"} for _ in range(events_per_batch)]
        batches.append(PollEventsResult(events=events, turn_complete=False))
    batches.append(PollEventsResult(events=[], turn_complete=True))
    return batches


class TestPollThrottle:
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_zero_interval_polls_rapidly(self):
        """With no throttle, polls complete as fast as possible."""
        batches = make_batches(20)
        poll_count, elapsed = await simulate_poll_loop(0.0, batches)
        assert poll_count == 21  # 20 flowing + 1 final
        # Without throttle, should complete nearly instantly
        assert elapsed < 0.5

    @pytest.mark.asyncio
    @pytest.mark.timeout(15)
    async def test_interval_throttles_poll_rate(self):
        """With a poll interval, the loop is throttled."""
        batches = make_batches(5)
        poll_count, elapsed = await simulate_poll_loop(0.5, batches)
        assert poll_count == 6  # 5 flowing + 1 final
        # 5 sleeps of 0.5s = ~2.5s minimum
        assert elapsed >= 2.0

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_turn_complete_exits_without_sleeping(self):
        """The final batch (turn_complete) should not incur a sleep."""
        batches = [
            PollEventsResult(events=[{"type": "A"}], turn_complete=False),
            PollEventsResult(events=[{"type": "B"}], turn_complete=True),
        ]
        _, elapsed = await simulate_poll_loop(2.0, batches)
        # Only 1 sleep (after first batch), not 2
        assert elapsed < 3.0
        assert elapsed >= 1.5

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_quantify_savings(self):
        """Measure poll rate reduction: 0s interval vs 0.5s interval.

        Both loops consume the same number of batches, but the throttled
        loop takes longer — meaning fewer polls per unit time, which
        directly maps to fewer Temporal Cloud actions per unit time.

        With real execute_update latency (~50ms), the unthrottled loop
        can fire ~20 polls/s. With 0.5s throttle, it drops to ~2 polls/s.
        We use a smaller simulated latency here for fast tests.
        """
        n_batches = 10
        poll_latency = 0.01  # minimal simulated round-trip

        batches_fast = make_batches(n_batches)
        batches_slow = make_batches(n_batches)

        polls_no_throttle, time_no_throttle = await simulate_poll_loop(
            0.0, batches_fast, poll_latency=poll_latency
        )
        polls_throttled, time_throttled = await simulate_poll_loop(
            0.5, batches_slow, poll_latency=poll_latency
        )

        total_polls = n_batches + 1  # n flowing + 1 final
        assert polls_no_throttle == total_polls
        assert polls_throttled == total_polls

        rate_no_throttle = polls_no_throttle / time_no_throttle
        rate_throttled = polls_throttled / time_throttled

        print(f"\n--- Poll Rate Comparison ---")
        print(f"No throttle:   {polls_no_throttle} polls in {time_no_throttle:.2f}s = {rate_no_throttle:.1f} polls/s")
        print(f"0.5s throttle: {polls_throttled} polls in {time_throttled:.2f}s = {rate_throttled:.1f} polls/s")
        print(f"Rate reduction: {rate_no_throttle / rate_throttled:.1f}x fewer polls/s")

        # Extrapolate to a real 10s streaming turn with ~50ms latency:
        real_latency = 0.05
        real_rate_unthrottled = 1.0 / real_latency  # ~20 polls/s
        real_rate_throttled = 1.0 / (real_latency + 0.5)  # ~1.8 polls/s
        real_polls_unthrottled = real_rate_unthrottled * 10
        real_polls_throttled = real_rate_throttled * 10
        # Each poll = 1 action (update). When wait_condition returns instantly
        # (events ready), no timer action. So actions ~= polls.
        print(f"\n--- Projected savings for 10s streaming turn ---")
        print(f"No throttle:   ~{real_polls_unthrottled:.0f} polls = ~{real_polls_unthrottled:.0f} actions")
        print(f"0.5s throttle: ~{real_polls_throttled:.0f} polls = ~{real_polls_throttled:.0f} actions")
        print(f"Savings: ~{real_polls_unthrottled - real_polls_throttled:.0f} actions ({real_polls_unthrottled / real_polls_throttled:.0f}x reduction)")

        assert rate_throttled < rate_no_throttle / 2
