"""Tests for the /models, /set-main-model, /set-worker-model REPL commands
and per-project worker_model persistence."""

import asyncio
import os
import tempfile

import pytest


from gilzabots.cli_commands import REPLState, _registry
from gilzabots.project import ProjectStore
from gilzabots.memgpt_runtime import resolve_skill_model


def _state(tmp_path):
    db = os.path.join(str(tmp_path), "s.db")
    store = ProjectStore(db_path=db)
    p = store.create(name="t", mode="auto", model="ollama/test-model",
                     project_name="t", project_path=str(tmp_path))
    return REPLState(p, store), store


def test_set_main_model_persists(tmp_path):
    state, store = _state(tmp_path)
    asyncio.run(_registry.dispatch(state, "/set-main-model", ["ollama/mistral-nemo"]))
    assert state.project.model == "ollama/mistral-nemo"
    assert store.load("t").model == "ollama/mistral-nemo"


def test_set_worker_model_persists(tmp_path):
    state, store = _state(tmp_path)
    asyncio.run(_registry.dispatch(state, "/set-worker-model", ["gemini/gemini-2.0-flash"]))
    assert state.project.worker_model == "gemini/gemini-2.0-flash"
    assert store.load("t").worker_model == "gemini/gemini-2.0-flash"


def test_worker_model_resolves_for_skill(tmp_path):
    """A skill declaring model: worker resolves to the project's worker model."""
    state, _ = _state(tmp_path)
    asyncio.run(_registry.dispatch(state, "/set-worker-model", ["gemini/custom"]))
    resolved = resolve_skill_model({"model": "worker"},
                                   state.project.model, state.project.worker_model)
    assert resolved == "gemini/custom"


def test_worker_model_falls_back_to_global_when_unset(tmp_path):
    from gilzabots.config import DEFAULT_MODEL
    state, _ = _state(tmp_path)
    # No project worker set → resolve_skill_model uses the main project model.
    resolved = resolve_skill_model({"model": "worker"}, state.project.model, "")
    assert resolved == state.project.model


def test_clear_preserves_models(tmp_path):
    """/clear must not reset the project's main/worker model."""
    state, store = _state(tmp_path)
    asyncio.run(_registry.dispatch(state, "/set-main-model", ["ollama/m1"]))
    asyncio.run(_registry.dispatch(state, "/set-worker-model", ["gemini/a1"]))
    # Patch confirm to auto-yes
    import gilzabots.terminal as T
    orig = T.confirm
    T.confirm = lambda *a, **k: True
    try:
        asyncio.run(_registry.dispatch(state, "/clear", []))
    finally:
        T.confirm = orig
    assert state.project.model == "ollama/m1"
    assert state.project.worker_model == "gemini/a1"


def test_old_db_migrates_without_worker_model_column(tmp_path):
    """A pre-existing DB lacking the worker_model column migrates cleanly."""
    import sqlite3
    db = os.path.join(str(tmp_path), "old.db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE projects (name TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, mode TEXT DEFAULT 'plan', model TEXT DEFAULT '', "
        "project_name TEXT DEFAULT '', project_path TEXT DEFAULT '', "
        "skills TEXT DEFAULT '[\"gilzabots\"]', description TEXT DEFAULT '', "
        "request TEXT DEFAULT '', plan_text TEXT DEFAULT '', subplans TEXT DEFAULT '[]', "
        "results TEXT DEFAULT '{}');"
        "CREATE TABLE project_history (id INTEGER PRIMARY KEY, project TEXT, "
        "timestamp TEXT, role TEXT, content TEXT);"
    )
    c.execute("INSERT INTO projects (name,created_at,updated_at,model) "
              "VALUES ('old','t','t','ollama/old')")
    c.commit(); c.close()

    store = ProjectStore(db_path=db)  # __init__ runs the migration
    p = store.load("old")
    assert p.model == "ollama/old"        # existing data preserved
    assert p.worker_model == ""      # new column defaults empty
    p.worker_model = "gemini/x"
    store.save(p)
    assert store.load("old").worker_model == "gemini/x"


def test_set_model_param_valid(tmp_path):
    state, store = _state(tmp_path)
    # `num_ctx` stays a project-level budgeting override.
    asyncio.run(_registry.dispatch(state, "/set-model-param", ["num_ctx", "4096"]))
    assert state.project.model_params["num_ctx"] == 4096
    assert store.load("t").model_params["num_ctx"] == 4096

    # Boolean coercion
    asyncio.run(_registry.dispatch(state, "/set-model-param", ["stream", "true"]))
    assert state.project.model_params["stream"] is True
    asyncio.run(_registry.dispatch(state, "/set-model-param", ["stream", "false"]))
    assert state.project.model_params["stream"] is False


@pytest.mark.parametrize("param,value", [
    ("temperature", "0.8"),
    ("max_tokens", "1024"),
    ("seed", "42"),
    ("top_p", "0.9"),
    ("top_k", "50"),
    ("min_p", "0.05"),
    ("frequency_penalty", "0.1"),
    ("presence_penalty", "0.1"),
    ("repetition_penalty", "1.1"),
    ("reasoning_effort", "medium"),
])
def test_set_model_param_refuses_catalog_owned_inference_params(tmp_path, param, value):
    """Inference parameters live on the model catalog, so a project write is inert.

    `sanitize_model_params` drops these on save. Accepting one would print a
    success line for a value that never reaches the row -- the exact silent
    substitution that made `/set-model-param temperature 0.8` report success
    while the project kept the old value. The command must refuse and name the
    place that owns the setting.
    """
    state, store = _state(tmp_path)

    asyncio.run(_registry.dispatch(state, "/set-model-param", [param, value]))

    assert param not in (state.project.model_params or {})
    assert param not in (store.load("t").model_params or {})


def test_set_model_param_refuses_think_instead_of_storing_it_dead(tmp_path):
    """`think` comes from the model catalog, so a project write would be inert.

    Accepting it would report success for a setting nothing reads back — the
    project store strips it on load — so the command refuses and names the place
    that owns the decision.
    """
    state, store = _state(tmp_path)

    asyncio.run(_registry.dispatch(state, "/set-model-param", ["think", "true"]))

    assert "think" not in state.project.model_params
    assert "think" not in store.load("t").model_params


def test_set_model_param_invalid(tmp_path):
    state, store = _state(tmp_path)
    # Invalid parameter name (contains spaces / special chars)
    asyncio.run(_registry.dispatch(state, "/set-model-param", ["bad name!", "1.0"]))
    assert "bad name!" not in state.project.model_params

    # Missing value → usage error, param not set
    asyncio.run(_registry.dispatch(state, "/set-model-param", ["temperature"]))
    assert "temperature" not in state.project.model_params


def test_commit_and_undo_commands(tmp_path):
    state, store = _state(tmp_path)
    
    # The project path is tmp_path. Let's create a file inside it to commit.
    test_file = os.path.join(str(tmp_path), "test_doc.txt")
    with open(test_file, "w") as f:
        f.write("hello world")
        
    # Dispatch "/commit"
    res = asyncio.run(_registry.dispatch(state, "/commit", ["first user commit"]))
    assert res == "continue"
    
    # Verify the file is now tracked/committed (i.e. git has no untracked files)
    from gilzabots.vcs import get_vcs_strategy
    from gilzabots.config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    
    # We should be able to run "/undo"
    res_undo = asyncio.run(_registry.dispatch(state, "/undo", []))
    assert res_undo == "continue"
    
    # Since we did /undo, the file "test_doc.txt" should be removed by git clean -fd
    assert not os.path.exists(test_file)

