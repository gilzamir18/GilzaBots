"""Regression tests for git root validation in `/api/gilzabots/update-project`.

The edit modal re-sends every project field on save, including the git root it
loaded from the database. Validating that echoed value made a *stored* git root
that no longer resolves -- a folder that moved, or a Windows path (`G:\\...`) in
a database synced to a Linux machine -- reject unrelated edits such as changing
the description, with "Git root path does not exist or is not a directory".

The rules pinned here:

1. An unchanged git root never blocks a save, and is persisted verbatim (never
   silently cleared or rewritten), with a warning returned to the client.
2. A *changed* git root is still validated exactly as before.
"""

import json
import subprocess
import types
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.project import ProjectStore


@pytest.fixture()
def api(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    responses = []
    server = AsyncHTTPServer()
    server.send_response = lambda _w, status, body, ctype="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store.create(
        name="myproj",
        mode="plan",
        model="ollama/gemma4:latest",
        project_name="My Project",
        project_path=str(project_dir),
        description="before",
    )
    return types.SimpleNamespace(
        store=store, server=server, responses=responses, project_dir=project_dir
    )


async def _update(api, payload):
    await api.server.route_api(
        "POST",
        "/api/gilzabots/update-project",
        {},
        {},
        json.dumps(payload).encode("utf-8"),
        AsyncMock(),
    )
    return api.responses[-1]


def _set_stored_git_root(api, value):
    """Write a git root straight to the store, bypassing endpoint validation."""
    project = api.store.load("myproj")
    project.git_root_path = value
    api.store.save(project)


@pytest.mark.asyncio
async def test_unrelated_edit_survives_a_stale_stored_git_root(api):
    """The bug: a stored Windows path blocked every save on Linux."""
    stale = "G:\\Meu Drive\\GilzaBots\\AgenticBook"
    _set_stored_git_root(api, stale)

    status, body = await _update(api, {
        "project_name": "myproj",
        "description": "after",
        "git_root_path": stale,
    })

    assert status == 200
    assert body["description"] == "after"
    # Kept verbatim: repairing it stays the user's decision.
    assert body["git_root_path"] == stale
    assert body["git_root_warning"]
    assert api.store.load("myproj").description == "after"
    assert api.store.load("myproj").git_root_path == stale


@pytest.mark.asyncio
async def test_unchanged_valid_git_root_reports_no_warning(api):
    subprocess.run(["git", "init"], cwd=api.project_dir, check=True, capture_output=True)
    _set_stored_git_root(api, str(api.project_dir))

    status, body = await _update(api, {
        "project_name": "myproj",
        "git_root_path": str(api.project_dir),
    })

    assert status == 200
    assert body["git_root_warning"] == ""


@pytest.mark.asyncio
async def test_clearing_a_stale_git_root_still_works(api):
    _set_stored_git_root(api, "G:\\Meu Drive\\GilzaBots\\AgenticBook")

    status, body = await _update(api, {"project_name": "myproj", "git_root_path": ""})

    assert status == 200
    assert body["git_root_path"] == ""
    assert api.store.load("myproj").git_root_path == ""


@pytest.mark.asyncio
async def test_changed_git_root_is_still_validated(api):
    _set_stored_git_root(api, "G:\\Meu Drive\\GilzaBots\\AgenticBook")

    status, body = await _update(api, {
        "project_name": "myproj",
        "git_root_path": str(api.project_dir / "does-not-exist"),
    })

    assert status == 400
    assert body["error"] == "Git root path does not exist or is not a directory"


@pytest.mark.asyncio
async def test_changed_git_root_must_contain_a_repository(api):
    sub = api.project_dir / "chapter"
    sub.mkdir()

    status, body = await _update(api, {"project_name": "myproj", "git_root_path": str(sub)})

    assert status == 400
    assert body["error"] == "Git root path must contain a .git repository"
