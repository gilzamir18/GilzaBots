"""MCP connectors: registry, permission gate, connection manager, API and CLI."""

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from gilzabots.connectors import agent_tools, api, credentials, oauth, store
from gilzabots.connectors.manager import ConnectorUnavailable, get_manager

ROOT = Path(__file__).resolve().parent.parent

_STDIO_SERVER = textwrap.dedent(
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("notes")

    @server.tool()
    def add_note(text: str) -> str:
        \"\"\"Store a note.\"\"\"
        return "stored: " + text

    @server.tool()
    def list_notes() -> str:
        return "none"

    @server.tool()
    def broken() -> str:
        raise RuntimeError("nope")

    server.run("stdio")
    """
)


@pytest.fixture(autouse=True)
def gilzabots_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GILZABOTS_HOME", str(home))
    return home


@pytest.fixture
def stdio_server(tmp_path):
    script = tmp_path / "notes_server.py"
    script.write_text(_STDIO_SERVER, encoding="utf-8")
    return script


def _stdio_connector(script, **extra):
    data = {"id": "notes", "name": "Notes", "transport": "stdio", "command": sys.executable,
            "args": [str(script)], **extra}
    return store.create_connector(data)


# ─── Registry ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, message", [
    ({"id": "Bad Id", "name": "x", "url": "https://a.example/mcp"}, "id must be"),
    ({"id": "a", "name": "x", "url": "http://a.example/mcp"}, "must use https"),
    ({"id": "a", "name": "x", "url": "https://user:pw@a.example/mcp"}, "not in the URL"),
    ({"id": "a", "name": "x", "url": "https://a.example/mcp", "auth": "bearer"}, "needs a token"),
    ({"id": "a", "name": "x", "transport": "stdio", "command": "srv", "auth": "oauth"}, "own environment"),
    ({"id": "a", "name": "x", "transport": "stdio"}, "needs the command"),
    ({"id": "a", "name": "x", "url": "https://a.example/mcp", "auth": "oauth",
      "headers": [{"name": "Authorization", "value": "x"}]}, "Authorization header"),
    ({"id": "a", "name": "x", "url": "https://a.example/mcp", "approval": "never"}, "approval policy"),
])
def test_invalid_connectors_are_rejected(data, message):
    with pytest.raises(store.ConnectorError) as info:
        store.create_connector(data)
    assert message in str(info.value)


def test_plain_http_is_accepted_for_loopback_only():
    store.create_connector({"id": "local", "name": "Local", "url": "http://127.0.0.1:8000/mcp"})
    store.create_connector({"id": "local2", "name": "Local", "url": "http://localhost:8000/mcp"})
    assert [c.id for c in store.list_connectors()] == ["local", "local2"]


def test_secrets_never_leave_through_the_public_view():
    connector = store.create_connector({
        "id": "api", "name": "API", "url": "https://api.example/mcp", "auth": "bearer",
        "bearer_token": "s3cret", "headers": [{"name": "X-Key", "value": "k3y"}],
    })
    view = json.dumps(store.public_view(connector))
    assert "s3cret" not in view and "k3y" not in view
    assert store.public_view(connector)["has_bearer_token"] is True
    assert store.public_view(connector)["headers"] == [{"name": "X-Key", "set": True}]


def test_registry_file_is_owner_only(gilzabots_home):
    store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp"})
    mode = stat.S_IMODE(os.stat(store.connectors_path()).st_mode)
    if os.name == "posix":
        assert mode == 0o600


def test_update_keeps_secrets_that_are_not_resent():
    store.create_connector({
        "id": "api", "name": "API", "url": "https://api.example/mcp", "auth": "bearer",
        "bearer_token": "s3cret", "headers": [{"name": "X-Key", "value": "k3y"}, {"name": "X-Old", "value": "o"}],
    })
    # The form sends names without values for secrets it cannot know.
    updated, changed = store.update_connector("api", {"name": "Renamed", "headers": [{"name": "X-Key"}]})
    assert updated.name == "Renamed" and not changed
    assert updated.bearer_token == "s3cret"
    assert updated.headers == {"X-Key": "k3y"}  # X-Old was removed
    with pytest.raises(store.ConnectorError):
        store.update_connector("api", {"headers": [{"name": "X-New"}]})  # a new header needs its value


def test_an_endpoint_change_drops_the_cached_catalog():
    store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp"})
    store.save_catalog("a", [{"name": "t"}])
    store.update_connector("a", {"enabled_tools": ["t"]})
    updated, changed = store.update_connector("a", {"url": "https://b.example/mcp"})
    assert changed and updated.tools == [] and updated.enabled_tools is None


def test_switching_authentication_discards_the_old_secret():
    store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp",
                            "auth": "bearer", "bearer_token": "t"})
    updated, _ = store.update_connector("a", {"auth": "none"})
    assert updated.bearer_token == ""


def test_corrupt_registry_is_an_error_and_is_left_untouched():
    path = store.connectors_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text("{not json", encoding="utf-8")
    with pytest.raises(store.ConnectorStoreError):
        store.list_connectors()
    with pytest.raises(store.ConnectorStoreError):
        store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp"})
    assert Path(path).read_text(encoding="utf-8") == "{not json"


# ─── Agent tools and the permission gate ─────────────────────────────────────

def _catalog_connector(**extra):
    connector = store.create_connector({"id": "svc", "name": "Service", "url": "https://svc.example/mcp", **extra})
    store.save_catalog("svc", [
        {"name": "post.create", "description": "Create a post",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "read", "description": "Read"},
    ])
    return connector


def test_tools_come_from_the_cached_catalog_with_a_prefix():
    _catalog_connector()
    tools = agent_tools.build_connector_tools()
    assert [t.name for t in tools] == ["mcp__svc__post_create", "mcp__svc__read"]
    assert tools[0].remote_name == "post.create"
    assert tools[0].description.startswith("[Connector: Service]")
    assert "mcp__svc__" in agent_tools.connectors_prompt_block()


def test_the_tool_selection_and_the_switch_are_honoured():
    _catalog_connector()
    store.update_connector("svc", {"enabled_tools": ["read"]})
    assert [t.name for t in agent_tools.build_connector_tools()] == ["mcp__svc__read"]
    store.update_connector("svc", {"enabled": False})
    assert agent_tools.build_connector_tools() == []
    assert agent_tools.connectors_prompt_block() == ""


class _Session:
    def __init__(self, mode):
        self.mode = mode
        self.results = {}


@pytest.fixture
def gate(monkeypatch):
    """Patch the session mode, the user's answer and the connection manager."""
    import gilzabots.agent_stdin as agent_stdin
    import gilzabots.tools as tools_module

    record = {"asked": [], "calls": [], "answer": "yes"}

    async def fake_request_user_input(question, **_kwargs):
        record["asked"].append(question)
        return record["answer"]

    class _FakeManager:
        async def call_tool(self, connector_id, name, arguments):
            from agenticblocks.tools.mcp_client import MCPToolResult
            record["calls"].append((connector_id, name, arguments))
            return MCPToolResult(content=[f"done {name}"])

    def set_mode(mode):
        monkeypatch.setattr(tools_module, "_PROJECT_SESSION", _Session(mode))

    monkeypatch.setattr(agent_stdin, "request_user_input", fake_request_user_input)
    monkeypatch.setattr(agent_stdin, "unattended_turn", lambda: None)
    monkeypatch.setattr("gilzabots.connectors.manager.get_manager", lambda: _FakeManager())
    tools_module._DENIED_TOOLS.clear()
    record["set_mode"] = set_mode
    yield record
    tools_module._DENIED_TOOLS.clear()


async def _run(tool, **arguments):
    return await tool.run(tool.input_schema()(**arguments))


@pytest.mark.asyncio
async def test_always_policy_asks_even_in_auto_mode_and_shows_the_arguments(gate):
    _catalog_connector()
    gate["set_mode"]("auto")
    tool = agent_tools.build_connector_tools()[0]
    output = await _run(tool, text="Hello, world")
    assert output.result == ["done post.create"]
    assert len(gate["asked"]) == 1
    assert "Hello, world" in gate["asked"][0] and "Service" in gate["asked"][0]


@pytest.mark.asyncio
async def test_a_refusal_blocks_the_call(gate):
    _catalog_connector()
    gate["set_mode"]("auto")
    gate["answer"] = "no"
    tool = agent_tools.build_connector_tools()[0]
    output = await _run(tool, text="x")
    assert output.result[0].startswith("Execution blocked")
    assert gate["calls"] == []


@pytest.mark.asyncio
async def test_mode_policy_follows_the_chat_mode(gate):
    _catalog_connector(approval="mode")
    tool = agent_tools.build_connector_tools()[1]
    gate["set_mode"]("auto")
    await _run(tool)
    assert gate["asked"] == [] and len(gate["calls"]) == 1
    gate["set_mode"]("edit")
    await _run(tool)
    assert len(gate["asked"]) == 1 and len(gate["calls"]) == 2


@pytest.mark.asyncio
async def test_plan_mode_blocks_every_connector_call(gate):
    _catalog_connector(approval="mode")
    gate["set_mode"]("plan")
    output = await _run(agent_tools.build_connector_tools()[1])
    assert output.result[0].startswith("Execution blocked") and gate["calls"] == []


@pytest.mark.asyncio
async def test_always_answer_is_remembered_for_that_tool(gate):
    _catalog_connector()
    gate["set_mode"]("auto")
    gate["answer"] = "always"
    tool = agent_tools.build_connector_tools()[0]
    await _run(tool, text="a")
    await _run(tool, text="b")
    assert len(gate["asked"]) == 1 and len(gate["calls"]) == 2


@pytest.mark.asyncio
async def test_an_unattended_turn_is_refused_without_asking(gate, monkeypatch):
    import gilzabots.agent_stdin as agent_stdin

    _catalog_connector()
    gate["set_mode"]("auto")
    monkeypatch.setattr(agent_stdin, "unattended_turn", lambda: object())
    output = await _run(agent_tools.build_connector_tools()[0], text="x")
    assert output.result[0].startswith("Execution blocked")
    assert gate["asked"] == [] and gate["calls"] == []


# ─── Connection manager against a real stdio server ──────────────────────────

@pytest.mark.asyncio
async def test_connect_lists_and_caches_the_tools(stdio_server):
    _stdio_connector(stdio_server)
    manager = get_manager()
    try:
        specs = await manager.connect("notes", interactive=True)
        assert {s.name for s in specs} == {"add_note", "list_notes", "broken"}
        assert {t["name"] for t in store.get_connector("notes").tools} == {"add_note", "list_notes", "broken"}
        assert manager.state("notes")["status"] == "connected"
    finally:
        await manager.shutdown()
    assert manager.state("notes")["status"] == "disconnected"


@pytest.mark.asyncio
async def test_a_call_opens_the_connection_on_demand(stdio_server):
    _stdio_connector(stdio_server)
    manager = get_manager()
    try:
        result = await manager.call_tool("notes", "add_note", {"text": "milk"})
        assert result.content == ["stored: milk"]
        assert manager.state("notes")["status"] == "connected"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_agent_tool_end_to_end_with_a_server_error(stdio_server, monkeypatch):
    import gilzabots.tools as tools_module
    from agenticblocks.tools.mcp_client import MCPToolError

    _stdio_connector(stdio_server, approval="mode")
    manager = get_manager()
    await manager.connect("notes", interactive=False)
    monkeypatch.setattr(tools_module, "_PROJECT_SESSION", _Session("auto"))
    try:
        tools = {t.remote_name: t for t in agent_tools.build_connector_tools()}
        assert (await _run(tools["add_note"], text="eggs")).result == ["stored: eggs"]
        with pytest.raises(MCPToolError):
            await _run(tools["broken"])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_turned_off_connector_refuses_calls(stdio_server):
    _stdio_connector(stdio_server, enabled=False)
    with pytest.raises(ConnectorUnavailable):
        await get_manager().call_tool("notes", "list_notes", {})


@pytest.mark.asyncio
async def test_a_server_that_does_not_start_reports_why():
    store.create_connector({"id": "ghost", "name": "Ghost", "transport": "stdio",
                            "command": "gilzabots-no-such-server"})
    manager = get_manager()
    with pytest.raises(ConnectorUnavailable) as info:
        await manager.connect("ghost", interactive=True)
    assert "gilzabots-no-such-server" in str(info.value)
    assert manager.state("ghost")["status"] == "error"


# ─── API ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_create_list_update_delete():
    refreshed = []
    status, body = await api.handle("/api/connectors/create", "POST", {"connector": {
        "id": "api", "name": "API", "url": "https://api.example/mcp", "auth": "bearer", "bearer_token": "s3cr3t-value",
    }}, {}, refresh_agent=lambda: refreshed.append(1))
    assert status == 200 and body["connector"]["has_bearer_token"] and "bearer_token" not in body["connector"]

    status, body = await api.handle("/api/connectors", "GET", {}, {})
    assert status == 200 and [c["id"] for c in body["connectors"]] == ["api"]
    assert body["connectors"][0]["state"]["status"] == "disconnected"
    assert "s3cr3t-value" not in json.dumps(body)

    status, body = await api.handle("/api/connectors/update", "POST",
                                    {"id": "api", "connector": {"url": "https://other.example/mcp"}}, {},
                                    refresh_agent=lambda: refreshed.append(1))
    assert status == 200 and body["endpoint_changed"]

    status, body = await api.handle("/api/connectors/create", "POST",
                                    {"connector": {"id": "api", "name": "dup", "url": "https://x.example/mcp"}}, {})
    assert status == 400 and "already exists" in body["error"]

    status, _ = await api.handle("/api/connectors/delete", "POST", {"id": "api"}, {},
                                 refresh_agent=lambda: refreshed.append(1))
    assert status == 200 and store.list_connectors() == []
    assert len(refreshed) == 3


@pytest.mark.asyncio
async def test_api_reports_an_unreadable_registry():
    path = store.connectors_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text("[]", encoding="utf-8")
    status, body = await api.handle("/api/connectors", "GET", {}, {})
    assert status == 200 and body["connectors"] == [] and "cannot be read" in body["error"]


# ─── Client metadata document and CLI parsing ────────────────────────────────

def test_published_client_metadata_matches_the_code():
    published = json.loads((ROOT / "site" / "oauth" / "client-metadata.json").read_text(encoding="utf-8"))
    assert published == oauth.client_metadata_document()
    assert published["client_id"] == oauth.CLIENT_METADATA_URL


def test_registration_metadata_lists_every_callback_port_first_the_bound_one():
    metadata = oauth.client_metadata(oauth.LOOPBACK_PORTS[1])
    uris = [str(uri) for uri in metadata.redirect_uris]
    assert uris[0] == oauth.redirect_uri(oauth.LOOPBACK_PORTS[1])
    assert sorted(uris) == sorted(oauth.redirect_uri(p) for p in oauth.LOOPBACK_PORTS)


def test_cli_parsers():
    from gilzabots.cli_connectors import _parse_add, _parse_add_stdio

    data = _parse_add(["x", "https://x.example/mcp", "--oauth", "--name", "X", "--header", "A=b"])
    assert data["auth"] == "oauth" and data["name"] == "X" and data["headers"] == [{"name": "A", "value": "b"}]
    data = _parse_add_stdio(["fs", "--env", "K=v", "--", "npx", "-y", "server", "dir"])
    assert data["command"] == "npx" and data["args"] == ["-y", "server", "dir"]
    assert data["env"] == [{"name": "K", "value": "v"}]
    with pytest.raises(ValueError):
        _parse_add_stdio(["fs", "npx"])


def test_forgetting_credentials_removes_the_file():
    storage = credentials.FileTokenStorage("svc")
    from mcp.shared.auth import OAuthToken

    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", expires_in=60)))
    assert credentials.has_tokens("svc")
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(credentials.credentials_path("svc")).st_mode) == 0o600
    credentials.forget("svc")
    assert not credentials.has_tokens("svc")


