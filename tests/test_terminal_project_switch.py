"""Bottom-panel terminals follow the open project.

The panel's tabs are named ``main-1``, ``main-2``, ... and those ids survive a
project switch. The server used to look them up by id alone, so after opening
project Y the terminal was still the shell started in project X's root.
"""
import asyncio
import os
import sys

import pytest

from gilzabots import terminal_manager
from gilzabots.ide_server import AsyncHTTPServer


class FakeSession:
    def __init__(self, project_path):
        self.project_path = project_path
        self.is_running = True
        self.closed = False

    def start_reading(self, loop):
        pass

    def close(self):
        self.closed = True
        self.is_running = False


@pytest.fixture
def fake_sessions(monkeypatch):
    monkeypatch.setattr(terminal_manager, "TerminalSession", FakeSession)


@pytest.fixture
def projects(tmp_path):
    x = tmp_path / "project_x"
    y = tmp_path / "project_y"
    x.mkdir()
    y.mkdir()
    return str(x), str(y)


def _lookup(server, term_id, project_path):
    async def run():
        return server._project_terminal(term_id, project_path)
    return asyncio.run(run())


@pytest.mark.parametrize("term_id", ["main", "main-1", "main-2"])
def test_switching_project_restarts_the_shell_in_the_new_project(fake_sessions, projects, term_id):
    x, y = projects
    server = AsyncHTTPServer()

    first = _lookup(server, term_id, x)
    second = _lookup(server, term_id, y)

    assert first.project_path == x
    assert first.closed
    assert second.project_path == y
    assert not second.closed


@pytest.mark.parametrize("term_id", ["main", "main-1"])
def test_same_project_reuses_the_session(fake_sessions, projects, term_id):
    x, _ = projects
    server = AsyncHTTPServer()

    first = _lookup(server, term_id, x)

    assert _lookup(server, term_id, x) is first
    # Input without a project path goes to whatever shell is running.
    assert _lookup(server, term_id, None) is first
    assert not first.closed


def test_exited_shell_is_restarted(fake_sessions, projects):
    x, _ = projects
    server = AsyncHTTPServer()
    first = _lookup(server, "main-1", x)
    first.close()  # the user typed `exit`

    second = _lookup(server, "main-1", x)

    assert second is not first
    assert second.is_running


def test_other_tabs_are_left_alone(fake_sessions, projects):
    x, y = projects
    server = AsyncHTTPServer()
    other = _lookup(server, "main-2", x)

    _lookup(server, "main-1", y)

    assert server.temp_terminals["main-2"] is other
    assert not other.closed


def test_missing_project_starts_nothing(fake_sessions, tmp_path):
    server = AsyncHTTPServer()

    assert _lookup(server, "main-1", None) is None
    assert _lookup(server, "main-1", str(tmp_path / "gone")) is None
    assert "main-1" not in server.temp_terminals


@pytest.mark.skipif(sys.platform != "linux", reason="reads the shell's cwd from /proc")
def test_real_shell_runs_in_the_newly_opened_project(projects):
    x, y = projects
    server = AsyncHTTPServer()

    async def run():
        first = server._project_terminal("main-1", x)
        first_cwd = os.readlink(f"/proc/{first.process.pid}/cwd")
        second = server._project_terminal("main-1", y)
        second_cwd = os.readlink(f"/proc/{second.process.pid}/cwd")
        second.close()
        return first, first_cwd, second_cwd

    first, first_cwd, second_cwd = asyncio.run(run())

    assert first_cwd == os.path.realpath(x)
    assert second_cwd == os.path.realpath(y)
    assert not first.is_running
    assert first.process is None  # the old shell was terminated, not orphaned
