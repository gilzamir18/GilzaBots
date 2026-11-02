import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import DEFAULT_DB_PATH, get_opalatex_home

_DEFAULT_MODELS_STORE_PATH = Path(get_opalatex_home()) / "models.json"
_MODELS_STORE_PATH = _DEFAULT_MODELS_STORE_PATH
_MODELS_TABLE = "global_models"
_CONNECTIONS_TABLE = "provider_connections"

_DEFAULT_MODELS: List[Dict[str, Any]] = []
_LOCAL_OLLAMA_API_BASE = "http://localhost:11434/v1"
_LOCAL_OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
_LOCAL_OLLAMA_CONNECTION_ID = "ollama-local"

OPENROUTER_DEFAULT_CONNECTION_ID = "openrouter"
OPENROUTER_DEFAULT_CONNECTION_LABEL = "OpenRouter"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_FREE_MODEL_NAME = "openrouter/free"

# Transport adapters a model entry may name; kept in sync with the routes
# registered by `agenticblocks.blocks.image.adapters`.
IMAGE_ROUTES = ("images_api", "chat_multimodal")

# Likewise for `agenticblocks.blocks.speech.adapters`. One route today, but the
# field exists so a speech engine LiteLLM does not cover is a catalog entry
# rather than a code change, exactly as for images.
SPEECH_ROUTES = ("audio_speech",)

# User-defined model parameters are forwarded as LiteLLM/model kwargs.  Keep
# transport, credentials, AgenticBlocks execution controls, and catalog fields
# under application control; accepting any of these through the generic editor
# would create a second, hidden source of truth for existing settings.
RESERVED_EXTRA_MODEL_PARAM_NAMES = frozenset({
    "id", "previous_id", "provider", "name", "connection_id", "connection_label",
    "api_key", "api_base", "api_version", "base_url", "custom_llm_provider",
    "organization", "default_headers", "extra_headers", "extra_body",
    "timeout", "request_timeout", "force_timeout", "stream_timeout", "max_retries",
    "drop_params", "allowed_openai_params", "additional_drop_params",
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls", "stream",
    "stream_options", "client", "async_client", "http_client", "mock_response",
    "custom_prompt_dict", "logger_fn", "logging_obj", "litellm_logging_obj",
    "dynamic_input_callbacks", "dynamic_success_callbacks",
    "dynamic_async_success_callbacks", "dynamic_failure_callbacks",
    "dynamic_async_failure_callbacks", "proxy_server_request", "secret_fields",
    "completion_call_id", "litellm_call_id", "function_id", "deployment_id",
    "model_info", "model_group", "model_id", "fallbacks", "context_window_fallbacks",
    "input_cost_per_token", "output_cost_per_token", "input_cost_per_second",
    "output_cost_per_second",
    "supports_thinking", "requires_single_system_message", "prompt_profile",
    "orchestrator_policy", "supports_image_generation", "image_route", "num_ctx",
    "discovered_num_ctx",
    "supports_speech_synthesis", "speech_route",
    "think", "extra_model_params",
    "temperature", "max_tokens", "seed", "top_p", "top_k", "min_p",
    "frequency_penalty", "presence_penalty", "repetition_penalty", "reasoning_effort",
    "max_heartbeats", "max_context_tokens", "eviction_threshold",
    "memory_pressure_threshold", "empty_response_reasoning_fallback",
    "max_idle_heartbeats", "max_iterations", "max_tool_calls", "on_max_iterations",
    "debug", "use_shared_router", "loop_detection", "loop_detection_limit",
    "force_vision", "pdf_truncate", "pdf_truncate_pct",
})
_EXTRA_MODEL_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_EXTRA_MODEL_PARAMS = 100


def normalize_extra_model_params(params: Any, *, strict: bool = False) -> Dict[str, Any]:
    """Return JSON-safe, non-reserved user-defined model parameters.

    ``strict`` is used on writes so malformed input fails with an actionable
    diagnostic. Read-time normalization remains tolerant so one damaged legacy
    row cannot make the whole model catalog unavailable.
    """
    if params in (None, ""):
        return {}
    if not isinstance(params, dict):
        if strict:
            raise ValueError("Additional model parameters must be a JSON object")
        return {}
    if len(params) > _MAX_EXTRA_MODEL_PARAMS:
        if strict:
            raise ValueError(
                f"Additional model parameters are limited to {_MAX_EXTRA_MODEL_PARAMS} entries"
            )
        return {}

    normalized: Dict[str, Any] = {}
    for raw_name, value in params.items():
        name = str(raw_name or "").strip()
        if not _EXTRA_MODEL_PARAM_NAME_RE.fullmatch(name):
            if strict:
                raise ValueError(
                    f"Invalid additional model parameter name '{name}': use letters, numbers, and underscores"
                )
            continue
        if name in RESERVED_EXTRA_MODEL_PARAM_NAMES:
            if strict:
                raise ValueError(f"Additional model parameter '{name}' is reserved")
            continue
        try:
            # Round-trip both validates the complete value tree and converts
            # harmless JSON-compatible subclasses/tuples to canonical values.
            normalized[name] = json.loads(
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"Additional model parameter '{name}' must contain a valid JSON value"
                ) from None
    return normalized