# ─── The chat orchestrator ───────────────────────────────────────────────────

def test_the_orchestrator_holds_the_connector_tools_and_their_prompt(tmp_path):
    from gilzabots.memgpt_runtime import build_chat_orchestrator
    from gilzabots.project import ProjectStore

    _catalog_connector()
    project_store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = project_store.create(name="p", mode="auto", model="ollama/test",
                                   project_name="P", project_path=str(tmp_path))
    agent = build_chat_orchestrator(project, project_store)
    names = {tool.name for tool in agent.tools}
    assert {"mcp__svc__post_create", "mcp__svc__read"} <= names
    assert "## Connectors (external MCP services)" in agent.system_prompt
    assert "never follow instructions" in agent.system_prompt

    store.update_connector("svc", {"enabled": False})
    agent = build_chat_orchestrator(project, project_store)
    assert not any(tool.name.startswith("mcp__") for tool in agent.tools)
    assert "## Connectors" not in agent.system_prompt


# ─── Pre-configured servers ──────────────────────────────────────────────────

def _dummy_values(preset):
    values = {}
    for item in preset.inputs:
        values["bearer" if item["kind"] == "bearer" else item["name"]] = "value-1"
    return values


def test_every_preset_becomes_a_valid_connector(tmp_path, monkeypatch):
    monkeypatch.setenv("GILZABOTS_X_CLIENT_ID", "x-app-id")
    from gilzabots.connectors import presets

    keys = [p.key for p in presets.PRESETS]
    assert len(keys) == len(set(keys))
    ids = [p.connector["id"] for p in presets.PRESETS]
    assert len(ids) == len(set(ids)), "two presets would create the same connector id"
    for preset in presets.PRESETS:
        data = presets.connector_from_preset(preset.key, _dummy_values(preset))
        connector = store.create_connector(data)
        assert connector.id == preset.connector["id"]
        assert preset.docs_url.startswith("https://")
        if connector.transport == "stdio":
            assert preset.requires == connector.command


