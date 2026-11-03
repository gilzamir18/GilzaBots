"""Open with default app: the snap must not shadow snapd's opener shim.

Inside strict confinement `$SNAP/usr/bin` precedes `/usr/bin` on PATH, so a bare
`xdg-open` resolves to any xdg-utils in the payload. That copy runs confined,
finds none of the helpers it delegates to, and exits 3 without opening anything
-- while the endpoint still answered 200. These cover both halves.
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
    _running_in_snap,
)


def _enter_snap(monkeypatch, snap_dir="/snap/gilzabots/33"):
    monkeypatch.setenv("SNAP", snap_dir)
    monkeypatch.setenv("SNAP_NAME", "gilzabots")


def _leave_snap(monkeypatch):
    monkeypatch.delenv("SNAP", raising=False)
    monkeypatch.delenv("SNAP_NAME", raising=False)


def test_running_in_snap_requires_both_markers(monkeypatch):
    _leave_snap(monkeypatch)
    assert _running_in_snap() is False
    monkeypatch.setenv("SNAP", "/snap/gilzabots/33")
    assert _running_in_snap() is False
    monkeypatch.setenv("SNAP_NAME", "gilzabots")
    assert _running_in_snap() is True


def test_snap_addresses_the_shim_by_full_path(monkeypatch):
    """A bare "xdg-open" would hit the payload copy; the shim must win."""
    _enter_snap(monkeypatch)
    monkeypatch.setattr("os.path.exists", lambda p: p == "/usr/bin/xdg-open")
    assert _desktop_open_command("/home/u/doc.pdf") == [
        "/usr/bin/xdg-open",
        "/home/u/doc.pdf",
    ]


def test_outside_a_snap_the_command_stays_on_path(monkeypatch):
    _leave_snap(monkeypatch)
    assert _desktop_open_command("/home/u/doc.pdf") == ["xdg-open", "/home/u/doc.pdf"]


def test_snap_without_the_shim_falls_back_to_path(monkeypatch):
    _enter_snap(monkeypatch)
    monkeypatch.setattr("os.path.exists", lambda p: False)
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
async def test_endpoint_opens_through_the_shim_inside_a_snap(monkeypatch, linux_server):
    server, responses, launched, _popen, project = linux_server
    _enter_snap(monkeypatch)
    monkeypatch.setattr("os.path.exists", lambda p: True)

    await _call_open_explorer(server, project, "report.pdf", AsyncMock())

    assert responses == [(200, {"success": True})]
    assert launched["args"][0] == "/usr/bin/xdg-open"


@pytest.mark.asyncio
async def test_snap_keeps_its_own_ld_library_path(monkeypatch, linux_server):
    """That variable is the snap's runtime, not a frozen loader's to strip."""
    server, _responses, launched, _popen, project = linux_server
    _enter_snap(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/snap/gilzabots/33/lib")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    await _call_open_explorer(server, project, "report.pdf", AsyncMock())

    assert launched["env"]["LD_LIBRARY_PATH"] == "/snap/gilzabots/33/lib"


@pytest.mark.asyncio
async def test_pyinstaller_ld_library_path_is_still_stripped(monkeypatch, linux_server):
    server, _responses, launched, _popen, project = linux_server
    _leave_snap(monkeypatch)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIxxxx")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)

    await _call_open_explorer(server, project, "report.pdf", AsyncMock())

    assert "LD_LIBRARY_PATH" not in launched["env"]


@pytest.mark.asyncio
async def test_a_refused_launcher_is_not_reported_as_opened(monkeypatch, linux_server):
    server, responses, _launched, fake_popen, project = linux_server
    _leave_snap(monkeypatch)
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
async def test_wsl_inside_snap_opens_directories_via_shim(monkeypatch, linux_server):
    """Inside snap on WSL, explorer.exe cannot run; opening folders must use the snapd shim."""
    server, responses, launched, _popen, project = linux_server
    _enter_snap(monkeypatch)
    monkeypatch.setattr(
        platform,
        "uname",
        lambda: SimpleNamespace(release="5.15.153.1-microsoft-standard-WSL2"),
    )
    monkeypatch.setattr("os.path.exists", lambda p: True)

    await _call_open_explorer(server, project, None, AsyncMock())

    assert responses == [(200, {"success": True})]
    assert launched["args"][0] == "/usr/bin/xdg-open"
    assert launched["args"][1] == str(project)


@pytest.mark.asyncio
async def test_wsl_outside_snap_opens_directories_via_explorer_with_status_1(
    monkeypatch, linux_server
):
    """Outside snap on WSL, explorer.exe is used and exit code 1 is treated as success."""
    import shutil

    server, responses, launched, fake_popen, project = linux_server
    _leave_snap(monkeypatch)
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
async def test_wsl_outside_snap_falls_back_when_explorer_not_on_path(
    monkeypatch, linux_server
):
    """If explorer.exe is not on PATH outside snap, fall back to desktop opener command."""
    import shutil

    server, responses, launched, _popen, project = linux_server
    _leave_snap(monkeypatch)
    monkeypatch.setattr(
        platform,
        "uname",
        lambda: SimpleNamespace(release="5.15.153.1-microsoft-standard-WSL2"),
    )
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    await _call_open_explorer(server, project, None, AsyncMock())

    assert responses == [(200, {"success": True})]
    assert launched["args"] == ["xdg-open", str(project)]

