"""Session authentication for the loopback IDE, including native media requests."""
from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
import secrets
from urllib.parse import urlsplit


_active_api: tuple[str, "LocalSession"] | None = None


def register_local_api(host: str, port: int, session: "LocalSession") -> None:
    """Give in-process agent tools the same authenticated API as the frontend."""
    global _active_api
    address = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in address:
        address = f"[{address}]"
    _active_api = (f"http://{address}:{port}", session)


def unregister_local_api(session: "LocalSession") -> None:
    global _active_api
    if _active_api and _active_api[1] is session:
        _active_api = None


def local_api_connection() -> tuple[str, str]:
    if _active_api is None:
        raise ValueError("No local IDE server is running for this agent.")
    url, session = _active_api
    return url, f"{session.cookie_name}={session.token}"


class LocalSession:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(32)

    @property
    def cookie_name(self) -> str:
        # Cookies are not port-scoped. Separate simultaneous IDE instances.
        return f"gilzabots_session_{self.port}"

    def cookie(self) -> str:
        return f"{self.cookie_name}={self.token}; Path=/api/; HttpOnly; SameSite=Strict"

    def trusted_request(self, headers: dict) -> bool:
        """Reject foreign hosts/origins before reading a body or issuing cookies."""
        host = headers.get("host", "")
        try:
            authority = urlsplit("http://" + host)
            allowed_hosts = {"localhost", "127.0.0.1", "::1"}
            if self.host not in {"0.0.0.0", "::"}:
                allowed_hosts.add(self.host)
            if (authority.hostname not in allowed_hosts or authority.port != self.port
                    or authority.username or authority.password or authority.path
                    or authority.query or authority.fragment):
                return False
            origin = headers.get("origin")
            if origin is not None:
                parsed = urlsplit(origin)
                if (parsed.scheme != "http" or parsed.netloc != authority.netloc
                        or parsed.path or parsed.query or parsed.fragment):
                    return False
        except ValueError:
            return False
        # Same-site is weaker than same-origin (another localhost port is
        # same-site). Media GETs often omit Origin, so check Fetch Metadata too.
        return headers.get("sec-fetch-site", "none") in {"none", "same-origin"}

    def authenticated(self, headers: dict) -> bool:
        try:
            cookies = SimpleCookie()
            cookies.load(headers.get("cookie", ""))
            cookie = cookies.get(self.cookie_name)
            return cookie is not None and secrets.compare_digest(cookie.value, self.token)
        except (CookieError, TypeError, ValueError):
            return False
