"""The MCP authorization flow end to end, against a real OAuth-protected MCP server.

The server is the SDK's `MCPServer` with an in-memory authorization server that
supports dynamic client registration; the "browser" follows the consent redirect
to GilzaBots' loopback listener, as a user clicking Allow would.
"""

import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from gilzabots.connectors import credentials, oauth, store
from gilzabots.connectors.manager import ConnectorUnavailable, get_manager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MemoryAuthorizationServer:
    """An OAuthAuthorizationServerProvider that approves every authorization."""

    def __init__(self, access_lifetime: int = 3600):
        self.access_lifetime = access_lifetime
        self.clients = {}
        self.codes = {}
        self.access = {}
        self.refresh = {}
        self.authorizations = 0
        self.refreshes = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri

        self.authorizations += 1
        code = secrets.token_urlsafe(24)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300,
            client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    def _issue(self, client_id, scopes, resource):
        from mcp.server.auth.provider import AccessToken, RefreshToken
        from mcp.shared.auth import OAuthToken

        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time() + self.access_lifetime), resource=resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=access, expires_in=self.access_lifetime, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refreshes += 1
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        stored = self.access.get(token)
        if stored is None or (stored.expires_at and stored.expires_at < time.time()):
            return None
        return stored

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)

    def expire_all_access_tokens(self):
        self.access.clear()


@pytest.fixture
def protected_server():
    import uvicorn
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from mcp.server.mcpserver import MCPServer

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    provider = MemoryAuthorizationServer()
    server = MCPServer(
        "protected",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )

    @server.tool()
    def whoami() -> str:
        """Who is calling."""
        return "authorized caller"

    config = uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port,
                            log_level="error", lifespan="on")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        if time.monotonic() > deadline:
            raise RuntimeError("protected MCP server did not start")
        time.sleep(0.05)
    yield f"{base}/mcp", provider
    uv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path / "home"))
    # Test ports, so a GilzaBots running on this machine cannot collide.
    monkeypatch.setattr(oauth, "LOOPBACK_PORTS", (_free_port(), _free_port()))
    # The authorization server of the test does not fetch client metadata documents.
    monkeypatch.setenv(oauth.CLIENT_METADATA_URL_ENV, "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _consenting_browser(opened):
    """Open the consent page and follow its redirect, like a user clicking Allow."""

    def open_url(url):
        opened.append(url)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(url, timeout=10)
            raise AssertionError("the authorization endpoint did not redirect")
        except urllib.error.HTTPError as exc:
            location = exc.headers["Location"]
        with urllib.request.urlopen(location, timeout=10) as response:
            assert response.status == 200
        return True

    return open_url


@pytest.mark.asyncio
async def test_oauth_connect_refresh_and_reauthorization(protected_server, monkeypatch):
    url, provider = protected_server
    opened = []
    monkeypatch.setattr(oauth, "open_in_browser", _consenting_browser(opened))
    store.create_connector({"id": "prot", "name": "Protected", "url": url, "auth": "oauth"})
    manager = get_manager()

    # 1. Without credentials, a turn cannot open a browser: it is told to ask the user.
    with pytest.raises(ConnectorUnavailable) as info:
        await manager.call_tool("prot", "whoami", {})
    assert info.value.needs_authorization
    assert opened == [] and provider.authorizations == 0

    # 2. Connect (the user's click): registration, consent in the browser, token.
    specs = await manager.connect("prot", interactive=True)
    assert [s.name for s in specs] == ["whoami"]
    assert len(opened) == 1 and provider.authorizations == 1
    redirect = urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)["redirect_uri"][0]
    assert redirect in [oauth.redirect_uri(p) for p in oauth.LOOPBACK_PORTS]
    assert credentials.has_tokens("prot")
    stored = json.loads(open(credentials.credentials_path("prot"), encoding="utf-8").read())
    assert stored["expires_at"] > time.time()
    # The registration names every callback port, so another port still matches later.
    assert len(stored["client_info"]["redirect_uris"]) == len(oauth.LOOPBACK_PORTS)

    result = await manager.call_tool("prot", "whoami", {})
    assert result.content == ["authorized caller"]
    await manager.shutdown()

    # 3. After a "restart" with an expired access token, the stored refresh token
    #    is used silently -- no browser, no new authorization.
    provider.expire_all_access_tokens()
    data = json.loads(open(credentials.credentials_path("prot"), encoding="utf-8").read())
    data["expires_at"] = time.time() - 10
    open(credentials.credentials_path("prot"), "w", encoding="utf-8").write(json.dumps(data))
    result = await manager.call_tool("prot", "whoami", {})
    assert result.content == ["authorized caller"]
    assert provider.refreshes == 1 and provider.authorizations == 1 and len(opened) == 1
    await manager.shutdown()

    # 4. Forgetting the credentials brings back the request to authorize.
    await manager.disconnect("prot", forget_credentials=True)
    with pytest.raises(ConnectorUnavailable) as info:
        await manager.call_tool("prot", "whoami", {})
    assert info.value.needs_authorization
    await manager.shutdown()


