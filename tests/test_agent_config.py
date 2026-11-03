"""Tests for agents.yaml configuration correctness (skills-oriented architecture).

Covers:
1. reasoning_effort disables gemma4 thinking for agents that depend on tool_calls
   (ollama issue #15288) — the worker and the enricher/memgpt path.
2. think comes from the selected model's catalog capability alone — no role
   default and no project setting can override it.
3. The orchestrator and memgpt roles get a large num_ctx (long histories).
"""

import pytest
from gilzabots.config import get_agent_llm_kwargs


# Agents whose tool_calls field must be populated.
TOOL_CALL_AGENTS = [
    "worker",
]

# Planning sub-agents.
SUBAGENT_AGENTS = [
    "landscape_planner",
    "refinement_agent",
]

PLANNING_AGENTS = [
    "orchestrator",
]


from unittest.mock import patch


@pytest.mark.parametrize("agent", PLANNING_AGENTS + ["memgpt"])
def test_orchestrator_agents_enable_thinking_when_model_supports_it(agent):
    """Orchestrator and memgpt may think, but only for models that declare support."""
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": True}):
                kwargs = get_agent_llm_kwargs(agent)
            assert kwargs.get("think") is True


@pytest.mark.parametrize("agent", PLANNING_AGENTS + ["memgpt"])
def test_orchestrator_agents_do_not_send_think_without_model_support(agent):
    """Thinking defaults off at the model capability layer unless explicitly enabled."""
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
                kwargs = get_agent_llm_kwargs(agent)
            assert "think" not in kwargs


@pytest.mark.parametrize("agent", TOOL_CALL_AGENTS + SUBAGENT_AGENTS)
def test_subagents_follow_the_model_capability_like_every_other_role(agent):
    """`think` is decided by the selected model's catalog entry, never by the role.

    The role used to carry its own default, but `resolve_think_request` reads only
    `supports_thinking`, so a role-level value was configuration that no call site
    could act on. One capability, one answer, for workers and orchestrators alike.
    """
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
                assert "think" not in get_agent_llm_kwargs(agent)
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": True}):
                assert get_agent_llm_kwargs(agent).get("think") is True


@pytest.mark.parametrize("agent", PLANNING_AGENTS + ["memgpt", "worker"])
def test_a_role_default_cannot_override_the_model_capability(agent):
    """An agents.yaml `think` for a role is ignored, not merged.

    Two switches for one behaviour is what the consolidation removed: a role
    default that survived here would silently send `think` to a model whose
    catalog entry says it cannot parse it.
    """
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            with patch(
                "gilzabots.config._get_agent_overrides",
                return_value={agent: {"think": True}},
            ):
                with patch(
                    "gilzabots.models_store.get_model",
                    return_value={"supports_thinking": False},
                ):
                    assert "think" not in get_agent_llm_kwargs(agent)


def test_orchestrator_has_large_num_ctx():
    """Orchestrator accumulates long tool-call histories — needs at least 16k context."""
    from unittest.mock import patch
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            kwargs = get_agent_llm_kwargs("orchestrator")
    assert kwargs.get("num_ctx", 0) >= 16384


def test_memgpt_has_large_num_ctx():
    """The MemGPT chat-orchestrator runs multi-turn sessions with skill calls —
    it needs a generous context window so turns are not cut off."""
    from unittest.mock import patch
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            kwargs = get_agent_llm_kwargs("memgpt")
    assert kwargs.get("num_ctx", 0) >= 16384


def test_orchestrator_has_no_restrictive_max_tokens():
    """Orchestrator must not be limited to a small max_tokens — it produces
    long reasoning chains and final reports."""
    from unittest.mock import patch
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", None):
            kwargs = get_agent_llm_kwargs("orchestrator")
    max_tok = kwargs.get("max_tokens", None)
    assert max_tok is None or max_tok >= 1024


def test_catalog_extra_model_params_reach_model_kwargs_and_override_role_defaults():
    catalog_model = {
        "supports_thinking": False,
        "temperature": 0.25,
        "extra_model_params": {
            "provider_extension": {"mode": "strict"},
        },
    }
    with patch("gilzabots.tools._PROJECT_SESSION", None):
        with patch("gilzabots.models_store.get_model", return_value=catalog_model):
            with patch("litellm.get_supported_openai_params", return_value=["temperature"]):
                kwargs = get_agent_llm_kwargs("memgpt", "openai/example-model")

    assert kwargs["temperature"] == 0.25
    assert kwargs["provider_extension"] == {"mode": "strict"}


