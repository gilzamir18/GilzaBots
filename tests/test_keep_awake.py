"""The system must not idle-sleep through an agent turn.

Regression for a turn lost to a standby: the machine suspended mid-turn, every
socket died with it, and on resume the window had lost the stream of a turn the
server still owned -- so the user was met with "An agent turn is still running,
but this window lost its connection to it" and offered to kill the work they
were waiting for. The turn surviving its stream is the other half of this
(`tests/test_agent_turn_overlap.py`); this half is not suspending in the first
place.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import gilzabots.agent_stdin as stdin_mod
from gilzabots import keep_awake
from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.keep_awake import SleepInhibitor, backend_for_platform


class FakeBackend:
    name = "fake"

    def __init__(self, fail=False):
        self.starts = []
        self.stops = 0
        self.fail = fail

    def start(self, reason):
        if self.fail:
            raise OSError("no inhibitor here")
        self.starts.append(reason)

    def stop(self):
        self.stops += 1


def test_the_hold_is_taken_once_and_released_once():
    backend = FakeBackend()
    inhibitor = SleepInhibitor(backend)

    assert inhibitor.acquire("turn") is True
    assert inhibitor.active is True
    assert backend.starts == ["turn"]

    inhibitor.release()
    assert inhibitor.active is False
    assert backend.stops == 1


def test_a_nested_holder_cannot_release_the_outer_one():
    """A worker delegation inside a turn must not let the system sleep."""
    backend = FakeBackend()
    inhibitor = SleepInhibitor(backend)

    inhibitor.acquire("turn")
    inhibitor.acquire("worker")
    assert backend.starts == ["turn"]

    inhibitor.release()
    assert inhibitor.active is True, "the turn still holds it"
    assert backend.stops == 0

    inhibitor.release()
    assert inhibitor.active is False
    assert backend.stops == 1


def test_a_release_without_a_hold_does_nothing():
    backend = FakeBackend()
    inhibitor = SleepInhibitor(backend)

    inhibitor.release()
    assert backend.stops == 0
    assert inhibitor.holders == 0

    # And the counter did not go negative, so the next real pair still works.
    inhibitor.acquire("turn")
    inhibitor.release()
    assert backend.stops == 1


def test_a_machine_that_cannot_inhibit_sleep_still_runs_its_turns(capsys):
    """Failing to hold off sleep is reported once, never raised into the turn."""
    backend = FakeBackend(fail=True)
    inhibitor = SleepInhibitor(backend)

    assert inhibitor.acquire("turn") is False
    assert inhibitor.active is False
    assert "Could not keep the system awake" in capsys.readouterr().err

    inhibitor.release()
    inhibitor.acquire("turn")
    assert capsys.readouterr().err == "", "the warning does not repeat every turn"


@pytest.mark.parametrize(
    "platform, expected",
    [("win32", "windows"), ("darwin", "macos"), ("linux", "linux"), ("sunos5", "none")],
)
def test_every_platform_resolves_to_its_own_backend(platform, expected):
    assert backend_for_platform(platform).name == expected


@pytest.mark.asyncio
async def test_a_turn_holds_off_sleep_until_it_ends(monkeypatch):
    """The hold is tied to the turn, not to the HTTP stream that started it."""
    backend = FakeBackend()
    monkeypatch.setattr(keep_awake, "inhibitor", SleepInhibitor(backend))

    running = asyncio.Event()
    finish = asyncio.Event()

    async def a_turn(_data):
        running.set()
        await finish.wait()

    monkeypatch.setattr(stdin_mod, "handle_run", a_turn)
    monkeypatch.setattr(stdin_mod, "_persist_activity_event", lambda *_a, **_k: None)

    server = AsyncHTTPServer()
    server.local_session.trusted_request = lambda _headers: True
    server.local_session.authenticated = lambda _headers: True

    listener = await asyncio.start_server(server.handle_request, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"agent": "chat_orchestrator", "prompt": "hi"}).encode("utf-8")
        writer.write(
            b"POST /api/gilzabots/run HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
        )
        await writer.drain()

        await asyncio.wait_for(running.wait(), timeout=5)
        assert keep_awake.inhibitor.active is True, "the machine may not sleep mid-turn"

        # Losing the stream does not end the turn, so the hold stays.
        writer.transport.abort()
        await asyncio.sleep(0.2)
        assert keep_awake.inhibitor.active is True

        finish.set()
        for _ in range(50):
            if not keep_awake.inhibitor.active:
                break
            await asyncio.sleep(0.05)
        assert keep_awake.inhibitor.active is False
        assert backend.stops == 1
    finally:
        finish.set()
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_a_refused_turn_does_not_leak_a_hold(monkeypatch):
    """A 409 starts no turn, so it must take no hold that nothing would release."""
    backend = FakeBackend()
    monkeypatch.setattr(keep_awake, "inhibitor", SleepInhibitor(backend))

    async def must_not_run(_data):
        raise AssertionError("a second turn must not start")

    monkeypatch.setattr(stdin_mod, "handle_run", must_not_run)
    server = AsyncHTTPServer()
    server.send_response = lambda *_a, **_k: None
    alive = asyncio.create_task(asyncio.Event().wait())
    server.active_agent_task = alive
    try:
        await server.route_api(
            "POST", "/api/gilzabots/run", {}, {},
            json.dumps({"agent": "chat_orchestrator", "prompt": "hi"}).encode("utf-8"),
            AsyncMock(),
        )
    finally:
        alive.cancel()

    assert backend.starts == []
    assert keep_awake.inhibitor.holders == 0
