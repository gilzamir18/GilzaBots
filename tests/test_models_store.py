import pytest

from gilzabots import config as config_mod
from gilzabots import models_store


def _make_connection(store, **overrides):
    connection = {
        "id": "ollama-conn",
        "label": "Ollama",
        "provider": "ollama",
        "api_key": "",
        "api_base": "http://localhost:11434/v1",
    }
    connection.update(overrides)
    store.add_or_update_connection(connection)
    return connection["id"]


def test_add_or_update_model_replaces_previous_id(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/old-model",
            "connection_id": connection_id,
            "name": "old-model",
        }
    ])

    models_store.add_or_update_model({
        "previous_id": "ollama/old-model",
        "id": "ollama/new-model",
        "connection_id": connection_id,
        "name": "new-model",
    })

    saved = models_store.load_models()
    assert [model["id"] for model in saved] == [
        "ollama/new-model",
    ]
    assert saved[0]["supports_thinking"] is False
    assert saved[0]["api_base"] == "http://localhost:11434/v1"


def test_load_models_defaults_supports_thinking_to_false(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/test-model",
            "connection_id": connection_id,
            "name": "test-model",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["supports_thinking"] is False


def test_extra_model_params_round_trip_with_json_types(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)
    connection_id = _make_connection(models_store)

    models_store.add_or_update_model({
        "id": "ollama/custom-params",
        "connection_id": connection_id,
        "name": "custom-params",
        "extra_model_params": {
            "penalty_decay": 0.2,
            "feature_enabled": True,
            "provider_options": {"mode": "strict", "stops": [1, 2]},
        },
    })

    assert models_store.load_models()[0]["extra_model_params"] == {
        "penalty_decay": 0.2,
        "feature_enabled": True,
        "provider_options": {"mode": "strict", "stops": [1, 2]},
    }


@pytest.mark.parametrize(
    "name",
    ["api_key", "tools", "max_heartbeats", "num_ctx", "think", "temperature", "max_tokens", "seed", "bad-name"],
)
def test_extra_model_params_reject_reserved_or_invalid_names(tmp_path, monkeypatch, name):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)
    connection_id = _make_connection(models_store)

    with pytest.raises(ValueError, match="reserved|Invalid"):
        models_store.add_or_update_model({
            "id": "ollama/rejected-param",
            "connection_id": connection_id,
            "name": "rejected-param",
            "extra_model_params": {name: True},
        })


def test_model_inference_params_round_trip(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)
    connection_id = _make_connection(models_store)

    models_store.add_or_update_model({
        "id": "ollama/inference-model",
        "connection_id": connection_id,
        "name": "inference-model",
        "temperature": 0.7,
        "max_tokens": 4096,
        "seed": 42,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "frequency_penalty": 0.1,
        "presence_penalty": -0.2,
        "repetition_penalty": 1.1,
        "reasoning_effort": "medium",
    })

    loaded = models_store.load_models()[0]
    assert loaded["temperature"] == 0.7
    assert loaded["max_tokens"] == 4096
    assert loaded["seed"] == 42
    assert loaded["top_p"] == 0.9
    assert loaded["top_k"] == 40
    assert loaded["min_p"] == 0.05
    assert loaded["frequency_penalty"] == 0.1
    assert loaded["presence_penalty"] == -0.2
    assert loaded["repetition_penalty"] == 1.1
    assert loaded["reasoning_effort"] == "medium"


@pytest.mark.parametrize(
    ("key", "val", "match"),
    [
        ("temperature", 2.5, "Temperature must be a number between 0.0 and 2.0"),
        ("temperature", -0.1, "Temperature must be a number between 0.0 and 2.0"),
        ("max_tokens", 0, "Max tokens must be a positive integer"),
        ("max_tokens", -10, "Max tokens must be a positive integer"),
        ("seed", -1, "Seed must be a non-negative integer"),
        ("top_p", 1.5, "Top P must be a number between 0.0 and 1.0"),
        ("top_k", 0, "Top K must be a positive integer"),
        ("min_p", -0.1, "Min P must be a number between 0.0 and 1.0"),
        ("frequency_penalty", 3.0, "Frequency penalty must be a number between -2.0 and 2.0"),
        ("presence_penalty", -2.5, "Presence penalty must be a number between -2.0 and 2.0"),
        ("repetition_penalty", -0.5, "Repetition penalty must be a non-negative number"),
    ],
)
def test_model_inference_params_validation(tmp_path, monkeypatch, key, val, match):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)
    connection_id = _make_connection(models_store)

    with pytest.raises(ValueError, match=match):
        models_store.add_or_update_model({
            "id": "ollama/bad-param-model",
            "connection_id": connection_id,
            "name": "bad-param-model",
            key: val,
        })


