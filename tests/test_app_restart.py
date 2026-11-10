"""Tests for the in-app restart used after saving restart-only settings.

The Settings modal asks the user whether to restart once GILZABOTS_HOME changes;
answering yes hits ``POST /api/app/restart``, which relaunches the process.
"""

import json
import os
import pathlib
import sys
import threading
import subprocess
from unittest.mock import AsyncMock

import pytest

from gilzabots import ide_server
from gilzabots.ide_server import AsyncHTTPServer, build_relaunch_command, schedule_app_restart


@pytest.fixture(autouse=True)
def script_launch_context(monkeypatch):
    # The pytest runner may itself have been started with python -m pytest.
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None)


def test_module_relaunch_preserves_package_imports_and_arguments(tmp_path):
    """Exercise real -m startup and a child restart."""
    package = tmp_path / "restart_probe"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 'package import works'\n")
    (package / "cli.py").write_text(
        "from . import VALUE\n"
        "import json, subprocess, sys\n"
        "from gilzabots.ide_server import build_relaunch_command\n"
        "if sys.argv[-1] == 'child':\n"
        "    print(json.dumps([VALUE, sys.argv[1:]]))\n"
        "else:\n"
        "    sys.argv[-1] = 'child'\n"
        "    subprocess.run(build_relaunch_command(), check=True)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(pathlib.Path(ide_server.__file__).resolve().parent.parent),
         env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-m", "restart_probe.cli", "--gui", "path with spaces", "parent"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [
        "package import works", ["--gui", "path with spaces", "child"]
    ]


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


def test_relaunch_command_runs_the_frozen_binary_directly(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/GilzaBots/GilzaBots")
    monkeypatch.setattr(sys, "argv", ["/opt/GilzaBots/GilzaBots", "--mode", "edit"])

    assert build_relaunch_command() == ["/opt/GilzaBots/GilzaBots", "--mode", "edit"]


def test_relaunch_command_reruns_the_entry_script_with_the_same_interpreter(monkeypatch, tmp_path):
    script = tmp_path / "main.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", [str(script), "--model", "gpt"])

    assert build_relaunch_command() == ["/usr/bin/python3", str(script), "--model", "gpt"]


def test_relaunch_command_falls_back_to_the_cli_entry_point(monkeypatch):
    """``python -c ...`` launches leave argv[0] pointing at nothing runnable."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["-c"])

    assert build_relaunch_command() == [
        "/usr/bin/python3",
        "-c",
        "from gilzabots.cli import main; main()",
    ]


def test_schedule_app_restart_spawns_a_replacement_then_exits(monkeypatch):
    """The child must be spawned only as this process leaves, so the port is free."""
    events = []
    finished = threading.Event()

    monkeypatch.setattr(ide_server, "build_relaunch_command", lambda: ["python", "main.py"])
    monkeypatch.setattr(
        ide_server,
        "spawn_detached",
        lambda command, cwd=None, env=None: events.append(("spawn", command, cwd, env)),
    )

    def fake_exit(code):
        events.append(("exit", code))
        finished.set()

    monkeypatch.setattr(os, "_exit", fake_exit)

    # The run being replaced had acceleration turned off, so its own environment
    # carries the flags that decision produced.
    from gilzabots.webengine_env import (
        CHROMIUM_FLAGS_VAR,
        QUICK_BACKEND_VAR,
        apply_webengine_environment,
    )

    monkeypatch.delenv(CHROMIUM_FLAGS_VAR, raising=False)
    monkeypatch.delenv(QUICK_BACKEND_VAR, raising=False)
    # QtWebEngine's variables (macOS/Linux); the WebView2 one is covered in
    # test_webengine_env.
    apply_webengine_environment(os.environ, "darwin", {"webengine_gpu": "off"})
    assert os.environ[CHROMIUM_FLAGS_VAR] == "--disable-gpu"

    command = schedule_app_restart(delay=0)
    assert command == ["python", "main.py"]
    assert finished.wait(timeout=5)
    assert [e[0] for e in events] == ["spawn", "exit"]
    assert events[0][1] == ["python", "main.py"]
    assert events[1] == ("exit", 0)

    # The replacement decides its rendering mode from the saved setting, so it
    # must not inherit the mode of the run it is replacing.
    child_env = events[0][3]
    assert child_env is not None, "the restart must hand the child an environment"
    assert CHROMIUM_FLAGS_VAR not in child_env
    assert QUICK_BACKEND_VAR not in child_env


def test_schedule_app_restart_keeps_running_when_the_spawn_fails(monkeypatch):
    """A failed relaunch must not leave the user with no app at all."""
    exits = []
    monkeypatch.setattr(ide_server, "build_relaunch_command", lambda: ["python", "main.py"])

    def failing_spawn(command, cwd=None):
        raise OSError("no such file")

    monkeypatch.setattr(ide_server, "spawn_detached", failing_spawn)
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))

    schedule_app_restart(delay=0)
    for _ in range(50):
        if exits:
            break
        threading.Event().wait(0.02)
    assert exits == []


@pytest.mark.asyncio
async def test_restart_endpoint_reports_the_relaunch_command(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ide_server,
        "schedule_app_restart",
        lambda: (calls.append(True), ["python", "main.py"])[1],
    )

    server, responses = _server_with_capture()
    await server.route_api("POST", "/api/app/restart", {}, {}, b"", AsyncMock())

    assert calls == [True]
    assert responses[-1][:2] == (200, {"success": True, "command": ["python", "main.py"]})


@pytest.mark.asyncio
async def test_restart_endpoint_returns_500_when_the_relaunch_cannot_be_scheduled(monkeypatch):
    def failing():
        raise RuntimeError("boom")

    monkeypatch.setattr(ide_server, "schedule_app_restart", failing)

    server, responses = _server_with_capture()
    await server.route_api("POST", "/api/app/restart", {}, {}, b"", AsyncMock())

    assert responses[-1][0] == 500
    assert responses[-1][1] == {"error": "boom"}


@pytest.mark.asyncio
async def test_environment_endpoint_reports_platform_and_version(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/app/environment", {}, {}, b"", AsyncMock())

    assert responses[-1] == (
        200,
        {"platform": "Linux", "version": "0.2.44"},
        "application/json",
    )


@pytest.mark.asyncio
async def test_saving_the_data_directory_only_requires_a_restart_when_it_changed(tmp_path, monkeypatch):
    """Re-saving the same path must not nag the user with a restart prompt."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    server, responses = _server_with_capture()
    writer = AsyncMock()
    target = tmp_path / "data"

    async def post(path_value):
        await server.route_api(
            "POST",
            "/api/settings/gilzabotshome",
            {},
            {},
            json.dumps({"path": path_value}).encode("utf-8"),
            writer,
        )
        return responses[-1]

    assert await post(str(target)) == (200, {"success": True, "requiresRestart": True}, "application/json")
    assert (tmp_path / ".gilzabotshome").read_text(encoding="utf-8") == str(target)

    assert (await post(str(target)))[1] == {"success": True, "requiresRestart": False}

    assert (await post(""))[1] == {"success": True, "requiresRestart": True}
    assert not (tmp_path / ".gilzabotshome").exists()

    assert (await post(""))[1] == {"success": True, "requiresRestart": False}
