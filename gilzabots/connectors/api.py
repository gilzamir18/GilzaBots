"""The ``/api/connectors/*`` endpoints of the desktop server.

``handle`` returns ``(status, body)``; the server serializes the body. Changes
that alter the agent's tool list call ``refresh_agent`` so the next turn -- or,
between turns, the live orchestrator -- sees them.

    GET  /api/connectors                 every connector, with its connection state
    GET  /api/connectors/presets         the catalog of pre-configured servers
    POST /api/connectors/create          {connector}
    POST /api/connectors/update          {id, connector}
    POST /api/connectors/delete          {id}
    POST /api/connectors/connect         {id}         interactive, in the background
    POST /api/connectors/cancel          {id}         abandon a pending authorization
    POST /api/connectors/disconnect      {id, forget_credentials}
    POST /api/connectors/tools           {id, enabled_tools: [...] | null}
"""

from __future__ import annotations

from typing import Callable, Optional

from . import credentials, presets, store
from .manager import get_manager
from .oauth import client_metadata_url


def _view(connector: store.Connector) -> dict:
    view = store.public_view(connector)
    view["state"] = get_manager().state(connector.id)
    return view


def _required_id(data: dict) -> str:
    connector_id = str(data.get("id") or "").strip()
    if not connector_id:
        raise store.ConnectorError("The connector id is required.")
    return connector_id


async def handle(path: str, method: str, data: dict, query: dict,
                 refresh_agent: Optional[Callable[[], None]] = None) -> tuple[int, dict]:
    data = data if isinstance(data, dict) else {}
    refresh = refresh_agent or (lambda: None)
    manager = get_manager()
    try:
        if path == "/api/connectors" and method == "GET":
            try:
                connectors = [_view(c) for c in store.list_connectors()]
                error = ""
            except store.ConnectorStoreError as exc:
                connectors, error = [], str(exc)
            return 200, {
                "connectors": connectors,
                "error": error,
                "client_metadata_url": client_metadata_url() or "",
            }

        if path == "/api/connectors/presets" and method == "GET":
            return 200, {"presets": presets.list_presets()}

        if method != "POST":
            return 405, {"error": f"{method} is not supported here."}

        if path == "/api/connectors/create":
            connector = store.create_connector(data.get("connector") or {})
            refresh()
            return 200, {"connector": _view(connector)}

        if path == "/api/connectors/update":
            connector_id = _required_id(data)
            connector, endpoint_changed = store.update_connector(connector_id, data.get("connector") or {})
            # Another server, or another way into it: the open session and any
            # OAuth credentials were granted for the old one.
            if endpoint_changed or not connector.enabled:
                await manager.disconnect(connector_id, forget_credentials=endpoint_changed)
            refresh()
            return 200, {"connector": _view(connector), "endpoint_changed": endpoint_changed}

        if path == "/api/connectors/delete":
            connector_id = _required_id(data)
            store.get_connector(connector_id)
            await manager.disconnect(connector_id, forget_credentials=True)
            store.delete_connector(connector_id)
            refresh()
            return 200, {"success": True}

        if path == "/api/connectors/connect":
            connector_id = _required_id(data)
            if not store.get_connector(connector_id).enabled:
                raise store.ConnectorError("Turn the connector on before connecting it.")
            state = manager.start_connect(connector_id)
            return 200, {"state": state}

        if path == "/api/connectors/cancel":
            manager.cancel_authorization(_required_id(data))
            return 200, {"success": True}

        if path == "/api/connectors/disconnect":
            connector_id = _required_id(data)
            store.get_connector(connector_id)
            forget = bool(data.get("forget_credentials"))
            await manager.disconnect(connector_id, forget_credentials=forget)
            return 200, {"success": True, "has_credentials": credentials.has_tokens(connector_id)}

        if path == "/api/connectors/tools":
            connector_id = _required_id(data)
            connector, _ = store.update_connector(connector_id, {"enabled_tools": data.get("enabled_tools")})
            refresh()
            return 200, {"connector": _view(connector)}

        return 404, {"error": f"Unknown connectors endpoint {path}."}
    except store.ConnectorError as exc:
        return 400, {"error": str(exc)}
    except store.ConnectorStoreError as exc:
        return 500, {"error": str(exc)}
