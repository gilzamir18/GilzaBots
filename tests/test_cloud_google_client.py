"""Which Google OAuth client an installation connects with.

The normal path is one click: the build ships a client, the user presses
Connect and authorizes in the browser. A user who wants their own Cloud project
can still register a client, and that override has to win.
"""

import json
from unittest.mock import AsyncMock

import pytest

from gilzabots.cloud.providers import google_drive
from gilzabots.ide_server import AsyncHTTPServer


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """Point both credential files at a scratch directory."""
    user_file = tmp_path / "user" / "google_client.json"
    bundled_file = tmp_path / "bundled" / "bundled_google_client.json"
    user_file.parent.mkdir(parents=True)
    bundled_file.parent.mkdir(parents=True)
    monkeypatch.setattr(google_drive, "client_config_path", lambda: str(user_file))
    monkeypatch.setattr(google_drive, "bundled_client_path", lambda: str(bundled_file))
    monkeypatch.delenv("GILZABOTS_GDRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GILZABOTS_GDRIVE_CLIENT_SECRET", raising=False)
    return {"user": user_file, "bundled": bundled_file}


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


def test_no_client_anywhere_reports_none(isolated_client):
    assert google_drive.describe_client_config() == {
        "client_id": "",
        "client_secret": "",
        "source": "none",
    }


def test_the_bundled_client_is_used_without_the_user_configuring_anything(isolated_client):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})

    resolved = google_drive.describe_client_config()

    assert resolved == {"client_id": "ship-id", "client_secret": "ship-secret", "source": "bundled"}


def test_a_user_client_overrides_the_bundled_one(isolated_client):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    google_drive.save_client_config("mine-id", "mine-secret")

    resolved = google_drive.describe_client_config()

    assert resolved["source"] == "user"
    assert resolved["client_id"] == "mine-id"


def test_the_environment_overrides_both(isolated_client, monkeypatch):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    google_drive.save_client_config("mine-id", "mine-secret")
    monkeypatch.setenv("GILZABOTS_GDRIVE_CLIENT_ID", "env-id")
    monkeypatch.setenv("GILZABOTS_GDRIVE_CLIENT_SECRET", "env-secret")

    assert google_drive.describe_client_config() == {
        "client_id": "env-id",
        "client_secret": "env-secret",
        "source": "environment",
    }


def test_a_half_written_override_falls_through_to_the_bundled_client(isolated_client):
    """An override with no client id must not disable a working connection."""
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    _write(isolated_client["user"], {"client_id": "", "client_secret": "orphan"})

    assert google_drive.describe_client_config()["source"] == "bundled"


def test_clearing_the_override_returns_to_the_bundled_client(isolated_client):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    google_drive.save_client_config("mine-id", "mine-secret")

    google_drive.clear_client_config()

    assert google_drive.describe_client_config()["source"] == "bundled"
    # Clearing twice is not an error: the user can press it again.
    google_drive.clear_client_config()


def test_a_console_credentials_file_is_accepted_as_shipped(isolated_client):
    """Injecting the file the Google console hands out must work unchanged."""
    _write(isolated_client["bundled"], {"installed": {"client_id": "ship-id", "client_secret": "s"}})

    assert google_drive.describe_client_config()["client_id"] == "ship-id"


def test_the_provider_connects_with_the_bundled_client(isolated_client, monkeypatch):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    monkeypatch.setattr(google_drive, "_load_token", lambda: google_drive._Token())

    provider = google_drive.GoogleDriveProvider()

    assert provider.client_id == "ship-id"
    # No client configured is the only state that reports an OAuth-client error.
    assert provider.auth_status().error == ""


@pytest.mark.asyncio
async def test_the_endpoint_reports_where_the_client_came_from(isolated_client):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/cloud/google-client", {}, {}, b"", AsyncMock())

    status, body, _ = responses[-1]
    assert status == 200
    assert body["source"] == "bundled"
    assert body["configured"] is True
    assert body["bundled_available"] is True
    assert body["custom_client_id"] == ""
    # No secret ever crosses back to the UI.
    assert "client_secret" not in body


@pytest.mark.asyncio
async def test_saving_and_clearing_an_override_through_the_api(isolated_client):
    _write(isolated_client["bundled"], {"client_id": "ship-id", "client_secret": "ship-secret"})
    server, responses = _server_with_capture()
    writer = AsyncMock()

    await server.route_api(
        "POST",
        "/api/cloud/google-client",
        {}, {},
        json.dumps({"client_id": "mine-id", "client_secret": "mine-secret"}).encode("utf-8"),
        writer,
    )
    assert responses[-1][:2] == (200, {"success": True})

    await server.route_api("GET", "/api/cloud/google-client", {}, {}, b"", writer)
    assert responses[-1][1]["source"] == "user"
    assert responses[-1][1]["custom_client_id"] == "mine-id"
    assert responses[-1][1]["has_custom_client_secret"] is True

    # An empty secret keeps the stored one, so the id can be corrected alone.
    await server.route_api(
        "POST",
        "/api/cloud/google-client",
        {}, {},
        json.dumps({"client_id": "mine-id-2", "client_secret": ""}).encode("utf-8"),
        writer,
    )
    assert google_drive.load_user_client_config() == {
        "client_id": "mine-id-2",
        "client_secret": "mine-secret",
    }

    await server.route_api("DELETE", "/api/cloud/google-client", {}, {}, b"", writer)
    assert responses[-1][:2] == (200, {"success": True})
    await server.route_api("GET", "/api/cloud/google-client", {}, {}, b"", writer)
    assert responses[-1][1]["source"] == "bundled"


@pytest.mark.asyncio
async def test_saving_an_empty_client_id_is_rejected_rather_than_stored(isolated_client):
    """Blanking the field is DELETE's job; storing it would look like an override."""
    server, responses = _server_with_capture()

    await server.route_api(
        "POST",
        "/api/cloud/google-client",
        {}, {},
        json.dumps({"client_id": "  ", "client_secret": "x"}).encode("utf-8"),
        AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert not isolated_client["user"].exists()