class LocalOllamaNotInstalledError(RuntimeError):
    """Raised when local Ollama discovery is requested without Ollama installed."""


class LocalOllamaUnavailableError(RuntimeError):
    """Raised when Ollama is installed but its local API cannot be reached."""


def normalize_model_entry(model: Dict[str, Any]) -> Dict[str, Any]:
    """Return a model-store entry with stable optional capability defaults."""
    entry = dict(model or {})
    entry["id"] = str(entry.get("id", "")).strip()
    entry["provider"] = str(entry.get("provider", "") or "").strip()
    entry["name"] = str(entry.get("name", "")).strip()
    entry["api_key"] = str(entry.get("api_key", "") or "")
    entry["api_base"] = str(entry.get("api_base", "") or "")
    entry["supports_thinking"] = bool(entry.get("supports_thinking", False))
    entry["requires_single_system_message"] = bool(entry.get("requires_single_system_message", False))
    _profile = str(entry.get("prompt_profile", "") or "full").strip().lower()
    entry["prompt_profile"] = _profile if _profile in ("full", "light") else "full"
    # Orthogonal to prompt_profile on purpose: the profile says how verbose this
    # model's prompt should be, the policy says whether it may touch files when it
    # runs as the chat orchestrator. Keeping them in separate fields is what makes
    # "light prompt + delegate writes" -- the natural setting for a small local
    # model -- expressible at all. Only consulted for the orchestrator role; a
    # worker has no run_skill and therefore nothing to delegate to.
    _policy = str(entry.get("orchestrator_policy", "") or "direct").strip().lower()
    entry["orchestrator_policy"] = _policy if _policy in ("direct", "delegate") else "direct"
    _num_ctx = entry.get("num_ctx")
    try:
        entry["num_ctx"] = int(_num_ctx) if _num_ctx not in (None, "") else None
    except (TypeError, ValueError):
        entry["num_ctx"] = None
    # The window the provider reported for this model (see
    # opalatex/context_discovery.py). It fills in for an empty `num_ctx` and is
    # never user input: `add_or_update_model` carries it over only while the
    # entry still names the same model on the same connection.
    _discovered = entry.get("discovered_num_ctx")
    try:
        entry["discovered_num_ctx"] = int(_discovered) if _discovered not in (None, "") else None
    except (TypeError, ValueError):
        entry["discovered_num_ctx"] = None
    if entry["discovered_num_ctx"] is not None and entry["discovered_num_ctx"] <= 0:
        entry["discovered_num_ctx"] = None
    entry["connection_id"] = str(entry.get("connection_id", "") or "")
    entry["connection_label"] = str(entry.get("connection_label", "") or "")
    entry["extra_model_params"] = normalize_extra_model_params(
        entry.get("extra_model_params")
    )

    # Model inference parameters
    _temp = entry.get("temperature")
    if _temp not in (None, ""):
        try:
            entry["temperature"] = max(0.0, min(2.0, float(str(_temp).replace(",", "."))))
        except (TypeError, ValueError):
            entry["temperature"] = None
    else:
        entry["temperature"] = None

    _max_tokens = entry.get("max_tokens")
    if _max_tokens not in (None, ""):
        try:
            entry["max_tokens"] = max(1, int(_max_tokens))
        except (TypeError, ValueError):
            entry["max_tokens"] = None
    else:
        entry["max_tokens"] = None

    _seed = entry.get("seed")
    if _seed not in (None, ""):
        try:
            entry["seed"] = max(0, int(_seed))
        except (TypeError, ValueError):
            entry["seed"] = None
    else:
        entry["seed"] = None

    _top_p = entry.get("top_p")
    if _top_p not in (None, ""):
        try:
            entry["top_p"] = max(0.0, min(1.0, float(str(_top_p).replace(",", "."))))
        except (TypeError, ValueError):
            entry["top_p"] = None
    else:
        entry["top_p"] = None

    _top_k = entry.get("top_k")
    if _top_k not in (None, ""):
        try:
            entry["top_k"] = max(1, int(_top_k))
        except (TypeError, ValueError):
            entry["top_k"] = None
    else:
        entry["top_k"] = None

    _min_p = entry.get("min_p")
    if _min_p not in (None, ""):
        try:
            entry["min_p"] = max(0.0, min(1.0, float(str(_min_p).replace(",", "."))))
        except (TypeError, ValueError):
            entry["min_p"] = None
    else:
        entry["min_p"] = None

    _freq_pen = entry.get("frequency_penalty")
    if _freq_pen not in (None, ""):
        try:
            entry["frequency_penalty"] = max(-2.0, min(2.0, float(str(_freq_pen).replace(",", "."))))
        except (TypeError, ValueError):
            entry["frequency_penalty"] = None
    else:
        entry["frequency_penalty"] = None

    _pres_pen = entry.get("presence_penalty")
    if _pres_pen not in (None, ""):
        try:
            entry["presence_penalty"] = max(-2.0, min(2.0, float(str(_pres_pen).replace(",", "."))))
        except (TypeError, ValueError):
            entry["presence_penalty"] = None
    else:
        entry["presence_penalty"] = None

    _rep_pen = entry.get("repetition_penalty")
    if _rep_pen not in (None, ""):
        try:
            entry["repetition_penalty"] = max(0.0, float(str(_rep_pen).replace(",", ".")))
        except (TypeError, ValueError):
            entry["repetition_penalty"] = None
    else:
        entry["repetition_penalty"] = None

    _reasoning_effort = str(entry.get("reasoning_effort") or "").strip().lower()
    if _reasoning_effort in ("none", "low", "medium", "high", "xhigh"):
        entry["reasoning_effort"] = _reasoning_effort
    else:
        entry["reasoning_effort"] = None
    # Image generation is a separate capability from chat, not a stronger form of
    # it: a diffusion model answers /v1/images/generations and nothing else, so a
    # catalog entry that declares it is never a candidate for the orchestrator or
    # a worker. `image_route` names the transport adapter
    # (`agenticblocks.blocks.image`); "" means the framework default.
    entry["supports_image_generation"] = bool(entry.get("supports_image_generation", False))
    _image_route = str(entry.get("image_route", "") or "").strip().lower()
    entry["image_route"] = _image_route if _image_route in IMAGE_ROUTES else ""
    # Speech is a third capability, independent of the other two: a TTS endpoint
    # answers /v1/audio/speech and nothing else, so an entry that declares it is
    # never a candidate for the orchestrator, a worker, or image generation.
    entry["supports_speech_synthesis"] = bool(entry.get("supports_speech_synthesis", False))
    _speech_route = str(entry.get("speech_route", "") or "").strip().lower()
    entry["speech_route"] = _speech_route if _speech_route in SPEECH_ROUTES else ""
    return entry


