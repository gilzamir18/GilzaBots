"""One agent turn at a time, and a turn outlives its lost client stream.

Regression for a turn parked on `ask_question` whose GUI stream was lost (system
standby): the GUI started a second turn, the first turn's cancellation ran its
cleanup in the middle of the second, removed the second turn's GUI input hooks,
and the second turn's question went to `input()` on the server console. A lost
stream must also never end a turn that is only waiting for the user's answer.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import gilzabots.agent_stdin as stdin_mod
import gilzabots.terminal as T
from gilzabots.ide_server import AsyncHTTPServer


def _hooks():
    return {name: getattr(T, name) for name in stdin_mod._TURN_INPUT_HOOK_NAMES}


def test_removing_an_earlier_turns_hooks_keeps_a_later_turns_hooks(monkeypatch):
    for name in stdin_mod._TURN_INPUT_HOOK_NAMES:
        monkeypatch.setattr(T, name, None)

    async def a_confirm(*_a, **_k): ...
    async def a_ask(*_a, **_k): ...
    async def a_terminal(*_a, **_k): ...
    async def b_confirm(*_a, **_k): ...
    async def b_ask(*_a, **_k): ...
    async def b_terminal(*_a, **_k): ...

    restore_a = stdin_mod._install_turn_input_hooks(a_confirm, a_ask, a_terminal)
    restore_b = stdin_mod._install_turn_input_hooks(b_confirm, b_ask, b_terminal)

    restore_a()
    assert _hooks() == {
        "_async_confirm_hook": b_confirm,
        "_async_ask_hook": b_ask,
        "_async_interactive_terminal_hook": b_terminal,
    }

    restore_b()
    assert T._async_ask_hook is a_ask


def test_a_single_turn_restores_every_hook_it_installed(monkeypatch):
    for name in stdin_mod._TURN_INPUT_HOOK_NAMES:
        monkeypatch.setattr(T, name, None)

    async def hook(*_a, **_k): ...

    restore = stdin_mod._install_turn_input_hooks(hook, hook, hook)
    restore()
    assert set(_hooks().values()) == {None}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_requested, reason", [(False, "turn_active"), (True, "turn_stopping")])
async def test_run_is_refused_while_a_turn_is_alive(monkeypatch, cancel_requested, reason):
    async def must_not_run(_data):
        raise AssertionError("a second turn must not start")

    monkeypatch.setattr(stdin_mod, "handle_run", must_not_run)
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )
    alive = asyncio.create_task(asyncio.Event().wait())
    if cancel_requested:
        alive._gilzabots_cancel_requested = True
    server.active_agent_task = alive
    try:
        await server.route_api(
            "POST", "/api/gilzabots/run", {}, {},
            json.dumps({"agent": "chat_orchestrator", "prompt": "hi"}).encode("utf-8"),
            AsyncMock(),
        )
    finally:
        alive.cancel()

    assert len(responses) == 1
    status, payload = responses[0]
    assert status == 409
    assert payload["reason"] == reason


@pytest.mark.asyncio
async def test_a_lost_stream_keeps_a_turn_waiting_for_its_answer(monkeypatch):
    """A standby drops the GUI stream; answering later must still reach the turn."""
    server = AsyncHTTPServer()
    server.local_session.trusted_request = lambda _headers: True
    server.local_session.authenticated = lambda _headers: True
    monkeypatch.setattr(stdin_mod, "event_hook", lambda payload: [q.put_nowait(payload) for q in server.active_queues])

    cancelled = asyncio.Event()
    finished = asyncio.Event()
    answers = []

    async def parked_turn(_data):
        future = asyncio.get_running_loop().create_future()
        stdin_mod._gui_input_pending["q1"] = future
        stdin_mod.print_event("input_request", {"id": "q1", "prompt": "Which?", "type": "ask"})
        try:
            answers.append(await future)
            # Keeps working after the answer, with nobody reading the stream.
            stdin_mod.print_event("stream_chunk", {"content": "working"})
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            stdin_mod._gui_input_pending.pop("q1", None)
        finished.set()

    monkeypatch.setattr(stdin_mod, "handle_run", parked_turn)
    monkeypatch.setattr(stdin_mod, "_persist_activity_event", lambda *_a, **_k: None)

    listener = await asyncio.start_server(server.handle_request, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"agent": "chat_orchestrator", "prompt": "turn A"}).encode("utf-8")
        writer.write(
            b"POST /api/gilzabots/run HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
        )
        await writer.drain()

        received = b""
        while b'"input_request"' not in received:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert chunk, "stream ended before the question was sent"
            received += chunk

        # The stream drops while the user has not answered yet.
        writer.transport.abort()
        await asyncio.sleep(1.0)
        assert not cancelled.is_set()
        assert server.active_agent_task is not None and not server.active_agent_task.done()

        responses = []
        server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(status)
        await server.route_api(
            "POST", "/api/gilzabots/input_response", {}, {},
            json.dumps({"id": "q1", "value": "the answer"}).encode("utf-8"),
            AsyncMock(),
        )
        assert responses == [200]

        await asyncio.wait_for(finished.wait(), timeout=5)
        assert answers == ["the answer"]
        assert not cancelled.is_set()
        for _ in range(50):
            if server.active_agent_task is None:
                break
            await asyncio.sleep(0.05)
        assert server.active_agent_task is None
    finally:
        listener.close()
        await listener.wait_closed()