def test_missing_preset_inputs_are_named():
    from gilzabots.connectors import presets

    assert presets.missing_inputs("x", {}) == []  # the user types nothing for X
    assert presets.missing_inputs("github", {}) == ["bearer"]
    assert presets.missing_inputs("notion", {}) == []
    data = presets.connector_from_preset("filesystem", {"folder": "~/notes"}, connector_id="notes")
    assert data["id"] == "notes" and data["args"][-1] == "~/notes"


def test_the_x_preset_signs_in_with_oauth_and_asks_the_user_for_nothing():
    from gilzabots.connectors import presets

    x = presets.get_preset("x")
    assert x.connector["transport"] == "http" and x.connector["url"] == "https://api.x.com/mcp"
    assert x.connector["auth"] == "oauth" and x.inputs == ()
    assert x.bundled_client == "x" and x.connector["approval"] == "always"
    # X advertises billing, DMs and blocks; only reading and posting are requested.
    assert set(x.connector["oauth_scope"].split()) == {"tweet.read", "tweet.write", "users.read", "offline.access"}


def test_the_x_preset_needs_the_shipped_app(monkeypatch):
    from gilzabots.connectors import bundled_clients, presets

    monkeypatch.delenv("GILZABOTS_X_CLIENT_ID", raising=False)
    monkeypatch.setattr(bundled_clients, "bundled_clients_path", lambda: "/nonexistent/bundled.json")
    assert not presets.get_preset("x").available()
    monkeypatch.setenv("GILZABOTS_X_CLIENT_ID", "x-app-id")
    assert presets.get_preset("x").available()
    assert bundled_clients.bundled_client_id("x") == "x-app-id"


