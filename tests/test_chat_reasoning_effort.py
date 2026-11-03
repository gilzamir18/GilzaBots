"""A chat can ask for a different reasoning effort than its model's catalog entry.

The model catalog stays the default source of every inference parameter
(PROJECT_DESIGN.md §2.8). The reasoning effort is the one a user also wants to
turn per conversation: a quick question and a hard derivation can share a model
and a project. The value is stored on the chat row, read when the chat's
orchestrator is built and when it delegates to a `run_skill` worker, and handed
to `get_agent_llm_kwargs` through the existing `reasoning_effort_override`, so it
reaches the provider by the same path as the catalog value -- as `think` on
Ollama, as `reasoning_effort` elsewhere.
"""

import os
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.project import (
    CHAT_REASONING_EFFORTS,
    ChatNotFoundError,
    ProjectStore,
)


@pytest.fixture
def store(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="myproj", mode="auto", model="ollama/thinker",
        project_name="My Project", project_path=str(tmp_path),
    )
    store.create_chat("myproj", "first", "First Chat")
    store.create_chat("myproj", "second", "Second Chat")
    return store


@pytest.fixture(autouse=True)
def _no_leaked_session():
    yield
    import gilzabots.tools as tools

    tools._PROJECT_SESSION = None


def _register(tmp_path, monkeypatch, model_id, provider="ollama", **fields):
    """Register *model_id* in an isolated model catalog."""
    from gilzabots import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    connection = f"c-{provider}"
    models_store.add_or_update_connection({
        "id": connection, "label": provider, "provider": provider,
        "api_key": "", "api_base": fields.pop("api_base", ""),
    })
    models_store.add_or_update_model(
        {"id": model_id, "connection_id": connection, "name": model_id.split("/", 1)[-1], **fields}
    )


# ── Storage ──────────────────────────────────────────────────────────────────


def test_a_chat_follows_the_catalog_until_an_effort_is_set(store):
    assert store.get_chat_reasoning_effort("myproj", "first") == ""
    assert store.load("myproj").chat_reasoning_effort == ""
    assert store.list_chat_reasoning_efforts("myproj") == {}


def test_the_effort_belongs_to_one_chat(store):
    assert store.set_chat_reasoning_effort("myproj", "second", "LOW ") == "low"

    assert store.load("myproj", chat_id="second").chat_reasoning_effort == "low"
    assert store.load("myproj", chat_id="first").chat_reasoning_effort == ""
    assert store.list_chat_reasoning_efforts("myproj") == {"second": "low"}


def test_clearing_the_effort_returns_the_chat_to_the_catalog(store):
    store.set_chat_reasoning_effort("myproj", "second", "high")
    assert store.set_chat_reasoning_effort("myproj", "second", "") == ""
    assert store.get_chat_reasoning_effort("myproj", "second") == ""
    assert store.list_chat_reasoning_efforts("myproj") == {}


@pytest.mark.parametrize("bad", ["none", "max", "true", "fast"])
def test_an_unknown_level_is_refused_and_nothing_is_stored(store, bad):
    store.set_chat_reasoning_effort("myproj", "second", "medium")
    with pytest.raises(ValueError):
        store.set_chat_reasoning_effort("myproj", "second", bad)
    assert store.get_chat_reasoning_effort("myproj", "second") == "medium"


def test_an_unknown_chat_is_refused(store):
    with pytest.raises(ChatNotFoundError):
        store.set_chat_reasoning_effort("myproj", "no-such-chat", "low")


def test_the_chat_list_shape_is_unchanged(store):
    """`ProjectData.chats` stays `{id, name}`; the effort has its own accessors."""
    store.set_chat_reasoning_effort("myproj", "second", "high")
    assert store.load("myproj").chats == [
        {"id": "second", "name": "Second Chat"},
        {"id": "first", "name": "First Chat"},
    ]


def test_a_branch_keeps_the_effort_of_the_chat_it_came_from(store):
    store.set_chat_reasoning_effort("myproj", "second", "high")
    project = store.load("myproj", chat_id="second")
    store.append_message(project, "user", "first")
    store.append_message(project, "assistant", "answer")
    anchor = store.load("myproj", chat_id="second").history[0]["id"]

    store.branch_chat("myproj", "second", "branch-a", "Branch A", 0, message_id=anchor)
    store.branch_chat_before_message("myproj", "second", "branch-b", "Branch B", message_id=anchor)
    store.branch_chat("myproj", "first", "branch-c", "Branch C", -1)

    assert store.get_chat_reasoning_effort("myproj", "branch-a") == "high"
    assert store.get_chat_reasoning_effort("myproj", "branch-b") == "high"
    assert store.get_chat_reasoning_effort("myproj", "branch-c") == ""


