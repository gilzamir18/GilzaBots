"""Live connections to the connectors' servers.

A connection is opened on demand -- when the user clicks Connect, or by the first
tool call of a turn -- and kept for the life of the process, so a turn that calls
a server ten times pays the handshake once. Each connector has one lock: a
connect that is replacing a connection and a call that would open one cannot
race into two sessions.

Connections belong to the event loop that opened them (the bridge's owner task
lives there). `get_manager` hands out one manager per running loop; the desktop
server and the terminal each run a single loop, so in practice there is one.

A tool call is never retried after it was sent: a call that posts a message or
moves money must not run twice because the connection dropped while the answer
was on its way. Only a connection that was already closed before the call is
reopened first.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

from agenticblocks.tools.mcp_client import (
    MCPClientBridge,
    MCPConnectionError,
    MCPToolResult,
    MCPToolSpec,
)

from . import credentials, store
from .oauth import AuthorizationFailed, AuthorizationFlow, AuthorizationRequired, build_provider


class ConnectorUnavailable(RuntimeError):
    """The connector cannot serve a call; the message says why and what to do."""

    def __init__(self, message: str, *, needs_authorization: bool = False):
        super().__init__(message)
        self.needs_authorization = needs_authorization


def _walk(exc: BaseException):
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend(getattr(current, "exceptions", None) or ())
        stack.append(current.__cause__)
        stack.append(current.__context__)


def _find(exc: BaseException, kinds) -> Optional[BaseException]:
    return next((item for item in _walk(exc) if isinstance(item, kinds)), None)


def _client_version() -> str:
    try:
        from importlib.metadata import version

        return version("gilzabots")
    except Exception:
        return "dev"


def describe_failure(connector: store.Connector, exc: BaseException) -> ConnectorUnavailable:
    """A failure to connect or call, as a message the user (and the agent) can act on."""
    if _find(exc, AuthorizationRequired) is not None:
        return ConnectorUnavailable(
            f"The connector '{connector.name}' needs the user's authorization. Ask the user to open "
            "Connectors and click Connect on it.",
            needs_authorization=True,
        )
    failed = _find(exc, AuthorizationFailed)
    if failed is not None:
        return ConnectorUnavailable(f"Authorizing '{connector.name}' failed: {failed}")
    try:
        from mcp.client.auth import OAuthRegistrationError
    except ImportError:  # pragma: no cover - the SDK is a declared dependency
        OAuthRegistrationError = ()  # type: ignore[assignment]
    registration = _find(exc, OAuthRegistrationError) if OAuthRegistrationError else None
    if registration is not None:
        return ConnectorUnavailable(
            f"The authorization server of '{connector.name}' did not accept GilzaBots' client "
            f"registration ({registration}). If it requires a pre-registered client, register one "
            "with that service and enter its client id in the connector's settings."
        )
    if isinstance(exc, ConnectorUnavailable):
        return exc
    return ConnectorUnavailable(f"'{connector.name}': {exc}")


class ConnectionManager:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._bridges: dict[str, MCPClientBridge] = {}
        self._signatures: dict[str, tuple] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._states: dict[str, dict] = {}
        self._flows: dict[str, AuthorizationFlow] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- state ----------------------------------------------------------------

    def _lock(self, connector_id: str) -> asyncio.Lock:
        return self._locks.setdefault(connector_id, asyncio.Lock())

    def _set(self, connector_id: str, **fields: Any) -> None:
        state = self._states.setdefault(connector_id, {"status": "disconnected", "error": ""})
        state.update(fields)

    def state(self, connector_id: str) -> dict:
        state = dict(self._states.get(connector_id) or {"status": "disconnected", "error": ""})
        bridge = self._bridges.get(connector_id)
        if state.get("status") == "connected" and (bridge is None or not bridge.connected):
            state["status"] = "disconnected"
        flow = self._flows.get(connector_id)
        if flow is not None and flow.status == "waiting":
            state["status"] = "authorizing"
            state["authorization_url"] = flow.authorization_url
            state["browser_opened"] = flow.browser_opened
        state["has_credentials"] = credentials.has_tokens(connector_id)
        return state

    # -- connections ----------------------------------------------------------

    def _bridge_for(self, connector: store.Connector, flow: AuthorizationFlow) -> MCPClientBridge:
        common = dict(client_name="GilzaBots", client_version=_client_version())
        connect_timeout = float(connector.connect_timeout)
        if connector.transport == "stdio":
            return MCPClientBridge(
                os.path.expanduser(connector.command),
                connector.args,
                connector.env or None,
                cwd=os.path.expanduser(connector.cwd) if connector.cwd else None,
                connect_timeout=connect_timeout,
                **common,
            )
        headers = dict(connector.headers)
        auth = None
        timeout = connect_timeout
        if connector.auth == "bearer":
            headers["Authorization"] = f"Bearer {connector.bearer_token}"
        elif connector.auth == "oauth":
            auth = build_provider(connector, flow)
            if flow.interactive:
                # The user may take minutes on the consent page.
                timeout = flow.timeout + connect_timeout
        return MCPClientBridge(url=connector.url, headers=headers or None, auth=auth,
                               connect_timeout=timeout, **common)

    async def _close(self, connector_id: str) -> None:
        bridge = self._bridges.pop(connector_id, None)
        self._signatures.pop(connector_id, None)
        if bridge is not None:
            await bridge.disconnect()

    async def connect(self, connector_id: str, *, interactive: bool) -> list[MCPToolSpec]:
        """(Re)open the connection, list the tools and cache the catalog."""
        connector = store.get_connector(connector_id)
        async with self._lock(connector_id):
            await self._close(connector_id)
            flow = AuthorizationFlow(interactive=interactive and connector.auth == "oauth",
                                     scope=connector.oauth_scope)
            self._set(connector_id, status="connecting", error="", needs_authorization=False)
            try:
                flow.prepare()
                self._flows[connector_id] = flow
                bridge = self._bridge_for(connector, flow)
                try:
                    await bridge.open()
                    specs = await bridge.list_tools()
                except BaseException:
                    await bridge.disconnect()
                    raise
            except asyncio.CancelledError:
                self._set(connector_id, status="disconnected", error="")
                raise
            except Exception as exc:
                failure = describe_failure(connector, exc)
                if flow.status == "cancelled":
                    self._set(connector_id, status="disconnected", error="")
                else:
                    self._set(connector_id, status="error", error=str(failure),
                              needs_authorization=failure.needs_authorization)
                raise failure from exc
            finally:
                flow.close()
                self._flows.pop(connector_id, None)
            self._bridges[connector_id] = bridge
            self._signatures[connector_id] = connector.endpoint_signature()
            store.save_catalog(connector_id, [spec.to_dict() for spec in specs])
            self._set(connector_id, status="connected", error="", connected_at=time.time(),
                      tool_count=len(specs), needs_authorization=False)
            return specs

    def start_connect(self, connector_id: str) -> dict:
        """Connect in the background, interactively; the panel polls `state`."""
        store.get_connector(connector_id)  # fail fast on an unknown id
        running = self._tasks.get(connector_id)
        if running is None or running.done():
            async def _run():
                try:
                    await self.connect(connector_id, interactive=True)
                except Exception:
                    pass  # recorded in the state by `connect`

            self._set(connector_id, status="connecting", error="")
            self._tasks[connector_id] = self.loop.create_task(_run(), name=f"connect {connector_id}")
        return self.state(connector_id)

    def cancel_authorization(self, connector_id: str) -> None:
        flow = self._flows.get(connector_id)
        if flow is not None:
            flow.cancel()

    async def disconnect(self, connector_id: str, *, forget_credentials: bool = False) -> None:
        self.cancel_authorization(connector_id)
        task = self._tasks.pop(connector_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - it is being stopped
                pass
        async with self._lock(connector_id):
            await self._close(connector_id)
        if forget_credentials:
            credentials.forget(connector_id)
        self._set(connector_id, status="disconnected", error="", needs_authorization=False)

    async def shutdown(self) -> None:
        for connector_id in list(set(self._bridges) | set(self._tasks)):
            try:
                await self.disconnect(connector_id)
            except Exception:
                pass

    # -- calls ------------------------------------------------------------------

    async def call_tool(self, connector_id: str, remote_name: str, arguments: dict) -> MCPToolResult:
        connector = store.get_connector(connector_id)
        if not connector.enabled:
            raise ConnectorUnavailable(f"The connector '{connector.name}' is turned off.")
        bridge = self._bridges.get(connector_id)
        if (bridge is None or not bridge.connected
                or self._signatures.get(connector_id) != connector.endpoint_signature()):
            await self.connect(connector_id, interactive=False)
            bridge = self._bridges[connector_id]
        try:
            return await bridge.call_tool(remote_name, arguments)
        except MCPConnectionError as exc:
            failure = describe_failure(connector, exc)
            self._set(connector_id, status="error", error=str(failure),
                      needs_authorization=failure.needs_authorization)
            raise failure from exc


_MANAGERS: dict[int, ConnectionManager] = {}


def get_manager() -> ConnectionManager:
    """The manager of the running event loop."""
    loop = asyncio.get_running_loop()
    manager = _MANAGERS.get(id(loop))
    if manager is None or manager.loop is not loop:
        # A closed loop's id can be reused; its manager's connections are gone.
        for key in [key for key, value in _MANAGERS.items() if value.loop.is_closed()]:
            _MANAGERS.pop(key, None)
        manager = ConnectionManager(loop)
        _MANAGERS[id(loop)] = manager
    return manager