def test_the_shipped_client_file_is_read(tmp_path, monkeypatch):
    from gilzabots.connectors import bundled_clients

    path = tmp_path / "bundled.json"
    path.write_text(json.dumps({"x": {"client_id": "from-file"}}), encoding="utf-8")
    monkeypatch.delenv("GILZABOTS_X_CLIENT_ID", raising=False)
    monkeypatch.setattr(bundled_clients, "bundled_clients_path", lambda: str(path))
    assert bundled_clients.bundled_client_id("x") == "from-file"
    monkeypatch.setenv("GILZABOTS_X_CLIENT_ID", "from-env")
    assert bundled_clients.bundled_client_id("x") == "from-env"


def test_embed_script_writes_the_file_the_app_reads(tmp_path, monkeypatch):
    import importlib.util

    from gilzabots.connectors import bundled_clients

    spec = importlib.util.spec_from_file_location("embed", ROOT / "scripts" / "embed_connector_clients.py")
    embed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(embed)
    target = tmp_path / "bundled_oauth_clients.json"
    monkeypatch.setattr(embed, "TARGET", str(target))
    monkeypatch.setattr(bundled_clients, "bundled_clients_path", lambda: str(target))
    monkeypatch.delenv("GILZABOTS_X_CLIENT_ID", raising=False)
    assert embed.main([]) == 1
    assert embed.main(["--x-client-id", "abc"]) == 0
    assert bundled_clients.bundled_client_id("x") == "abc"
    assert embed.main(["--clear"]) == 0 and not target.exists()


