"""Speech synthesis configuration for GilzaBots.

Global settings persisted at ``<gilzabots home>/speech.json``:

    {
        "enabled": false,            # whether "Pronounce" is offered at all
        "engine": "local",           # "local" (downloaded voice) or "remote"
        "local_voice": "",           # key of a voice in the voice store
        "model": "",                 # catalog id of the speech model ("remote")
        "voice": "",                 # "" → the framework default voice
        "response_format": "mp3",    # wire format asked of the provider
        "speed": 1.0,                # playback rate asked of the provider
        "route": ""                  # "" → the model's catalog route, else override
    }

Two engines, one block. ``local`` runs a downloaded Piper voice in this process
(``gilzabots/voice_store.py``, route ``local_onnx``) and needs no network, no
account and no endpoint -- it is the default because it is the one that keeps
§2.2's offline promise. ``remote`` speaks the OpenAI ``/v1/audio/speech``
contract through the model catalog, which covers hosted providers *and* a
speech server on localhost.

The model is a normal entry of the global model store, so its credentials and
``api_base`` come from the same place every other model's do. That is what makes
one setting cover OpenAI and Azure *and* a speech server on localhost
(Kokoro-FastAPI, openedai-speech, LocalAI, a Piper wrapper): they differ only in
the catalog entry, so the feature does not have to choose between working
offline and working with a hosted provider.

It ships **disabled**. Even on the local engine, speech needs something the
install does not have yet -- a downloaded voice, and a system espeak-ng to turn
text into phonemes. Defaulting it to on would put a menu item in front of every
user that can only fail, so it is opt-in and says exactly what is missing when
it is not ready (:func:`configuration_problem`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import get_gilzabots_home

_CONFIG_PATH = Path(get_gilzabots_home()) / "speech.json"

DEFAULT_RESPONSE_FORMAT = "mp3"
DEFAULT_SPEED = 1.0

# The formats the OpenAI speech contract defines. A value outside this list is
# refused rather than passed through: an unknown format reaches the browser as
# audio it cannot decode, which presents as silence rather than as an error.
SUPPORTED_FORMATS = ("mp3", "opus", "aac", "flac", "wav")

# Providers reject a speed outside their own range, and a reading at 0.1x is not
# a reading. The window is the intersection of what OpenAI and the common local
# servers accept.
MIN_SPEED = 0.25
MAX_SPEED = 4.0

ENGINES = ("local", "remote")
DEFAULT_ENGINE = "local"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "engine": DEFAULT_ENGINE,
    "local_voice": "",
    "model": "",
    "voice": "",
    "response_format": DEFAULT_RESPONSE_FORMAT,
    "speed": DEFAULT_SPEED,
    "route": "",
}


# ─── Persistence ──────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """Return the current speech config, falling back to defaults."""
    try:
        if _CONFIG_PATH.exists():
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {**_DEFAULTS, **raw}
    except Exception:
        pass
    return dict(_DEFAULTS)


def save_config(config: dict[str, Any]) -> None:
    """Persist *config*, keeping only known keys."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    to_save = {
        "enabled": bool(config.get("enabled", _DEFAULTS["enabled"])),
        "engine": sanitize_engine(config.get("engine")),
        "local_voice": str(config.get("local_voice", "") or "").strip(),
        "model": str(config.get("model", "") or "").strip(),
        "voice": str(config.get("voice", "") or "").strip(),
        "response_format": sanitize_response_format(config.get("response_format")),
        "speed": sanitize_speed(config.get("speed")),
        "route": str(config.get("route", "") or "").strip(),
    }
    _CONFIG_PATH.write_text(
        json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_enabled() -> bool:
    return bool(load_config().get("enabled", False))


def sanitize_engine(value: Any) -> str:
    engine = str(value or "").strip().lower()
    return engine if engine in ENGINES else DEFAULT_ENGINE


def sanitize_response_format(value: Any) -> str:
    fmt = str(value or "").strip().lower()
    return fmt if fmt in SUPPORTED_FORMATS else DEFAULT_RESPONSE_FORMAT


def sanitize_speed(value: Any) -> float:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    if speed != speed:  # NaN
        return DEFAULT_SPEED
    return max(MIN_SPEED, min(MAX_SPEED, round(speed, 2)))


# ─── Model / route resolution ────────────────────────────────────────────────

def resolve_route(model_id: str) -> str:
    """Return the adapter route for *model_id*.

    Priority: explicit config override > the model's catalog ``speech_route`` >
    the framework default (``audio_speech``).
    """
    from agenticblocks.blocks.speech import DEFAULT_SPEECH_ROUTE

    override = str(load_config().get("route", "") or "").strip()
    if override:
        return override

    try:
        from .models_store import get_model

        entry = get_model(model_id) or {}
        route = str(entry.get("speech_route", "") or "").strip()
        if route:
            return route
    except Exception:
        pass
    return DEFAULT_SPEECH_ROUTE


def transport_kwargs(model_id: str) -> dict[str, Any]:
    """Return only the credentials/endpoint kwargs for *model_id*.

    ``get_agent_llm_kwargs`` answers with a chat request's parameters --
    temperature, num_ctx, think, stream. A speech endpoint rejects those, so the
    transport fields are whitelisted out rather than the chat ones blacklisted:
    a parameter added to the chat path later cannot leak into a speech request.
    """
    from agenticblocks.blocks.speech import TRANSPORT_KWARGS
    from .config import get_agent_llm_kwargs

    try:
        merged = get_agent_llm_kwargs("memgpt", model_override=model_id) or {}
    except Exception:
        merged = {}

    return {k: v for k, v in merged.items() if k in TRANSPORT_KWARGS and v not in (None, "")}


def configuration_problem() -> str:
    """Return an actionable diagnostic when speech cannot run, else "".

    Each branch names the one thing to do next. "Speech is unavailable" tells
    the user nothing they can act on, and this string is what the disabled menu
    item shows as its reason.
    """
    cfg = load_config()
    if not cfg.get("enabled", False):
        return (
            "Speech synthesis is disabled. Enable it in "
            "Settings > General > Pronunciation."
        )

    if sanitize_engine(cfg.get("engine")) == "local":
        from . import voice_store

        voice = str(cfg.get("local_voice", "") or "").strip()
        if not voice:
            return (
                "No voice is installed yet. Open "
                "Settings > General > Pronunciation and download one "
                "(about 63 MB); it then works offline."
            )
        if not voice_store.is_installed(voice):
            return (
                f"The selected voice '{voice}' is not installed. Download it "
                "again in Settings > General > Pronunciation."
            )

        # Checked last because it is the only one the application cannot fix
        # for the user: espeak-ng is GPL-3.0 and is therefore used from the
        # system rather than bundled (see agenticblocks speech/phonemes.py).
        from agenticblocks.blocks.speech import availability_problem

        return availability_problem()

    if not str(cfg.get("model", "") or "").strip():
        return (
            "No speech synthesis model is configured. Open "
            "Settings > General > Pronunciation and pick a model marked as "
            "speech-capable in 'Edit Models' -- a hosted one (OpenAI tts-1, "
            "Azure) or a local server registered with its api_base "
            "(Kokoro-FastAPI, openedai-speech, LocalAI, Piper)."
        )
    return ""


def build_block(model_id: str = "", route: str = ""):
    """Return a ``SpeechSynthesisBlock`` wired to the configured engine."""
    from agenticblocks.blocks.speech import SpeechSynthesisBlock

    cfg = load_config()

    if sanitize_engine(cfg.get("engine")) == "local" and not model_id:
        from . import voice_store

        voice = str(cfg.get("local_voice", "") or "").strip()
        # The path is resolved here rather than in the adapter so the framework
        # block stays unaware of where this host keeps its voices.
        model_path = str(voice_store.voice_path(voice)) if voice else ""
        return SpeechSynthesisBlock(
            name="gilzabots_speech_synthesis",
            model=voice,
            route=route or "local_onnx",
            speed=sanitize_speed(cfg.get("speed")),
            model_kwargs={"model_path": model_path},
        )

    from .models_store import resolve_runtime_model_id

    catalog_id = (model_id or str(cfg.get("model", "") or "")).strip()
    runtime_model = resolve_runtime_model_id(catalog_id)

    return SpeechSynthesisBlock(
        name="gilzabots_speech_synthesis",
        model=runtime_model,
        route=route or resolve_route(catalog_id),
        voice=str(cfg.get("voice", "") or "").strip(),
        response_format=sanitize_response_format(cfg.get("response_format")),
        speed=sanitize_speed(cfg.get("speed")),
        model_kwargs=transport_kwargs(catalog_id),
    )
