"""Closing a source must not free a native capture while its read is running."""

import asyncio
import threading
from uuid import uuid4

import pytest

from odp_api.adapters.frame_sources.opencv import RecordedVideoSource


@pytest.mark.parametrize("cancel_reader", [False, True])
def test_close_waits_for_native_read_even_after_reader_cancellation(cancel_reader):
    entered, unblock, released = threading.Event(), threading.Event(), threading.Event()

    class Capture:
        def isOpened(self):
            return True

        def read(self):
            entered.set()
            assert unblock.wait(5)
            return True, b"frame"

        def release(self):
            released.set()

    async def scenario():
        source = RecordedVideoSource("unused", camera_id=uuid4(), session_id=uuid4(),
                                     capture_factory=lambda _: Capture())
        reader = asyncio.create_task(anext(source.frames()))
        closer = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if cancel_reader:
                reader.cancel()
            closer = asyncio.create_task(source.close())
            # Give close a chance to run while the native read is held by the gate.
            await asyncio.sleep(0.05)
            assert not released.is_set(), "capture freed during native read"
            assert not closer.done()
        finally:
            unblock.set()
            await asyncio.gather(reader, return_exceptions=True)
            if closer is not None:
                await closer
        assert released.is_set()

    asyncio.run(scenario())
