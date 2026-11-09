"""Default models: the catalog's global default, the project's, and the chat's own.

A chat runs on its own model when one was picked for it, else on the project's
default model (`projects.model`), else on the model the catalog marks as the
global default (PROJECT_DESIGN.md §2.8). The first model registered in an empty
catalog becomes the global default, so a fresh install -- e.g. an OpenRouter
sign-in that seeds `openrouter/free` -- has a chat that can run at once.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from gilzabots import models_store
from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.project import ChatNotFoundError, ProjectStore, resolve_chat_models


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """An isolated, empty model catalog with one connection."""
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    models_store.add_or_update_connection({
        "id": "conn", "label": "Conn", "provider": "ollama",
        "api_key": "", "api_base": "https://ollama.example/v1",
    })
    return models_store


def _add(catalog, model_id, **fields):
    catalog.add_or_update_model({
        "id": model_id, "connection_id": "conn", "name": model_id.split("/", 1)[-1], **fields,
    })


def _flags(catalog):
    return {m["id"]: m["is_default"] for m in catalog.load_models()}


@pytest.fixture(autouse=True)
def _no_leaked_session():
    yield
    import gilzabots.tools as tools

    tools._PROJECT_SESSION = None


# ── The catalog's global default ─────────────────────────────────────────────


def test_the_first_model_of_an_empty_catalog_becomes_the_default(catalog):
    _add(catalog, "ollama/first")
    _add(catalog, "ollama/second")

    assert _flags(catalog) == {"ollama/first": True, "ollama/second": False}
    assert catalog.get_default_model_id() == "ollama/first"


def test_a_caller_cannot_set_the_flag_through_the_model_form(catalog):
    _add(catalog, "ollama/first")
    _add(catalog, "ollama/second", is_default=True)
    _add(catalog, "ollama/first", previous_id="ollama/first", is_default=False)

    assert _flags(catalog) == {"ollama/first": True, "ollama/second": False}


def test_set_default_model_moves_the_flag_to_exactly_one_entry(catalog):
    _add(catalog, "ollama/first")
    _add(catalog, "ollama/second")

    entry = catalog.set_default_model("ollama/second")

    assert entry["id"] == "ollama/second" and entry["is_default"] is True
    assert _flags(catalog) == {"ollama/first": False, "ollama/second": True}


@pytest.mark.parametrize("bad", ["", "ollama/missing"])
def test_set_default_model_refuses_an_id_the_catalog_does_not_hold(catalog, bad):
    _add(catalog, "ollama/first")
    with pytest.raises(ValueError):
        catalog.set_default_model(bad)
    assert catalog.get_default_model_id() == "ollama/first"


def test_renaming_the_default_model_keeps_it_the_default(catalog):
    _add(catalog, "ollama/first")
    _add(catalog, "ollama/second")
    _add(catalog, "ollama/renamed", previous_id="ollama/first")

    assert catalog.get_default_model_id() == "ollama/renamed"


def test_deleting_the_default_promotes_nothing(catalog):
    _add(catalog, "ollama/first")
    _add(catalog, "ollama/second")

    assert catalog.delete_model("ollama/first") is True

    assert catalog.get_default_model_id() == ""
    assert _flags(catalog) == {"ollama/second": False}


def test_save_models_keeps_a_single_default(catalog):
    catalog.save_models([
        {"id": "ollama/a", "connection_id": "conn", "name": "a", "is_default": True},
        {"id": "ollama/b", "connection_id": "conn", "name": "b", "is_default": True},
    ])
    assert _flags(catalog) == {"ollama/a": True, "ollama/b": False}


def test_the_free_openrouter_model_becomes_the_default_of_an_empty_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    models_store.add_or_update_connection({
        "id": "openrouter", "label": "OpenRouter", "provider": "openrouter",
        "api_key": "sk-test", "api_base": models_store.OPENROUTER_API_BASE,
    })

    seeded = models_store.ensure_default_openrouter_free_model("openrouter")

    assert seeded["id"] == "openrouter/openrouter/free"
    assert seeded["is_default"] is True


def test_the_free_openrouter_model_does_not_displace_an_existing_default(catalog):
    _add(catalog, "ollama/first")
    catalog.add_or_update_connection({
        "id": "openrouter", "label": "OpenRouter", "provider": "openrouter",
        "api_key": "sk-test", "api_base": models_store.OPENROUTER_API_BASE,
    })

    seeded = catalog.ensure_default_openrouter_free_model("openrouter")

    assert seeded["is_default"] is False
    assert catalog.get_default_model_id() == "ollama/first"


def _fake_local_ollama(monkeypatch, names):
    monkeypatch.setattr("gilzabots.ollama_manager.check_ollama_status", lambda: {"installed": True})
    payload = json.dumps({"models": [{"name": n} for n in names]}).encode()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return payload

    monkeypatch.setattr(models_store.urllib_request, "urlopen", lambda *_a, **_k: FakeResponse())


def test_importing_local_ollama_into_an_empty_catalog_marks_the_first_model(tmp_path, monkeypatch):
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    _fake_local_ollama(monkeypatch, ["alpha:latest", "beta:latest"])

    models_store.load_local_ollama_models()

    assert _flags(models_store) == {"ollama/alpha:latest": True, "ollama/beta:latest": False}


def test_importing_local_ollama_into_a_catalog_with_models_marks_nothing(catalog, monkeypatch):
    _add(catalog, "ollama/first")
    catalog.set_default_model("ollama/first")
    _fake_local_ollama(monkeypatch, ["alpha:latest"])

    catalog.load_local_ollama_models()

    assert catalog.get_default_model_id() == "ollama/first"


# ── Resolution: chat, then project, then global ──────────────────────────────


@pytest.fixture
def store(tmp_path, catalog):
    _add(catalog, "ollama/global")      # first model: the global default
    _add(catalog, "ollama/project")
    _add(catalog, "ollama/chat")
    _add(catalog, "ollama/worker")
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(name="p", mode="auto", model="", project_name="P", project_path=str(tmp_path))
    store.create_chat("p", "c1", "Chat 1")
    store.create_chat("p", "c2", "Chat 2")
    return store


def test_a_chat_with_no_model_in_a_project_with_none_runs_on_the_global_default(store):
    resolution = resolve_chat_models(store.load("p", chat_id="c1"))

    assert (resolution.model, resolution.model_source) == ("ollama/global", "global")
    assert (resolution.worker_model, resolution.worker_source) == ("", "orchestrator")
    assert store.load("p", chat_id="c1").active_model == "ollama/global"


def test_the_project_default_wins_over_the_global_default(store):
    project = store.load("p", chat_id="c1")
    project.model = "ollama/project"
    store.save(project)

    resolution = resolve_chat_models(store.load("p", chat_id="c1"))

    assert (resolution.model, resolution.model_source) == ("ollama/project", "project")


def test_a_chat_model_applies_to_that_chat_only(store):
    project = store.load("p", chat_id="c1")
    project.model = "ollama/project"
    store.save(project)

    assert store.set_chat_model("p", "c1", "model", "ollama/chat") == "ollama/chat"

    assert store.load("p", chat_id="c1").chat_model == "ollama/chat"
    assert resolve_chat_models(store.load("p", chat_id="c1")).model_source == "chat"
    assert store.load("p", chat_id="c1").active_model == "ollama/chat"
    assert store.load("p", chat_id="c2").active_model == "ollama/project"
    # The project's default is untouched by a chat's choice.
    assert store.load("p").model == "ollama/project"


def test_saving_the_project_during_a_turn_does_not_store_the_chat_model_as_default(store):
    store.set_chat_model("p", "c1", "model", "ollama/chat")
    project = store.load("p", chat_id="c1")

    store.save(project)   # what a running turn does after each message

    assert store.load("p").model == ""


def test_the_worker_resolves_from_the_chat_then_the_project(store):
    project = store.load("p", chat_id="c1")
    project.worker_model = "ollama/project"
    store.save(project)

    assert resolve_chat_models(store.load("p", chat_id="c1")).worker_source == "project"

    store.set_chat_model("p", "c1", "worker_model", "ollama/worker")
    resolution = resolve_chat_models(store.load("p", chat_id="c1"))
    assert (resolution.worker_model, resolution.worker_source) == ("ollama/worker", "chat")


def test_clearing_a_chat_model_hands_the_chat_back_to_the_default(store):
    store.set_chat_model("p", "c1", "model", "ollama/chat")
    assert store.set_chat_model("p", "c1", "model", "") == ""
    assert store.load("p", chat_id="c1").active_model == "ollama/global"


def test_no_default_anywhere_resolves_to_no_model(store, catalog):
    catalog.delete_model("ollama/global")
    resolution = resolve_chat_models(store.load("p", chat_id="c1"))
    assert (resolution.model, resolution.model_source) == ("", "")


def test_a_chat_model_must_be_in_the_catalog(store):
    with pytest.raises(ValueError):
        store.set_chat_model("p", "c1", "model", "ollama/not-registered")
    with pytest.raises(ValueError):
        store.set_chat_model("p", "c1", "temperature", "ollama/chat")
    with pytest.raises(ChatNotFoundError):
        store.set_chat_model("p", "gone", "model", "ollama/chat")
    assert store.load("p", chat_id="c1").chat_model == ""


def test_a_branch_keeps_the_models_of_the_chat_it_came_from(store):
    store.set_chat_model("p", "c1", "model", "ollama/chat")
    store.set_chat_model("p", "c1", "worker_model", "ollama/worker")
    project = store.load("p", chat_id="c1")
    store.append_message(project, "user", "first")
    store.append_message(project, "assistant", "answer")

    store.branch_chat("p", "c1", "b1", "Branch", -1)

    assert store.list_chat_models("p")["b1"] == {"model": "ollama/chat", "worker_model": "ollama/worker"}


def test_the_chat_orchestrator_is_built_on_the_resolved_model(store):
    from gilzabots.memgpt_runtime import build_chat_orchestrator

    store.set_chat_model("p", "c1", "model", "ollama/chat")

    assert "chat" in build_chat_orchestrator(store.load("p", chat_id="c1"), store).model
    assert "global" in build_chat_orchestrator(store.load("p", chat_id="c2"), store).model


# ── The run guard ────────────────────────────────────────────────────────────


def _run(monkeypatch, store, chat_id):
    import gilzabots.agent_stdin as stdin_mod

    events = []

    class FakeMemGPT:
        async def run(self, agent_input):  # pragma: no cover - must not be reached
            raise AssertionError("the agent must not run without a model")

    project = store.load("p", chat_id=chat_id)
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    asyncio.run(stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "hello"}))
    return events


def test_a_turn_with_no_model_anywhere_fails_fast(store, catalog, monkeypatch):
    catalog.delete_model("ollama/global")

    events = _run(monkeypatch, store, "c1")

    errors = [data["message"] for event, data in events if event == "error"]
    assert errors and "default model" in errors[0]


# ── HTTP ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", store.db_path)
    monkeypatch.setattr("gilzabots.ollama_manager.pull_model_in_background", lambda *_a: None)
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, ctype="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    def call(method, path, payload=None, query=None):
        responses.clear()
        asyncio.run(server.route_api(
            method, path, query or {}, {},
            json.dumps(payload or {}).encode("utf-8"), AsyncMock(),
        ))
        return responses[-1]

    return call


def test_the_chat_model_endpoint_stores_and_the_list_reports_it(api, store):
    status, body = api("POST", "/api/chat/model", {
        "project_name": "p", "chat_id": "c1", "role": "model", "model": "ollama/chat",
    })
    assert (status, body) == (200, {"status": "ok", "chat_id": "c1", "role": "model", "model": "ollama/chat"})

    status, body = api("GET", "/api/chat/list", query={"project_name": ["p"]})
    assert status == 200
    by_id = {chat["id"]: chat for chat in body["chats"]}
    assert by_id["c1"]["model"] == "ollama/chat"
    assert by_id["c2"]["model"] == ""


@pytest.mark.parametrize("payload, status", [
    ({"project_name": "p", "chat_id": "c1", "role": "model"}, 400),
    ({"project_name": "p", "chat_id": "c1", "role": "model", "model": "ollama/unknown"}, 400),
    ({"project_name": "p", "chat_id": "c1", "role": "bogus", "model": "ollama/chat"}, 400),
    ({"project_name": "p", "chat_id": "gone", "role": "model", "model": "ollama/chat"}, 404),
])
def test_the_chat_model_endpoint_refuses_bad_requests(api, payload, status):
    assert api("POST", "/api/chat/model", payload)[0] == status


def test_the_default_endpoint_marks_the_global_default(api, catalog):
    status, body = api("POST", "/api/settings/models/default", {"id": "ollama/project"})
    assert status == 200 and body["model"]["id"] == "ollama/project"
    assert catalog.get_default_model_id() == "ollama/project"

    assert api("POST", "/api/settings/models/default", {"id": "ollama/unknown"})[0] == 400
    assert catalog.get_default_model_id() == "ollama/project"

    status, body = api("GET", "/api/settings/models")
    assert status == 200
    assert {m["id"]: m["is_default"] for m in body["models"]}["ollama/project"] is True


# ── A model picked before the chat exists ────────────────────────────────────
#
# A project may have no chat until the first message creates one, and a chat's
# model is stored on the chat. The toolbar holds the choice made with no chat
# open and creates the chat with it, so the store and the create endpoint take
# the initial models.


def test_a_chat_can_be_created_with_its_own_models(store):
    store.create_chat("p", "c3", "Chat 3", model="ollama/chat", worker_model="ollama/worker")

    resolution = resolve_chat_models(store.load("p", chat_id="c3"))
    assert (resolution.model, resolution.model_source) == ("ollama/chat", "chat")
    assert (resolution.worker_model, resolution.worker_source) == ("ollama/worker", "chat")
    assert store.load("p").model == ""   # the project's default is untouched


def test_creating_a_chat_with_an_unknown_model_leaves_no_chat(store):
    with pytest.raises(ValueError):
        store.create_chat("p", "c3", "Chat 3", model="ollama/not-registered")
    with pytest.raises(ValueError):
        store.create_chat("p", "c3", "Chat 3", worker_model="ollama/not-registered")
    assert "c3" not in store.list_chat_models("p")


def test_the_create_endpoint_stores_the_models_picked_before_the_chat(api, store):
    status, body = api("POST", "/api/chat/create", {
        "project_name": "p", "chat_name": "New Chat",
        "model": "ollama/chat", "worker_model": "ollama/worker",
    })

    assert status == 200
    assert (body["model"], body["worker_model"]) == ("ollama/chat", "ollama/worker")
    assert store.list_chat_models("p")[body["id"]] == {
        "model": "ollama/chat", "worker_model": "ollama/worker",
    }


def test_the_create_endpoint_without_models_follows_the_default(api, store):
    status, body = api("POST", "/api/chat/create", {"project_name": "p", "chat_name": "New Chat"})

    assert status == 200
    assert (body["model"], body["worker_model"]) == ("", "")
    assert store.load("p", chat_id=body["id"]).active_model == "ollama/global"


def test_the_create_endpoint_refuses_an_unknown_model(api, store):
    before = set(store.list_chat_models("p"))
    status, body = api("POST", "/api/chat/create", {
        "project_name": "p", "chat_name": "New Chat", "model": "ollama/unknown",
    })

    assert status == 400 and "not in the model catalog" in body["error"]
    assert set(store.list_chat_models("p")) == before
