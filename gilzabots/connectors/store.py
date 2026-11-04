"""The connector registry: ``<gilzabots home>/mcp_connectors.json``.

One JSON document holds every connector. It can carry secrets (a bearer token,
header values, a stdio server's environment), so it is written atomically with
owner-only permissions, and `public_view` -- what the API returns -- replaces
every secret value with a flag saying whether it is set. The front-end never
receives a secret back; an edit that omits a value keeps the stored one.

A corrupt file is an error, not an empty registry: treating it as empty and then
saving would overwrite the user's connectors with nothing.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ..atomic_file import write_text_atomic
from ..config import get_gilzabots_home

STORE_VERSION = 1

TRANSPORTS = ("http", "stdio")
AUTH_TYPES = ("none", "bearer", "oauth")
#: ``always``: every call asks the user, whatever the chat's mode (the default,
#: since these tools reach outside the machine). ``mode``: the chat's mode decides,
#: exactly as for the native tools (``auto`` runs, ``edit`` asks, ``plan`` blocks).
APPROVAL_POLICIES = ("always", "mode")

#: Short, because it becomes part of every tool name (``mcp__<id>__<tool>``) and
#: function-calling APIs cap names at 64 characters.
CONNECTOR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# RFC 6749 §3.3 scope-token characters.
_SCOPE_TOKEN_RE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_MAX_NAME = 80
DEFAULT_CONNECT_TIMEOUT = 60
MIN_CONNECT_TIMEOUT, MAX_CONNECT_TIMEOUT = 5, 900


class ConnectorError(ValueError):
    """An invalid connector definition or operation."""


class ConnectorStoreError(RuntimeError):
    """The registry file cannot be read."""


@dataclass
class Connector:
    id: str
    name: str
    transport: str = "http"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    auth: str = "none"
    bearer_token: str = ""
    #: A client registered in advance with the server's authorization server,
    #: for servers that support neither dynamic registration nor client metadata
    #: documents. Empty: GilzaBots registers itself.
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    #: Space-separated scopes to request instead of everything the server offers
    #: (an authorization server may advertise far more than the agent needs: X
    #: offers billing and direct messages). Empty: the server's own selection.
    oauth_scope: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    enabled: bool = True
    approval: str = "always"
    #: Seconds to wait for the server to start and answer the handshake. A bridge
    #: that logs the user in during startup needs minutes.
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT
    #: ``None``: every tool the server lists. A list: only those (remote names).
    enabled_tools: list[str] | None = None
    #: The server's catalog as last listed, so an agent can be built without
    #: opening a connection (`agenticblocks.tools.mcp_client.MCPToolSpec.to_dict`).
    tools: list[dict] = field(default_factory=list)
    tools_refreshed_at: float | None = None
    created_at: float = 0.0
    #: The catalog entry this connector was created from (`presets.py`), if any.
    #: A preset may supply the OAuth client GilzaBots ships for its service.
    preset: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Connector":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})

    def endpoint_signature(self) -> tuple:
        """What identifies the server; changing it invalidates credentials and the catalog."""
        return (self.transport, self.url, self.command, tuple(self.args), self.auth,
                self.oauth_client_id, self.oauth_scope)


def connectors_path() -> str:
    return os.path.join(get_gilzabots_home(), "mcp_connectors.json")


_LOCK = threading.RLock()


# ─── Validation ──────────────────────────────────────────────────────────────

def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConnectorError(f"The server URL must be an http(s) URL, got '{url}'.")
    if parsed.username or parsed.password:
        raise ConnectorError("Put credentials in the authentication settings, not in the URL.")
    # MCP's transport rules: anything leaving the machine goes over TLS.
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise ConnectorError(
            "A server on another machine must use https://. Plain http:// is only accepted for "
            "localhost / 127.0.0.1."
        )


def validate(connector: Connector) -> None:
    """Raise `ConnectorError` unless ``connector`` is complete and consistent."""
    if not CONNECTOR_ID_RE.match(connector.id or ""):
        raise ConnectorError(
            "The connector id must be 1-24 characters: lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit."
        )
    if not connector.name.strip() or len(connector.name) > _MAX_NAME:
        raise ConnectorError(f"The connector name must be 1-{_MAX_NAME} characters.")
    if connector.transport not in TRANSPORTS:
        raise ConnectorError(f"Unknown transport '{connector.transport}'. Use one of: {', '.join(TRANSPORTS)}.")
    if connector.auth not in AUTH_TYPES:
        raise ConnectorError(f"Unknown authentication '{connector.auth}'. Use one of: {', '.join(AUTH_TYPES)}.")
    if connector.approval not in APPROVAL_POLICIES:
        raise ConnectorError(
            f"Unknown approval policy '{connector.approval}'. Use one of: {', '.join(APPROVAL_POLICIES)}."
        )
    if connector.transport == "http":
        _validate_url(connector.url)
        if connector.command or connector.args or connector.env or connector.cwd:
            raise ConnectorError("Command, arguments, environment and folder only apply to a local (stdio) server.")
        for name in connector.headers:
            if not _HEADER_NAME_RE.match(name):
                raise ConnectorError(f"'{name}' is not a valid HTTP header name.")
            if name.lower() == "authorization" and connector.auth != "none":
                raise ConnectorError(
                    "The Authorization header is set by the chosen authentication; remove it from the headers."
                )
    else:
        if not connector.command.strip():
            raise ConnectorError("A local (stdio) server needs the command that starts it.")
        if connector.url or connector.headers:
            raise ConnectorError("URL and headers only apply to an HTTP server.")
        if connector.auth != "none":
            raise ConnectorError(
                "A local (stdio) server authenticates through its own environment variables; "
                "choose no authentication."
            )
        for key in connector.env:
            if not _ENV_KEY_RE.match(key):
                raise ConnectorError(f"'{key}' is not a valid environment variable name.")
        if connector.cwd and not os.path.isdir(os.path.expanduser(connector.cwd)):
            raise ConnectorError(f"The working folder '{connector.cwd}' does not exist.")
    if not (isinstance(connector.connect_timeout, int)
            and MIN_CONNECT_TIMEOUT <= connector.connect_timeout <= MAX_CONNECT_TIMEOUT):
        raise ConnectorError(
            f"The connection timeout must be {MIN_CONNECT_TIMEOUT}-{MAX_CONNECT_TIMEOUT} seconds."
        )
    if connector.auth == "bearer" and not connector.bearer_token:
        raise ConnectorError("Bearer authentication needs a token.")
    if connector.oauth_scope:
        if connector.auth != "oauth":
            raise ConnectorError("OAuth scopes only apply to OAuth authentication.")
        for token in connector.oauth_scope.split():
            if not _SCOPE_TOKEN_RE.match(token):
                raise ConnectorError(f"'{token}' is not a valid OAuth scope.")
    if connector.auth != "oauth" and (connector.oauth_client_id or connector.oauth_client_secret):
        raise ConnectorError("An OAuth client id only applies to OAuth authentication.")
    if connector.oauth_client_secret and not connector.oauth_client_id:
        raise ConnectorError("An OAuth client secret needs its client id.")
    if connector.enabled_tools is not None and not all(isinstance(n, str) for n in connector.enabled_tools):
        raise ConnectorError("The enabled tools must be a list of tool names.")


# ─── Persistence ─────────────────────────────────────────────────────────────

def _read_all() -> list[Connector]:
    path = connectors_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        items = raw.get("connectors", []) if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise ValueError("missing 'connectors' list")
        return [Connector.from_dict(item) for item in items if isinstance(item, dict)]
    except (OSError, ValueError, TypeError) as exc:
        raise ConnectorStoreError(
            f"The connector registry {path} cannot be read ({exc}). Fix or remove the file; "
            "it was left untouched."
        ) from exc


def _write_all(connectors: Iterable[Connector]) -> None:
    path = connectors_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = {"version": STORE_VERSION, "connectors": [c.to_dict() for c in connectors]}
    write_text_atomic(path, json.dumps(document, indent=2, ensure_ascii=False), mode=0o600)


def list_connectors() -> list[Connector]:
    with _LOCK:
        return _read_all()


def get_connector(connector_id: str) -> Connector:
    for connector in list_connectors():
        if connector.id == connector_id:
            return connector
    raise ConnectorError(f"No connector with id '{connector_id}'.")


def _apply_secret_map(existing: dict[str, str], entries: Any, label: str) -> dict[str, str]:
    """Merge ``[{name, value?}]`` from the API over ``existing``.

    A present value replaces; an absent value keeps the stored one (the API
    never sends secrets back, so the form cannot resend them); a name left out
    is removed.
    """
    if entries is None:
        return dict(existing)
    if isinstance(entries, dict):
        entries = [{"name": k, "value": v} for k, v in entries.items()]
    if not isinstance(entries, list):
        raise ConnectorError(f"{label} must be a list of name/value pairs.")
    merged: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConnectorError(f"{label} must be a list of name/value pairs.")
        name = str(entry.get("name") or entry.get("key") or "").strip()
        if not name:
            continue
        if name in merged:
            raise ConnectorError(f"'{name}' appears twice in {label}.")
        value = entry.get("value")
        if value is None:
            if name not in existing:
                raise ConnectorError(f"{label}: '{name}' needs a value.")
            merged[name] = existing[name]
        else:
            merged[name] = str(value)
    return merged


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise ConnectorError(f"{label} must be a list.")
    return [str(item) for item in value]


def _apply_fields(connector: Connector, data: dict) -> Connector:
    for key in ("name", "transport", "url", "auth", "command", "cwd", "approval", "oauth_client_id", "preset"):
        if key in data and data[key] is not None:
            setattr(connector, key, str(data[key]).strip())
    if "enabled" in data:
        connector.enabled = bool(data["enabled"])
    if data.get("connect_timeout") not in (None, ""):
        try:
            connector.connect_timeout = int(data["connect_timeout"])
        except (TypeError, ValueError):
            raise ConnectorError("The connection timeout must be a whole number of seconds.") from None
    if "args" in data:
        connector.args = _string_list(data["args"], "The arguments")
    if "headers" in data:
        connector.headers = _apply_secret_map(connector.headers, data["headers"], "Headers")
    if "env" in data:
        connector.env = _apply_secret_map(connector.env, data["env"], "Environment variables")
    if data.get("bearer_token"):
        connector.bearer_token = str(data["bearer_token"]).strip()
    if data.get("clear_bearer_token"):
        connector.bearer_token = ""
    if "oauth_scope" in data and data["oauth_scope"] is not None:
        connector.oauth_scope = " ".join(str(data["oauth_scope"]).split())
    if data.get("oauth_client_secret"):
        connector.oauth_client_secret = str(data["oauth_client_secret"]).strip()
    if data.get("clear_oauth_client_secret"):
        connector.oauth_client_secret = ""
    if "enabled_tools" in data:
        names = data["enabled_tools"]
        connector.enabled_tools = None if names is None else _string_list(names, "The enabled tools")
    # A secret kept for an authentication that is no longer chosen would still be
    # stored, and silently come back if the choice is switched again.
    if connector.auth != "bearer":
        connector.bearer_token = ""
    if connector.auth != "oauth":
        connector.oauth_client_id = ""
        connector.oauth_client_secret = ""
        connector.oauth_scope = ""
    return connector


def create_connector(data: dict) -> Connector:
    with _LOCK:
        connectors = _read_all()
        connector = _apply_fields(Connector(id=str(data.get("id") or "").strip(), name=""), data)
        connector.created_at = time.time()
        validate(connector)
        if any(c.id == connector.id for c in connectors):
            raise ConnectorError(f"A connector with id '{connector.id}' already exists.")
        connectors.append(connector)
        _write_all(connectors)
        return connector


def update_connector(connector_id: str, data: dict) -> tuple[Connector, bool]:
    """Apply ``data``; returns the connector and whether its endpoint changed.

    An endpoint change (transport, URL, command, authentication) drops the cached
    catalog: it described a different server.
    """
    with _LOCK:
        connectors = _read_all()
        for index, current in enumerate(connectors):
            if current.id != connector_id:
                continue
            before = current.endpoint_signature()
            updated = _apply_fields(Connector.from_dict(current.to_dict()), data)
            validate(updated)
            changed = updated.endpoint_signature() != before
            if changed:
                updated.tools = []
                updated.tools_refreshed_at = None
                updated.enabled_tools = None
            connectors[index] = updated
            _write_all(connectors)
            return updated, changed
    raise ConnectorError(f"No connector with id '{connector_id}'.")


def delete_connector(connector_id: str) -> None:
    with _LOCK:
        connectors = _read_all()
        remaining = [c for c in connectors if c.id != connector_id]
        if len(remaining) == len(connectors):
            raise ConnectorError(f"No connector with id '{connector_id}'.")
        _write_all(remaining)


def save_catalog(connector_id: str, tools: list[dict]) -> Connector:
    """Record the tools the server listed."""
    with _LOCK:
        connectors = _read_all()
        for connector in connectors:
            if connector.id == connector_id:
                connector.tools = list(tools)
                connector.tools_refreshed_at = time.time()
                _write_all(connectors)
                return connector
    raise ConnectorError(f"No connector with id '{connector_id}'.")


def active_tool_specs(connector: Connector) -> list[dict]:
    """The cached tools the agent may hold, after the user's selection."""
    if connector.enabled_tools is None:
        return list(connector.tools)
    allowed = set(connector.enabled_tools)
    return [tool for tool in connector.tools if tool.get("name") in allowed]


def public_view(connector: Connector) -> dict:
    """The connector as the API returns it: no secret values, only whether each is set."""
    data = connector.to_dict()
    data.pop("bearer_token", None)
    data.pop("oauth_client_secret", None)
    data["has_bearer_token"] = bool(connector.bearer_token)
    data["has_oauth_client_secret"] = bool(connector.oauth_client_secret)
    data["headers"] = [{"name": name, "set": bool(value)} for name, value in connector.headers.items()]
    data["env"] = [{"name": name, "set": bool(value)} for name, value in connector.env.items()]
    data["active_tool_count"] = len(active_tool_specs(connector))
    return data
