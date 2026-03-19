"""Tests for EventBatcher."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.event_batcher import EventBatcher
from src.types import ActivityEventsInput


@pytest.fixture
def mock_handle():
    handle = MagicMock()
    handle.signal = AsyncMock()
    return handle


class TestEventBatcher:
    def test_add_buffers_without_sending(self, mock_handle):
        batcher = EventBatcher(mock_handle, "receive_events")
        batcher.add({"type": "TEXT_DELTA"})
        batcher.add({"type": "TEXT_DELTA"})
        mock_handle.signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_sends_buffered_events(self, mock_handle):
        batcher = EventBatcher(mock_handle, "receive_events")
        batcher.add({"type": "A"})
        batcher.add({"type": "B"})
        await batcher.flush()
        mock_handle.signal.assert_called_once_with(
            "receive_events",
            ActivityEventsInput(events=[{"type": "A"}, {"type": "B"}]),
        )

    @pytest.mark.asyncio
    async def test_flush_clears_buffer(self, mock_handle):
        batcher = EventBatcher(mock_handle, "receive_events")
        batcher.add({"type": "A"})
        await batcher.flush()
        mock_handle.signal.reset_mock()
        await batcher.flush()
        mock_handle.signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_flush_is_noop(self, mock_handle):
        batcher = EventBatcher(mock_handle, "receive_events")
        await batcher.flush()
        mock_handle.signal.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.timeout(5)
    async def test_run_flusher_flushes_on_interval(self, mock_handle):
        batcher = EventBatcher(mock_handle, "receive_events", interval=0.1)
        batcher.add({"type": "A"})

        task = asyncio.create_task(batcher.run_flusher())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_handle.signal.assert_called()
