"""Tests for validating the configurable global data directory (GILZABOTS_HOME).

A directory that `os.path.isdir` accepts is not necessarily one GilzaBots can
write to (a read-only mount, or permissions that deny creating files). Accepting
such a path surfaced much later as
`sqlite3.OperationalError: unable to open database file` on the next launch.
"""

import json
import os
import pathlib
import sqlite3
import tempfile
from unittest.mock import AsyncMock

import pytest

from gilzabots import config, ide_server, project


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    config._verified_data_dirs.clear()
    config._reported_data_dir_problems.clear()
    yield
    config._verified_data_dirs.clear()
    config._reported_data_dir_problems.clear()


def _deny_writes(monkeypatch, denied_dir):
    """Simulate a directory that stats fine but rejects every file creation."""
    real_named_temp_file = tempfile.NamedTemporaryFile

    def guarded(*args, **kwargs):
        if os.path.abspath(str(kwargs.get("dir", ""))) == os.path.abspath(str(denied_dir)):
            raise PermissionError(13, "Permission denied")
        return real_named_temp_file(*args, **kwargs)

    monkeypatch.setattr(config.tempfile, "NamedTemporaryFile", guarded)


def test_a_writable_directory_is_accepted_and_created(tmp_path):
    target = tmp_path / "data"

    assert config.check_data_dir(str(target)) == ""
    assert target.is_dir()
    assert not list(target.iterdir()), "the write probe must not leave files behind"


def test_an_empty_path_is_rejected():
    assert config.check_data_dir("") == "the path is empty"


def test_a_path_that_is_a_file_is_rejected(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("", encoding="utf-8")

    assert "could not be created" in config.check_data_dir(str(target))


def test_a_directory_that_only_looks_valid_is_rejected(tmp_path, monkeypatch):
    """isdir() succeeds, opening a file in it does not."""
    target = tmp_path / ".gilzabots"
    target.mkdir()
    _deny_writes(monkeypatch, target)

    problem = config.check_data_dir(str(target))

    assert "not writable" in problem
    assert str(target) not in config._verified_data_dirs


def test_unusable_configured_home_falls_back_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GILZABOTS_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    broken = tmp_path / "broken"
    broken.mkdir()
    (tmp_path / ".gilzabotshome").write_text(str(broken), encoding="utf-8")
    _deny_writes(monkeypatch, broken)

    assert config.get_gilzabots_home() == str(tmp_path / ".gilzabots")
    assert "not writable" in capsys.readouterr().err

    # The warning is printed once per path, not on every lookup.
    config.get_gilzabots_home()
    assert capsys.readouterr().err == ""


def test_a_usable_configured_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv("GILZABOTS_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "data"
    (tmp_path / ".gilzabotshome").write_text(str(target), encoding="utf-8")

    assert config.get_gilzabots_home() == str(target)
    assert config.configured_gilzabots_home() == str(target)


def _server_with_capture():
    server = ide_server.AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


@pytest.mark.asyncio
async def test_saving_an_unusable_directory_is_rejected_without_storing_it(tmp_path, monkeypatch):
    monkeypatch.delenv("GILZABOTS_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    broken = tmp_path / "broken"
    broken.mkdir()
    _deny_writes(monkeypatch, broken)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/settings/gilzabotshome",
        {},
        {},
        json.dumps({"path": str(broken)}).encode("utf-8"),
        AsyncMock(),
    )

    status, body, _ = responses[-1]
    assert status == 400
    assert "not writable" in body["error"]
    assert not (tmp_path / ".gilzabotshome").exists()


@pytest.mark.asyncio
async def test_reading_the_setting_reports_a_broken_configured_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("GILZABOTS_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    broken = tmp_path / "broken"
    broken.mkdir()
    (tmp_path / ".gilzabotshome").write_text(str(broken), encoding="utf-8")
    _deny_writes(monkeypatch, broken)

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/settings/gilzabotshome", {}, {}, b"", AsyncMock())

    _, body, _ = responses[-1]
    assert body["configured_path"] == str(broken)
    assert "not writable" in body["error"]
    assert body["path"] == str(tmp_path / ".gilzabots")


def test_database_open_failure_names_the_directory(tmp_path, monkeypatch):
    db_path = str(tmp_path / "data" / "sessions.db")

    def refuse(_path):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(project.sqlite3, "connect", refuse)

    with pytest.raises(sqlite3.OperationalError) as excinfo:
        project._conn(db_path)

    message = str(excinfo.value)
    assert db_path in message
    assert str(tmp_path / "data") in message
