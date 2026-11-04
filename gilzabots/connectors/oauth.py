"""The MCP authorization flow for a connector, through the system browser.

The MCP SDK's ``OAuthClientProvider`` implements the protocol side: discovery of
the protected resource and its authorization server, client registration, PKCE,
the token exchange and refresh. What it leaves to the application is the user's
part -- showing the consent page and receiving the redirect -- and how
GilzaBots identifies itself:

*Client identity.* An authorization server that supports Client ID Metadata
Documents (CIMD) is given `CLIENT_METADATA_URL`, a JSON document GilzaBots
publishes on GitHub Pages (``site/oauth/client-metadata.json``), so no
registration is stored at all. One that does not gets a dynamic client
registration (RFC 7591). A server that supports neither needs a client the user
registered in advance (the connector's ``oauth_client_id``).

*Redirect URI.* The consent page runs in the system browser (identity providers
refuse embedded webviews, see cloud/oauth.py) and redirects to a loopback
listener. Authorization servers differ on whether a loopback redirect may vary
its port (RFC 8252 §7.3 says it may; many match exactly), so the listener binds
one of a few fixed ports, `LOOPBACK_PORTS`, and every one of them is listed in
the metadata document and in each dynamic registration. Whichever is free, the
redirect URI is one the server already knows.

*Interactive or not.* Authorization needs the user. It runs only when the user
asked to connect (the Connectors panel, ``/connectors connect``). During an agent
turn the flow is non-interactive: a refresh token is used silently, and when the
user's consent is needed the call fails with `AuthorizationRequired`, telling the
agent to ask the user to reconnect, rather than opening a browser tab nobody
asked for.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Callable, Optional

from ..cloud.oauth import DEFAULT_AUTH_TIMEOUT, LoopbackReceiver, open_in_browser
from .credentials import FileTokenStorage
from .store import Connector

#: The client metadata document published from ``site/oauth/client-metadata.json``.
CLIENT_METADATA_URL = "https://gilzamir18.github.io/GilzaBots/oauth/client-metadata.json"
#: Overrides `CLIENT_METADATA_URL`; an empty value disables CIMD (dynamic
#: registration only), for a fork that does not publish the document.
CLIENT_METADATA_URL_ENV = "GILZABOTS_MCP_CLIENT_METADATA_URL"

CLIENT_NAME = "GilzaBots"
CLIENT_URI = "https://github.com/gilzamir18/GilzaBots"
LOOPBACK_PORTS = (47821, 47822, 47823)
CALLBACK_PATH = "/oauth/callback"


class AuthorizationRequired(RuntimeError):
    """The connector needs the user's consent, which cannot be asked for now."""


class AuthorizationFailed(RuntimeError):
    """The authorization server or the user refused the authorization."""


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def client_metadata_url() -> Optional[str]:
    value = os.environ.get(CLIENT_METADATA_URL_ENV)
    if value is None:
        return CLIENT_METADATA_URL
    return value.strip() or None


def client_metadata_document() -> dict:
    """The published client metadata document (``site/oauth/client-metadata.json``)."""
    return {
        "client_id": CLIENT_METADATA_URL,
        "client_name": CLIENT_NAME,
        "client_uri": CLIENT_URI,
        "redirect_uris": [redirect_uri(port) for port in LOOPBACK_PORTS],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }


def client_metadata(bound_port: int):
    """Registration metadata whose first redirect URI is the bound listener's.

    The SDK sends ``redirect_uris[0]`` in the authorization request; the others
    are registered too, so a later authorization on another port still matches.
    """
    from mcp.shared.auth import OAuthClientMetadata

    ordered = [bound_port] + [port for port in LOOPBACK_PORTS if port != bound_port]
    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        client_uri=CLIENT_URI,
        redirect_uris=[redirect_uri(port) for port in ordered],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def resolved_client_id(connector: Connector) -> str:
    """The pre-registered client to use: the user's, else the one shipped for the connector's preset."""
    if connector.oauth_client_id:
        return connector.oauth_client_id
    if connector.preset:
        from .presets import get_preset

        try:
            shipped = get_preset(connector.preset).bundled_client
        except KeyError:
            shipped = ""
        if shipped:
            from .bundled_clients import bundled_client_id

            return bundled_client_id(shipped)
    return ""


def requires_shipped_client(connector: Connector) -> bool:
    """Whether the connector's service only works through the client GilzaBots ships."""
    if connector.oauth_client_id or not connector.preset:
        return False
    from .presets import get_preset

    try:
        return bool(get_preset(connector.preset).bundled_client)
    except KeyError:
        return False


def preset_client(connector: Connector):
    """The pre-registered client: the user's own, or the one this build ships for the preset."""
    client_id = resolved_client_id(connector)
    if not client_id:
        return None
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=connector.oauth_client_secret or None,
        redirect_uris=[redirect_uri(port) for port in LOOPBACK_PORTS],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post" if connector.oauth_client_secret else "none",
    )