def test_catalog_model_inference_params_take_precedence_over_legacy_project_params():
    class FakeSession:
        model = "openai/example-model"
        model_params = {"temperature": 0.6}
        api_base = ""
        api_key = ""

    catalog_model = {
        "supports_thinking": False,
        "temperature": 0.25,
    }
    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.models_store.get_model", return_value=catalog_model):
            with patch("litellm.get_supported_openai_params", return_value=["temperature"]):
                kwargs = get_agent_llm_kwargs("memgpt")

    # Catalog inference params are the single source of truth; legacy project params are ignored
    assert kwargs["temperature"] == 0.25


def test_worker_role_resolves_inference_params_from_worker_catalog_model():
    class FakeSession:
        model = "ollama/main-model"
        worker_model = "ollama/worker-model"
        model_params = {}
        worker_model_params = {}
        project_path = "/fake/path"

    def fake_get_model(model_id):
        if model_id == "ollama/main-model":
            return {"temperature": 0.7, "max_tokens": 4096}
        if model_id == "ollama/worker-model":
            return {"temperature": 0.2, "max_tokens": 8192}
        return None

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", side_effect=fake_get_model):
                orchestrator_kwargs = get_agent_llm_kwargs("chat_orchestrator")
                worker_kwargs = get_agent_llm_kwargs("worker")

    assert orchestrator_kwargs["temperature"] == 0.7
    assert orchestrator_kwargs["max_tokens"] == 4096
    assert worker_kwargs["temperature"] == 0.2
    assert worker_kwargs["max_tokens"] == 8192


def test_repeated_provider_sanitization_preserves_catalog_extra_param():
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    catalog_model = {"extra_model_params": {"provider_extension": True}}
    with patch("gilzabots.models_store.get_model_by_runtime_id", return_value=catalog_model):
        with patch("litellm.get_supported_openai_params", return_value=["temperature"]):
            cleaned = sanitize_litellm_kwargs_for_model(
                "openai/example-model",
                {"temperature": 0.2, "provider_extension": True},
            )

    assert cleaned["provider_extension"] is True


def test_ollama_cloud_model_uses_remote_api_base_by_default(monkeypatch):
    """Ollama cloud-tagged models must not silently fall back to localhost."""
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "ollama/qwen3.5:cloud"
        model_params = {}
        project_path = "/fake/path"
        api_base = ""
        api_key = ""

    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value=None):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert kwargs["api_base"] == "https://ollama.com"
    assert kwargs["api_key"] == "ollama-test-key"
    assert kwargs["timeout"] == 600.0
    assert kwargs["request_timeout"] == 600.0


def test_normalize_ollama_api_base_for_litellm_strips_v1_suffix():
    from gilzabots.config import normalize_ollama_api_base_for_litellm

    assert normalize_ollama_api_base_for_litellm(
        "ollama/gpt-oss:20b",
        "http://100.85.255.111:11434/v1",
    ) == "http://100.85.255.111:11434"
    assert normalize_ollama_api_base_for_litellm(
        "ollama_chat/gpt-oss:20b",
        "http://100.85.255.111:11434/v1/",
    ) == "http://100.85.255.111:11434"
    assert normalize_ollama_api_base_for_litellm(
        "openai/gpt-5.5",
        "http://example.test/v1",
    ) == "http://example.test/v1"


def test_resolve_model_route_requires_model_capability():
    """Thinking kwargs are dropped for a model that does not declare support."""
    from gilzabots.config import resolve_model_route
    from unittest.mock import patch

    kwargs = {"think": True}
    with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
        model = resolve_model_route("ollama/mistral-large-3:675b-cloud", kwargs)

    assert "think" not in kwargs

    supported_kwargs = {"think": True}
    with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": True}):
        supported_model = resolve_model_route("ollama/gemma4:12b", supported_kwargs)

    assert supported_model == "ollama_chat/gemma4:12b"
    assert supported_kwargs["think"] is True


def test_the_repetition_penalty_owns_ollamas_repeat_penalty():
    """It used to be forwarded verbatim, as a key Ollama does not define.

    So the setting did nothing, while the value that actually took effect came
    from LiteLLM mapping `frequency_penalty` onto the same wire field.
    """
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    cleaned = sanitize_litellm_kwargs_for_model(
        "ollama_chat/glm-5.3:cloud",
        {"repetition_penalty": 1.2, "temperature": 0.2},
    )

    assert cleaned["repeat_penalty"] == 1.2
    assert "repetition_penalty" not in cleaned


def test_a_frequency_penalty_on_its_own_is_left_alone():
    """Nothing claims the key, so LiteLLM's own mapping keeps working."""
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    cleaned = sanitize_litellm_kwargs_for_model(
        "ollama_chat/glm-5.3:cloud", {"frequency_penalty": 0.5}
    )

    assert cleaned["frequency_penalty"] == 0.5
    assert "repeat_penalty" not in cleaned