def test_load_models_defaults_requires_single_system_message_to_false(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/test-model",
            "connection_id": connection_id,
            "name": "test-model",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["requires_single_system_message"] is False


def test_requires_single_system_message_round_trips_through_save_and_load(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama_chat/qwen3.8:latest",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "requires_single_system_message": True,
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["requires_single_system_message"] is True


def test_config_model_requires_single_system_message_reads_catalog_flag(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama_chat/qwen3.8:latest",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "requires_single_system_message": True,
        },
        {
            "id": "ollama_chat/gemma4:26b",
            "connection_id": connection_id,
            "name": "gemma4:26b",
        },
    ])

    assert config_mod.model_requires_single_system_message("ollama_chat/qwen3.8:latest") is True
    assert config_mod.model_requires_single_system_message("ollama_chat/gemma4:26b") is False
    assert config_mod.model_requires_single_system_message("ollama_chat/unregistered:latest") is False


def test_load_models_defaults_prompt_profile_to_full(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/test-model",
            "connection_id": connection_id,
            "name": "test-model",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["prompt_profile"] == "full"


def test_prompt_profile_round_trips_through_save_and_load(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/light-model",
            "connection_id": connection_id,
            "name": "light-model",
            "prompt_profile": "light",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["prompt_profile"] == "light"


def test_prompt_profile_rejects_unknown_value(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/weird-model",
            "connection_id": connection_id,
            "name": "weird-model",
            "prompt_profile": "ultra-mega-light",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["prompt_profile"] == "full"


def test_config_model_prompt_profile_reads_catalog_flag(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/light-model",
            "connection_id": connection_id,
            "name": "light-model",
            "prompt_profile": "light",
        },
        {
            "id": "ollama/full-model",
            "connection_id": connection_id,
            "name": "full-model",
        },
    ])

    assert config_mod.model_prompt_profile("ollama/light-model") == "light"
    assert config_mod.model_prompt_profile("ollama/full-model") == "full"
    assert config_mod.model_prompt_profile("ollama/unregistered-model") == "full"


def test_load_models_defaults_num_ctx_to_none(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/test-model",
            "connection_id": connection_id,
            "name": "test-model",
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["num_ctx"] is None


def test_num_ctx_round_trips_through_save_and_load(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/big-context-model",
            "connection_id": connection_id,
            "name": "big-context-model",
            "num_ctx": 32768,
        }
    ])

    loaded = models_store.load_models()

    assert loaded[0]["num_ctx"] == 32768


def test_config_model_num_ctx_reads_catalog_flag(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/big-context-model",
            "connection_id": connection_id,
            "name": "big-context-model",
            "num_ctx": 32768,
        },
        {
            "id": "ollama/plain-model",
            "connection_id": connection_id,
            "name": "plain-model",
        },
    ])

    assert config_mod.model_num_ctx("ollama/big-context-model") == 32768
    assert config_mod.model_num_ctx("ollama/plain-model") is None
    assert config_mod.model_num_ctx("ollama/unregistered-model") is None


