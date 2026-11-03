"""Agent events published from a tool's worker thread reach the run stream."""
import asyncio

import pytest

from gilzabots.ide_server import AsyncHTTPServer


@pytest.mark.asyncio
async def test_event_published_from_a_worker_thread_wakes_the_stream():
    server = AsyncHTTPServer()
    server.loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    server.active_queues.append(queue)

    # Synchronous tools run through asyncio.to_thread, and this is how one of
    # them (compile_latex) publishes `latex_compiled`.
    await asyncio.to_thread(server.publish_event, {"event": "latex_compiled"})

    assert await asyncio.wait_for(queue.get(), timeout=1) == {"event": "latex_compiled"}


@pytest.mark.asyncio
async def test_events_keep_their_order_across_threads():
    # The loop may resume the coroutine awaiting to_thread before it runs a
    # callback the thread scheduled earlier (about 1 in 1,500 on Python 3.14),
    # so one round proves little: the order is checked over many.
    server = AsyncHTTPServer()
    server.loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    server.active_queues.append(queue)

    for _ in range(3000):
        server.publish_event({"n": 1})
        await asyncio.to_thread(server.publish_event, {"n": 2})
        server.publish_event({"n": 3})
        received = [await asyncio.wait_for(queue.get(), timeout=1) for _ in range(3)]
        assert [event["n"] for event in received] == [1, 2, 3]


def test_events_without_a_running_server_go_straight_to_the_queues():
    server = AsyncHTTPServer()
    received = []

    class _Queue:
        def put_nowait(self, payload):
            received.append(payload)

    server.active_queues.append(_Queue())
    server.publish_event({"event": "info"})
    assert received == [{"event": "info"}]
