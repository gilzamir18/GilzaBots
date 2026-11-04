"""MCP servers as agenticblocks tools.

`MCPClientBridge` connects to a Model Context Protocol server and exposes each of
its tools as an `MCPProxyBlock`, a Block an agent can hold like any native tool.
Three kinds of server are supported:

* a local command spoken to over stdio (``MCPClientBridge(command, args, env)``);
* a remote or local Streamable HTTP endpoint (``MCPClientBridge(url=...)``), with
  optional static ``headers`` and any ``httpx2.Auth`` -- for example the MCP SDK's
  ``OAuthClientProvider``, which implements the protocol's authorization flow;
* an in-process ``Server``/``MCPServer`` or a custom transport (``server=...``),
  mostly for tests.

Design notes
------------

*One task owns the connection.* The SDK's transports are built on anyio task
groups, whose cancel scopes must be entered and exited by the same task. A bridge
connected inside one agent turn and closed later from another task (application
shutdown, a settings change) would fail with "exit cancel scope in a different
task". The bridge therefore runs the whole client context inside a dedicated
owner task; other tasks only send requests through the established session.

*Proxies do not need a live connection.* `proxy_blocks` builds Blocks from a list
of `MCPToolSpec` and any object with ``call_tool`` (`MCPToolCaller`). An
application can cache a server's catalog and connect lazily on the first call
instead of opening every connection before an agent can be built.

*Results stay honest and bounded.* Text content is passed through. Binary content
(images, audio, blobs) is described rather than inlined as base64 -- an agent's
tool history is text, and a base64 image would consume the context without being
seen. A result the server marks ``isError`` raises `MCPToolError`, so the agent
loop records a failed call instead of treating an error message as success.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict

from agenticblocks.core.block import Block

#: Longest tool name the major function-calling APIs accept (OpenAI, Anthropic, Gemini).
MAX_TOOL_NAME_LENGTH = 64
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")

#: Connect/write timeout and the read timeout of a held response stream, matching
#: the MCP SDK's defaults for its own HTTP client.
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_HTTP_READ_TIMEOUT = 300.0

_TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
_MAX_LIST_PAGES = 100


class MCPError(RuntimeError):
    """Base class for errors raised by the MCP bridge."""


class MCPConnectionError(MCPError):
    """The server could not be reached, or the connection is not open."""


class MCPToolError(MCPError):
    """The server ran the tool and reported a failure (``isError``)."""

    def __init__(self, tool: str, message: str):
        super().__init__(f"MCP tool '{tool}' failed: {message}")
        self.tool = tool
        self.detail = message


@dataclass(frozen=True)
class MCPToolSpec:
    """A tool as a server lists it, in a form that can be cached as JSON."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    #: The server's behaviour hints, with their wire names (``readOnlyHint``,
    #: ``destructiveHint``, ``idempotentHint``, ``openWorldHint``, ``title``).
    #: They are claims made by the server, not guarantees.
    annotations: dict = field(default_factory=dict)
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "annotations": dict(self.annotations),
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MCPToolSpec":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("an MCP tool spec needs a name")
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            input_schema=dict(data.get("input_schema") or {}),
            annotations=dict(data.get("annotations") or {}),
            title=str(data.get("title") or ""),
        )


@dataclass
class MCPToolResult:
    """A tool call's result: each content item rendered as text."""

    content: list[str] = field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False

    def text(self) -> str:
        if self.content:
            return "\n".join(self.content)
        if self.structured_content is not None:
            return json.dumps(self.structured_content, ensure_ascii=False, default=str)
        return ""


class MCPToolCaller(Protocol):
    """Anything that can run a server tool by its remote name."""

    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult: ...


# ─── Names, schemas and content ──────────────────────────────────────────────

def exposed_tool_name(remote_name: str, prefix: str = "") -> str:
    """The name an agent sees for a server tool.

    Function-calling APIs accept ``[A-Za-z0-9_-]{1,64}``; MCP tool names may hold
    dots, slashes or be longer. Invalid characters become ``_`` and an overlong
    name is cut and suffixed with a hash of the original, so two long names that
    share a prefix stay distinct.
    """
    base = _INVALID_NAME_CHARS.sub("_", str(remote_name)) or "tool"
    name = f"{prefix}{base}"
    if len(name) > MAX_TOOL_NAME_LENGTH:
        digest = hashlib.sha1(str(remote_name).encode("utf-8")).hexdigest()[:8]
        name = f"{name[:MAX_TOOL_NAME_LENGTH - 9]}_{digest}"
    return name