def test_load_models_filters_cloud_models_in_community_mode(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    # Saved as raw legacy rows (no connection_id yet), exactly as a
    # pre-upgrade `models.json` import would look, to also exercise the
    # legacy-column fallback path during filtering.
    models_store.save_models([
        {"id": "GilzaBotsCloud", "provider": "GilzaBots", "name": "GilzaBots Cloud", "api_key": "", "api_base": ""},
        {"id": "GilzaBotsCloudGemini35Flash", "provider": "GilzaBots", "name": "GilzaBots Cloud Gemini 3.5 Flash (6x credits)", "api_key": "", "api_base": ""},
    ])

    loaded = models_store.load_models()
    assert [m["id"] for m in loaded] == []


def test_load_models_starts_with_an_empty_catalog(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    assert models_store.load_models() == []


def test_load_local_ollama_models_adds_only_unconfigured_models(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    # Seeded as a raw legacy row (no connection_id) to verify the migration
    # links it to the same well-known "ollama-local" connection that
    # discovery uses, so re-running discovery does not create a duplicate.
    models_store.save_models([
        {
            "id": "ollama/already-configured:latest",
            "provider": "ollama",
            "name": "already-configured:latest",
            "api_key": "",
            "api_base": "http://localhost:11434/v1",
        }
    ])
    monkeypatch.setattr(
        "gilzabots.ollama_manager.check_ollama_status",
        lambda: {"installed": True},
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"models":[{"name":"already-configured:latest"},{"name":"new-model:latest"}]}'

    monkeypatch.setattr(models_store.urllib_request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    added = models_store.load_local_ollama_models()

    assert [model["id"] for model in added] == ["ollama/new-model:latest"]
    assert added[0]["connection_id"] == "ollama-local"
    assert added[0]["api_base"] == "http://localhost:11434/v1"
    assert added[0]["supports_thinking"] is False
    assert [model["id"] for model in models_store.load_models()] == [
        "ollama/already-configured:latest",
        "ollama/new-model:latest",
    ]


def test_load_local_ollama_models_requires_ollama_installation(monkeypatch):
    monkeypatch.setattr(
        "gilzabots.ollama_manager.check_ollama_status",
        lambda: {"installed": False},
    )

    with pytest.raises(models_store.LocalOllamaNotInstalledError):
        models_store.load_local_ollama_models()


def test_add_or_update_model_requires_connection_id(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    with pytest.raises(ValueError):
        models_store.add_or_update_model({
            "id": "ollama/no-connection",
            "name": "no-connection",
        })


def test_duplicate_model_name_rejected_within_same_connection(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.add_or_update_model({
        "id": "ollama/dup-model",
        "connection_id": connection_id,
        "name": "dup-model",
    })

    with pytest.raises(ValueError):
        models_store.add_or_update_model({
            "id": "ollama/dup-model-2",
            "connection_id": connection_id,
            "name": "dup-model",
        })


def test_same_model_name_allowed_across_different_connections(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_a = _make_connection(models_store, id="conn-a", label="Connection A")
    connection_b = _make_connection(models_store, id="conn-b", label="Connection B")

    models_store.add_or_update_model({
        "id": "ollama/shared-name",
        "connection_id": connection_a,
        "name": "shared-name",
    })
    models_store.add_or_update_model({
        "id": "ollama/shared-name#conn-b",
        "connection_id": connection_b,
        "name": "shared-name",
    })

    loaded = models_store.load_models()
    assert sorted(m["id"] for m in loaded) == ["ollama/shared-name", "ollama/shared-name#conn-b"]
    assert {m["connection_id"] for m in loaded} == {"conn-a", "conn-b"}


def test_delete_connection_blocked_when_models_reference_it(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.add_or_update_model({
        "id": "ollama/blocked",
        "connection_id": connection_id,
        "name": "blocked",
    })

    with pytest.raises(ValueError):
        models_store.delete_connection(connection_id)

    assert models_store.delete_model("ollama/blocked") is True
    assert models_store.delete_connection(connection_id) is True


def test_migration_backfills_connection_id_and_preserves_model_ids(tmp_path, monkeypatch):
    """Backward-compatibility regression test: users who already have flat
    `global_models` rows from before provider connections existed must keep
    resolving the exact same credentials under the exact same model id after
    upgrading -- no manual re-entry, no broken projects."""
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    # Simulate a pre-upgrade DB: two flat rows sharing one provider/key/base
    # pair (as if registered separately, pre-split), one with a distinct pair.
    models_store.save_models([
        {
            "id": "openai/gpt-4o",
            "provider": "openai",
            "name": "gpt-4o",
            "api_key": "sk-shared",
            "api_base": "",
        },
        {
            "id": "openai/gpt-4o-mini",
            "provider": "openai",
            "name": "gpt-4o-mini",
            "api_key": "sk-shared",
            "api_base": "",
        },
        {
            "id": "openai/gpt-3.5-turbo",
            "provider": "openai",
            "name": "gpt-3.5-turbo",
            "api_key": "sk-other",
            "api_base": "",
        },
    ])

    loaded = models_store.load_models()
    by_id = {m["id"]: m for m in loaded}

    # Ids must be exactly preserved -- this is what project.model /
    # project.worker_model reference, and what makes existing projects keep
    # working after the upgrade.
    assert set(by_id) == {"openai/gpt-4o", "openai/gpt-4o-mini", "openai/gpt-3.5-turbo"}

    # Credentials must resolve identically to before the split.
    assert by_id["openai/gpt-4o"]["api_key"] == "sk-shared"
    assert by_id["openai/gpt-4o-mini"]["api_key"] == "sk-shared"
    assert by_id["openai/gpt-3.5-turbo"]["api_key"] == "sk-other"

    # The two rows sharing (provider, api_key, api_base) must collapse onto
    # the same connection; the distinct key gets its own connection.
    assert by_id["openai/gpt-4o"]["connection_id"] == by_id["openai/gpt-4o-mini"]["connection_id"]
    assert by_id["openai/gpt-3.5-turbo"]["connection_id"] != by_id["openai/gpt-4o"]["connection_id"]
    assert by_id["openai/gpt-4o"]["connection_id"] != ""

    assert len(models_store.load_connections()) == 2

    # resolve_runtime_model_id / get_model must behave exactly as before.
    assert models_store.resolve_runtime_model_id("openai/gpt-4o") == "openai/gpt-4o"
    assert models_store.get_model("openai/gpt-4o")["api_key"] == "sk-shared"

    # Migration is idempotent: loading again must not create more connections
    # or change any id.
    models_store.load_models()
    assert len(models_store.load_connections()) == 2


def test_get_model_by_runtime_id_resolves_thinking_remapped_ollama_model(tmp_path, monkeypatch):
    """A thinking-enabled ollama/ entry keeps its capabilities as ollama_chat/.

    `config.resolve_model_route` rewrites the provider prefix before the
    agent is built, so every capability lookup downstream sees the runtime id.
    """
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/qwen3.8:latest",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "supports_thinking": True,
            "requires_single_system_message": True,
            "num_ctx": 255000,
            "prompt_profile": "light",
        },
    ])

    resolved = models_store.get_model_by_runtime_id("ollama_chat/qwen3.8:latest")
    assert resolved is not None
    assert resolved["id"] == "ollama/qwen3.8:latest"

    assert config_mod.model_requires_single_system_message("ollama_chat/qwen3.8:latest") is True
    assert config_mod.model_supports_thinking("ollama_chat/qwen3.8:latest") is True
    assert config_mod.model_num_ctx("ollama_chat/qwen3.8:latest") == 255000
    assert config_mod.model_prompt_profile("ollama_chat/qwen3.8:latest") == "light"

    assert models_store.get_model_by_runtime_id("ollama_chat/unregistered:latest") is None


def test_get_model_by_runtime_id_prefers_exact_entry_over_ollama_chat_fallback(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/qwen3.8:latest",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "requires_single_system_message": True,
        },
        {
            "id": "ollama_chat/qwen3.8:latest",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "requires_single_system_message": False,
        },
    ])

    resolved = models_store.get_model_by_runtime_id("ollama_chat/qwen3.8:latest")
    assert resolved is not None
    assert resolved["id"] == "ollama_chat/qwen3.8:latest"
    assert config_mod.model_requires_single_system_message("ollama_chat/qwen3.8:latest") is False


def test_get_model_by_runtime_id_resolves_connection_suffixed_entry(tmp_path, monkeypatch):
    """Entries disambiguated with `#<connection_id>` are found by runtime id."""
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/qwen3.8:latest#ollama-conn",
            "connection_id": connection_id,
            "name": "qwen3.8:latest",
            "requires_single_system_message": True,
        },
    ])

    resolved = models_store.get_model_by_runtime_id("ollama/qwen3.8:latest")
    assert resolved is not None
    assert resolved["id"] == "ollama/qwen3.8:latest#ollama-conn"
    assert config_mod.model_requires_single_system_message("ollama/qwen3.8:latest") is True