def bind_loopback_receiver() -> LoopbackReceiver:
    """A listener on the first free port of `LOOPBACK_PORTS`."""
    errors = []
    for port in LOOPBACK_PORTS:
        try:
            return LoopbackReceiver(port=port)
        except OSError as exc:
            errors.append(f"{port}: {exc.strerror or exc}")
    raise AuthorizationFailed(
        "Could not listen for the authorization redirect: every GilzaBots callback port is in use "
        f"({'; '.join(errors)}). Close the program using them and try again."
    )


class AuthorizationFlow:
    """One connection attempt's user-facing authorization state.

    ``interactive`` decides whether the user may be sent to the browser. The
    state (``status``, ``authorization_url``) is read by the Connectors panel,
    which shows the link as a fallback when no browser opened.
    """

    def __init__(self, *, interactive: bool, timeout: float = DEFAULT_AUTH_TIMEOUT,
                 open_url: Optional[Callable[[str], bool]] = None, scope: str = ""):
        self.interactive = interactive
        #: Scopes to request instead of the SDK's selection (the connector's
        #: ``oauth_scope``); empty keeps the SDK's.
        self.scope = scope
        self.timeout = timeout
        self.open_url = open_url
        self.status = "idle"  # idle | waiting | authorized | failed | cancelled
        self.authorization_url = ""
        self.browser_opened = False
        self.error = ""
        self.receiver: Optional[LoopbackReceiver] = None
        self._cancelled = False

    @property
    def port(self) -> int:
        return self.receiver.port if self.receiver is not None else LOOPBACK_PORTS[0]

    def prepare(self) -> None:
        """Bind the redirect listener (interactive flows only)."""
        if self.interactive and self.receiver is None:
            self.receiver = bind_loopback_receiver()

    def close(self) -> None:
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None

    def cancel(self) -> None:
        self._cancelled = True
        self.status = "cancelled"
        if self.receiver is not None:
            # Wakes the callback handler, which reports the cancellation.
            self.receiver.deliver({"error": "cancelled"})

    async def redirect_handler(self, authorization_url: str) -> None:
        if not self.interactive or self.receiver is None:
            raise AuthorizationRequired(
                "This connector needs the user's authorization. Ask the user to open Connectors and "
                "click Connect."
            )
        authorization_url = with_scope(authorization_url, self.scope)
        self.authorization_url = authorization_url
        self.status = "waiting"
        try:
            opener = self.open_url or open_in_browser
            self.browser_opened = bool(await asyncio.to_thread(opener, authorization_url))
        except Exception:
            self.browser_opened = False

    async def callback_handler(self):
        from mcp.shared.auth import AuthorizationCodeResult

        if self.receiver is None:
            raise AuthorizationRequired("No authorization is in progress.")
        try:
            params = await asyncio.to_thread(self.receiver.wait, self.timeout)
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc)
            raise AuthorizationFailed(str(exc)) from exc
        if self._cancelled:
            raise AuthorizationFailed("The authorization was cancelled.")
        error = params.get("error")
        if error:
            description = params.get("error_description") or error
            self.status = "failed"
            self.error = f"The authorization was refused: {description}"
            raise AuthorizationFailed(self.error)
        self.status = "authorized"
        return AuthorizationCodeResult(code=params.get("code", ""), state=params.get("state"),
                                       iss=params.get("iss"))


def with_scope(authorization_url: str, scope: str) -> str:
    """``authorization_url`` requesting ``scope`` instead of the SDK's selection.

    The SDK requests whatever the protected resource advertises (MCP's scope
    selection rule), which for X means billing, direct messages and blocks. The
    scope appears only in the authorization request -- neither the code exchange
    nor a refresh sends one -- so narrowing it here is the whole change. The rest
    of the request (state, PKCE challenge, redirect URI, resource) is untouched.
    """
    if not scope:
        return authorization_url
    parts = urllib.parse.urlsplit(authorization_url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if key not in ("scope", "prompt")]
    query.append(("scope", scope))
    # The SDK's own rule, applied to the new scope: OIDC requires prompt=consent
    # when offline_access is requested.
    if "offline_access" in scope.split():
        query.append(("prompt", "consent"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def build_provider(connector: Connector, flow: AuthorizationFlow):
    """The SDK's OAuth provider for ``connector``, wired to ``flow`` and its stored credentials."""
    from mcp.client.auth import OAuthClientProvider

    if requires_shipped_client(connector) and not resolved_client_id(connector):
        raise AuthorizationFailed(
            f"This build of GilzaBots does not include its app for '{connector.name}', which the "
            "service requires for signing in. Official releases include it; a build made from source "
            "needs the client id set at build time (see PROJECT_DESIGN 2.25)."
        )
    storage = FileTokenStorage(connector.id, preset_client=preset_client(connector))
    provider = OAuthClientProvider(
        server_url=connector.url,
        client_metadata=client_metadata(flow.port),
        storage=storage,
        redirect_handler=flow.redirect_handler,
        callback_handler=flow.callback_handler,
        client_metadata_url=None if resolved_client_id(connector) else client_metadata_url(),
    )
    # See credentials.py: restore the absolute expiry the SDK does not persist, so
    # an expired token is refreshed instead of triggering a full authorization.
    # `_initialize` loads tokens and client info but leaves this field alone.
    provider.context.token_expiry_time = storage.stored_expiry()
    return provider
