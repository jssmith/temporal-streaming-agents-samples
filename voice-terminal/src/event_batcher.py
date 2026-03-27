"""Nagle-like batcher for activity -> workflow signaling."""

import asyncio

from .types import ActivityEventsInput


class EventBatcher:
    """Buffers events and flushes via signal on timer or explicit flush.

    Usage pattern (two concurrent tasks):
        batcher = EventBatcher(handle, "receive_events", interval=2.0)

        async def read_stream():
            async for event in stream:
                batcher.add(translate(event))
                if is_significant(event):
                    await batcher.flush()

        completed, pending = await asyncio.wait(
            [asyncio.create_task(read_stream()),
             asyncio.create_task(batcher.run_flusher())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await batcher.flush()  # final flush
    """

    def __init__(self, handle, signal_name: str, interval: float = 2.0):
        self._handle = handle
        self._signal_name = signal_name
        self._interval = interval
        self._buffer: list[dict] = []

    def add(self, event: dict):
        """Add an event to the buffer. Does NOT auto-flush."""
        self._buffer.append(event)

    async def flush(self):
        """Explicitly flush buffered events via signal."""
        if self._buffer:
            batch = self._buffer.copy()
            self._buffer.clear()
            await self._handle.signal(
                self._signal_name,
                ActivityEventsInput(events=batch),
            )

    async def run_flusher(self):
        """Background task: flush on timer interval. Run as concurrent task."""
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()
