"""Manual integration test: verify that saving project settings via
/api/gilzabots/update-project correctly persists and applies all model_params.

This test:
1. Creates a temporary project in an isolated DB.
2. POSTs to /api/gilzabots/update-project with every known parameter.
3. Reloads the project from the DB and asserts each value was persisted.
4. Asserts that get_agent_llm_kwargs() and get_project_agent_params() reflect
   the saved values (i.e., they are picked up by the runtime).

Run with:
    pytest tests/test_update_project_params.py -v
"""

import json
import os
import tempfile
import asyncio
import pytest

from gilzabots.project import ProjectStore, ProjectData
from gilzabots.config import get_agent_llm_kwargs, get_project_agent_params
from gilzabots import tools as _tools


# ---------------------------------------------------------------------------
# All parameters exposed in the EditProjectModal, with test values.
# Model inference parameters (temperature, top_p, etc.) have moved to the
# model catalog; project settings retain num_ctx, stream, and agent params.
# ---------------------------------------------------------------------------

PROJECT_MODEL_PARAMS = {
    "stream":              False,
}

RUNTIME_LITELLM_PARAMS = dict(PROJECT_MODEL_PARAMS)

AGENT_PARAMS = {
    "max_heartbeats":           15,
    "max_context_tokens":       8000,
    "eviction_threshold":       0.9,
    "memory_pressure_threshold": 0.6,
    "max_iterations":           5,
    "max_tool_calls":           20,
    "max_idle_heartbeats":      2,
    "debug":                    False,
}

ALL_PARAMS = {**PROJECT_MODEL_PARAMS, **AGENT_PARAMS}


@pytest.fixture(autouse=True)
def _reset_project_session(monkeypatch):
    """Restore shared runtime state and avoid leaking real UI settings into tests."""
    monkeypatch.setattr(
        "gilzabots.ui_settings.load_ui_settings",
        lambda: {},
    )
    yield
    import gilzabots.tools as _t
    _t._PROJECT_SESSION = None


@pytest.fixture()
def tmp_store(tmp_path):
    """Isolated ProjectStore backed by a temp DB."""
    db = str(tmp_path / "test.db")
    store = ProjectStore(db_path=db)
    project_path = str(tmp_path / "proj")
    os.makedirs(project_path)
    project = store.create(
        name="test_proj",
        mode="auto",
        model="ollama/test-model:latest",
        project_name="Test Project",
        project_path=project_path,
    )
    return store, project


def _apply_update(store: ProjectStore, project: ProjectData, params: dict) -> ProjectData:
    """Simulate what /api/gilzabots/update-project does."""
    import re
    validated = {}
    for k, v in params.items():
        if not k or not re.fullmatch(r'[A-Za-z0-9_-]+', k):
            raise ValueError(f"invalid parameter name: {k}")
        if v is None or v == "":
            continue
        validated[k] = v
    project.model_params = validated
    store.save(project)
    return project


def _reload(store: ProjectStore, name: str) -> ProjectData:
    return store.load(name)