def test_a_database_from_before_the_setting_gains_the_column(tmp_path):
    db_path = str(tmp_path / "old.db")
    store = ProjectStore(db_path=db_path)
    store.create(name="old", mode="auto", model="ollama/thinker", project_path=str(tmp_path))
    store.create_chat("old", "old-chat", "Chat")
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE project_chats DROP COLUMN reasoning_effort")

    reopened = ProjectStore(db_path=db_path)

    assert reopened.load("old").chat_reasoning_effort == ""
    assert reopened.set_chat_reasoning_effort("old", "old-chat", "low") == "low"


# ── What reaches the provider ────────────────────────────────────────────────


def _orchestrator_kwargs(store, chat_id):
    from gilzabots.memgpt_runtime import build_chat_orchestrator

    return build_chat_orchestrator(store.load("myproj", chat_id=chat_id), store).model_kwargs


def test_each_chat_runs_its_orchestrator_at_its_own_effort(store, tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=True, reasoning_effort="high")
    store.set_chat_reasoning_effort("myproj", "second", "low")

    # Ollama carries the level in `think` (config.resolve_think_request).
    assert _orchestrator_kwargs(store, "second")["think"] == "low"
    assert _orchestrator_kwargs(store, "first")["think"] == "high"


def test_the_effort_reaches_an_openai_compatible_provider_as_reasoning_effort(store, tmp_path, monkeypatch):
    _register(
        tmp_path, monkeypatch, "openai/google/gemini-flash", provider="openai",
        api_base="https://openrouter.ai/api/v1", supports_thinking=True, reasoning_effort="low",
    )
    project = store.load("myproj")
    project.model = "openai/google/gemini-flash"
    store.save(project)
    store.set_chat_reasoning_effort("myproj", "second", "high")

    assert _orchestrator_kwargs(store, "second")["reasoning_effort"] == "high"
    assert _orchestrator_kwargs(store, "first")["reasoning_effort"] == "low"


def test_a_model_that_does_not_reason_is_sent_no_effort(store, tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=False)
    store.set_chat_reasoning_effort("myproj", "second", "high")

    kwargs = _orchestrator_kwargs(store, "second")

    assert "think" not in kwargs
    assert "reasoning_effort" not in kwargs


