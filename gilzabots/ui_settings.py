"""Persistent UI settings for GilzaBots.

Stored at ~/.gilzabots/ui_settings.json so they survive webview sessions,
which do not persist localStorage between app restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomic_file import write_text_atomic
from .config import get_gilzabots_home

_SETTINGS_PATH = Path(get_gilzabots_home()) / "ui_settings.json"

_DEFAULTS: dict[str, Any] = {
    "lang": "",  # "" means detect from OS; "en" or "pt-BR" for explicit choice
    "draft_synctex_enabled": False,
    "show_hidden_workspace_files": False,
    "prompt_evolution_iterations": 1,
    "prompt_evolution_max_tokens": 4096,
    # Target language for the PDF viewer "Translate" action. "" means follow the UI language.
    "translate_target_lang": "",
    # Accessibility: global interface scale. 1.0 is the unscaled ("Medium")
    # size; the front-end applies it as a CSS zoom over the whole app. Stored
    # as the raw factor rather than a preset name so the preset ladder can be
    # changed later without migrating saved settings, and so a custom value
    # picked with the fine-tuning control is representable in the same field.
    "ui_scale": 1.0,
    # Agent reasoning, in tokens. The chat shows the most recent reasoning up
    # to this size, and resuming an interrupted turn replays the reasoning in
    # full up to this size or as a summary beyond it. Everything is stored.
    "thought_context_tokens": 32000,
    # How much of the running reasoning the *chat* shows while a turn
    # works. The chat is a preview: it keeps the last few lines in view
    # so the user can see the model is thinking, and expanding it opens
    # the Agent Thinking panel, which holds the whole thing. Rendering
    # tens of thousands of tokens inside a chat bubble is what made a
    # long turn freeze the window.
    "chat_thought_preview_tokens": 1000,
    # Graphics mode for the embedded browser window: "auto" uses the GPU,
    # "off" runs Chromium without it. QtWebEngine loads the graphics driver
    # into the GilzaBots process itself, so a driver fault kills the whole
    # application, which is why "off" is the default and acceleration is
    # opt-in. Only takes effect at the next launch (see
    # gilzabots/webengine_env.py: GPU_DEFAULT, which must match this value).
    # Windows (WebView2) reads "webview2_gpu" instead, which has no entry here:
    # absent means its own default, "auto" (webengine_env.WEBVIEW2_GPU_DEFAULT).
    "webengine_gpu": "off",
    # Name of the project the desktop window had open last, reopened at the
    # next launch. Kept here rather than in localStorage because localStorage
    # is per origin, and the origin carries the server port: a launch that
    # lands on 3001 instead of 3000 (port still held, a second instance)
    # would read another port's stale value and reopen an older project.
    "last_active_project": "",
    # Interface preferences the front-end used to keep in localStorage (theme,
    # editor font, chat zoom, window positions, ...), as string key/value
    # pairs with localStorage's own semantics. They live here for the reason
    # given for "last_active_project": localStorage is split per server port.
    # See update_client_prefs.
    "client_prefs": {},
    # Set once the first window has imported its old localStorage. Later
    # windows, possibly on another port with an older copy, must not import.
    "client_prefs_imported": False,
}

CLIENT_PREF_KEY_MAX_CHARS = 200
CLIENT_PREF_VALUE_MAX_CHARS = 1_000_000

# localStorage keys that are not interface preferences, and so are never
# imported into "client_prefs". "lastActiveProject" maps onto
# "last_active_project" instead; "uiLanguage" is superseded by "lang".
_LEGACY_KEYS_NOT_IMPORTED = {"lastActiveProject", "uiLanguage"}

# Bounds for "ui_scale". The upper bound keeps the app usable on a 1080p
# screen (at 2.0 the layout has ~960x540 of usable space left).
UI_SCALE_MIN = 0.8
UI_SCALE_MAX = 2.0

THOUGHT_CONTEXT_TOKENS_DEFAULT = 32000
THOUGHT_CONTEXT_TOKENS_MIN = 1000
THOUGHT_CONTEXT_TOKENS_MAX = 1_000_000


def clamp_thought_context_tokens(value: Any) -> int:
    """Coerce a stored value into a valid reasoning size, in tokens."""
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return THOUGHT_CONTEXT_TOKENS_DEFAULT
    return max(THOUGHT_CONTEXT_TOKENS_MIN, min(THOUGHT_CONTEXT_TOKENS_MAX, tokens))


CHAT_THOUGHT_PREVIEW_TOKENS_DEFAULT = 1000
CHAT_THOUGHT_PREVIEW_TOKENS_MIN = 100
CHAT_THOUGHT_PREVIEW_TOKENS_MAX = 100_000


def clamp_chat_thought_preview_tokens(value: Any) -> int:
    """Coerce a stored value into a valid chat preview size, in tokens."""
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return CHAT_THOUGHT_PREVIEW_TOKENS_DEFAULT
    return max(CHAT_THOUGHT_PREVIEW_TOKENS_MIN,
               min(CHAT_THOUGHT_PREVIEW_TOKENS_MAX, tokens))


def clamp_ui_scale(value: Any) -> float:
    """Coerce an arbitrary value into a valid ui_scale factor.

    Falls back to 1.0 for anything non-numeric so a corrupted settings file
    cannot render the interface unusable.
    """
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    if scale != scale or scale in (float("inf"), float("-inf")):  # NaN / inf
        return 1.0
    return max(UI_SCALE_MIN, min(UI_SCALE_MAX, scale))


def load_ui_settings() -> dict[str, Any]:
    res = dict(_DEFAULTS)
    try:
        if _SETTINGS_PATH.exists():
            raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            res.update(raw)
    except Exception:
        pass
    return res


def save_ui_settings(settings: dict[str, Any]) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = load_ui_settings()
    current.update(settings)
    write_text_atomic(str(_SETTINGS_PATH), json.dumps(current, indent=2, ensure_ascii=False))


def _validate_client_pref_changes(changes: Any, removals: Any) -> tuple[dict[str, str], list[str]]:
    if not isinstance(changes, dict):
        raise ValueError("'set' must be an object of string keys to string values")
    if not isinstance(removals, list):
        raise ValueError("'remove' must be a list of keys")
    for key in list(changes) + removals:
        if not isinstance(key, str) or not key or len(key) > CLIENT_PREF_KEY_MAX_CHARS:
            raise ValueError(
                f"preference keys must be non-empty strings of at most {CLIENT_PREF_KEY_MAX_CHARS} characters"
            )
    for key, value in changes.items():
        if not isinstance(value, str):
            raise ValueError(f"the value of preference {key!r} must be a string")
        if len(value) > CLIENT_PREF_VALUE_MAX_CHARS:
            raise ValueError(
                f"the value of preference {key!r} exceeds {CLIENT_PREF_VALUE_MAX_CHARS} characters"
            )
    return changes, removals


def load_client_prefs() -> dict[str, Any]:
    """The stored interface preferences and whether the legacy import ran."""
    settings = load_ui_settings()
    prefs = settings.get("client_prefs")
    return {
        "prefs": dict(prefs) if isinstance(prefs, dict) else {},
        "imported": bool(settings.get("client_prefs_imported", False)),
    }


def update_client_prefs(changes: Any, removals: Any = None, *, legacy_import: bool = False) -> bool:
    """Apply preference changes; return whether they were applied.

    Raises ValueError for a malformed request. ``legacy_import`` marks the
    one-time copy of a window's old localStorage: it is applied only while no
    import has happened yet, and never overwrites a key already stored here.
    It also carries the old ``lastActiveProject`` over when no last project is
    recorded yet.
    """
    changes, removals = _validate_client_pref_changes(changes, [] if removals is None else removals)
    settings = load_ui_settings()
    prefs = settings.get("client_prefs")
    prefs = dict(prefs) if isinstance(prefs, dict) else {}
    update: dict[str, Any] = {}
    if legacy_import:
        if settings.get("client_prefs_imported"):
            return False
        last_project = changes.get("lastActiveProject", "")
        if last_project.strip() and not settings.get("last_active_project"):
            update["last_active_project"] = last_project
        for key, value in changes.items():
            if key not in _LEGACY_KEYS_NOT_IMPORTED:
                prefs.setdefault(key, value)
        update["client_prefs_imported"] = True
    else:
        prefs.update(changes)
        for key in removals:
            prefs.pop(key, None)
    update["client_prefs"] = prefs
    save_ui_settings(update)
    return True
