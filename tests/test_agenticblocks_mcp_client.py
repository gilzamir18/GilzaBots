"""agenticblocks' MCP bridge against real MCP servers: in-process, stdio and HTTP."""


import asyncio
import socket
import sys
import textwrap
import threading
import time

import pytest

from agenticblocks.tools.a2a_bridge import block_to_tool_schema
from agenticblocks.tools.mcp_client import (
    MAX_TOOL_NAME_LENGTH,
    MCPClientBridge,
    MCPConnectionError,
    MCPToolError,
    MCPToolResult,
    MCPToolSpec,
    exposed_tool_name,
    normalize_input_schema,
    proxy_blocks,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _demo_server():
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.server.mcpserver.utilities.types import Image

    server = MCPServer("demo")

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @server.tool()
    def fail() -> str:
        """Always fails."""
        raise ValueError("deliberate failure")

    @server.tool()
    def picture() -> Image:
        """Return an image."""
        return Image(data=b"\x89PNG" + b"0" * 96, format="png")

    @server.tool(name="dotted.name")
    def dotted() -> str:
        return "dotted"

    @server.tool()
    def echo_header(ctx: Context) -> str:
        """Echo the X-Test header of the HTTP request."""
        request = ctx.request_context.request
        return request.headers.get("x-test", "") if request is not None else ""

    return server


def test_exposed_names_are_valid_function_names():
    assert exposed_tool_name("dotted.name/x", "srv__") == "srv__dotted_name_x"
    long_a = exposed_tool_name("a" * 80 + "1", "p__")
    long_b = exposed_tool_name("a" * 80 + "2", "p__")
    assert len(long_a) == MAX_TOOL_NAME_LENGTH
    assert long_a != long_b  # the hash suffix keeps truncated names distinct


def test_colliding_names_stay_unique():
    specs = [MCPToolSpec(name="a.b"), MCPToolSpec(name="a/b")]
    names = [block.name for block in proxy_blocks(specs, caller=None)]
    assert len(set(names)) == 2


def test_schema_is_normalized_to_an_object():
    assert normalize_input_schema(None) == {"type": "object", "properties": {}}
    assert normalize_input_schema({"type": "object"}) == {"type": "object", "properties": {}}
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    assert normalize_input_schema(schema) == schema


def test_spec_round_trips_through_json():
    spec = MCPToolSpec(name="t", description="d", input_schema={"type": "object", "properties": {}},
                       annotations={"readOnlyHint": True}, title="T")
    assert MCPToolSpec.from_dict(spec.to_dict()) == spec
    with pytest.raises(ValueError):
        MCPToolSpec.from_dict({"description": "no name"})


def test_constructor_rejects_ambiguous_targets():
    with pytest.raises(ValueError):
        MCPClientBridge()
    with pytest.raises(ValueError):
        MCPClientBridge("cmd", url="http://127.0.0.1:1/mcp")
    with pytest.raises(ValueError):
        MCPClientBridge(url="http://127.0.0.1:1/mcp", env={"A": "1"})
    with pytest.raises(ValueError):
        MCPClientBridge("cmd", headers={"A": "1"})


@pytest.mark.asyncio
async def test_in_process_server_tools_and_results():
    bridge = MCPClientBridge(server=_demo_server(), name_prefix="demo__")
    blocks = await bridge.connect()
    try:
        by_remote = {block.remote_name: block for block in blocks}
        assert set(by_remote) >= {"add", "fail", "picture", "dotted.name"}
        assert by_remote["dotted.name"].name == "demo__dotted_name"

        # The function schema an agent receives is the server's own schema.
        schema = block_to_tool_schema(by_remote["add"])
        assert schema["function"]["name"] == "demo__add"
        assert schema["function"]["parameters"]["required"] == ["a", "b"]

        add = by_remote["add"]
        output = await add.run(add.input_schema()(a=2, b=3))
        assert output.result == ["5"]
        assert output.structured_content == {"result": 5}

        picture = by_remote["picture"]
        output = await picture.run(picture.input_schema()())
        assert output.result == ["[image content omitted: image/png, 100 bytes]"]

        fail = by_remote["fail"]
        with pytest.raises(MCPToolError) as info:
            await fail.run(fail.input_schema()())
        assert info.value.tool == "fail"
    finally:
        await bridge.disconnect()
    assert not bridge.connected


@pytest.mark.asyncio
async def test_connection_is_usable_and_closable_from_other_tasks():
    bridge = MCPClientBridge(server=_demo_server())
    # Opened in one task, as an agent turn would ...
    await asyncio.create_task(bridge.open())
    # ... used from another ...
    result = await asyncio.create_task(bridge.call_tool("add", {"a": 1, "b": 1}))
    assert result == MCPToolResult(content=["2"], structured_content={"result": 2}, is_error=False)
    # ... and closed from a third, as application shutdown would.
    await asyncio.create_task(bridge.disconnect())
    assert not bridge.connected
    with pytest.raises(MCPConnectionError):
        await bridge.call_tool("add", {"a": 1, "b": 1})


@pytest.mark.asyncio
async def test_unknown_tool_is_reported_as_an_error_result():
    bridge = MCPClientBridge(server=_demo_server())
    await bridge.open()
    try:
        result = await bridge.call_tool("no_such_tool", {})
        assert result.is_error
        assert "no_such_tool" in result.text()
    finally:
        await bridge.disconnect()


@pytest.mark.asyncio
async def test_tools_list_changed_notification_is_recorded():
    from mcp_types import ToolListChangedNotification

    seen = []
    bridge = MCPClientBridge(server=_demo_server(), on_tools_changed=lambda: seen.append(True))
    await bridge._handle_message(ToolListChangedNotification())
    assert bridge.tools_changed and seen == [True]


_STDIO_SERVER = textwrap.dedent(
    """
    import os
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("stdio-demo")

    @server.tool()
    def env_value(name: str) -> str:
        return os.environ.get(name, "")

    server.run("stdio")
    """
)


@pytest.mark.asyncio
async def test_stdio_server_with_legacy_positional_arguments(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(_STDIO_SERVER, encoding="utf-8")
    bridge = MCPClientBridge(sys.executable, [str(script)], {"DEMO_VALUE": "from-env"})
    blocks = await bridge.connect()
    try:
        assert [block.name for block in blocks] == ["env_value"]
        result = await bridge.call_tool("env_value", {"name": "DEMO_VALUE"})
        assert result.content == ["from-env"]
    finally:
        await bridge.disconnect()


@pytest.mark.asyncio
async def test_missing_command_fails_with_a_diagnostic():
    bridge = MCPClientBridge("gilzabots-no-such-mcp-server-binary")
    with pytest.raises(MCPConnectionError) as info:
        await bridge.connect()
    assert "gilzabots-no-such-mcp-server-binary" in str(info.value)
    assert not bridge.connected


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server_url():
    import uvicorn

    port = _free_port()
    app = _demo_server().streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test MCP HTTP server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.asyncio
async def test_streamable_http_server_with_headers(http_server_url):
    bridge = MCPClientBridge(url=http_server_url, headers={"X-Test": "header-value"})
    blocks = await bridge.connect()
    try:
        assert "add" in {block.remote_name for block in blocks}
        assert (await bridge.call_tool("add", {"a": 20, "b": 22})).content == ["42"]
        assert (await bridge.call_tool("echo_header", {})).content == ["header-value"]
    finally:
        await bridge.disconnect()


@pytest.mark.asyncio
async def test_unreachable_http_server_fails_with_a_diagnostic():
    url = f"http://127.0.0.1:{_free_port()}/mcp"
    bridge = MCPClientBridge(url=url, connect_timeout=15)
    with pytest.raises(MCPConnectionError) as info:
        await bridge.connect()
    assert url in str(info.value)
