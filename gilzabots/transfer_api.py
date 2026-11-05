"""The ``/api/transfer/*`` endpoints of the desktop server (`gilzabots/transfer.py`).

    POST /api/transfer/export   {connectors: [id] | null, monitors: [id] | null,
                                 include_connectors, include_monitors}  -> {bundle, notes}
    POST /api/transfer/import   {bundle, dry_run, secrets, project_map} -> the import report
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from . import transfer


def _ids(value) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise transfer.TransferError("Ids must be a list, or null for every item.")
    return [str(item) for item in value]


async def handle(path: str, method: str, data: dict, *,
                 refresh_agent: Optional[Callable[[], None]] = None) -> tuple[int, dict]:
    try:
        if path == "/api/transfer/export" and method == "POST":
            bundle, notes = await asyncio.to_thread(
                transfer.build_bundle,
                connector_ids=_ids(data.get("connectors")),
                monitor_ids=_ids(data.get("monitors")),
                include_connectors=bool(data.get("include_connectors", True)),
                include_monitors=bool(data.get("include_monitors", True)),
            )
            return 200, {"bundle": bundle, "notes": notes}
        if path == "/api/transfer/import" and method == "POST":
            dry_run = bool(data.get("dry_run"))
            report = await asyncio.to_thread(
                transfer.import_bundle, data.get("bundle"),
                secrets=data.get("secrets") or {}, project_map=data.get("project_map") or {},
                dry_run=dry_run,
            )
            if not dry_run and report["imported"] and refresh_agent is not None:
                refresh_agent()
            return 200, report
    except transfer.TransferError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:  # a store that cannot be read, say: reported, never swallowed
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 404, {"error": f"Unknown transfer endpoint {method} {path}."}
