"""`/connectors`: the MCP connectors from the terminal (PROJECT_DESIGN 2.25).

The same registry and connection manager as the desktop Connectors panel. Kept
out of cli_commands.py, like cli_catalog.py, and registered into its registry on
import.
"""

from __future__ import annotations

import asyncio
import shlex

from rich.markup import escape as _escape

from . import terminal as T
from .cli_commands import REPLState, _registry

_USAGE = "[list|presets|add-preset|add|add-stdio|connect|disconnect|tools|on|off|approval|remove] ..."

_DETAILS = (
    "Examples:\n"
    "  /connectors                                   list connectors and their state\n"
    "  /connectors presets                           the pre-configured servers\n"
    "  /connectors add-preset x                      sign in to X in the browser\n"
    "  /connectors add-preset github --bearer <token>\n"
    "  /connectors add-preset filesystem --arg folder=~/notes\n"
    "  /connectors add-preset notion                 then: /connectors connect notion\n"
    "  /connectors add x https://example.com/mcp --oauth --name \"X\"\n"
    "  /connectors add local http://127.0.0.1:8000/mcp\n"
    "  /connectors add api https://example.com/mcp --bearer <token>\n"
    "  /connectors add-stdio fs -- npx -y @modelcontextprotocol/server-filesystem ~/notes\n"
    "  /connectors add-stdio gh --env GITHUB_TOKEN=<token> -- github-mcp-server stdio\n"
    "  /connectors connect x                         connect (authorizes in the browser if needed)\n"
    "  /connectors tools x                           list the server's tools\n"
    "  /connectors tools x createPosts,getUsersMe    let the agent use only these\n"
    "  /connectors tools x all                       let the agent use every tool\n"
    "  /connectors approval x mode                   follow the chat's mode instead of always asking\n"
    "  /connectors disconnect x --forget             disconnect and delete its OAuth credentials\n"
    "  /connectors off x | on x | remove x"
)