def test_without_the_shipped_app_x_fails_with_a_reason(monkeypatch):
    from gilzabots.connectors import bundled_clients
    from gilzabots.connectors.oauth import AuthorizationFailed, AuthorizationFlow, build_provider

    monkeypatch.delenv("GILZABOTS_X_CLIENT_ID", raising=False)
    monkeypatch.setattr(bundled_clients, "bundled_clients_path", lambda: "/nonexistent/bundled.json")
    connector = store.Connector(id="x", name="X", url="https://api.x.com/mcp", auth="oauth", preset="x")
    with pytest.raises(AuthorizationFailed, match="does not include its app"):
        build_provider(connector, AuthorizationFlow(interactive=False))


def test_the_requested_scope_replaces_the_advertised_one():
    from urllib.parse import parse_qs, urlsplit

    from gilzabots.connectors.oauth import with_scope

    url = ("https://x.com/i/oauth2/authorize?response_type=code&client_id=c&state=s"
           "&code_challenge=cc&scope=tweet.read+dm.write+developer.billing.write&resource=r")
    query = parse_qs(urlsplit(with_scope(url, "tweet.read offline.access")).query)
    assert query["scope"] == ["tweet.read offline.access"]
    assert query["state"] == ["s"] and query["code_challenge"] == ["cc"] and query["resource"] == ["r"]
    assert "prompt" not in query
    assert parse_qs(urlsplit(with_scope(url, "openid offline_access")).query)["prompt"] == ["consent"]
    assert with_scope(url, "") == url