def test_orchestrator_policy_defaults_to_direct(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/test-model",
            "connection_id": connection_id,
            "name": "test-model",
        }
    ])

    assert models_store.load_models()[0]["orchestrator_policy"] == "direct"


def test_orchestrator_policy_round_trips_through_save_and_load(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/delegating-model",
            "connection_id": connection_id,
            "name": "delegating-model",
            "orchestrator_policy": "delegate",
        }
    ])

    assert models_store.load_models()[0]["orchestrator_policy"] == "delegate"


def test_orchestrator_policy_rejects_unknown_value(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/weird-model",
            "connection_id": connection_id,
            "name": "weird-model",
            "orchestrator_policy": "sometimes",
        }
    ])

    assert models_store.load_models()[0]["orchestrator_policy"] == "direct"


def test_orchestrator_policy_is_independent_of_prompt_profile(tmp_path, monkeypatch):
    """The two axes must be storable in any combination.

    "light prompt + delegate writes" is the natural setting for a small local
    model, and folding the policy into prompt_profile would make it
    unexpressible -- which is why it is a separate column.
    """
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/small-local",
            "connection_id": connection_id,
            "name": "small-local",
            "prompt_profile": "light",
            "orchestrator_policy": "delegate",
        }
    ])

    saved = models_store.load_models()[0]
    assert saved["prompt_profile"] == "light"
    assert saved["orchestrator_policy"] == "delegate"