def _parse_add(args: list[str]) -> dict:
    if len(args) < 2:
        raise ValueError("usage: /connectors add <id> <url> [--name N] [--oauth | --bearer TOKEN] "
                         "[--header Name=Value] [--client-id ID] [--approval always|mode]")
    data: dict = {"id": args[0], "transport": "http", "url": args[1], "auth": "none",
                  "name": args[0], "headers": []}
    rest = args[2:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        value = rest[i + 1] if i + 1 < len(rest) else None
        if flag == "--oauth":
            data["auth"] = "oauth"
            i += 1
            continue
        if value is None:
            raise ValueError(f"{flag} needs a value")
        if flag == "--name":
            data["name"] = value
        elif flag == "--bearer":
            data["auth"], data["bearer_token"] = "bearer", value
        elif flag == "--header":
            name, _, header_value = value.partition("=")
            data["headers"].append({"name": name, "value": header_value})
        elif flag == "--client-id":
            data["oauth_client_id"] = value
        elif flag == "--client-secret":
            data["oauth_client_secret"] = value
        elif flag == "--approval":
            data["approval"] = value
        else:
            raise ValueError(f"unknown option {flag}")
        i += 2
    return data


def _parse_add_stdio(args: list[str]) -> dict:
    if "--" not in args or args.index("--") < 1 or args.index("--") == len(args) - 1:
        raise ValueError("usage: /connectors add-stdio <id> [--name N] [--env KEY=VALUE] [--cwd DIR] "
                         "[--approval always|mode] -- <command> [args...]")
    split = args.index("--")
    options, command = args[:split], args[split + 1:]
    data: dict = {"id": options[0], "transport": "stdio", "name": options[0], "auth": "none",
                  "command": command[0], "args": command[1:], "env": []}
    rest = options[1:]
    for i in range(0, len(rest), 2):
        flag = rest[i]
        if i + 1 >= len(rest):
            raise ValueError(f"{flag} needs a value")
        value = rest[i + 1]
        if flag == "--name":
            data["name"] = value
        elif flag == "--env":
            key, _, env_value = value.partition("=")
            data["env"].append({"name": key, "value": env_value})
        elif flag == "--cwd":
            data["cwd"] = value
        elif flag == "--approval":
            data["approval"] = value
        else:
            raise ValueError(f"unknown option {flag}")
    return data


def _parse_add_preset(args: list[str]) -> tuple[dict, str]:
    """``<key> [--id ID] [--name N] [--env K=V] [--bearer T] [--arg name=value]`` -> create data."""
    from .connectors import presets

    if not args:
        raise ValueError("usage: /connectors add-preset <preset> [--id ID] [--env K=V] [--bearer TOKEN] "
                         "[--arg name=value]  (see /connectors presets)")
    key, rest = args[0], args[1:]
    try:
        preset = presets.get_preset(key)
    except KeyError:
        raise ValueError(f"unknown preset '{key}'. See /connectors presets.") from None
    if not preset.available():
        raise ValueError(f"preset '{key}' is not available in this build: it needs the app GilzaBots ships "
                         "for that service, and this build does not include it.")
    values: dict = {}
    connector_id = name = ""
    for i in range(0, len(rest), 2):
        flag = rest[i]
        if i + 1 >= len(rest):
            raise ValueError(f"{flag} needs a value")
        value = rest[i + 1]
        if flag == "--id":
            connector_id = value
        elif flag == "--name":
            name = value
        elif flag == "--bearer":
            values["bearer"] = value
        elif flag in ("--env", "--arg"):
            item, _, item_value = value.partition("=")
            values[item] = item_value
        else:
            raise ValueError(f"unknown option {flag}")
    missing = presets.missing_inputs(key, values)
    if missing:
        raise ValueError(f"preset '{key}' needs: {', '.join(missing)}")
    return presets.connector_from_preset(key, values, connector_id=connector_id, name=name), key


def _print_presets() -> None:
    from .connectors import presets

    for preset in presets.list_presets():
        needs = ", ".join(
            "--bearer <token>" if item["kind"] == "bearer"
            else f"--{item['kind']} {item['name']}=<value>" for item in preset["inputs"]
        )
        missing = "" if preset["requirement_found"] else f"  (needs {preset['requires']}, not found)"
        if not preset["available"]:
            missing += "  (not available in this build)"
        T.info(f"{preset['key']:<12} {_escape(preset['name'])} — {_escape(preset['description'])}{missing}")
        if needs:
            T.info(f"{'':<12} add with: /connectors add-preset {preset['key']} {_escape(needs)}")


def _print_list() -> None:
    from .connectors import store
    from .connectors.manager import get_manager

    connectors = store.list_connectors()
    if not connectors:
        T.info("No connectors. Add one with /connectors add (see /help /connectors).")
        return
    manager = get_manager()
    for connector in connectors:
        state = manager.state(connector.id)
        target = connector.url if connector.transport == "http" else " ".join([connector.command, *connector.args])
        status = state.get("status", "disconnected") if connector.enabled else "off"
        tools = f"{len(store.active_tool_specs(connector))}/{len(connector.tools)} tools"
        T.info(f"{_escape(connector.id)}  [{status}]  {_escape(connector.name)}  {_escape(target)}  "
               f"auth={connector.auth} approval={connector.approval} {tools}")
        if state.get("error"):
            T.warning(f"    {_escape(state['error'])}")


async def _connect(connector_id: str) -> None:
    from .connectors.manager import ConnectorUnavailable, get_manager

    manager = get_manager()
    task = asyncio.ensure_future(manager.connect(connector_id, interactive=True))
    announced = False
    try:
        while not task.done():
            await asyncio.sleep(0.2)
            state = manager.state(connector_id)
            if state.get("status") == "authorizing" and not announced:
                announced = True
                T.info("Authorize GilzaBots in your browser. If no browser opened, visit:")
                T.info(_escape(state.get("authorization_url", "")))
        specs = task.result()
    except (KeyboardInterrupt, asyncio.CancelledError):
        manager.cancel_authorization(connector_id)
        task.cancel()
        T.warning("Connection cancelled.")
        return
    except ConnectorUnavailable as exc:
        T.error(_escape(str(exc)))
        return
    T.success(f"Connected to '{connector_id}': {len(specs)} tools.")


def _print_tools(connector_id: str) -> None:
    from .connectors import store

    connector = store.get_connector(connector_id)
    if not connector.tools:
        T.info("No tools cached yet. Run /connectors connect " + connector_id)
        return
    active = {tool.get("name") for tool in store.active_tool_specs(connector)}
    for tool in connector.tools:
        mark = "x" if tool.get("name") in active else " "
        description = (tool.get("description") or "").strip().splitlines()
        T.info(f"[{mark}] {_escape(tool.get('name', ''))}  {_escape(description[0] if description else '')}")


@_registry.register("/connectors", usage=_USAGE, cli_only=True,
                    description="Manage MCP connectors: external services the agent can use",
                    details=_DETAILS)
async def cmd_connectors(state: REPLState, args: list[str]) -> str:
    from .connectors import presets, store
    from .connectors.manager import get_manager

    try:
        argv = shlex.split(" ".join(args)) if args else ["list"]
    except ValueError as exc:
        T.error(f"/connectors: {exc}")
        return "continue"
    action, rest = argv[0], argv[1:]
    manager = get_manager()
    changed = False
    try:
        if action == "list":
            _print_list()
        elif action == "presets":
            _print_presets()
        elif action == "add-preset":
            data, key = _parse_add_preset(rest)
            connector = store.create_connector(data)
            setup = presets.get_preset(key).setup
            if setup:
                T.info(_escape(setup))
            T.success(f"Connector '{connector.id}' added.")
            if connector.auth == "oauth":
                await _connect(connector.id)
            else:
                T.info(f"Connect it with /connectors connect {connector.id}")
            changed = True
        elif action == "add":
            connector = store.create_connector(_parse_add(rest))
            T.success(f"Connector '{connector.id}' added. Connect it with /connectors connect {connector.id}")
            changed = True
        elif action == "add-stdio":
            connector = store.create_connector(_parse_add_stdio(rest))
            T.success(f"Connector '{connector.id}' added. Connect it with /connectors connect {connector.id}")
            changed = True
        elif action in ("connect", "disconnect", "tools", "on", "off", "approval", "remove") and not rest:
            T.error(f"usage: /connectors {action} <id>")
        elif action == "connect":
            await _connect(rest[0])
            changed = True
        elif action == "disconnect":
            await manager.disconnect(rest[0], forget_credentials="--forget" in rest[1:])
            T.success(f"Disconnected '{rest[0]}'.")
        elif action == "tools":
            if len(rest) == 1:
                _print_tools(rest[0])
            else:
                names = None if rest[1] == "all" else [n.strip() for n in rest[1].split(",") if n.strip()]
                connector, _ = store.update_connector(rest[0], {"enabled_tools": names})
                T.success(f"'{connector.id}': {len(store.active_tool_specs(connector))} tools enabled.")
                changed = True
        elif action in ("on", "off"):
            store.update_connector(rest[0], {"enabled": action == "on"})
            if action == "off":
                await manager.disconnect(rest[0])
            T.success(f"Connector '{rest[0]}' turned {action}.")
            changed = True
        elif action == "approval":
            if len(rest) < 2:
                T.error("usage: /connectors approval <id> always|mode")
            else:
                store.update_connector(rest[0], {"approval": rest[1]})
                T.success(f"'{rest[0]}' approval: {rest[1]}.")
                changed = True
        elif action == "remove":
            store.get_connector(rest[0])
            await manager.disconnect(rest[0], forget_credentials=True)
            store.delete_connector(rest[0])
            T.success(f"Connector '{rest[0]}' removed.")
            changed = True
        else:
            T.error(f"/connectors {action}: unknown action. See /help /connectors.")
    except (ValueError, store.ConnectorStoreError) as exc:
        T.error(f"/connectors: {_escape(str(exc))}")
    if changed:
        state.invalidate_memgpt()
    return "continue"
