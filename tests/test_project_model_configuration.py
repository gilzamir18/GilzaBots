"""Regression tests for the "a project starts with no model configured" rule.

Covers:
1. Creating a project without a model must not inject an implicit default.
2. update-project must be able to change *and* clear the model selection, so the
   chat model selector and the project settings dialog always agree.
3. Running the chat orchestrator on a project with no model must fail fast with a
   clear diagnostic instead of silently falling back to DEFAULT_MODEL.
"""

import os
import tempfile

import pytest

from gilzabots.project import ProjectStore


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(db_path=str(tmp_path / "sessions.db"))


def test_project_created_without_model_stays_unconfigured(store, tmp_path):
    project = store.create(
        name="p1",
        mode="auto",
        model="",
        project_name="P1",
        project_path=str(tmp_path / "p1"),
    )

    assert project.model == ""
    assert project.worker_model == ""

    listed = store.list_projects()[0]
    assert listed["model"] == ""
    assert listed["worker_model"] == ""


def test_model_selection_round_trips_through_the_store(store, tmp_path):
    store.create(
        name="p1",
        mode="auto",
        model="",
        project_name="P1",
        project_path=str(tmp_path / "p1"),
    )

    project = store.load("p1")
    project.model = "ollama/qwen3.5:cloud"
    project.worker_model = "ollama/qwen3.5:cloud"
    store.save(project)

    listed = store.list_projects()[0]
    assert listed["model"] == "ollama/qwen3.5:cloud"
    assert listed["worker_model"] == "ollama/qwen3.5:cloud"

    # The selection must also be clearable, back to the unconfigured state.
    project = store.load("p1")
    project.model = ""
    project.worker_model = ""
    store.save(project)

    listed = store.list_projects()[0]
    assert listed["model"] == ""
    assert listed["worker_model"] == ""


@pytest.mark.asyncio
async def test_handle_run_refuses_project_without_model(monkeypatch):
    import gilzabots.agent_stdin as stdin_mod

    events = []

    class FakeMemGPT:
        async def run(self, agent_input):  # pragma: no cover - must not be reached
            raise AssertionError("the agent must not run without a configured model")

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(db_path=os.path.join(tmp, "sessions.db"))
        project = store.create(
            name="p1",
            mode="auto",
            model="",
            project_name="P1",
            project_path=os.path.join(tmp, "p1"),
        )

        monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
        monkeypatch.setattr(stdin_mod, "current_project", project)
        monkeypatch.setattr(stdin_mod, "current_store", store)
        monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())

        await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "hello"})

    error_events = [data for event, data in events if event == "error"]
    assert error_events, f"expected an error event, got {[e for e, _ in events]}"
    assert "model" in error_events[0]["message"].lower()
    assert [event for event, _ in events if event == "agent_finished"]


@pytest.mark.asyncio
async def test_handle_run_accepts_project_with_configured_model(monkeypatch):
    """The guard must only trigger on the unconfigured case."""
    import gilzabots.agent_stdin as stdin_mod
    from types import SimpleNamespace

    events = []
    ran = []

    class FakeMemGPT:
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        model = "ollama/qwen3.5:cloud"

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            ran.append(agent_input.prompt)
            return SimpleNamespace(response="ok")

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(db_path=os.path.join(tmp, "sessions.db"))
        project = store.create(
            name="p1",
            mode="auto",
            model="ollama/qwen3.5:cloud",
            project_name="P1",
            project_path=os.path.join(tmp, "p1"),
        )
        store.create_chat("p1", "chat-1", "Chat")
        project = store.load("p1")

        monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
        monkeypatch.setattr(stdin_mod, "current_project", project)
        monkeypatch.setattr(stdin_mod, "current_store", store)
        monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())

        await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "hello"})

    assert ran, "the agent should run when the project has a configured model"
    assert not [data for event, data in events if event == "error"]


# ── model_params allow-list ──────────────────────────────────────────────────

def test_every_project_setting_the_ui_offers_survives_sanitizing():
    """A setting absent from the schema is dropped on save, silently.

    `empty_response_reasoning_fallback` is a checkbox in both project modals and
    was missing from the allow-list, so ticking it never persisted. This guards
    the whole set against the same drift.

    Inference parameters (temperature, top_k, reasoning_effort, ...) are NOT here
    on purpose: they moved to the model catalog and the project modals no longer
    offer them. `num_ctx` and `debug` stayed, so they must keep surviving a save.
    """
    from gilzabots.config import sanitize_model_params

    ui_settings = {
        "num_ctx": 8192, "stream": True,
        "force_vision": True, "max_heartbeats": 20, "max_context_tokens": 16384,
        "eviction_threshold": 0.85, "memory_pressure_threshold": 0.9,
        "heartbeat_pressure_threshold": 0.75,
        "max_iterations": 5, "max_tool_calls": 40, "loop_detection": True,
        "loop_detection_limit": 3, "max_idle_heartbeats": 2, "debug": False,
        "empty_response_reasoning_fallback": True,
    }

    survived = sanitize_model_params(ui_settings)
    dropped = set(ui_settings) - set(survived)
    assert not dropped, f"settings silently dropped on save: {sorted(dropped)}"


def test_unknown_keys_are_still_rejected():
    """The allow-list must keep doing its job; dead keys stay out."""
    from gilzabots.config import sanitize_model_params

    result = sanitize_model_params({"stream": True, "tool_role_workaround": "assistant"})

    assert result == {"stream": True}


def test_think_is_not_a_project_setting():
    """Thinking is resolved from the model catalog's `supports_thinking` alone.

    Keeping a project copy of it meant two switches for one behaviour: the wire
    flag came from the catalog while a stale project value still decided whether
    the reasoning was shown, so a project could look like thinking was off while
    the provider was reasoning on every turn.
    """
    from gilzabots.config import _MODEL_PARAMS_SCHEMA, sanitize_model_params

    assert "think" not in _MODEL_PARAMS_SCHEMA
    assert sanitize_model_params({"think": True, "stream": True}) == {"stream": True}


def test_ipc_project_update_sanitizes_stored_params(tmp_path, monkeypatch):
    """The IPC write path is the second door into the same stored dict.

    It used to store whatever it was handed, which is how dead keys accumulated
    in projects that the settings route would have filtered.
    """
    import asyncio

    import gilzabots.agent_stdin as stdin_mod
    from gilzabots.project import ProjectStore

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)

    db = tmp_path / "store.sqlite3"
    store = ProjectStore(db_path=str(db))
    asyncio.run(stdin_mod.handle_create_project({
        "db": str(db), "project_name": "p", "project_path": str(tmp_path / "p"),
    }))

    name = store.find_by_path(str(tmp_path / "p"))
    asyncio.run(stdin_mod.handle_update_project({
        "db": str(db),
        "project_name": name,
        "model_params": {
            "think": True,
            "empty_response_reasoning_fallback": True,
            "tool_role_workaround": "assistant",
        },
    }))

    saved = store.load(name).model_params
    assert saved.get("empty_response_reasoning_fallback") is True
    assert "tool_role_workaround" not in saved
    assert "think" not in saved, "thinking is a model capability, not a project setting"