def list_image_generation_models() -> List[Dict[str, Any]]:
    """Return the catalog entries declared as image-capable."""
    return [m for m in load_models() if m.get("supports_image_generation")]


def list_speech_synthesis_models() -> List[Dict[str, Any]]:
    """Return the catalog entries declared as speech-capable."""
    return [m for m in load_models() if m.get("supports_speech_synthesis")]


def normalize_connection_entry(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Return a provider-connection entry with stable field defaults."""
    entry = dict(connection or {})
    entry["id"] = str(entry.get("id", "")).strip()
    entry["label"] = str(entry.get("label", "") or "").strip()
    entry["provider"] = str(entry.get("provider", "") or "").strip()
    entry["api_key"] = str(entry.get("api_key", "") or "")
    entry["api_base"] = str(entry.get("api_base", "") or "")
    return entry


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "connection"


def _generate_connection_id(conn: sqlite3.Connection, base_label: str) -> str:
    base = _slugify(base_label)
    existing_ids = {
        row["id"] for row in conn.execute(f"SELECT id FROM {_CONNECTIONS_TABLE}").fetchall()
    }
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def suggest_connection_id(label: str) -> str:
    """A free connection id derived from *label*.

    The GUI never needs this: its form owns the whole dialog and generates the
    id inline. A text front-end has no form, so the same rule has to be callable
    (`opalatex/cli_catalog.py`).
    """
    base = _slugify(label)
    existing = {c.get("id") for c in load_connections()}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def suggest_model_id(provider: str, name: str, connection_id: str) -> str:
    """The catalog id for a model, matching the desktop form's rule.

    ``provider/name``, suffixed with the connection id when a *different* entry
    already holds that base id — the same shape ``resolve_runtime_model_id``
    knows how to strip back to ``provider/name`` for LiteLLM.
    """
    base = f"{provider}/{name}"
    taken = {m.get("id") for m in load_models()}
    return f"{base}#{connection_id}" if base in taken else base


def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
    """Backfill `connection_id` for rows saved before provider connections existed.

    Idempotent and additive: only touches rows where `connection_id` is still
    empty, so a crash mid-migration just leaves those rows for the next call
    to pick up. Existing provider/api_key/api_base columns on `global_models`
    are never dropped, so an unmigrated row still resolves credentials
    correctly through `_row_to_model`'s legacy-column fallback in the
    meantime.
    """
    rows = conn.execute(
        f"SELECT id, provider, api_key, api_base FROM {_MODELS_TABLE} WHERE connection_id = ''"
    ).fetchall()
    if not rows:
        return

    existing_connections = conn.execute(
        f"SELECT id, label, provider, api_key, api_base FROM {_CONNECTIONS_TABLE}"
    ).fetchall()
    lookup: Dict[Tuple[str, str, str], str] = {
        (row["provider"] or "", row["api_key"] or "", row["api_base"] or ""): row["id"]
        for row in existing_connections
    }
    existing_ids = {row["id"] for row in existing_connections}
    provider_label_counts: Dict[str, int] = {}
    for row in existing_connections:
        provider_label_counts[row["provider"] or ""] = provider_label_counts.get(row["provider"] or "", 0) + 1

    # Legacy local-Ollama rows must land on the same well-known connection id
    # that `load_local_ollama_models` looks up, or re-running discovery after
    # an upgrade would duplicate every already-configured local model instead
    # of recognizing it as already present.
    local_ollama_key = ("ollama", "", _LOCAL_OLLAMA_API_BASE)

    for row in rows:
        key = (row["provider"] or "", row["api_key"] or "", row["api_base"] or "")
        connection_id = lookup.get(key)
        if connection_id is None:
            if key == local_ollama_key:
                connection_id = _LOCAL_OLLAMA_CONNECTION_ID
                label = "Local Ollama"
            else:
                provider = key[0]
                count = provider_label_counts.get(provider, 0) + 1
                provider_label_counts[provider] = count
                label = provider if count == 1 else f"{provider} ({count})"
                connection_id = _generate_connection_id(conn, label or "connection")
            if connection_id not in existing_ids:
                conn.execute(
                    f"""
                    INSERT INTO {_CONNECTIONS_TABLE} (id, label, provider, api_key, api_base, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (connection_id, label, key[0], key[1], key[2], 0),
                )
                existing_ids.add(connection_id)
            lookup[key] = connection_id
        conn.execute(
            f"UPDATE {_MODELS_TABLE} SET connection_id = ? WHERE id = ?",
            (connection_id, row["id"]),
        )
    conn.commit()


def _resolve_db_path() -> Path:
    return (
        Path(DEFAULT_DB_PATH)
        if Path(_MODELS_STORE_PATH) == _DEFAULT_MODELS_STORE_PATH
        else Path(_MODELS_STORE_PATH).with_suffix(".sqlite3")
    )


# `load_models` sits on the hot path of every LLM request: `get_agent_llm_kwargs`
# and `sanitize_litellm_kwargs_for_model` both resolve a catalog entry, and
# sanitizing happens at more than one boundary per request. Without this, each
# call re-opened the database and replayed CREATE TABLE, five ALTER TABLEs and
# the legacy-row migration.
#
# The key is the database file's identity rather than a plain memo: the agent runs
# in a separate process from the IDE server and both read this store, so a model
# edited in the UI must be visible to the agent on its next request. A stat is
# cheap; a stale catalog is a wrong answer.
_MODELS_CACHE: Tuple[Any, List[Dict[str, Any]]] | None = None


def _db_fingerprint(db_path: Path) -> Any:
    """Identity of the store's current contents, or None when it cannot be read."""
    try:
        stat = db_path.stat()
    except OSError:
        return None
    return (str(db_path), stat.st_mtime_ns, stat.st_size)


def _invalidate_models_cache() -> None:
    global _MODELS_CACHE
    _MODELS_CACHE = None


def _connect() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_CONNECTIONS_TABLE} (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL DEFAULT '',
            api_base TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_MODELS_TABLE} (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            api_key TEXT NOT NULL DEFAULT '',
            api_base TEXT NOT NULL DEFAULT '',
            connection_id TEXT NOT NULL DEFAULT '',
            supports_thinking INTEGER NOT NULL DEFAULT 0,
            requires_single_system_message INTEGER NOT NULL DEFAULT 0,
            prompt_profile TEXT NOT NULL DEFAULT 'full',
            orchestrator_policy TEXT NOT NULL DEFAULT 'direct',
            num_ctx INTEGER,
            extra_json TEXT NOT NULL DEFAULT '{{}}',
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Additive migrations for a `global_models` table created before these
    # columns existed. Harmlessly no-op on a fresh table (the CREATE TABLE
    # above already includes them).
    try:
        conn.execute(f"ALTER TABLE {_MODELS_TABLE} ADD COLUMN connection_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"ALTER TABLE {_MODELS_TABLE} ADD COLUMN requires_single_system_message INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"ALTER TABLE {_MODELS_TABLE} ADD COLUMN prompt_profile TEXT NOT NULL DEFAULT 'full'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"ALTER TABLE {_MODELS_TABLE} ADD COLUMN orchestrator_policy TEXT NOT NULL DEFAULT 'direct'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"ALTER TABLE {_MODELS_TABLE} ADD COLUMN num_ctx INTEGER")
    except sqlite3.OperationalError:
        pass
    _migrate_legacy_rows(conn)
    return conn


def _row_to_model(row: sqlite3.Row) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    try:
        extra = json.loads(row["extra_json"] or "{}")
    except Exception:
        extra = {}
    has_connection = bool(row["connection_id"]) and row["conn_provider"] is not None
    provider = row["conn_provider"] if has_connection else row["legacy_provider"]
    api_key = row["conn_api_key"] if has_connection else row["legacy_api_key"]
    api_base = row["conn_api_base"] if has_connection else row["legacy_api_base"]
    return normalize_model_entry({
        **extra,
        "id": row["id"],
        "provider": provider,
        "name": row["name"],
        "api_key": api_key,
        "api_base": api_base,
        "supports_thinking": bool(row["supports_thinking"]),
        "requires_single_system_message": bool(row["requires_single_system_message"]),
        "prompt_profile": row["prompt_profile"] or "full",
        "orchestrator_policy": row["orchestrator_policy"] or "direct",
        "num_ctx": row["num_ctx"],
        "connection_id": row["connection_id"] or "",
        "connection_label": row["conn_label"] if has_connection else "",
    })


def _model_extra_json(model: Dict[str, Any]) -> str:
    known = {
        "id", "provider", "name", "api_key", "api_base", "supports_thinking",
        "requires_single_system_message", "prompt_profile", "orchestrator_policy",
        "num_ctx", "previous_id",
        "connection_id", "connection_label",
    }
    extra = {k: v for k, v in model.items() if k not in known}
    return json.dumps(extra, ensure_ascii=False)


def _load_legacy_json_models() -> List[Dict[str, Any]]:
    try:
        if _MODELS_STORE_PATH.exists():
            with open(_MODELS_STORE_PATH, "r", encoding="utf-8") as f:
                models = json.load(f)
            if isinstance(models, list):
                return [normalize_model_entry(m) for m in models if isinstance(m, dict)]
    except Exception:
        pass
    return []

def load_models() -> List[Dict[str, Any]]:
    """Load models from the global SQLite model store or import legacy entries once."""
    global _MODELS_CACHE

    fingerprint = _db_fingerprint(_resolve_db_path())
    if fingerprint is not None and _MODELS_CACHE is not None and _MODELS_CACHE[0] == fingerprint:
        # Copied so a caller mutating an entry cannot corrupt the cache.
        return [dict(model) for model in _MODELS_CACHE[1]]

    loaded_defaults = False
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                gm.id AS id,
                gm.name AS name,
                gm.supports_thinking AS supports_thinking,
                gm.requires_single_system_message AS requires_single_system_message,
                gm.prompt_profile AS prompt_profile,
                gm.orchestrator_policy AS orchestrator_policy,
                gm.num_ctx AS num_ctx,
                gm.extra_json AS extra_json,
                gm.sort_order AS sort_order,
                gm.connection_id AS connection_id,
                gm.provider AS legacy_provider,
                gm.api_key AS legacy_api_key,
                gm.api_base AS legacy_api_base,
                pc.provider AS conn_provider,
                pc.api_key AS conn_api_key,
                pc.api_base AS conn_api_base,
                pc.label AS conn_label
            FROM {_MODELS_TABLE} gm
            LEFT JOIN {_CONNECTIONS_TABLE} pc ON pc.id = gm.connection_id
            ORDER BY gm.sort_order ASC, gm.id ASC
            """
        ).fetchall()
        models = [_row_to_model(row) for row in rows]

    if len(models) == 0:
        legacy_models = _load_legacy_json_models()
        models = legacy_models or [normalize_model_entry(m) for m in _DEFAULT_MODELS]
        loaded_defaults = True

    # Filter out any stray Opala Cloud model entries left over from earlier versions.
    filtered = [
        m for m in models
        if isinstance(m, dict)
        and str(m.get("provider", "")).lower() not in ("opalatex", "opalatex_cloud")
        and not str(m.get("id", "")).lower().startswith("opalatex")
    ]
    if len(filtered) != len(models):
        models = filtered
        loaded_defaults = True

    if loaded_defaults:
        save_models(models)

    # Fingerprinted after the read (and after any import write above), so the
    # entry describes the contents that were actually returned.
    post_read_fingerprint = _db_fingerprint(_resolve_db_path())
    if post_read_fingerprint is not None:
        _MODELS_CACHE = (post_read_fingerprint, [dict(model) for model in models])

    return models



def save_models(models: List[Dict[str, Any]]) -> None:
    """Save models list to the global SQLite model store."""
    # Explicit, rather than relying on the file fingerprint alone: a write that
    # lands in the same mtime tick as the read that filled the cache would
    # otherwise keep serving the pre-write catalog for the rest of the process.
    _invalidate_models_cache()
    with _connect() as conn:
        conn.execute(f"DELETE FROM {_MODELS_TABLE}")
        for index, raw_model in enumerate(models or []):
            model = normalize_model_entry(raw_model)
            if not model.get("id"):
                continue
            conn.execute(
                f"""
                INSERT INTO {_MODELS_TABLE}
                (id, provider, name, api_key, api_base, connection_id, supports_thinking, requires_single_system_message, prompt_profile, orchestrator_policy, num_ctx, extra_json, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model["id"],
                    model.get("provider", ""),
                    model.get("name", ""),
                    model.get("api_key", ""),
                    model.get("api_base", ""),
                    model.get("connection_id", ""),
                    int(bool(model.get("supports_thinking", False))),
                    int(bool(model.get("requires_single_system_message", False))),
                    model.get("prompt_profile", "full"),
                    model.get("orchestrator_policy", "direct"),
                    model.get("num_ctx"),
                    _model_extra_json(model),
                    index,
                ),
            )