def test_every_preset_is_translated():
    from gilzabots.connectors import presets

    pt = json.loads((ROOT / "gui_src" / "src" / "i18n" / "locales" / "pt-BR.json").read_text(encoding="utf-8"))
    translated = pt["connectors"]["presets"]
    for preset in presets.PRESETS:
        assert {"name", "description"} <= set(translated[preset.key]), preset.key
        assert ("setup" in translated[preset.key]) == bool(preset.setup), preset.key


def test_connect_timeout_is_bounded():
    with pytest.raises(store.ConnectorError):
        store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp", "connect_timeout": 1})
    connector = store.create_connector({"id": "a", "name": "A", "url": "https://a.example/mcp", "connect_timeout": "120"})
    assert connector.connect_timeout == 120


@pytest.mark.asyncio
async def test_api_lists_the_presets():
    status, body = await api.handle("/api/connectors/presets", "GET", {}, {})
    assert status == 200
    by_key = {p["key"]: p for p in body["presets"]}
    assert {"x", "x-readonly", "x-docs", "notion", "filesystem"} <= set(by_key)
    assert isinstance(by_key["x"]["requirement_found"], bool)


def test_cli_add_preset_x_needs_nothing_but_the_shipped_app(monkeypatch):
    from gilzabots.cli_connectors import _parse_add_preset
    from gilzabots.connectors import bundled_clients

    monkeypatch.delenv("GILZABOTS_X_CLIENT_ID", raising=False)
    monkeypatch.setattr(bundled_clients, "bundled_clients_path", lambda: "/nonexistent/bundled.json")
    with pytest.raises(ValueError, match="not available in this build"):
        _parse_add_preset(["x"])
    monkeypatch.setenv("GILZABOTS_X_CLIENT_ID", "x-app-id")
    data, _ = _parse_add_preset(["x"])
    assert data["preset"] == "x" and data["auth"] == "oauth" and "oauth_client_id" not in data


def test_cli_add_preset_parser():
    from gilzabots.cli_connectors import _parse_add_preset

    data, key = _parse_add_preset(["github", "--bearer", "tok"])
    assert key == "github" and data["bearer_token"] == "tok" and data["preset"] == "github"
    with pytest.raises(ValueError, match="bearer"):
        _parse_add_preset(["github"])
    with pytest.raises(ValueError, match="unknown preset"):
        _parse_add_preset(["nope"])