def test_the_losing_penalty_is_named_rather_than_dropped_in_silence(capsys):
    """Both map to one wire field; a setting that quietly does nothing is the bug."""
    import gilzabots.config as config_mod
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    config_mod._reported_penalty_conflicts.clear()
    cleaned = sanitize_litellm_kwargs_for_model(
        "ollama_chat/glm-5.3:cloud",
        {"repetition_penalty": 1.2, "frequency_penalty": 0.5},
    )

    assert cleaned["repeat_penalty"] == 1.2
    assert "frequency_penalty" not in cleaned
    warning = capsys.readouterr().err
    assert "repetition_penalty" in warning and "frequency_penalty" in warning

    sanitize_litellm_kwargs_for_model(
        "ollama_chat/glm-5.3:cloud",
        {"repetition_penalty": 1.2, "frequency_penalty": 0.5},
    )
    assert capsys.readouterr().err == "", "reported once per model, not per request"


def test_ollamas_own_penalty_name_never_reaches_an_openai_endpoint():
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    cleaned = sanitize_litellm_kwargs_for_model(
        "openai/openai/gpt-5.6-luna", {"repeat_penalty": 1.2, "temperature": 0.2}
    )

    assert "repeat_penalty" not in cleaned


def test_ollama_reaches_the_native_route_even_with_thinking_off():
    """Turning reasoning off must not cost the agent its tool calling.

    Routing used to be conditional on thinking being requested, which coupled a
    reasoning preference to a transport choice: unchecking thinking dropped the
    orchestrator onto Ollama's OpenAI-compatible endpoint, where a capable model
    (observed with `ollama/glm-5.2:cloud`) printed `{"name": ..., "arguments": ...}`
    as text instead of issuing a tool call, so nothing ran.
    """
    from gilzabots.config import resolve_model_route
    from unittest.mock import patch

    for kwargs in ({}, {"think": False}, {"think": False, "reasoning_effort": None}):
        with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": True}):
            assert resolve_model_route("ollama/glm-5.2:cloud", dict(kwargs)) == (
                "ollama_chat/glm-5.2:cloud"
            ), f"thinking-off kwargs {kwargs} must still use the native route"


def test_a_model_without_thinking_support_still_uses_the_native_route():
    """The transport is not a reward for declaring thinking support."""
    from gilzabots.config import resolve_model_route
    from unittest.mock import patch

    kwargs = {"think": True}
    with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
        model = resolve_model_route("ollama/plain:latest", kwargs)

    assert model == "ollama_chat/plain:latest"
    assert "think" not in kwargs, "the unsupported parameter is still dropped"


def test_non_ollama_providers_are_never_rewritten():
    from gilzabots.config import resolve_model_route
    from unittest.mock import patch

    with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": True}):
        assert resolve_model_route("gemini/gemini-3.7-flash", {}) == "gemini/gemini-3.7-flash"
        assert resolve_model_route("ollama_chat/gemma4:26b", {}) == "ollama_chat/gemma4:26b"


def test_internal_attachment_flags_are_not_sent_to_litellm():
    """Internal attachment flags must not leak into provider request kwargs."""
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "openai/gpt-5.5"
        model_params = {
            "force_vision": True,
            "pdf_truncate": True,
            "pdf_truncate_pct": 50,
            "temperature": 0.2,
        }
        project_path = "/fake/path"

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert "temperature" not in kwargs
    assert kwargs["drop_params"] is True
    assert "force_vision" not in kwargs
    assert "pdf_truncate" not in kwargs
    assert "pdf_truncate_pct" not in kwargs


def test_openai_models_do_not_receive_local_only_litellm_kwargs():
    """OpenAI endpoints must only receive provider-supported request kwargs."""
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "openai/gpt-5.5"
        model_params = {
            "num_ctx": 8192,
            "top_k": 40,
            "min_p": 0.1,
            "repetition_penalty": 1.1,
            "think": False,
            "reasoning_effort": "none",
            "temperature": 0.2,
            "max_idle_heartbeats": 2,
            "max_heartbeats": 15,
            "unknown_param": "boom",
        }
        project_path = "/fake/path"

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value={"supports_thinking": False}):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert "temperature" not in kwargs
    assert kwargs["drop_params"] is True
    assert "num_ctx" not in kwargs
    assert "top_k" not in kwargs
    assert "min_p" not in kwargs
    assert "repetition_penalty" not in kwargs
    assert "think" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "max_idle_heartbeats" not in kwargs
    assert "max_heartbeats" not in kwargs
    assert "unknown_param" not in kwargs