def test_orchestrator_policy_column_is_added_to_an_existing_database(tmp_path, monkeypatch):
    """An installed catalog predating the column must migrate, not break."""
    import sqlite3

    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.save_models([
        {
            "id": "ollama/legacy-model",
            "connection_id": connection_id,
            "name": "legacy-model",
        }
    ])

    db_path = store_path.with_suffix(".sqlite3")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(f"ALTER TABLE {models_store._MODELS_TABLE} DROP COLUMN orchestrator_policy")
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({models_store._MODELS_TABLE})")}
    assert "orchestrator_policy" not in columns

    loaded = models_store.load_models()

    assert [m["id"] for m in loaded] == ["ollama/legacy-model"]
    assert loaded[0]["orchestrator_policy"] == "direct"


def test_config_model_orchestrator_policy_reads_catalog_field(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)

    models_store.save_models([
        {
            "id": "ollama/delegating-model",
            "connection_id": connection_id,
            "name": "delegating-model",
            "orchestrator_policy": "delegate",
        },
        {
            "id": "ollama/direct-model",
            "connection_id": connection_id,
            "name": "direct-model",
        },
    ])

    assert config_mod.model_orchestrator_policy("ollama/delegating-model") == "delegate"
    assert config_mod.model_orchestrator_policy("ollama/direct-model") == "direct"
    assert config_mod.model_orchestrator_policy("ollama/unregistered-model") == "direct"


# ── catalog inference params vs. provider compatibility ──────────────────────

def test_catalog_inference_params_stay_subject_to_the_provider_filter(tmp_path, monkeypatch):
    """A catalog `top_k`/`min_p`/`repetition_penalty` must not reach OpenAI.

    These are not OpenAI request parameters. LiteLLM forwards unknown kwargs in
    `extra_body`, so admitting them past `get_supported_openai_params` makes the
    provider reject every turn with a 400 ("Unrecognized request argument") the
    moment a user fills those fields in the model form. They are dropped instead,
    exactly as the equivalent project-level params always were.
    """
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(
        models_store, id="openai-conn", provider="openai", api_base="",
    )
    models_store.save_models([
        {
            "id": "openai/gpt-4o",
            "connection_id": connection_id,
            "name": "gpt-4o",
            "temperature": 0.3,
            "top_k": 40,
            "min_p": 0.05,
            "repetition_penalty": 1.1,
        }
    ])

    cleaned = config_mod.sanitize_litellm_kwargs_for_model("openai/gpt-4o", {
        "temperature": 0.3,
        "top_k": 40,
        "min_p": 0.05,
        "repetition_penalty": 1.1,
    })

    assert cleaned["temperature"] == 0.3
    for unsupported in ("top_k", "min_p", "repetition_penalty"):
        assert unsupported not in cleaned


