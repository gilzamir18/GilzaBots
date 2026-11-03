"""Under snap, user-facing default locations must be the real home.

Inside a snap ``~`` is ``~/snap/<name>/<revision>``. A project created there
(the directory picker's default, and ``~/GilzaBotsPilot``) was stored by its
absolute path, so after the next refresh it pointed at the previous revision's
directory, which AppArmor makes read-only: creating a file in the project then
failed with a read-only error.
"""

import json
import os
from unittest.mock import AsyncMock

import pytest

from gilzabots import config
from gilzabots.ide_server import AsyncHTTPServer


@pytest.fixture
def snap_env(monkeypatch, tmp_path):
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    revision_home = tmp_path / "real-home" / "snap" / "gilzabots" / "48"
    revision_home.mkdir(parents=True)
    monkeypatch.setenv("SNAP_NAME", "gilzabots")
    monkeypatch.setenv("SNAP_REAL_HOME", str(real_home))
    monkeypatch.setenv("HOME", str(revision_home))
    monkeypatch.setenv("GILZABOTS_HOME", str(revision_home / ".gilzabots"))
    return real_home


def test_user_home_is_the_real_home_under_snap(snap_env):
    assert config.user_home_dir() == str(snap_env)
    assert config.expand_user_path("~/GilzaBotsPilot") == str(snap_env / "GilzaBotsPilot")
    assert config.expand_user_path("~") == str(snap_env)


def test_user_home_is_plain_home_outside_snap(monkeypatch, tmp_path):
    monkeypatch.delenv("SNAP_NAME", raising=False)
    monkeypatch.setenv("SNAP_REAL_HOME", "/ignored")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert config.user_home_dir() == str(tmp_path)
    assert config.expand_user_path("~/p") == str(tmp_path / "p")
    assert config.expand_user_path("/abs/p") == "/abs/p"


def test_the_home_directory_itself_is_refused_as_a_project_folder_under_snap(snap_env, monkeypatch):
    """The `home` plug allows files in `~` but not `~/.gilzabots`.

    Stands in for the AppArmor denial by making `.gilzabots` unwritable to
    creation (a regular file in its place); the hint must name the snap rule.
    """
    (snap_env / ".gilzabots").write_text("")

    message = config.project_dir_error(str(snap_env))

    assert "as a project folder" in message
    assert "snap 'home' interface" in message
    assert config.project_dir_error(str(snap_env / "Thesis")) == ""


def _api_harness():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8"))))

    server.send_response = mock_send_response
    return server, responses


async def _post(server, path, payload):
    await server.route_api("POST", path, {}, {}, json.dumps(payload).encode("utf-8"), AsyncMock())


@pytest.mark.asyncio
async def test_directory_picker_starts_in_the_real_home_under_snap(snap_env):
    server, responses = _api_harness()

    await _post(server, "/api/fs/dirs", {"path": "~"})

    status, payload = responses[-1]
    assert status == 200
    assert payload["current"] == str(snap_env)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
@pytest.mark.asyncio
async def test_create_project_rejects_a_folder_it_cannot_write(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", str(tmp_path / "sessions.db"))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    server, responses = _api_harness()
    try:
        await _post(server, "/api/gilzabots/create-project",
                    {"project_name": "P", "project_path": str(locked)})
    finally:
        locked.chmod(0o755)

    status, payload = responses[-1]
    assert status == 400
    assert "not writable" in payload["error"]
