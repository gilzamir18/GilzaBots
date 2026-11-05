"""Moving monitors and connectors to another machine: a portable bundle.

``monitors.db`` and ``mcp_connectors.json`` cannot simply be copied. The
database also holds the machine's running state (wake-ups, turn leases,
front-end liveness, the notification outbox), and both files hold absolute
paths, chat ids and secrets that belong to the machine they were written on.
A bundle carries only the *definitions*:

- a connector's endpoint, authentication choice, approval policy, timeout and
  tool selection -- **never a secret value** (bearer token, OAuth client secret,
  header and environment values) and never its OAuth tokens or cached catalog;
- a monitor's specification (check, condition, action, interval, remaining
  lifetime, recurrence) and the project and chat it belongs to, by name -- not
  its history, counters or last value.

The bundle names each secret it left out (``needs``); the import receives the
values from the user. Nothing is guessed on the way in:

- a connector whose secrets were not supplied, whose id already exists, or that
  fails the registry's validation (a working folder missing here, say) is not
  imported, and the result says why;
- a monitor whose project does not exist here (after the user's project map),
  or whose action wakes a chat that does not exist in that project, is not
  imported; it is never redirected to another chat or turned into a new-chat
  monitor;
- what is imported arrives switched off: connectors disabled, monitors paused.
  A connector may start a local program and a monitor runs unattended (a
  command, the network, a model), so the user reviews each before turning it on.

Importing the same bundle twice imports nothing the second time: a connector
with the same id, or a monitor identical to one that is active or paused in the
same project, is reported as already present.
"""
from __future__ import annotations

import datetime
import math
import os
import platform
import shutil
import time
from typing import Any

from . import __version__

BUNDLE_FORMAT = "gilzabots-transfer"
BUNDLE_VERSION = 1

#: What an import result reports for each item.
RESULT_READY = "ready"            # dry run: would be imported
RESULT_IMPORTED = "imported"
RESULT_EXISTS = "exists"          # already here; left untouched
RESULT_NEEDS_SECRETS = "needs_secrets"
RESULT_NO_PROJECT = "no_project"
RESULT_NO_CHAT = "no_chat"
RESULT_INVALID = "invalid"

IMPORTED_MONITOR_REASON = "Imported from another machine: review it and resume it."

_CONNECTOR_FIELDS = (
    "id", "name", "transport", "url", "auth", "oauth_client_id", "oauth_scope",
    "command", "args", "cwd", "approval", "connect_timeout", "enabled_tools", "preset",
)


class TransferError(ValueError):
    """A bundle that cannot be read, or a request that makes no sense."""


# ─── Export ──────────────────────────────────────────────────────────────────

def _export_connector(connector) -> dict:
    item: dict[str, Any] = {key: getattr(connector, key) for key in _CONNECTOR_FIELDS}
    needs: list[str] = []
    if connector.bearer_token:
        needs.append("bearer_token")
    if connector.oauth_client_secret:
        needs.append("oauth_client_secret")
    # Header and environment values are all treated as secrets, as the
    # Connectors panel treats them; only a value that was empty travels.
    item["headers"] = list(connector.headers)
    item["env"] = list(connector.env)
    needs += [f"header:{name}" for name, value in connector.headers.items() if value]
    needs += [f"env:{name}" for name, value in connector.env.items() if value]
    item["needs"] = needs
    return item


def _export_monitor(monitor: dict, now: float) -> tuple[dict | None, str]:
    """The bundle entry for a monitor, or (None, why it was left out)."""
    from .monitors.spec import MIN_INTERVAL_SECONDS

    ttl = None
    if monitor.get("expires_at"):
        remaining = monitor["expires_at"] - now
        if remaining < MIN_INTERVAL_SECONDS:
            return None, "its lifetime is over"
        ttl = int(math.ceil(remaining))
    # The checks it has left, not the original allowance: the counter does not
    # travel, and a monitor must not get a second full run by being moved.
    max_checks = monitor.get("max_checks")
    if max_checks:
        max_checks -= int(monitor.get("check_count") or 0)
        if max_checks < 1:
            return None, "it has no checks left"
    check = dict(monitor["check"])
    needs: list[str] = []
    headers = check.pop("headers", None) or {}
    if headers:
        check["headers"] = {name: "" for name in headers}
        needs = [f"header:{name}" for name, value in headers.items() if value]
    item = {
        "source_id": monitor["id"],
        "name": monitor["name"],
        "description": monitor.get("description", ""),
        "owner": monitor.get("owner", "user"),
        "project": monitor.get("project", ""),
        "source_project_path": monitor.get("project_path", ""),
        "chat_id": monitor.get("chat_id", ""),
        "check": check,
        "condition": monitor["condition"],
        "action": monitor["action"],
        "interval_seconds": monitor["interval_seconds"],
        "max_checks": max_checks or None,
        "ttl_seconds": ttl,
        "repeat": bool(monitor.get("repeat")),
        "source_status": monitor["status"],
        "needs": needs,
    }
    return item, ""


