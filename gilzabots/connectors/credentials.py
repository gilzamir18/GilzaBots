"""OAuth credentials of a connector: ``<gilzabots home>/mcp/oauth/<id>.json``.

Implements the MCP SDK's ``TokenStorage`` protocol. Besides the token and the
client registration, the file records when the access token expires as an
absolute time. The SDK keeps only the relative ``expires_in`` it received, so
after a restart it would consider a stored, long-expired token valid, send it,
get a 401 and start a full interactive authorization -- instead of using the
refresh token it holds. `stored_expiry` lets the caller restore the expiry
(`oauth.build_provider`).

It also records the authorization server the SDK discovered (protected resource
metadata, authorization server URL and metadata). The SDK discovers it only
after a 401, so after a restart its refresh of an expired token went to its
guess, ``<origin>/token`` -- a 404 at X, whose endpoint is ``/2/oauth2/token`` --
dropped the tokens and demanded a new browser authorization although it held a
valid refresh token. `stored_discovery` lets the caller restore it.

The file holds bearer credentials, so it is owner-only and written atomically.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from ..atomic_file import write_text_atomic
from ..config import get_gilzabots_home

_LOCK = threading.Lock()


def credentials_dir() -> str:
    return os.path.join(get_gilzabots_home(), "mcp", "oauth")


def credentials_path(connector_id: str) -> str:
    return os.path.join(credentials_dir(), f"{connector_id}.json")


def _read(connector_id: str) -> dict:
    path = credentials_path(connector_id)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Unreadable credentials are equivalent to none: the next connection
        # asks the user to authorize again, which is the correct recovery.
        return {}


def _write(connector_id: str, data: dict) -> None:
    directory = credentials_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    write_text_atomic(credentials_path(connector_id), json.dumps(data, indent=2), mode=0o600)


def forget(connector_id: str) -> None:
    """Delete the connector's tokens and client registration."""
    with _LOCK:
        try:
            os.remove(credentials_path(connector_id))
        except FileNotFoundError:
            pass


def has_tokens(connector_id: str) -> bool:
    return bool(_read(connector_id).get("tokens"))


class FileTokenStorage:
    """``mcp.client.auth.TokenStorage`` over the connector's credentials file.

    ``preset_client`` is a client registered in advance by the user; when given it
    is returned as the client information, so the SDK neither registers a client
    nor uses a client metadata document.
    """

    def __init__(self, connector_id: str, preset_client: Any = None):
        self.connector_id = connector_id
        self.preset_client = preset_client
        #: The provider's ``OAuthContext``, bound by `oauth.build_provider`: its
        #: discovered metadata is saved with every token.
        self.context: Any = None

    def stored_expiry(self) -> float | None:
        value = _read(self.connector_id).get("expires_at")
        return float(value) if isinstance(value, (int, float)) else None

    def stored_discovery(self) -> dict:
        """What `_discovery` recorded: ``auth_server_url`` and the two metadata documents."""
        value = _read(self.connector_id).get("discovery")
        return value if isinstance(value, dict) else {}

    def _discovery(self) -> dict | None:
        context = self.context
        if context is None or context.oauth_metadata is None:
            return None
        found: dict = {"oauth_metadata": context.oauth_metadata.model_dump(mode="json", exclude_none=True)}
        if context.protected_resource_metadata is not None:
            found["protected_resource_metadata"] = context.protected_resource_metadata.model_dump(
                mode="json", exclude_none=True)
        if context.auth_server_url:
            found["auth_server_url"] = context.auth_server_url
        return found

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = _read(self.connector_id).get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:
            return None

    async def set_tokens(self, tokens) -> None:
        with _LOCK:
            data = _read(self.connector_id)
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            data["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in else None
            data["updated_at"] = time.time()
            discovery = self._discovery()
            if discovery is not None:
                data["discovery"] = discovery
            _write(self.connector_id, data)

    async def get_client_info(self):
        if self.preset_client is not None:
            return self.preset_client
        from mcp.shared.auth import OAuthClientInformationFull

        raw = _read(self.connector_id).get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, client_info) -> None:
        if self.preset_client is not None:
            return
        with _LOCK:
            data = _read(self.connector_id)
            data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
            _write(self.connector_id, data)
