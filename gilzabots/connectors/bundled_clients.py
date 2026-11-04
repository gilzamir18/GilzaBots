"""OAuth clients a build of GilzaBots ships for services that need one.

Most MCP authorization servers let GilzaBots identify itself without a prior
registration (client metadata document, dynamic registration). X does not: every
OAuth login to X goes through an app registered in the X Developer Console. For
the user, that would mean creating a developer account before connecting.

A release therefore carries GilzaBots' own X app, the way it carries the Google
Drive client (cloud/providers/google_drive.py): its client id is injected at
build time by ``scripts/embed_connector_clients.py`` into
``bundled_oauth_clients.json`` next to this module (git-ignored; the value comes
from the release pipeline's ``GILZABOTS_X_CLIENT_ID`` secret). The app is a
*public* client -- X's token endpoint accepts ``token_endpoint_auth_method:
none`` with PKCE -- so there is no secret to embed.

``GILZABOTS_<NAME>_CLIENT_ID`` in the environment overrides the bundled value,
for development and for a build made without the file. A build with neither
falls back to asking the user for the client id of their own app.
"""

from __future__ import annotations

import json
import os

BUNDLED_FILENAME = "bundled_oauth_clients.json"


def bundled_clients_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), BUNDLED_FILENAME)


def _env_name(name: str) -> str:
    return f"GILZABOTS_{name.upper().replace('-', '_')}_CLIENT_ID"


def bundled_client_id(name: str) -> str:
    """The client id this build carries for ``name`` (``"x"``), or ``""``."""
    override = os.environ.get(_env_name(name), "").strip()
    if override:
        return override
    try:
        with open(bundled_clients_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    entry = data.get(name) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("client_id") or "").strip()