def build_bundle(*, connector_ids: list[str] | None = None, monitor_ids: list[str] | None = None,
                 include_connectors: bool = True, include_monitors: bool = True,
                 monitor_store=None, clock=time.time) -> tuple[dict, list[str]]:
    """The bundle of the chosen items, and notes on what was left out.

    ``None`` ids select every connector, and every active or paused monitor
    (a finished monitor has nothing left to do on another machine). Explicit
    ids must all exist.
    """
    from .connectors import store as cstore
    from .monitors import store as mstore

    notes: list[str] = []
    connectors: list[dict] = []
    if include_connectors:
        available = {c.id: c for c in cstore.list_connectors()}
        if connector_ids is None:
            chosen = list(available.values())
        else:
            missing = [cid for cid in connector_ids if cid not in available]
            if missing:
                raise TransferError(f"No connector with id {', '.join(repr(m) for m in missing)}.")
            chosen = [available[cid] for cid in connector_ids]
        connectors = [_export_connector(c) for c in chosen]

    monitors: list[dict] = []
    if include_monitors:
        store = monitor_store or mstore.MonitorStore()
        if monitor_ids is None:
            chosen_monitors = store.list_monitors((mstore.STATUS_ACTIVE, mstore.STATUS_PAUSED))
        else:
            chosen_monitors = []
            for monitor_id in monitor_ids:
                monitor = store.get_monitor(monitor_id)
                if monitor is None:
                    raise TransferError(f"No monitor with id '{monitor_id}'.")
                chosen_monitors.append(monitor)
        now = clock()
        for monitor in chosen_monitors:
            item, why = _export_monitor(monitor, now)
            if item is None:
                notes.append(f"Monitor '{monitor['name']}' ({monitor['id']}) was left out: {why}.")
            else:
                monitors.append(item)

    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": {"gilzabots": __version__, "platform": platform.system()},
        "connectors": connectors,
        "monitors": monitors,
    }
    return bundle, notes


# ─── Import ──────────────────────────────────────────────────────────────────

def check_bundle(bundle: Any) -> dict:
    """Raise `TransferError` unless ``bundle`` is a bundle this version reads."""
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise TransferError("This is not a GilzaBots transfer bundle (no 'format': "
                            f"'{BUNDLE_FORMAT}').")
    version = bundle.get("version")
    if not isinstance(version, int) or version < 1:
        raise TransferError("The bundle has no valid 'version'.")
    if version > BUNDLE_VERSION:
        raise TransferError(
            f"The bundle was written by a newer GilzaBots (format version {version}; this one reads "
            f"up to {BUNDLE_VERSION}). Update GilzaBots on this machine."
        )
    for key in ("connectors", "monitors"):
        if not isinstance(bundle.get(key, []), list) or not all(isinstance(i, dict) for i in bundle.get(key, [])):
            raise TransferError(f"The bundle's '{key}' must be a list of objects.")
    return bundle


def _missing_secrets(needs: list[str], supplied: dict) -> list[str]:
    return [name for name in needs if not str(supplied.get(name) or "")]


def _connector_data(item: dict, secrets: dict) -> dict:
    data = {key: item.get(key) for key in _CONNECTOR_FIELDS if key in item}
    data["bearer_token"] = secrets.get("bearer_token", "")
    data["oauth_client_secret"] = secrets.get("oauth_client_secret", "")
    data["headers"] = [{"name": n, "value": str(secrets.get(f"header:{n}", ""))} for n in item.get("headers") or []]
    data["env"] = [{"name": n, "value": str(secrets.get(f"env:{n}", ""))} for n in item.get("env") or []]
    data["enabled"] = False
    return data