def load_connections() -> List[Dict[str, Any]]:
    """Load provider connections from the global SQLite store."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {_CONNECTIONS_TABLE} ORDER BY sort_order ASC, id ASC"
        ).fetchall()
    return [normalize_connection_entry(dict(row)) for row in rows]


def save_connections(connections: List[Dict[str, Any]]) -> None:
    """Save provider connections list to the global SQLite store."""
    # A model entry carries its connection's credentials, so editing a connection
    # changes what `load_models` returns.
    _invalidate_models_cache()
    with _connect() as conn:
        conn.execute(f"DELETE FROM {_CONNECTIONS_TABLE}")
        for index, raw_connection in enumerate(connections or []):
            connection = normalize_connection_entry(raw_connection)
            if not connection.get("id"):
                continue
            conn.execute(
                f"""
                INSERT INTO {_CONNECTIONS_TABLE}
                (id, label, provider, api_key, api_base, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    connection["id"],
                    connection.get("label", ""),
                    connection.get("provider", ""),
                    connection.get("api_key", ""),
                    connection.get("api_base", ""),
                    index,
                ),
            )


def get_connection(connection_id: str) -> Dict[str, Any] | None:
    """Get a specific provider connection by ID."""
    for c in load_connections():
        if c.get("id") == connection_id:
            return c
    return None