@pytest.mark.asyncio
async def test_a_shipped_app_signs_in_without_registration_and_with_narrow_scopes(protected_server, monkeypatch):
    """The X path: a pre-registered public client supplied by the build, PKCE, chosen scopes."""
    from mcp.shared.auth import OAuthClientInformationFull

    url, provider = protected_server
    provider.clients["shipped-app"] = OAuthClientInformationFull(
        client_id="shipped-app",
        redirect_uris=[oauth.redirect_uri(p) for p in oauth.LOOPBACK_PORTS],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="tweet.read tweet.write users.read offline.access",
    )
    registrations_before = set(provider.clients)
    monkeypatch.setenv("GILZABOTS_X_CLIENT_ID", "shipped-app")
    opened = []
    monkeypatch.setattr(oauth, "open_in_browser", _consenting_browser(opened))
    # The X preset's settings, pointed at the test server.
    store.create_connector({"id": "x", "name": "X", "url": url, "auth": "oauth", "preset": "x",
                            "oauth_scope": "tweet.read users.read offline.access"})
    manager = get_manager()
    specs = await manager.connect("x", interactive=True)
    assert [s.name for s in specs] == ["whoami"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)
    assert query["client_id"] == ["shipped-app"]
    assert query["scope"] == ["tweet.read users.read offline.access"]
    assert set(provider.clients) == registrations_before  # nothing was registered
    assert (await manager.call_tool("x", "whoami", {})).content == ["authorized caller"]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_a_refused_consent_is_reported(protected_server, monkeypatch):
    url, _provider = protected_server

    def refusing_browser(auth_url):
        redirect = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["redirect_uri"][0]
        with urllib.request.urlopen(f"{redirect}?error=access_denied", timeout=10):
            pass
        return True

    monkeypatch.setattr(oauth, "open_in_browser", refusing_browser)
    store.create_connector({"id": "prot", "name": "Protected", "url": url, "auth": "oauth"})
    manager = get_manager()
    with pytest.raises(ConnectorUnavailable) as info:
        await manager.connect("prot", interactive=True)
    assert "access_denied" in str(info.value)
    assert not credentials.has_tokens("prot")
    await manager.shutdown()


@pytest.mark.asyncio
async def test_a_cancelled_authorization_leaves_the_connector_disconnected(protected_server, monkeypatch):
    import asyncio

    url, _provider = protected_server
    monkeypatch.setattr(oauth, "open_in_browser", lambda _url: True)  # the user never answers
    store.create_connector({"id": "prot", "name": "Protected", "url": url, "auth": "oauth"})
    manager = get_manager()
    manager.start_connect("prot")
    for _ in range(100):
        if manager.state("prot")["status"] == "authorizing":
            break
        await asyncio.sleep(0.05)
    state = manager.state("prot")
    assert state["status"] == "authorizing" and state["authorization_url"].startswith("http://127.0.0.1")
    manager.cancel_authorization("prot")
    for _ in range(100):
        if manager.state("prot")["status"] not in ("authorizing", "connecting"):
            break
        await asyncio.sleep(0.05)
    assert manager.state("prot")["status"] == "disconnected"
    await manager.shutdown()


import urllib.parse  # noqa: E402  (used by the tests above)
