"""Image generation configuration for GilzaBots.

Global settings persisted at ``<gilzabots home>/image_gen.json``:

    {
        "enabled": true,             # whether generate_image is offered at all
        "model": "",                 # catalog id of the image model to use
        "size": "1024x1024",         # default output size
        "output_dir": "figures",     # project-relative directory for the files
        "route": ""                  # "" → the model's catalog route, else override
    }

The model is a normal entry of the global model store, so its credentials and
``api_base`` come from the same place every other model's do. That is what makes
one setting cover OpenAI, Gemini/Imagen, Stability *and* a diffusion server on
localhost (LocalAI, Docker Model Runner, vLLM-Omni, or an Ollama build that
serves ``/v1/images/generations``): they differ only in the catalog entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import get_gilzabots_home

_CONFIG_PATH = Path(get_gilzabots_home()) / "image_gen.json"

DEFAULT_OUTPUT_DIR = "figures"
DEFAULT_SIZE = "1024x1024"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "",
    "size": DEFAULT_SIZE,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "route": "",
}


# ─── Persistence ──────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """Return the current image generation config, falling back to defaults."""
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
        "model": str(config.get("model", "") or "").strip(),
        "size": str(config.get("size", DEFAULT_SIZE) or DEFAULT_SIZE).strip(),
        "output_dir": _sanitize_output_dir(config.get("output_dir", DEFAULT_OUTPUT_DIR)),
        "route": str(config.get("route", "") or "").strip(),
    }
    _CONFIG_PATH.write_text(
        json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_enabled() -> bool:
    return bool(load_config().get("enabled", True))


def _sanitize_output_dir(value: Any) -> str:
    """Return a project-relative output directory.

    An absolute path or a ``..`` segment would put generated files outside the
    project, where neither the chat preview (`/api/file/raw` refuses anything
    above the project root) nor the turn checkpoint can reach them. Rejecting it
    here keeps the tool's contract honest instead of writing somewhere the user
    cannot see.
    """
    raw = str(value or DEFAULT_OUTPUT_DIR).strip().replace("\\", "/")
    parts = [p for p in raw.split("/") if p not in ("", ".", "..")]
    cleaned = "/".join(parts)
    return cleaned or DEFAULT_OUTPUT_DIR


# ─── Model / route resolution ────────────────────────────────────────────────

def resolve_route(model_id: str) -> str:
    """Return the adapter route for *model_id*.

    Priority: explicit config override > the model's catalog ``image_route`` >
    the framework default (``images_api``).
    """
    from agenticblocks.blocks.image import DEFAULT_IMAGE_ROUTE

    override = str(load_config().get("route", "") or "").strip()
    if override:
        return override

    try:
        from .models_store import get_model

        entry = get_model(model_id) or {}
        route = str(entry.get("image_route", "") or "").strip()
        if route:
            return route
    except Exception:
        pass
    return DEFAULT_IMAGE_ROUTE


def transport_kwargs(model_id: str) -> dict[str, Any]:
    """Return only the credentials/endpoint kwargs for *model_id*.

    ``get_agent_llm_kwargs`` answers with a chat request's parameters --
    temperature, num_ctx, think, stream. An images endpoint rejects those (and a
    diffusion server has no idea what num_ctx is), so the transport fields are
    whitelisted out rather than the chat ones blacklisted: a parameter added to
    the chat path later cannot leak into an image request.
    """
    from agenticblocks.blocks.image.adapters import TRANSPORT_KWARGS
    from .config import get_agent_llm_kwargs

    try:
        merged = get_agent_llm_kwargs("memgpt", model_override=model_id) or {}
    except Exception:
        merged = {}

    return {k: v for k, v in merged.items() if k in TRANSPORT_KWARGS and v not in (None, "")}


def configuration_problem() -> str:
    """Return an actionable diagnostic when generation cannot run, else ""."""
    cfg = load_config()
    if not cfg.get("enabled", True):
        return (
            "Image generation is disabled. The user can enable it in "
            "Settings > General > Image Generation."
        )
    if not str(cfg.get("model", "") or "").strip():
        return (
            "No image generation model is configured. Tell the user to open "
            "Settings > General > Image Generation and pick a model marked as "
            "image-capable in 'Edit Models' (for example an OpenAI/Gemini image "
            "model, or a local server such as LocalAI or Docker Model Runner "
            "registered with its api_base)."
        )
    return ""


def build_block(model_id: str = "", route: str = ""):
    """Return an ``ImageGenerationBlock`` wired to the configured model."""
    from agenticblocks.blocks.image import ImageGenerationBlock
    from .models_store import resolve_runtime_model_id

    cfg = load_config()
    catalog_id = (model_id or str(cfg.get("model", "") or "")).strip()
    runtime_model = resolve_runtime_model_id(catalog_id)

    return ImageGenerationBlock(
        name="gilzabots_image_generation",
        model=runtime_model,
        route=route or resolve_route(catalog_id),
        model_kwargs=transport_kwargs(catalog_id),
    )
