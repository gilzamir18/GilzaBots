"""Tests for the graphics-mode setting and the startup diagnostics endpoint.

Both exist for the same failure: the embedded browser can take the whole
application down without an error message, so the app has to be able to say
that it happened and to offer the one control that can stop it.
"""

import json
import sys
from unittest.mock import AsyncMock

import pytest

from gilzabots import ide_server
from gilzabots.ide_server import AsyncHTTPServer


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    monkeypatch.setattr(ide_server, "_STARTUP_DIAGNOSTICS", {}, raising=False)
    return tmp_path / "ui_settings.json"


@pytest.fixture
def qt_platform(monkeypatch):
    """macOS/Linux: the window is QtWebEngine."""
    monkeypatch.setattr(sys, "platform", "linux")


@pytest.fixture
def windows_platform(monkeypatch):
    """Windows: the window is WebView2."""
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.asyncio
async def test_graphics_defaults_to_hardware_acceleration_off(settings_file, qt_platform):
    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/settings/graphics", {}, {}, b"", AsyncMock())

    status, body, _ = responses[0]
    assert status == 200
    assert body["webengine_gpu"] == "off"
    assert body["source"] == "settings"
    assert body["engine"] == "qtwebengine"


@pytest.mark.asyncio
async def test_webview2_defaults_to_hardware_acceleration_on(settings_file, windows_platform):
    """WebView2 keeps the driver in its own GPU process, so acceleration is opt-out.

    A settings file always holds ``webengine_gpu: "off"`` (saving writes the
    defaults); it is the QtWebEngine setting and must not decide for WebView2.
    """
    settings_file.write_text(json.dumps({"webengine_gpu": "off"}), encoding="utf-8")
    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/settings/graphics", {}, {}, b"", AsyncMock())

    status, body, _ = responses[0]
    assert status == 200
    assert body["webengine_gpu"] == "auto"
    assert body["source"] == "default"
    assert body["engine"] == "webview2"


@pytest.mark.asyncio
async def test_webview2_saves_its_own_setting(settings_file, windows_platform):
    ide_server._STARTUP_DIAGNOSTICS = {"webengine_gpu": "auto"}
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/settings/graphics", {}, {},
        json.dumps({"webengine_gpu": "off"}).encode(), AsyncMock(),
    )

    assert responses[0][1]["webengine_gpu"] == "off"
    assert responses[0][1]["requiresRestart"] is True
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["webview2_gpu"] == "off"
    assert saved["webengine_gpu"] == "off"  # the untouched QtWebEngine default

    await server.route_api("GET", "/api/settings/graphics", {}, {}, b"", AsyncMock())
    assert responses[1][1]["webengine_gpu"] == "off"
    assert responses[1][1]["source"] == "settings"


@pytest.mark.asyncio
async def test_an_unknown_value_is_saved_as_the_webview2_default(settings_file, windows_platform):
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/settings/graphics", {}, {},
        json.dumps({"webengine_gpu": "nonsense"}).encode(), AsyncMock(),
    )
    assert responses[0][1]["webengine_gpu"] == "auto"


@pytest.mark.asyncio
async def test_turning_the_gpu_on_is_saved_and_asks_for_a_restart(settings_file, qt_platform):
    """QtWebEngine reads its flags before the window exists, so it needs one."""
    ide_server._STARTUP_DIAGNOSTICS = {"webengine_gpu": "off"}
    server, responses = _server_with_capture()
    monkey_body = json.dumps({"webengine_gpu": "auto"}).encode()
    await server.route_api(
        "POST", "/api/settings/graphics", {}, {}, monkey_body, AsyncMock()
    )

    status, body, _ = responses[0]
    assert status == 200
    assert body["webengine_gpu"] == "auto"
    assert body["requiresRestart"] is True
    assert json.loads(settings_file.read_text(encoding="utf-8"))["webengine_gpu"] == "auto"

    # Saving the mode the running process already uses asks for nothing.
    ide_server._STARTUP_DIAGNOSTICS = {"webengine_gpu": "auto"}
    await server.route_api(
        "POST", "/api/settings/graphics", {}, {}, monkey_body, AsyncMock()
    )
    assert responses[1][1]["requiresRestart"] is False


@pytest.mark.asyncio
async def test_an_unknown_value_is_saved_as_the_default_mode(settings_file, qt_platform):
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/settings/graphics", {}, {},
        json.dumps({"webengine_gpu": "nonsense"}).encode(), AsyncMock(),
    )
    assert responses[0][1]["webengine_gpu"] == "off"


@pytest.mark.asyncio
async def test_startup_report_carries_the_previous_crash_to_the_window(settings_file):
    failure = {
        "pid": 1234,
        "started": "2026-09-15T21:59:00+00:00",
        "native_fault": True,
        "log_path": "/home/user/.gilzabots/logs/crash.log",
    }
    ide_server._STARTUP_DIAGNOSTICS = {
        "previous_run_failures": [failure],
        "crash_log_path": failure["log_path"],
        "webengine_gpu": "auto",
    }

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/diagnostics/startup-report", {}, {}, b"", AsyncMock())

    status, body, _ = responses[0]
    assert status == 200
    assert body["previous_run_failures"] == [failure]
    assert body["webengine_gpu"] == "auto"


@pytest.mark.asyncio
async def test_startup_report_is_not_consumed_by_reading_it(settings_file):
    """Reloading the window must not erase the only notice about a silent crash."""
    ide_server._STARTUP_DIAGNOSTICS = {
        "previous_run_failures": [{"pid": 7, "native_fault": True}],
        "crash_log_path": "crash.log",
        "webengine_gpu": "auto",
    }

    server, responses = _server_with_capture()
    for _ in range(2):
        await server.route_api("GET", "/api/diagnostics/startup-report", {}, {}, b"", AsyncMock())

    assert responses[0][1]["previous_run_failures"] == responses[1][1]["previous_run_failures"]