def _connector_warnings(item: dict) -> list[str]:
    warnings = []
    if item.get("transport") == "stdio":
        command = str(item.get("command") or "")
        if command and not (shutil.which(command) or os.path.exists(os.path.expanduser(command))):
            warnings.append(f"The command '{command}' was not found on this machine.")
    if item.get("auth") == "oauth":
        warnings.append("Sign in again here: open Connectors and click Connect.")
    return warnings


def _import_connector(item: dict, secrets: dict, existing: set[str], dry_run: bool) -> dict:
    from .connectors import store as cstore

    connector_id = str(item.get("id") or "")
    result = {"kind": "connector", "key": connector_id, "id": connector_id,
              "name": str(item.get("name") or connector_id), "warnings": []}
    if connector_id in existing:
        return {**result, "status": RESULT_EXISTS,
                "message": f"A connector with id '{connector_id}' already exists here; it was left untouched."}
    missing = _missing_secrets(list(item.get("needs") or []), secrets)
    if missing:
        return {**result, "status": RESULT_NEEDS_SECRETS, "missing": missing,
                "message": "Secrets were not exported; supply " + ", ".join(missing) + "."}
    data = _connector_data(item, secrets)
    try:
        if dry_run:
            cstore.prepare_connector(data)
        else:
            cstore.create_connector(data)
    except cstore.ConnectorError as exc:
        return {**result, "status": RESULT_INVALID, "message": str(exc)}
    existing.add(connector_id)
    return {**result, "status": RESULT_READY if dry_run else RESULT_IMPORTED,
            "warnings": _connector_warnings(item),
            "message": "Imported disabled: review it and turn it on in Connectors." if not dry_run else ""}


def _project_paths(sessions) -> dict[str, str]:
    return {p["name"]: p.get("project_path") or "" for p in sessions.list_projects()}


def _same_monitor(existing: dict, spec: dict) -> bool:
    return (existing["name"].casefold() == spec["name"].casefold()
            and existing.get("project", "") == spec.get("project", "")
            and existing["check"] == spec["check"]
            and existing["condition"] == spec["condition"]
            and existing["action"] == spec["action"]
            and existing.get("chat_id", "") == spec.get("chat_id", ""))


def _monitor_warnings(spec: dict) -> list[str]:
    check = spec["check"]
    if check["type"] == "command":
        return [f"It runs a command on this machine: {check['command']}"]
    if check["type"] == "file" and os.path.isabs(check["path"]) and not os.path.exists(check["path"]):
        return [f"The watched path '{check['path']}' does not exist on this machine."]
    return []