def normalize_input_schema(schema: Any) -> dict:
    """A tool's input schema as an object schema function-calling APIs accept."""
    normalized = dict(schema) if isinstance(schema, Mapping) else {}
    normalized.setdefault("type", "object")
    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), Mapping):
        normalized["properties"] = {}
    return normalized


def _decoded_size(data: Any) -> int:
    if not isinstance(data, str):
        return 0
    try:
        return len(base64.b64decode(data, validate=False))
    except Exception:
        return len(data) * 3 // 4


def render_content_item(item: Any) -> str:
    """One MCP content item as text an agent can read."""
    kind = getattr(item, "type", "")
    if kind == "text":
        return str(getattr(item, "text", ""))
    if kind in ("image", "audio"):
        mime = getattr(item, "mime_type", "") or "unknown type"
        size = _decoded_size(getattr(item, "data", ""))
        return f"[{kind} content omitted: {mime}, {size} bytes]"
    if kind == "resource":
        resource = getattr(item, "resource", None)
        uri = str(getattr(resource, "uri", "") or "")
        text = getattr(resource, "text", None)
        if text is not None:
            return f"[resource {uri}]\n{text}" if uri else str(text)
        mime = getattr(resource, "mime_type", "") or "unknown type"
        size = _decoded_size(getattr(resource, "blob", ""))
        return f"[binary resource omitted: {uri} ({mime}, {size} bytes)]"
    if kind == "resource_link":
        name = getattr(item, "name", "") or ""
        uri = str(getattr(item, "uri", "") or "")
        description = getattr(item, "description", "") or ""
        line = f"[resource link: {name} <{uri}>]" if name else f"[resource link: <{uri}>]"
        return f"{line} {description}".rstrip()
    return f"[unsupported '{kind or type(item).__name__}' content omitted]"


def convert_call_result(result: Any) -> MCPToolResult:
    """An SDK ``CallToolResult`` as an `MCPToolResult`."""
    items = getattr(result, "content", None) or []
    return MCPToolResult(
        content=[render_content_item(item) for item in items],
        structured_content=getattr(result, "structured_content", None),
        is_error=bool(getattr(result, "is_error", False)),
    )


def spec_from_tool(tool: Any) -> MCPToolSpec:
    """An SDK ``Tool`` as an `MCPToolSpec`."""
    schema = getattr(tool, "input_schema", None)
    if schema is None:  # SDK 1.x spelling
        schema = getattr(tool, "inputSchema", None)
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        annotation_dict: dict = {}
    elif hasattr(annotations, "model_dump"):
        annotation_dict = annotations.model_dump(by_alias=True, exclude_none=True)
    else:
        annotation_dict = dict(annotations)
    return MCPToolSpec(
        name=str(tool.name),
        description=str(getattr(tool, "description", "") or ""),
        input_schema=normalize_input_schema(schema),
        annotations=annotation_dict,
        title=str(getattr(tool, "title", "") or annotation_dict.get("title") or ""),
    )


def _root_cause(exc: BaseException) -> BaseException:
    """The first real error inside (possibly nested) exception groups."""
    # Duck-typed: BaseExceptionGroup is builtin only from Python 3.11, and anyio
    # raises the `exceptiongroup` backport's class on 3.10.
    while isinstance(getattr(exc, "exceptions", None), (list, tuple)) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


# ─── Proxy blocks ────────────────────────────────────────────────────────────

class MCPProxyInput(BaseModel):
    # The arguments the model generated; the server validates them against the
    # tool's own schema.
    model_config = ConfigDict(extra="allow")


class MCPProxyOutput(BaseModel):
    result: Any
    structured_content: Any = None


