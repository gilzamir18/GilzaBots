"""Regression tests for the local-Ollama model pre-fetch.

The pre-fetch exists so the first message of a project configured against a
local Ollama model does not fail on a model that was never downloaded. It used
to run on *every* `update-project` call and always shelled out to `ollama pull`,
so unrelated edits (main file, git root, compile flags) silently downloaded
gigabytes. These tests pin the two rules that keep it honest:

1. `update-project` pre-fetches only when the model selection actually changed.
2. `pull_model_in_background` never re-downloads a model Ollama already serves,
   and never runs the same pull twice concurrently.
"""

import json
import threading
import time
import types
from unittest.mock import AsyncMock

import pytest

from gilzabots import ollama_manager
from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.project import ProjectStore


def _wait_until(predicate, timeout=5.0):
    """Poll `predicate` until it holds or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------- #
# Endpoint gating
# --------------------------------------------------------------------------- #

@pytest.fixture()
def api(tmp_path, monkeypatch):
    """A server harness whose pre-fetch calls are recorded instead of executed."""
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    pulled = []
    monkeypatch.setattr(
        "gilzabots.ollama_manager.pull_model_in_background",
        lambda name, report=None: pulled.append(name) or True,
    )

    server = AsyncHTTPServer()
    server.send_response = lambda _w, status, body, ctype="text/plain": None
    return types.SimpleNamespace(store=store, server=server, pulled=pulled, tmp_path=tmp_path)


async def _update(api, payload):
    await api.server.route_api(
        "POST",
        "/api/gilzabots/update-project",
        {},
        {},
        json.dumps(payload).encode("utf-8"),
        AsyncMock(),
    )


def _project(api, model):
    project_dir = api.tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return api.store.create(
        name="myproj",
        mode="plan",
        model=model,
        project_name="My Project",
        project_path=str(project_dir),
    )


@pytest.mark.asyncio
async def test_unrelated_update_does_not_prefetch(api):
    """The bug: setting the main file re-downloaded the project's model."""
    _project(api, "ollama/gemma4:latest")

    await _update(api, {"project_name": "myproj", "main_file": "main.tex"})

    assert api.pulled == []


@pytest.mark.asyncio
async def test_resending_the_same_model_does_not_prefetch(api):
    """The settings dialog re-sends every field, including the unchanged model."""
    _project(api, "ollama/gemma4:latest")

    await _update(api, {
        "project_name": "myproj",
        "model": "ollama/gemma4:latest",
        "description": "edited",
    })

    assert api.pulled == []


@pytest.mark.asyncio
async def test_changing_to_a_local_model_prefetches_it(api):
    _project(api, "ollama/gemma4:latest")

    await _update(api, {"project_name": "myproj", "model": "ollama/qwen3.5:8b"})

    assert api.pulled == ["qwen3.5:8b"]


@pytest.mark.asyncio
async def test_clearing_the_model_does_not_prefetch(api):
    _project(api, "ollama/gemma4:latest")

    await _update(api, {"project_name": "myproj", "model": ""})

    assert api.pulled == []


@pytest.mark.asyncio
async def test_cloud_and_non_ollama_models_are_never_prefetched(api):
    _project(api, "")

    await _update(api, {"project_name": "myproj", "model": "ollama/qwen3.5:cloud"})
    await _update(api, {"project_name": "myproj", "model": "openai/gpt-5"})

    assert api.pulled == []


# --------------------------------------------------------------------------- #
# pull_model_in_background
# --------------------------------------------------------------------------- #

@pytest.fixture()
def fake_pull(monkeypatch):
    """Replace the `ollama pull` subprocess with a recorder."""
    calls = []
    gate = threading.Event()
    gate.set()

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        gate.wait(timeout=5.0)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ollama_manager, "subprocess", types.SimpleNamespace(run=fake_run))
    monkeypatch.setattr(ollama_manager, "_report", lambda message: None)
    yield types.SimpleNamespace(calls=calls, gate=gate)

    gate.set()
    assert _wait_until(lambda: not ollama_manager._PULLS_IN_FLIGHT)


def test_installed_model_is_not_downloaded_again(monkeypatch, fake_pull):
    monkeypatch.setattr(
        ollama_manager, "list_local_model_names", lambda: {"gemma4:latest", "gemma4"}
    )

    assert ollama_manager.pull_model_in_background("gemma4:latest") is True
    assert _wait_until(lambda: not ollama_manager._PULLS_IN_FLIGHT)
    assert fake_pull.calls == []


def test_implicit_latest_tag_counts_as_installed(monkeypatch, fake_pull):
    """`ollama/gemma4` and the reported `gemma4:latest` are the same model."""
    monkeypatch.setattr(
        ollama_manager, "list_local_model_names", lambda: {"gemma4:latest", "gemma4"}
    )

    ollama_manager.pull_model_in_background("gemma4")

    assert _wait_until(lambda: not ollama_manager._PULLS_IN_FLIGHT)
    assert fake_pull.calls == []


def test_missing_model_is_downloaded(monkeypatch, fake_pull):
    monkeypatch.setattr(ollama_manager, "list_local_model_names", lambda: {"gemma4:latest"})

    assert ollama_manager.pull_model_in_background("qwen3.5:8b") is True

    assert _wait_until(lambda: fake_pull.calls)
    assert fake_pull.calls == [["ollama", "pull", "qwen3.5:8b"]]


def test_unreachable_ollama_still_attempts_the_download(monkeypatch, fake_pull):
    """An unknown local catalog must not be read as "already installed"."""
    monkeypatch.setattr(ollama_manager, "list_local_model_names", lambda: None)

    ollama_manager.pull_model_in_background("qwen3.5:8b")

    assert _wait_until(lambda: fake_pull.calls)
    assert fake_pull.calls == [["ollama", "pull", "qwen3.5:8b"]]


def test_concurrent_pulls_of_the_same_model_are_deduplicated(monkeypatch, fake_pull):
    monkeypatch.setattr(ollama_manager, "list_local_model_names", lambda: set())
    fake_pull.gate.clear()

    assert ollama_manager.pull_model_in_background("qwen3.5:8b") is True
    assert _wait_until(lambda: fake_pull.calls)
    assert ollama_manager.pull_model_in_background("qwen3.5:8b") is False

    fake_pull.gate.set()
    assert _wait_until(lambda: not ollama_manager._PULLS_IN_FLIGHT)
    assert fake_pull.calls == [["ollama", "pull", "qwen3.5:8b"]]


def test_blank_model_name_is_ignored(fake_pull):
    assert ollama_manager.pull_model_in_background("") is False
    assert ollama_manager.pull_model_in_background("   ") is False
    assert fake_pull.calls == []


def test_local_model_names_parses_the_tags_payload(monkeypatch):
    class _Response:
        def read(self):
            return json.dumps({
                "models": [{"name": "gemma4:latest"}, {"name": "qwen3.5:8b"}, {}, "junk"]
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(ollama_manager.urllib.request, "urlopen", lambda *a, **k: _Response())

    assert ollama_manager.list_local_model_names() == {"gemma4:latest", "gemma4", "qwen3.5:8b"}


def test_local_model_names_returns_none_when_ollama_is_unreachable(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(ollama_manager.urllib.request, "urlopen", _boom)

    assert ollama_manager.list_local_model_names() is None