def _import_monitor(item: dict, secrets: dict, project_map: dict, projects: dict[str, str],
                    sessions, store, dry_run: bool) -> dict:
    from .monitors import store as mst
    from .monitors.spec import RESUMING_ACTIONS, WAKE_CHAT_ORIGIN, MonitorSpecError, normalize_spec

    source_project = str(item.get("project") or "")
    project = str(project_map.get(source_project) or source_project) if source_project else ""
    result = {"kind": "monitor", "key": str(item.get("source_id") or ""),
              "name": str(item.get("name") or ""), "project": project, "warnings": []}
    if project and project not in projects:
        hint = (f" Map it to a project of this machine (--project {source_project}=<name>)."
                if project == source_project else "")
        return {**result, "status": RESULT_NO_PROJECT,
                "message": f"The project '{project}' does not exist here.{hint}"}
    missing = _missing_secrets(list(item.get("needs") or []), secrets)
    if missing:
        return {**result, "status": RESULT_NEEDS_SECRETS, "missing": missing,
                "message": "Secrets were not exported; supply " + ", ".join(missing) + "."}
    check = dict(item.get("check") or {})
    if isinstance(check.get("headers"), dict):
        check["headers"] = {name: str(secrets.get(f"header:{name}", "")) for name in check["headers"]}
    raw = {
        "name": item.get("name"),
        "description": item.get("description", ""),
        "owner": item.get("owner", ""),
        "project": project,
        "project_path": projects.get(project, "") if project else "",
        "chat_id": item.get("chat_id", ""),
        "sessions_db": "",
        "check": check,
        "condition": item.get("condition") or {},
        "action": item.get("action") or {},
        "interval_seconds": item.get("interval_seconds"),
        "max_checks": item.get("max_checks"),
        "ttl_seconds": item.get("ttl_seconds"),
        "repeat": item.get("repeat"),
    }
    try:
        spec = normalize_spec(raw)
    except MonitorSpecError as exc:
        return {**result, "status": RESULT_INVALID, "message": str(exc)}
    action = spec["action"]
    if (action["type"] in RESUMING_ACTIONS and action.get("chat") == WAKE_CHAT_ORIGIN
            and not sessions.has_chat(project, spec["chat_id"])):
        return {**result, "status": RESULT_NO_CHAT,
                "message": f"It wakes a chat ({spec['chat_id']}) that does not exist in '{project}' here. "
                           "Mirror the project's conversations to this machine first, or register the "
                           "monitor again from a chat here."}
    for existing in store.list_monitors((mst.STATUS_ACTIVE, mst.STATUS_PAUSED), project=project or None):
        if _same_monitor(existing, spec):
            return {**result, "status": RESULT_EXISTS, "id": existing["id"],
                    "message": f"An identical monitor is already here ({existing['id']}, {existing['status']})."}
    warnings = _monitor_warnings(spec)
    if dry_run:
        return {**result, "status": RESULT_READY, "warnings": warnings, "message": ""}
    monitor = store.create_monitor(spec)
    store.set_status(monitor["id"], mst.STATUS_PAUSED, IMPORTED_MONITOR_REASON)
    return {**result, "status": RESULT_IMPORTED, "id": monitor["id"], "warnings": warnings,
            "message": "Imported paused: review it and resume it in Monitors."}


def import_bundle(bundle: Any, *, secrets: dict | None = None, project_map: dict | None = None,
                  dry_run: bool = False, monitor_store=None, sessions=None) -> dict:
    """Import what can be imported; returns ``{"results": [...], "imported": n, ...}``.

    ``secrets`` maps an item's key (a connector id, a monitor's ``source_id``)
    to ``{secret name: value}`` with the names the bundle's ``needs`` lists.
    ``project_map`` maps a project name of the source machine to one here.
    """
    from .connectors import store as cstore
    from .monitors import store as mst
    from .project import ProjectStore

    check_bundle(bundle)
    secrets = secrets or {}
    project_map = project_map or {}
    if not isinstance(secrets, dict) or not all(isinstance(v, dict) for v in secrets.values()):
        raise TransferError("'secrets' must map each connector id or monitor id to {name: value}.")
    if not isinstance(project_map, dict):
        raise TransferError("'project_map' must map source project names to project names here.")

    results: list[dict] = []
    projects: dict[str, str] = {}
    connectors = bundle.get("connectors", [])
    if connectors:
        existing = {c.id for c in cstore.list_connectors()}
        for item in connectors:
            results.append(_import_connector(item, secrets.get(str(item.get("id") or ""), {}), existing, dry_run))

    monitors = bundle.get("monitors", [])
    if monitors:
        store = monitor_store or mst.MonitorStore()
        if sessions is None:
            from .config import get_gilzabots_home

            sessions = ProjectStore(db_path=os.path.join(get_gilzabots_home(), "sessions.db"))
        projects = _project_paths(sessions)
        for item in monitors:
            results.append(_import_monitor(item, secrets.get(str(item.get("source_id") or ""), {}),
                                           project_map, projects, sessions, store, dry_run))

    done = RESULT_READY if dry_run else RESULT_IMPORTED
    return {
        "dry_run": dry_run,
        "results": results,
        "imported": sum(1 for r in results if r["status"] == done),
        "existing": sum(1 for r in results if r["status"] == RESULT_EXISTS),
        "failed": sum(1 for r in results if r["status"] not in (done, RESULT_EXISTS)),
        # The projects of this machine, for a front-end to offer as map targets.
        "projects": sorted(projects),
    }
