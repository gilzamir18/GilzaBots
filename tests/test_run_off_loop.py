"""`agent_stdin._run_off_loop` moves a blocking store call off the event loop.

Saving a chat message also embeds it into archival memory (ChromaDB), which on
the first message of a session builds the client and may download the
embedding model. Run inline, that froze the whole window. The helper runs it in
a worker thread and must keep everything else the inline call guaranteed: the
call always completes, its result and exceptions reach the caller unchanged,
and a cancel requested meanwhile lands at the turn's next await, after the code
following the call has run — the point it landed at before.
"""

import asyncio
import sys
import threading

import pytest

from gilzabots.agent_stdin import _run_off_loop


@pytest.mark.asyncio
async def test_returns_the_result_and_runs_in_a_worker_thread():
    seen = []

    def call(a, b, *, c):
        seen.append(threading.current_thread())
        return a + b + c

    assert await _run_off_loop(call, 1, 2, c=3) == 6
    assert seen[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_exceptions_reach_the_caller_unchanged():
    def call():
        raise TypeError("unexpected keyword argument")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await _run_off_loop(call)


@pytest.mark.asyncio
async def test_the_loop_keeps_running_during_the_call():
    release = threading.Event()
    ticks = []

    async def ticker():
        while not release.is_set():
            ticks.append(1)
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    call_task = asyncio.create_task(_run_off_loop(release.wait, 5))
    await asyncio.sleep(0.05)
    assert len(ticks) > 2, "the event loop was blocked by the call"
    release.set()
    assert await call_task is True
    await ticker_task


async def _turn(call, log):
    result = await _run_off_loop(call)
    log.append(("after", result))
    await asyncio.sleep(0)
    log.append("unreachable")


@pytest.mark.asyncio
async def test_a_cancel_during_the_call_lands_at_the_next_await():
    entered = threading.Event()
    release = threading.Event()
    completed = []

    def call():
        entered.set()
        assert release.wait(5)
        completed.append(True)
        return 42

    log = []
    task = asyncio.create_task(_turn(call, log))
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done(), "the cancel must wait for the call to finish"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completed == [True]
    assert log == [("after", 42)], "the code following the call runs before the cancel lands"
    if sys.version_info >= (3, 11):
        assert task.cancelling() == 1


@pytest.mark.asyncio
async def test_a_cancel_during_a_failing_call_still_lands():
    entered = threading.Event()
    release = threading.Event()

    def call():
        entered.set()
        assert release.wait(5)
        raise ValueError("boom")

    async def turn():
        try:
            await _run_off_loop(call)
        except ValueError:
            pass
        await asyncio.sleep(0)
        return "unreachable"

    task = asyncio.create_task(turn())
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
