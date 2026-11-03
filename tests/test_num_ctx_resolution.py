"""Tests for num_ctx resolution moving from project config to the model catalog.

Covers gilzabots.config.resolve_effective_num_ctx and its integration into
get_agent_llm_kwargs. Precedence (highest first):
1. Explicit project model_params/worker_model_params.num_ctx.
2. The model's global catalog entry (gilzabots/models_store.py).
3. The per-agent num_ctx floor in agents.yaml/_CORE_AGENT_DEFAULTS, but only
   when there is no live project session at all (bare CLI/no-project case) --
   otherwise it would permanently shadow every model's own catalog entry.
4. The local/cloud heuristic default (config.default_num_ctx_for_model).
"""

from types import SimpleNamespace
from unittest.mock import patch

from gilzabots.config import get_agent_llm_kwargs, resolve_effective_num_ctx


def _session(**overrides):
    defaults = dict(
        model="ollama/local-model",
        worker_model="",
        model_params={},
        worker_model_params={},
        api_base=None,
        worker_api_base=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_project_override_wins_over_catalog_and_heuristic():
    session = _session(model_params={"num_ctx": 4096})
    with patch("gilzabots.tools._PROJECT_SESSION", session):
        with patch("gilzabots.models_store.get_model", return_value={"num_ctx": 32768}):
            assert resolve_effective_num_ctx("memgpt") == 4096


def test_catalog_value_used_when_project_has_no_override():
    session = _session()
    with patch("gilzabots.tools._PROJECT_SESSION", session):
        with patch("gilzabots.models_store.get_model", return_value={"num_ctx": 32768}):
            assert resolve_effective_num_ctx("memgpt") == 32768


def test_catalog_ranks_above_per_agent_floor_for_a_real_project():
    """_CORE_AGENT_DEFAULTS sets num_ctx=16384 for memgpt, but a real project
    session with an unset catalog entry must fall to the local/cloud heuristic,
    not the static per-agent floor -- otherwise the catalog could never win."""
    session = _session(model="ollama/local-model")
    with patch("gilzabots.tools._PROJECT_SESSION", session):
        with patch("gilzabots.models_store.get_model", return_value=None):
            assert resolve_effective_num_ctx("memgpt") == 8192  # local heuristic, not 16384


def test_per_agent_floor_applies_only_without_a_project_session():
    with patch("gilzabots.tools._PROJECT_SESSION", None):
        with patch("gilzabots.models_store.get_model", return_value=None):
            assert resolve_effective_num_ctx("memgpt") == 16384


def test_worker_role_reads_worker_model_params_and_worker_catalog_model():
    # An empty model_params/worker_model_params (no explicit num_ctx anywhere)
    # is the common case for a new project (see ProjectStore.create, which no
    # longer bakes num_ctx into either dict). The worker role must still
    # resolve its OWN model's catalog entry, not the orchestrator's.
    session = _session(
        model="ollama/orchestrator-model",
        worker_model="gemini/worker-model",
        model_params={},
        worker_model_params={},
    )
    with patch("gilzabots.tools._PROJECT_SESSION", session):
        def fake_get_model(model_id):
            if model_id == "gemini/worker-model":
                return {"num_ctx": 65536}
            return None

        with patch("gilzabots.models_store.get_model", side_effect=fake_get_model):
            # Must not pick up the orchestrator's explicit override.
            assert resolve_effective_num_ctx("worker") == 65536


def test_get_agent_llm_kwargs_uses_catalog_num_ctx_for_new_project():
    """A project created without an explicit num_ctx (the new default, see
    ProjectStore.create) must resolve its window from the model catalog."""
    session = _session(model="ollama/catalog-model", model_params={"stream": True})
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", session):
            with patch("gilzabots.models_store.get_model", return_value={"num_ctx": 20000}):
                kwargs = get_agent_llm_kwargs("memgpt")
    assert kwargs["num_ctx"] == 20000


def test_get_agent_llm_kwargs_falls_back_to_heuristic_without_catalog_entry():
    session = _session(model="ollama/unregistered-model", model_params={})
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", session):
            with patch("gilzabots.models_store.get_model", return_value=None):
                kwargs = get_agent_llm_kwargs("memgpt")
    assert kwargs["num_ctx"] == 8192
