"""Open with default app: the desktop opener and its failure reporting.

A launcher that exits with an error (xdg-open exiting 3, for example) must not
be reported as "opened" while nothing was opened.
"""

import json
import platform
import subprocess
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from gilzabots.ide_server import (
    AsyncHTTPServer,
    _desktop_open_command,
    _desktop_open_failure,
)


def test_the_command_is_xdg_open_on_path():
    assert _desktop_open_command("/home/u/doc.pdf") == ["xdg-open", "/home/u/doc.pdf"]


class _FakeProc:
    """A launcher whose exit status is decided up front."""

    def __init__(self, returncode):
        self._returncode = returncode
        self.args = None
        self.env = None

    def poll(self):
        return self._returncode


@pytest.mark.asyncio
async def test_failure_window_reports_a_refused_launcher():
    failure = await _desktop_open_failure(_FakeProc(3), window=0.05)
    assert failure is not None
    assert "3" in failure


@pytest.mark.asyncio
async def test_failure_window_accepts_a_clean_exit():
    assert await _desktop_open_failure(_FakeProc(0), window=0.05) is None


@pytest.mark.asyncio
async def test_failure_window_accepts_a_launcher_still_running():
    """The launcher forks the application, so "still alive" is a success."""
    assert await _desktop_open_failure(_FakeProc(None), window=0.05) is None


@pytest.mark.asyncio
async def test_no_process_to_watch_is_not_a_failure():
    assert await _desktop_open_failure(None, window=0.05) is None


async def _call_open_explorer(server, project_dir, file_name, writer):
    body = json.dumps({"projectPath": str(project_dir), "filePath": file_name})
    await server.route_api(
        "POST", "/api/file/open-explorer", {}, {}, body.encode("utf-8"), writer
    )


@pytest.fixture
def linux_server(monkeypatch, tmp_path):
    """A server on Linux with a recorded, non-executing Popen."""
    if platform.system() == "Windows":
        pytest.skip("POSIX launcher path")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        platform, "uname", lambda: SimpleNamespace(release="6.8.0-generic")
    )

    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    launched = {}

    def fake_popen(args, env=None, **kwargs):
        launched["args"] = args
        launched["env"] = env
        return _FakeProc(fake_popen.returncode)

    fake_popen.returncode = 0
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    project = tmp_path / "project"
    project.mkdir()
    (project / "report.pdf").write_bytes(b"%PDF-1.4\n")
    return server, responses, launched, fake_popen, project


@pytest.mark.asyncio
async def test_pyinstaller_ld_library_path_is_still_stripped(monkeypatch, linux_server):
    server, _responses, launched, _popen, project = linux_server
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    await _call_open_explorer(server, project, "report.pdf", AsyncMock())

    assert "LD_LIBRARY_PATH" not in launched["env"]


@pytest.mark.asyncio
async def test_a_refused_launcher_is_not_reported_as_opened(monkeypatch, linux_server):
    server, responses, _launched, fake_popen, project = linux_server
    fake_popen.returncode = 3

    await _call_open_explorer(server, project, "report.pdf", AsyncMock())

    assert len(responses) == 1
    status, payload = responses[0]
    assert status == 500
    assert "3" in payload["error"]


@pytest.mark.asyncio
async def test_failure_window_accepts_custom_codes():
    assert (
        await _desktop_open_failure(_FakeProc(1), window=0.05, accept_codes=(0, 1))
        is None
    )
    failure = await _desktop_open_failure(
        _FakeProc(2), window=0.05, accept_codes=(0, 1)
    )
    assert failure is not None
    assert "2" in failure


@pytest.mark.asyncio
async def test_wsl_opens_directories_via_explorer_with_status_1(
    monkeypatch, linux_server
):
    """On WSL, explorer.exe is used and exit code 1 is treated as success."""
    import shutil

    server, responses, launched, fake_popen, project = linux_server
    monkeypatch.setattr(
        platform,
        "uname",
        lambda: SimpleNamespace(release="5.15.153.1-microsoft-standard-WSL2"),
    )
    monkeypatch.setattr(
        shutil, "which", lambda cmd: "/mnt/c/Windows/explorer.exe" if cmd == "explorer.exe" else None
    )
    fake_popen.returncode = 1

    await _call_open_explorer(server, project, None, AsyncMock())

    assert responses == [(200, {"success": True})]
    assert launched["args"] == ["explorer.exe", "."]


@pytest.mark.asyncio
async def test_wsl_falls_back_when_explorer_not_on_path(
    monkeypatch, linux_server
):
    """If explorer.exe is not on PATH, fall back to the desktop opener command."""
    import shutil

    server, responses, launched, _popen, project = linux_server
    monkeypatch.setattr(
        platform,
        "uname",
        lambda: SimpleNamespace(release="5.15.153.1-microsoft-standard-WSL2"),
    )
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    await _call_open_explorer(server, project, None, AsyncMock())

    assert responses == [(200, {"success": True})]
    assert launched["args"] == ["xdg-open", str(project)]