def add_or_update_connection(connection_data: Dict[str, Any]) -> None:
    """Add a new provider connection or update an existing one by ID.

    A connection's ID is stable once created: this function never renames a
    connection's ID, it only replaces the row's fields in place, so models
    referencing it by `connection_id` never need a rename cascade.
    """
    if not str(connection_data.get("id", "")).strip():
        raise ValueError("Connection data must contain an 'id' field")

    connections = load_connections()
    connection_data = normalize_connection_entry(connection_data)
    connection_id = connection_data["id"]

    updated = False
    for i, c in enumerate(connections):
        if c.get("id") == connection_id:
            connections[i] = connection_data
            updated = True
            break

    if not updated:
        connections.append(connection_data)

    save_connections(connections)


def delete_connection(connection_id: str) -> bool:
    """Delete a provider connection by ID.

    Refuses to delete a connection that still has models referencing it:
    silently cascading would leave a project's stored model id pointing at a
    row with no resolvable credentials, turning a clear "connection in use"
    error into a confusing downstream provider-auth failure later.
    """
    blocking = [m.get("id") for m in load_models() if m.get("connection_id") == connection_id]
    if blocking:
        raise ValueError(
            f"Connection '{connection_id}' is used by {len(blocking)} model(s): {', '.join(blocking)}"
        )

    connections = load_connections()
    initial_length = len(connections)
    connections = [c for c in connections if c.get("id") != connection_id]

    if len(connections) < initial_length:
        save_connections(connections)
        return True
    return False


