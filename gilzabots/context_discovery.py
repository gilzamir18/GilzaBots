"""Learn a hosted model's context window from its provider.

A catalog entry whose ``num_ctx`` is left empty ("Auto") used to be given a
fixed guess: 65,536 tokens for anything that was not a local Ollama model. For a
model that really holds a million tokens that guess became an GilzaBots-side
ceiling -- tool reads were refused and the conversation was evicted at a
fraction of what the provider would have accepted. Providers publish the real
number, so it is asked for instead:

- OpenAI-compatible model listings (OpenRouter, vLLM, LM Studio, LiteLLM proxy,
  ...): ``GET {api_base}/models``, reading ``context_length`` or the equivalent
  field of the entry whose ``id`` is the model's name.
- Gemini: ``GET .../v1beta/models/{name}``, reading ``inputTokenLimit``.
- Ollama Cloud: ``POST {root}/api/show``, reading ``<arch>.context_length``.
- Anthropic: ``GET /v1/models/{name}``, reading ``max_input_tokens`` when the
  API reports it.

A self-hosted Ollama model is never probed. There ``num_ctx`` is sent to the
server and decides how much memory it allocates, so the model's maximum is not
a safe default; `config.default_num_ctx_for_model` keeps the parsimonious one.

When the provider does not report a window the answer is ``None`` -- unknown --
and callers impose no limit of their own rather than substituting a guess.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

DISCOVERY_TIMEOUT_SECONDS = 5.0
# A provider that did not answer is asked again after this long, not on every
# request: resolution sits on the path of every LLM call.
FAILED_DISCOVERY_RETRY_SECONDS = 600.0

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
OLLAMA_CLOUD_ROOT = "https://ollama.com"

# Field names OpenAI-compatible servers use for the window, most specific first.
_LISTING_WINDOW_FIELDS = (
    "context_length",
    "max_model_len",
    "context_window",
    "max_context_length",
    "max_input_tokens",
)

_failed_at: Dict[str, float] = {}
_lock = threading.Lock()


def _http_json(url: str, *, headers: Dict[str, str] | None = None,
               body: Dict[str, Any] | None = None,
               timeout: float = DISCOVERY_TIMEOUT_SECONDS) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _bearer(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _window_from_listing(payload: Any, name: str) -> Optional[int]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or str(item.get("id") or "") != name:
            continue
        for field in _LISTING_WINDOW_FIELDS:
            window = _positive_int(item.get(field))
            if window:
                return window
        top_provider = item.get("top_provider")
        if isinstance(top_provider, dict):
            return _positive_int(top_provider.get("context_length"))
        return None
    return None


def _openai_compatible(api_base: str, name: str, api_key: str, http: Callable) -> Optional[int]:
    payload = http(f"{api_base.rstrip('/')}/models", headers=_bearer(api_key))
    return _window_from_listing(payload, name)


def _gemini(name: str, api_key: str, http: Callable) -> Optional[int]:
    model_path = name if name.startswith("models/") else f"models/{name}"
    headers = {"x-goog-api-key": api_key} if api_key else {}
    payload = http(f"{GEMINI_API_BASE}/{urllib.parse.quote(model_path, safe='/')}", headers=headers)
    return _positive_int(payload.get("inputTokenLimit")) if isinstance(payload, dict) else None


def _anthropic(name: str, api_key: str, http: Callable) -> Optional[int]:
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    payload = http(f"{ANTHROPIC_API_BASE}/models/{urllib.parse.quote(name, safe='')}", headers=headers)
    return _positive_int(payload.get("max_input_tokens")) if isinstance(payload, dict) else None


def _ollama_cloud(api_base: str, name: str, api_key: str, http: Callable) -> Optional[int]:
    root = (api_base or OLLAMA_CLOUD_ROOT).rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    payload = http(f"{root}/api/show", headers=_bearer(api_key), body={"model": name})
    model_info = payload.get("model_info") if isinstance(payload, dict) else None
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if str(key).endswith(".context_length"):
            return _positive_int(value)
    return None


def discover_context_window(entry: Dict[str, Any], http: Callable | None = None) -> Optional[int]:
    """Ask the provider behind catalog *entry* for its context window.

    Returns None when the provider does not report one, the request fails, or
    the entry is a self-hosted Ollama model (see the module docstring).
    """
    from .config import is_ollama_cloud_model

    http = http or _http_json
    provider = str(entry.get("provider") or "").strip().lower()
    name = str(entry.get("name") or "").strip()
    api_base = str(entry.get("api_base") or "").strip()
    api_key = str(entry.get("api_key") or "")
    if not name:
        return None
    if not api_key and provider:
        # The same credential the request itself would use: a connection may
        # leave the key to the provider's environment variable, as LiteLLM does.
        from .api_keys import get_env_var_for_model

        api_key = os.getenv(get_env_var_for_model(f"{provider}/{name}") or "", "")
    try:
        if provider in ("ollama", "ollama_chat"):
            cloud = is_ollama_cloud_model(f"{provider}/{name}") or OLLAMA_CLOUD_ROOT in api_base
            return _ollama_cloud(api_base, name, api_key, http) if cloud else None
        if provider == "gemini" and not api_base:
            return _gemini(name, api_key, http)
        if provider == "anthropic" and not api_base:
            return _anthropic(name, api_key, http)
        if provider == "openrouter" and not api_base:
            return _openai_compatible(OPENROUTER_API_BASE, name, api_key, http)
        if api_base:
            return _openai_compatible(api_base, name, api_key, http)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return None


def provider_context_window(entry: Dict[str, Any] | None, *, allow_network: bool = True) -> Optional[int]:
    """The provider-reported window for catalog *entry*, discovering it once.

    A value already recorded on the entry is returned as is. Otherwise the
    provider is asked (at most once per ``FAILED_DISCOVERY_RETRY_SECONDS`` after a
    failure) and a reported window is saved on the entry, so later requests and
    later sessions read it from the catalog. ``allow_network=False`` only reads
    what is recorded -- for listings that must not wait on a provider.
    """
    if not entry:
        return None
    recorded = _positive_int(entry.get("discovered_num_ctx"))
    if recorded or not allow_network:
        return recorded
    model_id = str(entry.get("id") or "")
    if not model_id:
        return None
    with _lock:
        failed = _failed_at.get(model_id)
        if failed is not None and time.monotonic() - failed < FAILED_DISCOVERY_RETRY_SECONDS:
            return None
        window = discover_context_window(entry)
        if window is None:
            _failed_at[model_id] = time.monotonic()
            return None
        _failed_at.pop(model_id, None)
    from .models_store import set_discovered_num_ctx

    set_discovered_num_ctx(model_id, window)
    return window


def ensure_provider_context_window(model_id: str) -> Optional[int]:
    """Discover the window of an "Auto" entry right after it was saved.

    Clears a pending retry delay first: a save is the user asking for this entry
    now, possibly with a corrected connection.
    """
    from .models_store import get_model

    with _lock:
        _failed_at.pop(model_id, None)
    entry = get_model(model_id)
    if not entry or entry.get("num_ctx"):
        return None
    return provider_context_window(entry)


def reset_discovery_state() -> None:
    with _lock:
        _failed_at.clear()