def _worker_kwargs(store, chat_id, monkeypatch, worker_model):
    """Delegate once from *chat_id* and return the model kwargs the worker got."""
    from types import SimpleNamespace

    import gilzabots.memgpt_runtime as runtime

    built = []

    class FakeLLMAgentBlock:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.model = kwargs.get("model", "")
            self.tools = kwargs.get("tools", [])
            self.model_kwargs = kwargs.get("model_kwargs", {})
            self.on_iteration = self.on_thinking = self.on_chunk = None
            built.append(self)

        async def _acompletion(self, _messages, **_kwargs):
            return SimpleNamespace(choices=[])

        async def run(self, _input):
            return SimpleNamespace(response="done", tool_calls_made=1)

    monkeypatch.setattr(runtime, "LLMAgentBlock", FakeLLMAgentBlock)
    project = store.load("myproj", chat_id=chat_id)
    project.worker_model = worker_model
    orchestrator = runtime.build_chat_orchestrator(project, store)
    run_skill = runtime.build_run_skill_tool(
        orchestrator, project.project_path,
        project_model=project.model, project_worker=project.worker_model,
        _project_ref=project, _store_ref=store,
    )
    raw = getattr(run_skill, "_func", None) or run_skill
    import yaml
    with open(os.path.join(project.project_path, "skills.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"skills": ["staged-reader"]}, f)  # run_skill accepts helpers only
    asyncio.run(raw("staged-reader", "inspect the project"))
    assert built, "the worker sub-agent should have been constructed"
    return built[-1].model_kwargs


def test_the_workers_of_a_chat_run_at_its_effort(store, tmp_path, monkeypatch):
    """The effort covers the whole turn: a delegated worker thinks at it too."""
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=True, reasoning_effort="high")
    _register(tmp_path, monkeypatch, "ollama/helper", supports_thinking=True, reasoning_effort="medium")
    store.set_chat_reasoning_effort("myproj", "second", "low")

    assert _worker_kwargs(store, "second", monkeypatch, "ollama/helper")["think"] == "low"
    # A chat without an override leaves the worker on its own catalog entry.
    assert _worker_kwargs(store, "first", monkeypatch, "ollama/helper")["think"] == "medium"


def test_a_worker_model_that_does_not_reason_is_sent_no_effort(store, tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=True)
    _register(tmp_path, monkeypatch, "ollama/helper", supports_thinking=False)
    store.set_chat_reasoning_effort("myproj", "second", "high")

    kwargs = _worker_kwargs(store, "second", monkeypatch, "ollama/helper")

    assert "think" not in kwargs
    assert "reasoning_effort" not in kwargs


# ── HTTP ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", store.db_path)
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, ctype="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    async def post(payload):
        await server.route_api(
            "POST", "/api/chat/reasoning-effort", {}, {},
            json.dumps(payload).encode("utf-8"), AsyncMock(),
        )
        return responses[-1]

    async def chat_list():
        await server.route_api(
            "GET", "/api/chat/list", {"project_name": ["myproj"]}, {}, b"", AsyncMock(),
        )
        return responses[-1]

    return post, chat_list


def test_the_endpoint_stores_the_effort_and_the_chat_list_reports_it(store, api):
    post, chat_list = api

    status, body = asyncio.run(post(
        {"project_name": "myproj", "chat_id": "second", "reasoning_effort": "high"}
    ))
    assert (status, body) == (200, {"status": "ok", "chat_id": "second", "reasoning_effort": "high"})
    assert store.get_chat_reasoning_effort("myproj", "second") == "high"

    status, body = asyncio.run(chat_list())
    assert status == 200
    assert body["chats"] == [
        {"id": "second", "name": "Second Chat", "reasoning_effort": "high"},
        {"id": "first", "name": "First Chat", "reasoning_effort": ""},
    ]


def test_the_endpoint_clears_the_effort_with_an_empty_value(store, api):
    post, _chat_list = api
    store.set_chat_reasoning_effort("myproj", "second", "high")

    status, body = asyncio.run(post(
        {"project_name": "myproj", "chat_id": "second", "reasoning_effort": ""}
    ))

    assert (status, body["reasoning_effort"]) == (200, "")
    assert store.get_chat_reasoning_effort("myproj", "second") == ""


@pytest.mark.parametrize(
    "payload, expected_status",
    [
        ({"project_name": "myproj", "chat_id": "second", "reasoning_effort": "max"}, 400),
        ({"project_name": "myproj", "chat_id": "second"}, 400),
        ({"project_name": "myproj", "reasoning_effort": "low"}, 400),
        ({"project_name": "myproj", "chat_id": "gone", "reasoning_effort": "low"}, 404),
    ],
)
def test_the_endpoint_refuses_what_it_cannot_store(store, api, payload, expected_status):
    post, _chat_list = api

    status, body = asyncio.run(post(payload))

    assert status == expected_status
    assert body["error"]
    assert store.list_chat_reasoning_efforts("myproj") == {}


# ── Command-line interface ───────────────────────────────────────────────────


def _dispatch(state, *args):
    from gilzabots.cli_commands import _registry

    return asyncio.run(_registry.dispatch(state, "/effort", list(args)))


@pytest.fixture
def cli(store, monkeypatch):
    import gilzabots.terminal as T
    from gilzabots.cli_commands import REPLState

    said = []
    for level in ("info", "success", "warning", "error"):
        monkeypatch.setattr(T, level, lambda msg, level=level: said.append((level, msg)))
    state = REPLState(store.load("myproj", chat_id="second"), store, renderer=object())
    return state, said


def test_the_cli_sets_and_clears_the_effort_of_the_current_chat(store, tmp_path, monkeypatch, cli):
    state, said = cli
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=True)
    state.memgpt = "an orchestrator built at the previous effort"

    assert _dispatch(state, "high") == "continue"
    assert store.get_chat_reasoning_effort("myproj", "second") == "high"
    assert store.get_chat_reasoning_effort("myproj", "first") == ""
    assert state.project.chat_reasoning_effort == "high"
    assert state._memgpt is None
    assert [level for level, _msg in said] == ["success"]

    _dispatch(state, "default")
    assert store.get_chat_reasoning_effort("myproj", "second") == ""
    assert state.project.chat_reasoning_effort == ""


def test_the_cli_reports_the_current_effort(store, cli):
    state, said = cli
    store.set_chat_reasoning_effort("myproj", "second", "medium")

    _dispatch(state)

    assert said and said[-1][0] == "info" and "medium" in said[-1][1]


def test_the_cli_refuses_an_unknown_level(store, cli):
    state, said = cli

    _dispatch(state, "max")

    assert said[-1][0] == "error"
    assert store.get_chat_reasoning_effort("myproj", "second") == ""


def test_the_cli_says_so_when_no_model_has_an_effort_to_change(store, tmp_path, monkeypatch, cli):
    state, said = cli
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=False)

    _dispatch(state, "low")

    assert [level for level, _msg in said] == ["success", "warning"]
    assert store.get_chat_reasoning_effort("myproj", "second") == "low"


def test_the_cli_does_not_warn_when_only_the_worker_reasons(store, tmp_path, monkeypatch, cli):
    state, said = cli
    _register(tmp_path, monkeypatch, "ollama/thinker", supports_thinking=False)
    _register(tmp_path, monkeypatch, "ollama/helper", supports_thinking=True)
    state.project.worker_model = "ollama/helper"

    _dispatch(state, "low")

    assert [level for level, _msg in said] == ["success"]


def test_every_level_the_cli_and_the_composer_offer_is_storable(store):
    for level in CHAT_REASONING_EFFORTS:
        assert store.set_chat_reasoning_effort("myproj", "second", level) == level