def ensure_default_openrouter_free_model(connection_id: str) -> Dict[str, Any] | None:
    """Ensure the canonical free OpenRouter model exists for *connection_id*.

    OpenRouter exposes ``openrouter/free`` as a zero-cost router across its
    currently available free models (including tool-calling models when tool
    schemas are supplied). When an OpenRouter connection is registered, we
    seed that entry once unless ``openrouter/free`` is already configured
    under the same connection.
    """
    clean_connection_id = str(connection_id or "").strip()
    if not clean_connection_id:
        return None
    connection = get_connection(clean_connection_id)
    if not connection:
        return None
    if str(connection.get("provider") or "").strip().lower() != "openrouter":
        return None

    for existing in load_models():
        if (
            existing.get("connection_id") == clean_connection_id
            and str(existing.get("name") or "").strip() == OPENROUTER_FREE_MODEL_NAME
        ):
            return None

    model_id = suggest_model_id("openrouter", OPENROUTER_FREE_MODEL_NAME, clean_connection_id)
    add_or_update_model({
        "id": model_id,
        "connection_id": clean_connection_id,
        "provider": "openrouter",
        "name": OPENROUTER_FREE_MODEL_NAME,
        "supports_thinking": True,
    })
    return get_model(model_id)


def load_local_ollama_models() -> List[Dict[str, Any]]:
    """Import unconfigured models exposed by the local Ollama API."""
    from .ollama_manager import check_ollama_status

    if not check_ollama_status().get("installed"):
        raise LocalOllamaNotInstalledError("Ollama is not installed")

    try:
        request = urllib_request.Request(_LOCAL_OLLAMA_TAGS_URL, method="GET")
        with urllib_request.urlopen(request, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise LocalOllamaUnavailableError("The local Ollama API is unavailable") from exc

    remote_models = payload.get("models", []) if isinstance(payload, dict) else []
    if not isinstance(remote_models, list):
        raise LocalOllamaUnavailableError("The local Ollama API returned an invalid model list")

    if not get_connection(_LOCAL_OLLAMA_CONNECTION_ID):
        add_or_update_connection({
            "id": _LOCAL_OLLAMA_CONNECTION_ID,
            "label": "Local Ollama",
            "provider": "ollama",
            "api_key": "",
            "api_base": _LOCAL_OLLAMA_API_BASE,
        })

    configured_models = load_models()
    configured_names = {
        model.get("name", "")
        for model in configured_models
        if model.get("connection_id") == _LOCAL_OLLAMA_CONNECTION_ID
    }
    configured_ids = {model.get("id", "") for model in configured_models}
    additions: List[Dict[str, Any]] = []

    for remote_model in remote_models:
        model_name = str(remote_model.get("name", "") if isinstance(remote_model, dict) else "").strip()
        if not model_name or model_name in configured_names:
            continue

        base_id = f"ollama/{model_name}"
        model_id = base_id if base_id not in configured_ids else f"{base_id}#{_LOCAL_OLLAMA_CONNECTION_ID}"

        additions.append(normalize_model_entry({
            "id": model_id,
            "connection_id": _LOCAL_OLLAMA_CONNECTION_ID,
            "provider": "ollama",
            "name": model_name,
            "api_key": "",
            "api_base": _LOCAL_OLLAMA_API_BASE,
        }))
        configured_names.add(model_name)
        configured_ids.add(model_id)

    if additions:
        save_models([*configured_models, *additions])

    return additions

def get_model(model_id: str) -> Dict[str, Any] | None:
    """Get a specific model by ID."""
    models = load_models()
    for m in models:
        if m.get("id") == model_id:
            return m
    return None

def resolve_runtime_model_id(model_id: str | None) -> str:
    """Return the provider/model identifier used by LiteLLM for a stored entry.

    Model-store IDs identify a configuration entry and may include a suffix when
    the same provider/model is configured against multiple connections.
    LiteLLM must always receive the original ``provider/name`` identifier.
    """
    configured_model = get_model(str(model_id or ""))
    if configured_model:
        provider = configured_model.get("provider", "")
        name = configured_model.get("name", "")
        if provider and name:
            return f"{provider}/{name}"
    return str(model_id or "")


def _ollama_runtime_variants(runtime_model: str) -> List[str]:
    """Return the catalog identifiers a runtime Ollama model id may come from.

    ``config.resolve_model_route`` rewrites ``ollama/<name>`` to
    ``ollama_chat/<name>`` when thinking is requested, so a runtime id served
    by the native chat endpoint can originate from an ``ollama/`` catalog
    entry. The rewrite is one-way, hence only that direction is considered.
    """
    if runtime_model.startswith("ollama_chat/"):
        return [runtime_model, "ollama/" + runtime_model[len("ollama_chat/"):]]
    return [runtime_model]


def get_model_by_runtime_id(model_id: str) -> Dict[str, Any] | None:
    """Get the catalog entry behind a model identifier, runtime form included.

    ``get_model`` only matches a stored entry ID. Runtime identifiers handed to
    LiteLLM are not always that string: entries configured against multiple
    connections carry a ``#<connection_id>`` suffix that
    ``resolve_runtime_model_id`` strips, and thinking-enabled Ollama models are
    rewritten from ``ollama/`` to ``ollama_chat/``. Capability lookups keyed on
    the runtime identifier (thinking, single-system-message, num_ctx, prompt
    profile) must still find the entry the user actually configured, otherwise
    every per-model capability silently reads as unset for those models.

    Resolution order: exact entry ID, then the entry whose ``provider/name``
    equals the runtime id, then the same match for the pre-rewrite ``ollama/``
    form. When several entries share a ``provider/name`` (the same model under
    different connections), the first in catalog order wins.
    """
    raw = str(model_id or "")
    if not raw:
        return None

    direct = get_model(raw)
    if direct:
        return direct

    models = load_models()
    for candidate in _ollama_runtime_variants(raw):
        for model in models:
            provider = str(model.get("provider") or "")
            name = str(model.get("name") or "")
            if provider and name and f"{provider}/{name}" == candidate:
                return model
    return None


def _has_duplicate_configuration(
    models: List[Dict[str, Any]],
    model_data: Dict[str, Any],
    previous_id: str | None,
) -> bool:
    """Return whether the connection and model name already exist together."""
    connection_id = model_data["connection_id"]
    name = model_data["name"]
    return any(
        model.get("id") != previous_id
        and model.get("connection_id") == connection_id
        and model.get("name") == name
        for model in models
    )


def validate_model_inference_params(model_data: Dict[str, Any]) -> None:
    """Validate user-supplied model inference parameters on write."""
    temp = model_data.get("temperature")
    if temp not in (None, ""):
        try:
            val = float(str(temp).replace(",", "."))
            if not (0.0 <= val <= 2.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Temperature must be a number between 0.0 and 2.0")

    max_tokens = model_data.get("max_tokens")
    if max_tokens not in (None, ""):
        try:
            val = int(max_tokens)
            if val < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Max tokens must be a positive integer")

    seed = model_data.get("seed")
    if seed not in (None, ""):
        try:
            val = int(seed)
            if val < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Seed must be a non-negative integer")

    top_p = model_data.get("top_p")
    if top_p not in (None, ""):
        try:
            val = float(str(top_p).replace(",", "."))
            if not (0.0 <= val <= 1.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Top P must be a number between 0.0 and 1.0")

    top_k = model_data.get("top_k")
    if top_k not in (None, ""):
        try:
            val = int(top_k)
            if val < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Top K must be a positive integer")

    min_p = model_data.get("min_p")
    if min_p not in (None, ""):
        try:
            val = float(str(min_p).replace(",", "."))
            if not (0.0 <= val <= 1.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Min P must be a number between 0.0 and 1.0")

    freq_pen = model_data.get("frequency_penalty")
    if freq_pen not in (None, ""):
        try:
            val = float(str(freq_pen).replace(",", "."))
            if not (-2.0 <= val <= 2.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Frequency penalty must be a number between -2.0 and 2.0")

    pres_pen = model_data.get("presence_penalty")
    if pres_pen not in (None, ""):
        try:
            val = float(str(pres_pen).replace(",", "."))
            if not (-2.0 <= val <= 2.0):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Presence penalty must be a number between -2.0 and 2.0")

    rep_pen = model_data.get("repetition_penalty")
    if rep_pen not in (None, ""):
        try:
            val = float(str(rep_pen).replace(",", "."))
            if val < 0.0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Repetition penalty must be a non-negative number")

    effort = model_data.get("reasoning_effort")
    if effort not in (None, ""):
        clean_effort = str(effort).strip().lower()
        if clean_effort not in ("none", "low", "medium", "high", "xhigh"):
            raise ValueError("Reasoning effort must be one of: none, low, medium, high, xhigh")


def add_or_update_model(model_data: Dict[str, Any]) -> None:
    """Add a new model or update an existing one by ID.

    When editing a model whose ID changed, callers can pass ``previous_id`` to
    replace the old entry instead of appending a duplicate under the new ID.
    """
    if "id" not in model_data:
        raise ValueError("Model data must contain an 'id' field")
    if not str(model_data.get("connection_id", "")).strip():
        raise ValueError("Model data must contain a 'connection_id' field")
    model_data = dict(model_data)
    validate_model_inference_params(model_data)
    model_data["extra_model_params"] = normalize_extra_model_params(
        model_data.get("extra_model_params"), strict=True
    )

    models = load_models()
    model_data = normalize_model_entry(model_data)
    previous_id = model_data.pop("previous_id", None)
    model_id = model_data["id"]

    if _has_duplicate_configuration(models, model_data, previous_id):
        raise ValueError(
            "A model with this name already exists for this connection"
        )

    if previous_id and previous_id != model_id:
        for m in models:
            if m.get("id") == model_id:
                raise ValueError(f"Model '{model_id}' already exists")

    # The provider-reported window describes one model on one connection. It is
    # never taken from the caller (a form echoes back whatever the listing held)
    # and survives an edit only while the entry still names the same model.
    model_data["discovered_num_ctx"] = None
    updated = False
    for i, m in enumerate(models):
        if m.get("id") == (previous_id or model_id):
            if _same_model_identity(m, model_data):
                model_data["discovered_num_ctx"] = m.get("discovered_num_ctx")
            models[i] = model_data
            updated = True
            break

    if not updated:
        models.append(model_data)

    save_models(models)


def _same_model_identity(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    return all(
        str(old.get(field) or "") == str(new.get(field) or "")
        for field in ("connection_id", "name")
    )


def set_discovered_num_ctx(model_id: str, value: int | None) -> bool:
    """Record the context window the provider reported for *model_id*.

    Only that entry's stored ``extra_json`` changes, so a concurrent edit of any
    other entry is not overwritten. Returns False when the entry does not exist.
    """
    _invalidate_models_cache()
    with _connect() as conn:
        row = conn.execute(
            f"SELECT extra_json FROM {_MODELS_TABLE} WHERE id = ?", (model_id,)
        ).fetchone()
        if row is None:
            return False
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except ValueError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        if value:
            extra["discovered_num_ctx"] = int(value)
        else:
            extra.pop("discovered_num_ctx", None)
        conn.execute(
            f"UPDATE {_MODELS_TABLE} SET extra_json = ? WHERE id = ?",
            (json.dumps(extra, ensure_ascii=False), model_id),
        )
    _invalidate_models_cache()
    return True

def delete_model(model_id: str) -> bool:
    """Delete a model by ID. Returns True if deleted, False if not found."""
    models = load_models()
    initial_length = len(models)

    models = [m for m in models if m.get("id") != model_id]

    if len(models) < initial_length:
        save_models(models)
        return True
    return False
