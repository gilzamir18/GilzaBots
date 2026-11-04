"""Pre-configured connectors: well-known MCP servers the user can add in one step.

A preset is a *template*, never a connector that exists by default: nothing is
contacted until the user picks one, fills in what it asks for (a token, an app's
client id, a folder) and saves it. Choosing a preset only fills the connector
form; the result is an ordinary connector the user can edit.

Every endpoint here was checked against the live service when it was added
(2026-11-03): the open ones by listing their tools through
`agenticblocks.tools.mcp_client.MCPClientBridge`, the OAuth ones by reading their
protected-resource and authorization-server metadata. ``auth="oauth"`` is only
used where the authorization server supports dynamic client registration or
client metadata documents, which GilzaBots can use without a registered app, or
where the build ships GilzaBots' own app for the service (``bundled_client``):
X supports neither, so its preset signs in through the X app a release carries
(bundled_clients.py) and the user only logs in to X. GitHub supports neither
either and takes a personal access token.

The texts are English defaults; the desktop translates them by preset key
(``connectors.presets.<key>`` in the locale files).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

#: A value the user must provide: ``kind`` is ``env`` (an environment variable of
#: a stdio server), ``bearer`` (the bearer token) or ``arg`` (a command-line
#: argument appended to the preset's arguments, such as a folder).
Input = dict


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    category: str
    description: str
    connector: dict
    inputs: tuple = ()
    setup: str = ""
    docs_url: str = ""
    #: The executable a stdio preset needs on PATH (``npx``, ``uvx``).
    requires: str = ""
    #: The OAuth client this build ships for the service (bundled_clients.py), for
    #: a service that accepts no other way in. Without it the preset is unavailable.
    bundled_client: str = ""

    def to_dict(self) -> dict:
        data = {
            "key": self.key,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "connector": dict(self.connector),
            "inputs": [dict(item) for item in self.inputs],
            "setup": self.setup,
            "docs_url": self.docs_url,
            "requires": self.requires,
        }
        data["requirement_found"] = bool(shutil.which(self.requires)) if self.requires else True
        data["available"] = self.available()
        return data

    def available(self) -> bool:
        if not self.bundled_client:
            return True
        from .bundled_clients import bundled_client_id

        return bool(bundled_client_id(self.bundled_client))


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="x",
        name="X (your account)",
        category="social",
        description=(
            "Sign in to X and let the assistant read and search posts, and post and repost as you, "
            "through X's hosted MCP server."
        ),
        connector={
            "id": "x", "name": "X", "transport": "http", "url": "https://api.x.com/mcp",
            "auth": "oauth", "approval": "always",
            # X advertises every scope it has, billing and direct messages included;
            # request only what reading and posting need (offline.access: refresh token).
            "oauth_scope": "tweet.read tweet.write users.read offline.access",
        },
        setup=(
            "Click Connect and sign in to X in your browser. X's rules still apply: no automated likes "
            "or follows, and AI-generated replies need X's approval."
        ),
        docs_url="https://docs.x.com/tools/mcp",
        bundled_client="x",
    ),
    Preset(
        key="x-readonly",
        name="X (read-only, app token)",
        category="social",
        description="X's hosted MCP server with an app-only bearer token: read endpoints only, nothing as you.",
        connector={"id": "x-read", "name": "X (read-only)", "transport": "http",
                   "url": "https://api.x.com/mcp", "auth": "bearer", "approval": "mode"},
        inputs=({"kind": "bearer"},),
        setup="Copy the app-only Bearer Token from your X app's \"Keys and tokens\" page.",
        docs_url="https://docs.x.com/tools/mcp",
    ),
    Preset(
        key="x-docs",
        name="X developer docs",
        category="social",
        description="Search and read the X API documentation. No account needed.",
        connector={"id": "x-docs", "name": "X docs", "transport": "http",
                   "url": "https://docs.x.com/mcp", "auth": "none", "approval": "mode"},
        docs_url="https://docs.x.com/tools/mcp",
    ),
    Preset(
        key="github",
        name="GitHub",
        category="development",
        description="GitHub's remote MCP server: repositories, issues, pull requests and more.",
        connector={"id": "github", "name": "GitHub", "transport": "http",
                   "url": "https://api.githubcopilot.com/mcp/", "auth": "bearer", "approval": "always"},
        inputs=({"kind": "bearer"},),
        setup=(
            "Create a fine-grained personal access token on GitHub (Settings → Developer settings) "
            "with only the repositories and permissions the agent should have."
        ),
        docs_url="https://github.com/github/github-mcp-server",
    ),
    Preset(
        key="notion",
        name="Notion",
        category="productivity",
        description="Search, read and edit your Notion pages and databases.",
        connector={"id": "notion", "name": "Notion", "transport": "http",
                   "url": "https://mcp.notion.com/mcp", "auth": "oauth", "approval": "always"},
        setup="Click Connect and sign in to Notion in your browser.",
        docs_url="https://developers.notion.com/docs/mcp",
    ),
    Preset(
        key="linear",
        name="Linear",
        category="productivity",
        description="Find, create and update Linear issues and projects.",
        connector={"id": "linear", "name": "Linear", "transport": "http",
                   "url": "https://mcp.linear.app/mcp", "auth": "oauth", "approval": "always"},
        setup="Click Connect and sign in to Linear in your browser.",
        docs_url="https://linear.app/docs/mcp",
    ),
    Preset(
        key="sentry",
        name="Sentry",
        category="development",
        description="Look up Sentry issues, errors and releases.",
        connector={"id": "sentry", "name": "Sentry", "transport": "http",
                   "url": "https://mcp.sentry.dev/mcp", "auth": "oauth", "approval": "always"},
        setup="Click Connect and sign in to Sentry in your browser.",
        docs_url="https://docs.sentry.io/product/sentry-mcp/",
    ),
    Preset(
        key="huggingface",
        name="Hugging Face",
        category="development",
        description="Search models, datasets and Spaces on the Hugging Face Hub.",
        connector={"id": "huggingface", "name": "Hugging Face", "transport": "http",
                   "url": "https://huggingface.co/mcp", "auth": "none", "approval": "mode"},
        docs_url="https://huggingface.co/settings/mcp",
    ),
    Preset(
        key="deepwiki",
        name="DeepWiki",
        category="development",
        description="Ask questions about public GitHub repositories and read their generated docs.",
        connector={"id": "deepwiki", "name": "DeepWiki", "transport": "http",
                   "url": "https://mcp.deepwiki.com/mcp", "auth": "none", "approval": "mode"},
        docs_url="https://docs.devin.ai/work-with-devin/deepwiki-mcp",
    ),
    Preset(
        key="context7",
        name="Context7",
        category="development",
        description="Up-to-date documentation and code examples for programming libraries.",
        connector={"id": "context7", "name": "Context7", "transport": "http",
                   "url": "https://mcp.context7.com/mcp", "auth": "none", "approval": "mode"},
        docs_url="https://github.com/upstash/context7",
    ),
    Preset(
        key="filesystem",
        name="Files (a local folder)",
        category="local",
        description="Read and write files in one folder of your computer that you choose.",
        connector={"id": "files", "name": "Files", "transport": "stdio", "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-filesystem"], "approval": "always"},
        inputs=({"kind": "arg", "name": "folder"},),
        setup="Choose the folder the server may access; it cannot reach anything outside it. Needs Node.js (npx).",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        requires="npx",
    ),
    Preset(
        key="fetch",
        name="Fetch (web pages)",
        category="local",
        description="Fetch a web page and convert it to Markdown.",
        connector={"id": "fetch", "name": "Fetch", "transport": "stdio", "command": "uvx",
                   "args": ["mcp-server-fetch"], "approval": "mode"},
        setup="Needs uv (uvx) installed: https://docs.astral.sh/uv/",
        docs_url="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        requires="uvx",
    ),
)


def list_presets() -> list[dict]:
    return [preset.to_dict() for preset in PRESETS]


def get_preset(key: str) -> Preset:
    for preset in PRESETS:
        if preset.key == key:
            return preset
    raise KeyError(key)


def missing_inputs(key: str, values: dict[str, Any] | None) -> list[str]:
    """The preset inputs ``values`` leaves empty, by name (``bearer`` for the token)."""
    values = values or {}
    missing = []
    for item in get_preset(key).inputs:
        name = "bearer" if item["kind"] == "bearer" else item["name"]
        if not str(values.get(name, "")).strip():
            missing.append(name)
    return missing


def connector_from_preset(key: str, values: dict[str, Any] | None = None, *, connector_id: str = "",
                          name: str = "") -> dict:
    """The ``create_connector`` data for a preset, with the user's values filled in.

    ``values`` maps an env input's name to its value, ``"bearer"`` to the token and
    an arg input's name to the argument. A missing value is left empty, so the
    registry's validation names what is still needed.
    """
    preset = get_preset(key)
    values = dict(values or {})
    data = {key_: (list(value) if isinstance(value, list) else value) for key_, value in preset.connector.items()}
    data["preset"] = preset.key
    if connector_id:
        data["id"] = connector_id
    if name:
        data["name"] = name
    env = []
    for item in preset.inputs:
        if item["kind"] == "env":
            env.append({"name": item["name"], "value": str(values.get(item["name"], ""))})
        elif item["kind"] == "bearer":
            data["bearer_token"] = str(values.get("bearer", ""))
        elif item["kind"] == "arg":
            value = str(values.get(item["name"], "")).strip()
            if value:
                data["args"] = [*data.get("args", []), value]
    if env:
        data["env"] = env
    return data