def _inject_session(project: ProjectData, store: ProjectStore):
    """Point config._PROJECT_SESSION at our project so get_agent_llm_kwargs reads it."""
    _tools.set_project_context(project, store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUpdateProjectPersistence:

    def test_all_params_persisted_in_db(self, tmp_store):
        """Every parameter sent to update-project must survive a DB round-trip."""
        store, project = tmp_store
        _apply_update(store, project, ALL_PARAMS)
        reloaded = _reload(store, project.name)

        for key, expected in ALL_PARAMS.items():
            assert key in reloaded.model_params, f"param '{key}' missing after reload"
            assert reloaded.model_params[key] == expected, (
                f"param '{key}': expected {expected!r}, got {reloaded.model_params[key]!r}"
            )

    def test_load_with_custom_chat_id(self, tmp_store):
        """Verify that ProjectStore.load loads project with custom chat_id correctly."""
        store, project = tmp_store
        chat_id = "test-chat-123"
        store.create_chat(project.name, chat_id, "Test Chat")
        
        reloaded = store.load(project.name, chat_id=chat_id)
        assert reloaded is not None
        assert reloaded.current_chat_id == chat_id

    def test_litellm_params_applied_to_agent_kwargs(self, tmp_store):
        """LiteLLM params must appear in get_agent_llm_kwargs() after save."""
        store, project = tmp_store
        _apply_update(store, project, ALL_PARAMS)
        reloaded = _reload(store, project.name)
        _inject_session(reloaded, store)

        kwargs = get_agent_llm_kwargs("worker")

        for key, expected in RUNTIME_LITELLM_PARAMS.items():
            assert key in kwargs, f"LiteLLM param '{key}' missing from get_agent_llm_kwargs()"
            assert kwargs[key] == expected, (
                f"LiteLLM param '{key}': expected {expected!r}, got {kwargs[key]!r}"
            )
        assert "think" not in kwargs

    def test_agent_constructor_params_applied(self, tmp_store):
        """Agent constructor params must appear in get_project_agent_params() after save."""
        store, project = tmp_store
        _apply_update(store, project, ALL_PARAMS)
        reloaded = _reload(store, project.name)
        _inject_session(reloaded, store)

        agent_params = get_project_agent_params()

        for key, expected in AGENT_PARAMS.items():
            assert key in agent_params, f"agent param '{key}' missing from get_project_agent_params()"
            assert agent_params[key] == expected, (
                f"agent param '{key}': expected {expected!r}, got {agent_params[key]!r}"
            )

    def test_empty_value_not_persisted(self, tmp_store):
        """Empty string values must be dropped (not stored as empty strings)."""
        store, project = tmp_store
        params_with_empty = {**ALL_PARAMS, "max_heartbeats": ""}
        _apply_update(store, project, params_with_empty)
        reloaded = _reload(store, project.name)

        assert reloaded.model_params.get("max_heartbeats") != "", (
            "empty string for max_heartbeats must not be persisted"
        )
        assert "max_heartbeats" not in reloaded.model_params or reloaded.model_params["max_heartbeats"] != "", (
            "empty value leaked into model_params"
        )

    def test_max_tokens_absent_means_unlimited(self, tmp_store):
        """When max_tokens is not in catalog/params, get_agent_llm_kwargs must not
        include it — letting the model generate without a token cap."""
        store, project = tmp_store
        _apply_update(store, project, ALL_PARAMS)
        reloaded = _reload(store, project.name)
        _inject_session(reloaded, store)

        kwargs = get_agent_llm_kwargs("worker")
        assert "max_tokens" not in kwargs, (
            "max_tokens must be absent from kwargs when not set — model should be unlimited"
        )

    def test_partial_update_preserves_other_fields(self, tmp_store):
        """Saving only some params must not wipe out unrelated project fields."""
        store, project = tmp_store
        original_model = project.model
        original_path = project.project_path

        _apply_update(store, project, {"stream": False, "max_heartbeats": 25})
        reloaded = _reload(store, project.name)

        assert reloaded.model == original_model, "model changed unexpectedly after param update"
        assert reloaded.project_path == original_path, "project_path changed unexpectedly"
        assert reloaded.model_params.get("stream") is False
        assert reloaded.model_params.get("max_heartbeats") == 25

    def test_retired_inference_params_stripped_from_project_model_params(self, tmp_store):
        """Inference params sent to update-project must be stripped from project.model_params.

        `num_ctx` is not one of them: it is a prompt-budgeting override that is
        never sent to an `openai/` provider, and a project pointed at a host with
        a smaller runtime budget than its model's catalog entry advertises must be
        able to lower it for that project alone.
        """
        store, project = tmp_store
        _apply_update(store, project, {
            "temperature": 0.5,
            "max_tokens": 2048,
            "top_p": 0.8,
            "num_ctx": 4096,
            "stream": False,
        })
        reloaded = _reload(store, project.name)

        assert "temperature" not in reloaded.model_params
        assert "max_tokens" not in reloaded.model_params
        assert "top_p" not in reloaded.model_params
        assert reloaded.model_params["num_ctx"] == 4096
        assert reloaded.model_params["stream"] is False

    def test_update_rebuilds_memgpt_with_new_params(self, tmp_store, monkeypatch):
        """After save, the rebuilt MemGPT must carry the updated model_kargs."""
        store, project = tmp_store
        _apply_update(store, project, ALL_PARAMS)
        reloaded = _reload(store, project.name)
        _inject_session(reloaded, store)

        from gilzabots.memgpt_runtime import build_chat_orchestrator
        memgpt = build_chat_orchestrator(reloaded, store)

        for key, expected in RUNTIME_LITELLM_PARAMS.items():
            assert memgpt.model_kargs.get(key) == expected, (
                f"MemGPT model_kargs['{key}']: expected {expected!r}, "
                f"got {memgpt.model_kargs.get(key)!r}"
            )
        assert "think" not in memgpt.model_kargs

    def test_sanitize_and_clamp_model_params(self):
        """Verify that sanitize_model_params correctly handles string numbers with commas, and clamps out of bounds values."""
        from gilzabots.config import sanitize_model_params
        
        raw_params = {
            "max_heartbeats": "25",  # string int
            "eviction_threshold": "0,8",  # string comma float
            "stream": "true",  # string bool
            "temperature": "0.7",  # retired inference param, not in project schema
            "num_ctx": "4096",  # budgeting override: still a project setting
            "think": "true",  # a model capability, never a project param
            "invalid_param": "some_value"  # not in schema
        }
        
        sanitized = sanitize_model_params(raw_params)
        
        assert sanitized["max_heartbeats"] == 25
        assert sanitized["eviction_threshold"] == 0.8
        assert sanitized["stream"] is True
        assert "temperature" not in sanitized
        assert sanitized["num_ctx"] == 4096
        assert "think" not in sanitized
        assert "invalid_param" not in sanitized