class MCPProxyBlock(Block[MCPProxyInput, MCPProxyOutput]):
    """A server tool as a Block. Calls go through ``caller``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    is_mcp_proxy: bool = True
    raw_mcp_schema: dict = {}
    #: The tool's name on the server; ``name`` is what the agent sees.
    remote_name: str = ""
    annotations: dict = {}
    caller: Any = None

    async def run(self, input: MCPProxyInput) -> MCPProxyOutput:
        if self.caller is None:
            raise MCPConnectionError(f"MCP tool '{self.name}' has no connection to call through")
        args = {key: getattr(input, key) for key in input.model_fields_set | set(input.model_extra or {})}
        remote = self.remote_name or self.name
        result = await self.caller.call_tool(remote, args)
        if result.is_error:
            raise MCPToolError(remote, result.text() or "the server reported an error without details")
        return MCPProxyOutput(result=list(result.content), structured_content=result.structured_content)


def proxy_blocks(specs: Sequence[MCPToolSpec], caller: Any, *, name_prefix: str = "") -> list[MCPProxyBlock]:
    """Blocks for ``specs`` that call through ``caller``, with unique agent-facing names."""
    blocks: list[MCPProxyBlock] = []
    used: set[str] = set()
    for spec in specs:
        name = exposed_tool_name(spec.name, name_prefix)
        if name in used:
            digest = hashlib.sha1(spec.name.encode("utf-8")).hexdigest()[:8]
            name = f"{name[:MAX_TOOL_NAME_LENGTH - 9]}_{digest}"
        used.add(name)
        blocks.append(MCPProxyBlock(
            name=name,
            description=spec.description or spec.title or f"MCP tool '{spec.name}'",
            raw_mcp_schema=normalize_input_schema(spec.input_schema),
            remote_name=spec.name,
            annotations=dict(spec.annotations),
            caller=caller,
        ))
    return blocks


# ─── Connection ──────────────────────────────────────────────────────────────

class MCPClientBridge:
    """A connection to one MCP server, exposing its tools as Blocks.

    Exactly one of ``command`` (stdio), ``url`` (Streamable HTTP) or ``server``
    (in-process server or custom transport) must be given.
    """

    def __init__(
        self,
        command: str | None = None,
        args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        *,
        cwd: str | None = None,
        url: str | None = None,
        headers: Mapping[str, str] | None = None,
        auth: Any = None,
        server: Any = None,
        name_prefix: str = "",
        client_name: str = "agenticblocks",
        client_version: str = "1.0",
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        http_read_timeout: float = DEFAULT_HTTP_READ_TIMEOUT,
        read_timeout_seconds: float | None = None,
        connect_timeout: float | None = 60.0,
        on_tools_changed: Any = None,
    ):
        given = [value is not None for value in (command, url, server)]
        if sum(given) != 1:
            raise ValueError("MCPClientBridge needs exactly one of command=, url= or server=")
        if command is None and (args or env or cwd):
            raise ValueError("args, env and cwd only apply to a stdio server (command=)")
        if url is None and (headers or auth is not None):
            raise ValueError("headers and auth only apply to an HTTP server (url=)")
        self.command = command
        self.args = list(args or [])
        self.env = dict(env) if env else None
        self.cwd = cwd
        self.url = url
        self.headers = dict(headers) if headers else None
        self.auth = auth
        self.server = server
        self.name_prefix = name_prefix
        self.client_name = client_name
        self.client_version = client_version
        self.http_timeout = http_timeout
        self.http_read_timeout = http_read_timeout
        self.read_timeout_seconds = read_timeout_seconds
        self.connect_timeout = connect_timeout
        self.on_tools_changed = on_tools_changed
        #: Set when the server announced that its tool list changed.
        self.tools_changed = False
        self._client: Any = None
        self._owner: asyncio.Task | None = None
        self._closing: asyncio.Event | None = None
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None and self._owner is not None and not self._owner.done()

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The event loop the connection lives on."""
        return self._loop

    def _describe(self) -> str:
        if self.command is not None:
            return f"'{self.command}'"
        if self.url is not None:
            return self.url
        return "the in-process server"

    def _client_context(self):
        from mcp import Client, StdioServerParameters
        from mcp_types import Implementation

        options = dict(
            client_info=Implementation(name=self.client_name, version=self.client_version),
            read_timeout_seconds=self.read_timeout_seconds,
            message_handler=self._handle_message,
        )
        if self.command is not None:
            params = StdioServerParameters(command=self.command, args=self.args, env=self.env, cwd=self.cwd)
            return None, Client(params, **options)
        if self.url is not None:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            http_client = httpx2.AsyncClient(
                headers=self.headers,
                auth=self.auth,
                timeout=httpx2.Timeout(self.http_timeout, read=self.http_read_timeout),
            )
            return http_client, Client(streamable_http_client(self.url, http_client=http_client), **options)
        return None, Client(self.server, **options)

    async def _handle_message(self, message: Any) -> None:
        inner = getattr(message, "root", message)
        if getattr(inner, "method", None) == _TOOLS_LIST_CHANGED:
            self.tools_changed = True
            if self.on_tools_changed is not None:
                outcome = self.on_tools_changed()
                if asyncio.iscoroutine(outcome):
                    await outcome

    async def _own_connection(self, ready: asyncio.Future) -> None:
        try:
            http_client, client = self._client_context()
            if http_client is not None:
                async with http_client:
                    async with client:
                        await self._hold(client, ready)
            else:
                async with client:
                    await self._hold(client, ready)
        except BaseException as exc:  # noqa: BLE001 - reported to whoever waits
            cause = _root_cause(exc)
            if isinstance(cause, asyncio.CancelledError):
                if not ready.done():
                    ready.cancel()
                return
            if not ready.done():
                ready.set_exception(cause)
            else:
                self._failure = cause
        finally:
            self._client = None

    async def _hold(self, client: Any, ready: asyncio.Future) -> None:
        self._client = client
        if not ready.done():
            ready.set_result(None)
        assert self._closing is not None
        await self._closing.wait()

    async def open(self) -> None:
        """Open the connection without listing tools."""
        if self.connected:
            return
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._failure = None
        self._closing = asyncio.Event()
        ready: asyncio.Future = loop.create_future()
        self._owner = loop.create_task(self._own_connection(ready), name=f"mcp-connection {self._describe()}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout=self.connect_timeout)
        except asyncio.TimeoutError:
            await self.disconnect()
            raise MCPConnectionError(
                f"Timed out after {self.connect_timeout:g}s connecting to the MCP server {self._describe()}."
            ) from None
        except MCPError:
            await self.disconnect()
            raise
        except Exception as exc:
            await self.disconnect()
            if self.command is not None and isinstance(exc, OSError):
                raise MCPConnectionError(
                    f"Failed to start the MCP server {self._describe()}. Make sure it is installed. Error: {exc}"
                ) from exc
            raise MCPConnectionError(f"Could not connect to the MCP server {self._describe()}: {exc}") from exc

    async def connect(self) -> list[MCPProxyBlock]:
        """Open the connection and return the server's tools as Blocks."""
        await self.open()
        return proxy_blocks(await self.list_tools(), self, name_prefix=self.name_prefix)

    async def disconnect(self) -> None:
        """Close the connection. Safe to call when it is not open."""
        owner = self._owner
        if owner is None:
            return
        if self._closing is not None:
            self._closing.set()
        if not owner.done():
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                owner.cancel()
                try:
                    await owner
                except BaseException:  # noqa: BLE001 - shutting down regardless
                    pass
        self._owner = None
        self._client = None

    def _require_client(self) -> Any:
        if not self.connected:
            reason = f": {self._failure}" if self._failure is not None else ""
            raise MCPConnectionError(f"The connection to the MCP server {self._describe()} is not open{reason}")
        if self._loop is not asyncio.get_running_loop():
            raise MCPConnectionError(
                f"The connection to the MCP server {self._describe()} belongs to another event loop"
            )
        return self._client

    # -- requests -----------------------------------------------------------

    async def list_tools(self) -> list[MCPToolSpec]:
        """Every tool the server lists, across all pages."""
        client = self._require_client()
        specs: list[MCPToolSpec] = []
        cursor: str | None = None
        try:
            for _ in range(_MAX_LIST_PAGES):
                page = await client.list_tools(cursor=cursor, cache_mode="refresh")
                specs.extend(spec_from_tool(tool) for tool in page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        except MCPError:
            raise
        except Exception as exc:
            raise MCPConnectionError(
                f"Listing the tools of {self._describe()} failed: {_root_cause(exc)}"
            ) from exc
        self.tools_changed = False
        return specs

    async def call_tool(self, name: str, arguments: dict | None = None) -> MCPToolResult:
        """Run one tool on the server."""
        client = self._require_client()
        try:
            result = await client.call_tool(name, dict(arguments or {}))
        except Exception as exc:
            cause = _root_cause(exc)
            raise MCPConnectionError(f"Calling '{name}' on {self._describe()} failed: {cause}") from exc
        return convert_call_result(result)