def test_ollama_still_receives_its_local_only_inference_params(tmp_path, monkeypatch):
    """The filter is per-provider, not a blanket ban: Ollama understands these."""
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.save_models([
        {
            "id": "ollama/llama3",
            "connection_id": connection_id,
            "name": "llama3",
            "top_k": 40,
            "min_p": 0.05,
            "repetition_penalty": 1.1,
        }
    ])

    cleaned = config_mod.sanitize_litellm_kwargs_for_model("ollama/llama3", {
        "top_k": 40, "min_p": 0.05, "repetition_penalty": 1.1,
    })

    assert cleaned["top_k"] == 40
    assert cleaned["min_p"] == 0.05
    # `repetition_penalty` is this project's name for it; `repeat_penalty` is
    # Ollama's, and the request has to use the server's. The value surviving the
    # provider filter is what this test guards -- the key it survives under is
    # settled by `config._resolve_ollama_repeat_penalty`.
    assert cleaned["repeat_penalty"] == 1.1
    assert "repetition_penalty" not in cleaned


def test_user_defined_extra_model_params_still_bypass_the_provider_filter(tmp_path, monkeypatch):
    """`extra_model_params` exists to name a parameter LiteLLM does not model.

    Filtering those out would make the feature inert, so they are admitted and
    the provider decides. This is the one documented bypass; the catalog's typed
    inference fields are not part of it.
    """
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(
        models_store, id="openai-conn", provider="openai", api_base="",
    )
    models_store.save_models([
        {
            "id": "openai/gpt-4o",
            "connection_id": connection_id,
            "name": "gpt-4o",
            "extra_model_params": {"my_provider_flag": True},
        }
    ])

    cleaned = config_mod.sanitize_litellm_kwargs_for_model(
        "openai/gpt-4o", {"my_provider_flag": True, "top_k": 40},
    )

    assert cleaned["my_provider_flag"] is True
    assert "top_k" not in cleaned


# ── hot-path caching ─────────────────────────────────────────────────────────

def test_load_models_caches_reads_without_going_stale(tmp_path, monkeypatch):
    """Repeated reads must not re-run migrations, and must not serve stale rows.

    `load_models` runs on every LLM request (twice per sanitize pass), so it is
    cached. The IDE server and the agent are separate processes sharing this
    store, so a model edited in one must be visible in the other: the cache is
    keyed on the database file's identity, not memoized outright.
    """
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.save_models([
        {"id": "ollama/a", "connection_id": connection_id, "name": "a"},
    ])

    assert [m["id"] for m in models_store.load_models()] == ["ollama/a"]

    # A cache hit must not open the database again.
    calls = []
    real_connect = models_store._connect
    monkeypatch.setattr(models_store, "_connect", lambda: (calls.append(1), real_connect())[1])
    assert [m["id"] for m in models_store.load_models()] == ["ollama/a"]
    assert calls == []

    # A write through this process is visible immediately.
    monkeypatch.setattr(models_store, "_connect", real_connect)
    models_store.add_or_update_model(
        {"id": "ollama/b", "connection_id": connection_id, "name": "b"}
    )
    assert [m["id"] for m in models_store.load_models()] == ["ollama/a", "ollama/b"]

    # A write by another process (same file, no cache invalidation call) is too.
    models_store._MODELS_CACHE = (
        models_store._db_fingerprint(models_store._resolve_db_path()),
        [{"id": "stale/entry", "name": "stale"}],
    )
    with models_store._connect() as conn:
        conn.execute(
            f"INSERT INTO {models_store._MODELS_TABLE} (id, name, connection_id, sort_order) VALUES (?, ?, ?, ?)",
            ("ollama/c", "c", connection_id, 2),
        )
        conn.commit()
    assert [m["id"] for m in models_store.load_models()] == ["ollama/a", "ollama/b", "ollama/c"]


def test_a_cached_entry_cannot_be_mutated_by_a_caller(tmp_path, monkeypatch):
    """Callers receive copies, so editing a returned dict cannot poison the cache."""
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)

    connection_id = _make_connection(models_store)
    models_store.save_models([
        {"id": "ollama/a", "connection_id": connection_id, "name": "a"},
    ])

    first = models_store.load_models()
    first[0]["name"] = "tampered"

    assert models_store.load_models()[0]["name"] == "a"
