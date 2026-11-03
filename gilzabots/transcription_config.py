"""Dictation configuration and the Whisper model store.

Global settings persisted at ``<gilzabots home>/transcription.json``:

    {
        "enabled": false,       # whether the chat offers a microphone button
        "model": "small",       # which Whisper size to use
        "language": "",         # "" → let Whisper detect
        "compute_type": "int8"  # CTranslate2 quantization
    }

Models are downloaded on demand into ``<gilzabots home>/whisper/<size>/`` and
never bundled, exactly as voices are (§2.13.2): they are large, and which one a
user wants depends on the language they speak and the disk they have.

**Why ``small`` is the default, measured rather than assumed.** Transcribing a
Portuguese sentence, ``tiny`` and ``base`` both produced the same substitution
error ("Este é" heard as "deixa") and ``base`` additionally dropped accents;
``small`` reproduced the sentence exactly, punctuation included. For a language
other than English the first two sizes are not merely worse, they are wrong
often enough to make dictation annoying, so the default is the first size that
actually works and the smaller ones stay available for whoever wants the disk
back.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from .config import get_gilzabots_home

_CONFIG_PATH = Path(get_gilzabots_home()) / "transcription.json"

# Sizes offered, with the on-disk cost measured for the int8 build. `.en`
# variants are deliberately not offered: this application is bilingual by
# design, and an English-only model silently mistranscribes everything else.
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")

MODEL_SIZE_MB = {
    "tiny": 78,
    "base": 148,
    "small": 486,
    "medium": 1530,
    "large-v3": 3090,
}

DEFAULT_MODEL = "small"

# int8 is the only one worth defaulting to on CPU: float16 is not faster there
# and doubles the memory.
COMPUTE_TYPES = ("int8", "int8_float32", "float32")
DEFAULT_COMPUTE_TYPE = "int8"

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "model": DEFAULT_MODEL,
    "language": "",
    "compute_type": DEFAULT_COMPUTE_TYPE,
}


class ModelStoreError(RuntimeError):
    """Raised when a transcription model cannot be downloaded or removed."""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


# ─── Persistence ─────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    try:
        if _CONFIG_PATH.exists():
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {**_DEFAULTS, **raw}
    except Exception:
        pass
    return dict(_DEFAULTS)


def save_config(config: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps({
        "enabled": bool(config.get("enabled", _DEFAULTS["enabled"])),
        "model": sanitize_model(config.get("model")),
        "language": str(config.get("language", "") or "").strip(),
        "compute_type": sanitize_compute_type(config.get("compute_type")),
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def sanitize_model(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in MODEL_SIZES else DEFAULT_MODEL


def sanitize_compute_type(value: Any) -> str:
    name = str(value or "").strip().lower()
    return name if name in COMPUTE_TYPES else DEFAULT_COMPUTE_TYPE


def is_enabled() -> bool:
    return bool(load_config().get("enabled", False))


# ─── Model store ─────────────────────────────────────────────────────────────

_progress: dict[str, dict[str, Any]] = {}
_progress_lock = threading.Lock()


def models_dir() -> Path:
    return Path(get_gilzabots_home()) / "whisper"


def model_path(size: str) -> Path:
    """Return the directory a model has, installed or not."""
    return models_dir() / sanitize_model(size)


def is_installed(size: str) -> bool:
    target = model_path(size)
    # faster-whisper needs the weights and the tokenizer; a directory holding
    # only one of them would load and then fail mid-transcription.
    return (target / "model.bin").is_file() and (
        (target / "tokenizer.json").is_file() or (target / "vocabulary.json").is_file()
    )


def installed_models() -> list[str]:
    return [size for size in MODEL_SIZES if is_installed(size)]


def download_progress() -> dict[str, dict[str, Any]]:
    with _progress_lock:
        return {key: dict(value) for key, value in _progress.items()}


def is_downloading(size: str) -> bool:
    with _progress_lock:
        return _progress.get(str(size or ""), {}).get("state") == "downloading"


def _set_progress(size: str, **fields: Any) -> None:
    with _progress_lock:
        entry = _progress.setdefault(size, {"size": size})
        entry.update(fields)


def download_model(size: str, progress: Callable[[int, int], None] | None = None) -> str:
    """Download *size* into the model store and return its path.

    Synchronous and network-bound: call it from a worker thread.
    """
    name = sanitize_model(size)
    if str(size or "").strip().lower() not in MODEL_SIZES:
        raise ModelStoreError(f"'{size}' is not an offered model size.", kind="bad_request")

    target = model_path(name)
    _set_progress(name, state="downloading", error="")
    try:
        from faster_whisper.utils import download_model as hub_download
    except ImportError as exc:
        _set_progress(name, state="error", error="faster-whisper is not installed.")
        raise ModelStoreError(
            "faster-whisper is not installed, so no model can be downloaded.",
            kind="not_configured",
        ) from exc

    target.mkdir(parents=True, exist_ok=True)
    try:
        hub_download(name, output_dir=str(target))
    except Exception as exc:  # noqa: BLE001 - classified for the caller
        # A partial model directory would pass a naive "does it exist" check and
        # then fail at load time, which reads as a bug rather than a bad
        # download.
        if not is_installed(name):
            shutil.rmtree(target, ignore_errors=True)
        _set_progress(name, state="error", error=str(exc))
        raise ModelStoreError(
            f"The '{name}' model could not be downloaded: {exc}", kind="connection"
        ) from exc

    if not is_installed(name):
        shutil.rmtree(target, ignore_errors=True)
        _set_progress(name, state="error", error="incomplete download")
        raise ModelStoreError(
            f"The '{name}' model arrived incomplete and was discarded.", kind="corrupt"
        )

    _set_progress(name, state="done", error="")
    return str(target)


def remove_model(size: str) -> None:
    name = sanitize_model(size)
    target = model_path(name)
    if not target.is_dir():
        raise ModelStoreError(f"The '{name}' model is not installed.", kind="not_found")

    # A loaded model holds its files open on Windows and would keep serving a
    # model the user just deleted everywhere else.
    try:
        from agenticblocks.blocks.transcription import clear_cache

        clear_cache()
    except Exception:
        pass

    shutil.rmtree(target)
    with _progress_lock:
        _progress.pop(name, None)


def catalog() -> list[dict[str, Any]]:
    """Return every offered model with its size and install state."""
    return [
        {
            "size": name,
            "size_mb": MODEL_SIZE_MB.get(name, 0),
            "installed": is_installed(name),
        }
        for name in MODEL_SIZES
    ]


# ─── Readiness ───────────────────────────────────────────────────────────────

def configuration_problem() -> str:
    """Return an actionable diagnostic when dictation cannot run, else ""."""
    cfg = load_config()
    if not cfg.get("enabled", False):
        return (
            "Dictation is disabled. Enable it in Settings > General > Dictation."
        )

    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return (
            "faster-whisper is not installed, so speech cannot be transcribed. "
            "Reinstall GilzaBots or run 'pip install faster-whisper'."
        )

    model = sanitize_model(cfg.get("model"))
    if not is_installed(model):
        return (
            f"The '{model}' transcription model is not downloaded yet "
            f"(about {MODEL_SIZE_MB.get(model, 0)} MB). Download it in "
            "Settings > General > Dictation; it then works offline."
        )
    return ""


def build_block():
    """Return a ``TranscriptionBlock`` wired to the configured model."""
    from agenticblocks.blocks.transcription import TranscriptionBlock

    cfg = load_config()
    model = sanitize_model(cfg.get("model"))

    return TranscriptionBlock(
        name="gilzabots_transcription",
        model=model,
        route="local_whisper",
        language=str(cfg.get("language", "") or "").strip(),
        model_kwargs={
            "model_path": str(model_path(model)),
            "compute_type": sanitize_compute_type(cfg.get("compute_type")),
        },
    )
