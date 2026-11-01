"""The desktop window reopens the project it had open last.

The choice used to live in localStorage, which is per origin. The origin
carries the server port, and the port probe treated a port still holding
TIME_WAIT connections from the previous run as taken, so closing and quickly
reopening the app moved it to 3001, 3002, ... — each with its own stale
``lastActiveProject``. The value is now owned by the backend, and the probe
uses the listener's bind semantics.
"""

import asyncio
import json
import os
import socket
from unittest.mock import AsyncMock

import pytest

from opalatex.ide_server import AsyncHTTPServer, find_available_port
from opalatex.ui_settings import load_ui_settings


def _capture(server):
    responses = []
    server.send_response = lambda _w, code, body, ct="text/plain": responses.append(
        (code, json.loads(body.decode("utf-8")))
    )
    return responses


@pytest.mark.asyncio
async def test_last_project_round_trips_through_list_projects(tmp_path, monkeypatch):
    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    monkeypatch.setattr("opalatex.config.DEFAULT_DB_PATH", str(tmp_path / "sessions.db"))
    server = AsyncHTTPServer()
    responses = _capture(server)
    writer = AsyncMock()

    await server.route_api("GET", "/api/opalatex/list-projects", {}, {}, b"", writer)
    assert responses[-1] == (200, {"projects": [], "last_active_project": ""})

    await server.route_api(
        "POST", "/api/settings/last-project", {}, {},
        json.dumps({"project_name": "paper_a"}).encode("utf-8"), writer,
    )
    assert responses[-1] == (200, {"success": True, "last_active_project": "paper_a"})
    # A fresh session reads it back from disk.
    assert load_ui_settings()["last_active_project"] == "paper_a"

    await server.route_api("GET", "/api/opalatex/list-projects", {}, {}, b"", writer)
    assert responses[-1][1]["last_active_project"] == "paper_a"


@pytest.mark.asyncio
async def test_last_project_rejects_a_missing_name(tmp_path, monkeypatch):
    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    server = AsyncHTTPServer()
    responses = _capture(server)
    writer = AsyncMock()

    for body in ({}, {"project_name": ""}, {"project_name": "  "}, {"project_name": 3}):
        await server.route_api(
            "POST", "/api/settings/last-project", {}, {},
            json.dumps(body).encode("utf-8"), writer,
        )
        assert responses[-1][0] == 400, f"for {body!r}"
    assert load_ui_settings()["last_active_project"] == ""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(os.name == "nt", reason="TIME_WAIT reuse is the POSIX listener's semantics")
def test_port_probe_reuses_a_port_left_in_time_wait():
    port = _free_port()

    async def run_and_close_server():
        async def handle(reader, writer):
            await reader.read(16)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()  # the server closes first, so TIME_WAIT is on its side

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await reader.read()
        writer.close()
        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()

    asyncio.run(run_and_close_server())
    assert find_available_port("127.0.0.1", port, port + 5) == port


def test_port_probe_skips_a_port_that_is_listening():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert find_available_port("127.0.0.1", port, port + 20) != port


# ── Interface preferences (formerly localStorage) ──────────────────────────


@pytest.mark.asyncio
async def test_client_prefs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    server = AsyncHTTPServer()
    responses = _capture(server)
    writer = AsyncMock()

    await server.route_api("GET", "/api/settings/client-prefs", {}, {}, b"", writer)
    assert responses[-1] == (200, {"prefs": {}, "imported": False})

    await server.route_api(
        "POST", "/api/settings/client-prefs", {}, {},
        json.dumps({"set": {"theme": "light", "editorFontSize": "15"}}).encode("utf-8"), writer,
    )
    assert responses[-1] == (200, {"success": True, "applied": True})
    await server.route_api(
        "POST", "/api/settings/client-prefs", {}, {},
        json.dumps({"set": {"editorFontSize": "16"}, "remove": ["theme"]}).encode("utf-8"), writer,
    )
    await server.route_api("GET", "/api/settings/client-prefs", {}, {}, b"", writer)
    assert responses[-1][1]["prefs"] == {"editorFontSize": "16"}


@pytest.mark.asyncio
async def test_client_prefs_reject_malformed_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    server = AsyncHTTPServer()
    responses = _capture(server)
    writer = AsyncMock()

    for body in (
        {"set": {"theme": 3}},
        {"set": ["theme"]},
        {"set": {"": "x"}},
        {"set": {"k" * 201: "x"}},
        {"remove": "theme"},
        {"remove": [5]},
    ):
        await server.route_api(
            "POST", "/api/settings/client-prefs", {}, {},
            json.dumps(body).encode("utf-8"), writer,
        )
        assert responses[-1][0] == 400, f"for {body!r}"
    assert load_ui_settings()["client_prefs"] == {}


def test_legacy_import_runs_once_and_never_overwrites(tmp_path, monkeypatch):
    from opalatex.ui_settings import load_client_prefs, update_client_prefs

    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    update_client_prefs({"theme": "dark"})

    assert update_client_prefs(
        {"theme": "light", "chatZoom": "1.2", "lastActiveProject": "paper_a", "uiLanguage": "en"},
        legacy_import=True,
    ) is True
    stored = load_client_prefs()
    # A key already owned by the backend wins; the rest is imported, except the
    # keys that are not interface preferences.
    assert stored == {"prefs": {"theme": "dark", "chatZoom": "1.2"}, "imported": True}
    assert load_ui_settings()["last_active_project"] == "paper_a"

    # A second window — possibly on another port, with an older copy — is ignored.
    assert update_client_prefs({"chatZoom": "2.0", "editorMinimap": "off"}, legacy_import=True) is False
    assert load_client_prefs()["prefs"] == {"theme": "dark", "chatZoom": "1.2"}


def test_legacy_import_keeps_a_recorded_last_project(tmp_path, monkeypatch):
    from opalatex.ui_settings import save_ui_settings, update_client_prefs

    monkeypatch.setattr("opalatex.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    save_ui_settings({"last_active_project": "current"})
    update_client_prefs({"lastActiveProject": "older"}, legacy_import=True)
    assert load_ui_settings()["last_active_project"] == "current"
