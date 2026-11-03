"""An "Auto" context window comes from the provider, never from a guess.

A catalog entry with an empty num_ctx used to be given 65,536 tokens whenever it
was not a local Ollama model. For a model that really holds a million tokens,
that guess became GilzaBots's own ceiling: read_file refused a file at 109K
tokens of a 1M window ("the context window is exhausted (109,239 of 65,536
tokens already used)") and MemGPT evicted the conversation at a fraction of
what the provider accepted. "Auto" now asks the provider; a self-hosted Ollama
model keeps a parsimonious fixed default, since there num_ctx sizes the memory
the server allocates; and a window nobody reports stays unknown, in which case
GilzaBots applies no limit of its own.
"""
import asyncio
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gilzabots import config, context_discovery, models_store, token_usage, tools


class _Transport:
    """Answers discovery requests from a url -> payload table and records them."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers=None, body=None, **_kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "body": body})
        if url not in self.routes:
            raise urllib.error.URLError(f"unexpected url {url}")
        answer = self.routes[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- discovery per protocol -------------------------------------------------


def test_openrouter_listing_reports_the_window_of_the_named_model():
    http = _Transport({"https://openrouter.ai/api/v1/models": {"data": [
        {"id": "openai/other", "context_length": 8000},
        {"id": "openai/gpt-5.6-luna-pro", "context_length": 1_000_000},
    ]}})
    entry = {"provider": "openai", "name": "openai/gpt-5.6-luna-pro",
             "api_base": "https://openrouter.ai/api/v1", "api_key": "sk-test"}

    assert context_discovery.discover_context_window(entry, http=http) == 1_000_000
    assert http.calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_openai_compatible_server_reports_max_model_len():
    http = _Transport({"http://gpu-box:8000/v1/models": {"data": [
        {"id": "qwen3-32b", "max_model_len": 131072},
    ]}})
    entry = {"provider": "openai", "name": "qwen3-32b", "api_base": "http://gpu-box:8000/v1"}

    assert context_discovery.discover_context_window(entry, http=http) == 131072


def test_listing_without_a_window_field_reports_unknown():
    # api.openai.com lists models without any context-size field.
    http = _Transport({"https://api.example.com/v1/models": {"data": [{"id": "gpt-x"}]}})
    entry = {"provider": "openai", "name": "gpt-x", "api_base": "https://api.example.com/v1"}

    assert context_discovery.discover_context_window(entry, http=http) is None


def test_gemini_reports_input_token_limit():
    http = _Transport({
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash":
            {"name": "models/gemini-3.8-flash", "inputTokenLimit": 1_048_576},
    })
    entry = {"provider": "gemini", "name": "gemini-3.8-flash", "api_key": "g-key"}

    assert context_discovery.discover_context_window(entry, http=http) == 1_048_576
    assert http.calls[0]["headers"]["x-goog-api-key"] == "g-key"


def test_gemini_without_url_or_stored_key_uses_the_provider_env_var(monkeypatch):
    # No api_base: LiteLLM routes "gemini/..." to Google by itself, and the key
    # may live only in the environment. Discovery follows the same route.
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    http = _Transport({
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash":
            {"inputTokenLimit": 1_048_576},
    })
    entry = {"provider": "gemini", "name": "gemini-3.8-flash", "api_base": "", "api_key": ""}

    assert context_discovery.discover_context_window(entry, http=http) == 1_048_576
    assert http.calls[0]["headers"]["x-goog-api-key"] == "env-key"


def test_ollama_cloud_reports_the_architecture_context_length():
    http = _Transport({"https://ollama.com/api/show": {"model_info": {
        "general.architecture": "glm", "glm.context_length": 202752,
    }}})
    entry = {"provider": "ollama", "name": "glm-5.3:cloud", "api_base": "https://ollama.com"}

    assert context_discovery.discover_context_window(entry, http=http) == 202752
    assert http.calls[0]["body"] == {"model": "glm-5.3:cloud"}


def test_self_hosted_ollama_is_never_probed():
    # There num_ctx sizes the KV cache the server allocates: the model's maximum
    # is not a safe default, so it is not even asked for.
    http = _Transport({})
    for api_base in ("http://localhost:11434/v1", "http://100.85.255.111:11434/v1"):
        entry = {"provider": "ollama", "name": "gpt-oss:20b", "api_base": api_base}
        assert context_discovery.discover_context_window(entry, http=http) is None
    assert http.calls == []


def test_unreachable_provider_reports_unknown():
    http = _Transport({"https://openrouter.ai/api/v1/models": TimeoutError("slow")})
    entry = {"provider": "openrouter", "name": "x/y"}

    assert context_discovery.discover_context_window(entry, http=http) is None


# --- recording the answer in the catalog -----------------------------------


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    models_store.add_or_update_connection({
        "id": "openrouter", "label": "OpenRouter", "provider": "openai",
        "api_key": "sk-test", "api_base": "https://openrouter.ai/api/v1",
    })
    models_store.save_models([{
        "id": "openai/openai/gpt-5.6-luna-pro", "connection_id": "openrouter",
        "name": "openai/gpt-5.6-luna-pro",
    }])
    return models_store


def _serve(monkeypatch, routes):
    transport = _Transport(routes)
    monkeypatch.setattr(context_discovery, "_http_json", transport)
    return transport


_LUNA_LISTING = {"https://openrouter.ai/api/v1/models": {"data": [
    {"id": "openai/gpt-5.6-luna-pro", "context_length": 1_000_000},
]}}


def test_auto_window_is_discovered_once_and_recorded_on_the_entry(catalog, monkeypatch):
    transport = _serve(monkeypatch, _LUNA_LISTING)

    assert config.auto_num_ctx("openai/openai/gpt-5.6-luna-pro") == (1_000_000, "provider")
    assert catalog.get_model("openai/openai/gpt-5.6-luna-pro")["discovered_num_ctx"] == 1_000_000
    # Later requests read the catalog, not the provider.
    assert config.auto_num_ctx("openai/openai/gpt-5.6-luna-pro") == (1_000_000, "provider")
    assert len(transport.calls) == 1


def test_a_failed_discovery_is_not_retried_on_every_request(catalog, monkeypatch):
    transport = _serve(monkeypatch, {})

    assert config.auto_num_ctx("openai/openai/gpt-5.6-luna-pro") == (None, "unknown")
    assert config.auto_num_ctx("openai/openai/gpt-5.6-luna-pro") == (None, "unknown")
    assert len(transport.calls) == 1


def test_listings_never_wait_on_the_provider(catalog, monkeypatch):
    transport = _serve(monkeypatch, _LUNA_LISTING)

    assert config.auto_num_ctx("openai/openai/gpt-5.6-luna-pro", allow_network=False) == (None, "unknown")
    assert config.resolve_display_num_ctx("openai/openai/gpt-5.6-luna-pro", {}) is None
    assert transport.calls == []


def test_editing_the_entry_keeps_the_window_only_for_the_same_model(catalog, monkeypatch):
    catalog.set_discovered_num_ctx("openai/openai/gpt-5.6-luna-pro", 1_000_000)

    # A form echoes back the listing, including a stale discovered value; it is
    # never taken from the caller, and an unrelated edit keeps the stored one.
    catalog.add_or_update_model({
        "previous_id": "openai/openai/gpt-5.6-luna-pro",
        "id": "openai/openai/gpt-5.6-luna-pro", "connection_id": "openrouter",
        "name": "openai/gpt-5.6-luna-pro", "temperature": 0.3, "discovered_num_ctx": 5,
    })
    assert catalog.get_model("openai/openai/gpt-5.6-luna-pro")["discovered_num_ctx"] == 1_000_000

    # Pointing the entry at another model drops the old model's window.
    catalog.add_or_update_model({
        "previous_id": "openai/openai/gpt-5.6-luna-pro",
        "id": "openai/openai/gpt-5.6-luna-pro", "connection_id": "openrouter",
        "name": "openai/gpt-5.6-luna-mini",
    })
    assert catalog.get_model("openai/openai/gpt-5.6-luna-pro")["discovered_num_ctx"] is None


def test_saving_an_auto_entry_discovers_its_window(catalog, monkeypatch):
    _serve(monkeypatch, _LUNA_LISTING)

    assert context_discovery.ensure_provider_context_window("openai/openai/gpt-5.6-luna-pro") == 1_000_000
    assert catalog.get_model("openai/openai/gpt-5.6-luna-pro")["discovered_num_ctx"] == 1_000_000


def test_discovered_window_is_not_sent_to_the_provider(catalog, monkeypatch):
    # Catalog metadata, not a request argument.
    catalog.set_discovered_num_ctx("openai/openai/gpt-5.6-luna-pro", 1_000_000)
    session = SimpleNamespace(
        model="openai/openai/gpt-5.6-luna-pro", worker_model="", model_params={},
        worker_model_params={}, api_base=None, worker_api_base=None,
        api_key=None, worker_api_key=None,
    )
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", session):
            kwargs = config.get_agent_llm_kwargs("memgpt")
            window = config.resolve_effective_num_ctx("memgpt")
    assert "discovered_num_ctx" not in kwargs
    assert window == 1_000_000


# --- resolution -------------------------------------------------------------


def _session(model, **overrides):
    values = dict(model=model, worker_model="", model_params={}, worker_model_params={},
                  api_base=None, worker_api_base=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_hosted_model_without_a_reported_window_is_unknown_not_65536():
    entry = {"id": "openai/some-model", "provider": "openai", "name": "some-model",
             "api_base": "https://api.example.com/v1", "num_ctx": None}
    with patch("gilzabots.tools._PROJECT_SESSION", _session("openai/some-model")):
        with patch("gilzabots.models_store.get_model", return_value=entry):
            assert config.resolve_effective_num_ctx("memgpt") is None


def test_unknown_window_sends_no_num_ctx():
    entry = {"id": "openai/some-model", "provider": "openai", "name": "some-model",
             "api_base": "https://api.example.com/v1", "num_ctx": None}
    session = _session("openai/some-model", model_params={"stream": True})
    with patch("gilzabots.ui_settings.load_ui_settings", return_value={}):
        with patch("gilzabots.tools._PROJECT_SESSION", session):
            with patch("gilzabots.models_store.get_model", return_value=entry):
                kwargs = config.get_agent_llm_kwargs("memgpt")
    assert "num_ctx" not in kwargs


def test_self_hosted_ollama_on_auto_keeps_the_parsimonious_defaults():
    local = {"id": "ollama/m", "provider": "ollama", "name": "m",
             "api_base": "http://localhost:11434/v1", "num_ctx": None}
    remote = dict(local, api_base="http://100.85.255.111:11434/v1")
    with patch("gilzabots.models_store.get_model", return_value=local):
        assert config.auto_num_ctx("ollama/m") == (8192, "local_default")
    with patch("gilzabots.models_store.get_model", return_value=remote):
        assert config.auto_num_ctx("ollama/m") == (65536, "local_default")


def test_a_catalog_read_failure_is_not_turned_into_a_guess():
    with patch("gilzabots.models_store.get_model", side_effect=RuntimeError("catalog unreadable")):
        with pytest.raises(RuntimeError):
            config.model_num_ctx("openai/some-model")


# --- consumers impose no limit on an unknown window -------------------------


@pytest.fixture
def unknown_window(monkeypatch):
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    token_usage.set_context_scope("unknown-window-tests")
    monkeypatch.setattr(tools, "context_window_tokens", lambda: None)
    token_usage.record_context_tokens(109_239)
    yield
    token_usage.set_context_scope("reset")


def test_read_file_is_not_refused_when_the_window_is_unknown(tmp_path, monkeypatch, unknown_window):
    # The reported case: 109,239 tokens in use on a model whose window was never
    # 65,536. With no known window there is no GilzaBots-side refusal.
    target = tmp_path / "gridworld_online.py"
    target.write_text("x = 1\n" * 100_000, encoding="utf-8")
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    content = asyncio.run(tools.read_file._func(str(target)))

    assert len(content) == len("x = 1\n" * 100_000)
    assert tools.free_context_chars() is None


def test_read_content_pos_returns_the_whole_range_when_the_window_is_unknown(tmp_path, monkeypatch, unknown_window):
    target = tmp_path / "notes.tex"
    target.write_text("".join(f"line {i}\n" for i in range(1, 50_001)), encoding="utf-8")
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    content = asyncio.run(tools.read_content_pos._func(str(target), 1, 50_000))

    assert content.endswith("line 50000\n")
    assert "capped" not in content


def test_tool_results_are_not_truncated_when_the_window_is_unknown(unknown_window):
    text = "y" * 1_000_000
    assert tools._truncate_to_context_budget(text) == text
