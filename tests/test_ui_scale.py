"""Tests for the accessibility interface scale setting.

The scale is persisted in ui_settings.json rather than localStorage because it
is the one setting a user with low vision cannot work around if it silently
resets between sessions.
"""

import json
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.ui_settings import (
    UI_SCALE_MAX,
    UI_SCALE_MIN,
    clamp_ui_scale,
    load_ui_settings,
    save_ui_settings,
)


def test_clamp_ui_scale_bounds():
    assert clamp_ui_scale(5.0) == UI_SCALE_MAX
    assert clamp_ui_scale(0.1) == UI_SCALE_MIN
    assert clamp_ui_scale(1.25) == 1.25
    assert clamp_ui_scale("1.4") == 1.4


def test_clamp_ui_scale_falls_back_instead_of_shrinking():
    # A corrupted settings file must not leave the interface unreadable, so
    # anything non-numeric becomes the unscaled default rather than the minimum.
    for value in ("not a number", None, float("nan"), float("inf"), float("-inf"), [], {}):
        assert clamp_ui_scale(value) == 1.0, f"for {value!r}"


def test_ui_scale_defaults_to_unscaled(tmp_path, monkeypatch):
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    assert load_ui_settings()["ui_scale"] == 1.0


def test_ui_scale_survives_a_restart(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)
    save_ui_settings({"ui_scale": 1.4})
    # Re-read from disk, as a fresh session would.
    assert load_ui_settings()["ui_scale"] == 1.4
    assert json.loads(settings_file.read_text(encoding="utf-8"))["ui_scale"] == 1.4


def test_ui_scale_does_not_disturb_the_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")
    save_ui_settings({"lang": "pt-BR"})
    save_ui_settings({"ui_scale": 1.2})
    settings = load_ui_settings()
    assert settings["lang"] == "pt-BR"
    assert settings["ui_scale"] == 1.2


@pytest.mark.asyncio
async def test_appearance_settings_endpoints(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api("GET", "/api/settings/appearance", {}, {}, b"", writer)
    assert responses[-1] == (
        200,
        {"ui_scale": 1.0, "ui_scale_min": UI_SCALE_MIN, "ui_scale_max": UI_SCALE_MAX},
        "application/json",
    )

    await server.route_api(
        "POST", "/api/settings/appearance", {}, {},
        json.dumps({"ui_scale": 1.4}).encode("utf-8"), writer,
    )
    assert responses[-1] == (200, {"success": True, "ui_scale": 1.4}, "application/json")

    await server.route_api("GET", "/api/settings/appearance", {}, {}, b"", writer)
    assert responses[-1][1]["ui_scale"] == 1.4


@pytest.mark.asyncio
async def test_appearance_endpoint_clamps_hostile_input(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []
    server.send_response = lambda _w, code, body, ct="text/plain": responses.append(
        (code, json.loads(body.decode("utf-8")), ct)
    )

    # An out-of-range or nonsense value must never be written through to the
    # store, or the next launch would come up unusable.
    for sent, expected in ((99, UI_SCALE_MAX), (0.01, UI_SCALE_MIN), ("huge", 1.0)):
        await server.route_api(
            "POST", "/api/settings/appearance", {}, {},
            json.dumps({"ui_scale": sent}).encode("utf-8"), writer,
        )
        assert responses[-1][1]["ui_scale"] == expected
        assert load_ui_settings()["ui_scale"] == expected