def test_openai_gpt55_chat_model_strips_reasoning_effort_to_avoid_responses_bridge():
    """GPT-5.4+ with tools + reasoning_effort makes LiteLLM use Responses API.

    AgenticBlocks uses Chat Completions-style tool loops, so the chat model id
    must not carry reasoning_effort. The explicit openai/responses/* model id is
    still allowed to opt into Responses behavior.
    """
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    chat_kwargs = sanitize_litellm_kwargs_for_model(
        "openai/gpt-5.5",
        {"reasoning_effort": "medium", "temperature": 0.2},
    )
    responses_kwargs = sanitize_litellm_kwargs_for_model(
        "openai/responses/gpt-5.5",
        {"reasoning_effort": "medium", "temperature": 0.2},
    )

    assert "reasoning_effort" not in chat_kwargs
    assert responses_kwargs["reasoning_effort"] == "medium"


def test_openai_gpt55_chat_model_strips_default_only_sampling_params():
    """GPT-5 chat models reject non-default sampling values such as temperature=0.7."""
    from gilzabots.config import sanitize_litellm_kwargs_for_model

    kwargs = sanitize_litellm_kwargs_for_model(
        "openai/gpt-5.5",
        {
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
            "max_tokens": 128,
        },
    )

    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "frequency_penalty" not in kwargs
    assert "presence_penalty" not in kwargs
    assert kwargs["max_tokens"] == 128
    assert kwargs["drop_params"] is True


def test_gemini_models_drop_unknown_params_and_deprecated_sampling():
    """Gemini 3 direct calls drop deprecated sampling params and unknown fields."""
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "gemini/gemini-3.1-flash-lite"
        model_params = {
            "num_ctx": 8192,
            "unknown_param": "boom",
        }
        project_path = "/fake/path"

    catalog_model = {
        "supports_thinking": False,
        "temperature": 0.2,
        "top_p": 0.9,
        "frequency_penalty": 0.3,
        "presence_penalty": 0.2,
        "top_k": 40,
    }

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value=catalog_model):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert kwargs["frequency_penalty"] == 0.3
    assert kwargs["presence_penalty"] == 0.2
    assert kwargs["drop_params"] is True
    assert "num_ctx" not in kwargs
    assert "top_k" not in kwargs
    assert "think" not in kwargs
    assert "unknown_param" not in kwargs


def test_ollama_models_keep_local_only_litellm_kwargs_except_unsupported_think():
    """Ollama keeps local tuning params, but think is capability-gated."""
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "ollama/gemma4:12b"
        model_params = {
            "num_ctx": 8192,
            "unknown_param": "boom",
        }
        project_path = "/fake/path"

    catalog_model = {
        "supports_thinking": False,
        "temperature": 0.2,
        "top_k": 40,
        "min_p": 0.1,
        "repetition_penalty": 1.1,
    }

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value=catalog_model):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert kwargs["temperature"] == 0.2
    assert kwargs["num_ctx"] == 8192
    assert kwargs["top_k"] == 40
    assert kwargs["min_p"] == 0.1
    # Still carried, under the name Ollama actually defines for it: the catalog
    # field used to travel verbatim as a key the server has no meaning for.
    assert kwargs["repeat_penalty"] == 1.1
    assert "repetition_penalty" not in kwargs
    assert "think" not in kwargs
    assert kwargs.get("drop_params") is True
    assert "unknown_param" not in kwargs

def test_project_ollama_api_base_is_used_when_model_store_has_no_entry():
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "ollama/gpt-oss:20b"
        model_params = {}
        project_path = "/fake/path"
        api_base = "http://100.85.255.111:11434/v1"
        api_key = "remote-key"

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value=None):
                kwargs = get_agent_llm_kwargs("custom_agent")

    assert kwargs["api_base"] == "http://100.85.255.111:11434"
    assert kwargs["api_key"] == "remote-key"


def test_worker_ollama_api_base_uses_worker_setting_first():
    from gilzabots.config import get_agent_llm_kwargs
    from unittest.mock import patch

    class FakeSession:
        model = "ollama/main:latest"
        worker_model = "ollama/gpt-oss:20b"
        model_params = {}
        worker_model_params = {}
        project_path = "/fake/path"
        api_base = "http://main-host:11434/v1"
        api_key = "main-key"
        worker_api_base = "http://worker-host:11434/v1"
        worker_api_key = "worker-key"

    with patch("gilzabots.tools._PROJECT_SESSION", FakeSession()):
        with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
            with patch("gilzabots.models_store.get_model", return_value=None):
                kwargs = get_agent_llm_kwargs("worker")

    assert kwargs["api_base"] == "http://worker-host:11434"
    assert kwargs["api_key"] == "worker-key"
