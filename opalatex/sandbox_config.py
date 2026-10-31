"""Docker sandbox configuration for OpalaTex.

Global settings persisted at ``<opalatex home>/sandbox.json``:

    {
        "enabled": false,            # whether commands execute in a Docker container
        "image": "python:3.12-slim", # Docker image used for the sandbox
        "network": "bridge"          # "bridge" (default, network enabled) or "none"
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import get_opalatex_home

DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_NETWORK = "bridge"
NETWORKS = ("bridge", "none")

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "image": DEFAULT_IMAGE,
    "network": DEFAULT_NETWORK,
}


def load_config() -> dict[str, Any]:
    """Return the current sandbox config, falling back to defaults."""
    try:
        path = Path(get_opalatex_home()) / "sandbox.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {**_DEFAULTS, **raw}
    except Exception:
        pass
    return dict(_DEFAULTS)


def save_config(config: dict[str, Any]) -> None:
    """Persist *config* over the saved settings, keeping only known keys.

    Keys missing from *config* keep their saved values.
    """
    path = Path(get_opalatex_home()) / "sandbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {**load_config(), **config}
    network = str(config.get("network", DEFAULT_NETWORK) or DEFAULT_NETWORK).strip().lower()
    if network not in NETWORKS:
        network = DEFAULT_NETWORK

    image = str(config.get("image", DEFAULT_IMAGE) or DEFAULT_IMAGE).strip()
    if not image:
        image = DEFAULT_IMAGE

    to_save = {
        "enabled": bool(config.get("enabled", False)),
        "image": image,
        "network": network,
    }
    path.write_text(
        json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_enabled() -> bool:
    return bool(load_config().get("enabled", False))


def get_sandbox_image() -> str:
    return str(load_config().get("image", DEFAULT_IMAGE) or DEFAULT_IMAGE).strip()


def get_sandbox_network() -> str:
    net = str(load_config().get("network", DEFAULT_NETWORK) or DEFAULT_NETWORK).strip().lower()
    return net if net in NETWORKS else DEFAULT_NETWORK
