"""The window's monitor API (``/api/monitors/*``), served by ``ide_server``.

Besides managing monitors, the window *polls* (``POST /api/monitors/poll``)
every few seconds. The poll is how the window

- tells the daemon it is alive and which project it has open (``hosts``), so
  wake-ups of that project are left to it;
- claims a wake-up of its project when it is idle, and runs it as an ordinary
  turn the user sees streaming (``App.jsx``); it reports the outcome with
  ``/api/monitors/wake/finish``;
- receives a notification click (``open_requests``).

A wake-up the window runs is marked ``monitor_wake`` like one the daemon runs,
so it asks nothing outside its chat either (``agent_stdin.unattended_turn``).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from . import store as st

GUI_HOST = "gui"


def _store() -> st.MonitorStore:
    return st.MonitorStore()


def on_server_start() -> None:
    """Restart the daemon after a reboot or a crash when it has work."""
    try:
        store = _store()
        if store.count_active() or store.list_wakes((st.WAKE_PENDING,)):
            from .service import ensure_daemon_running

            ensure_daemon_running(store)
    except Exception as exc:
        print(f"[monitors] could not check the monitor daemon: {exc}")


async def handle(path: str, method: str, data: dict, query: dict, *,
                 turn_active: bool, focus_window: Callable[[], None] | None = None) -> tuple[int, dict]:
    """Route one ``/api/monitors`` request; returns (status, JSON body)."""
    store = _store()

    if path == "/api/monitors" and method == "GET":
        from .service import daemon_status

        # daemon_status runs the service manager: off the server's event loop.
        return 200, {
            "monitors": store.list_monitors(),
            "daemon": await asyncio.to_thread(daemon_status),
            "events": store.recent_events(30),
        }

    if path == "/api/monitors/events" and method == "GET":
        monitor_id = (query.get("id") or [""])[0]
        return 200, {"events": store.list_events(monitor_id, limit=100)}

    if path == "/api/monitors/action" and method == "POST":
        monitor_id = str(data.get("id") or "")
        action = str(data.get("action") or "")
        monitor = store.get_monitor(monitor_id)
        if monitor is None:
            return 404, {"error": f"No monitor '{monitor_id}'."}
        if action == "pause":
            if monitor["status"] != st.STATUS_ACTIVE:
                return 409, {"error": f"The monitor is {monitor['status']}, not active."}
            store.set_status(monitor_id, st.STATUS_PAUSED, "paused in the Monitors panel")
        elif action == "resume":
            if not store.resume_monitor(monitor_id):
                return 409, {"error": f"The monitor is {monitor['status']}, not paused."}
            from .service import ensure_daemon_running

            await asyncio.to_thread(ensure_daemon_running, store)
        elif action == "cancel":
            if monitor["status"] in st.FINAL_STATUSES:
                return 409, {"error": f"The monitor is already {monitor['status']}."}
            store.set_status(monitor_id, st.STATUS_CANCELLED, "cancelled in the Monitors panel")
        elif action == "delete":
            store.delete_monitor(monitor_id)
            return 200, {"ok": True, "deleted": True}
        else:
            return 400, {"error": f"Unknown action '{action}'."}
        return 200, {"ok": True, "monitor": store.get_monitor(monitor_id)}

    if path == "/api/monitors/test" and method == "POST":
        from .engine import MonitorEngine
        from .llm_check import run_llm_check_subprocess

        monitor = store.get_monitor(str(data.get("id") or ""))
        if monitor is None:
            return 404, {"error": "No such monitor."}
        result = await MonitorEngine(store, llm_runner=run_llm_check_subprocess).dry_run(monitor)
        result.pop("raw_value", None)
        return 200, result

    if path == "/api/monitors/create" and method == "POST":
        from .agent_tools import register
        from .spec import MonitorSpecError, display_value, normalize_spec

        raw = data.get("spec")
        if not isinstance(raw, dict):
            return 400, {"error": "'spec' must be an object."}
        try:
            spec = normalize_spec(raw)
            monitor, observed, daemon = await register(spec)
        except MonitorSpecError as exc:
            return 400, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"The monitor could not be registered: {type(exc).__name__}: {exc}"}
        return 200, {
            "monitor": monitor,
            "value": display_value(observed["value"]) if observed else None,
            "met": bool(observed["evaluation"].met) if observed else None,
            "daemon": daemon,
        }

    if path == "/api/monitors/autostart" and method == "POST":
        from .service import set_autostart

        result = await asyncio.to_thread(set_autostart, store, bool(data.get("enabled")))
        return (200 if result.get("ok") else 500), result

    if path == "/api/monitors/daemon/start" and method == "POST":
        from .service import ensure_daemon_running

        return 200, await asyncio.to_thread(ensure_daemon_running, store, wait=3.0)

    if path == "/api/monitors/notify-test" and method == "POST":
        from .notify import NotifyError, default_notifier

        notifier = default_notifier()
        try:
            await asyncio.to_thread(notifier.send, str(data.get("title") or "OpalaTex"), str(data.get("body") or ""))
        except NotifyError as exc:
            return 500, {"ok": False, "backend": notifier.backend, "error": str(exc)}
        return 200, {"ok": True, "backend": notifier.backend}

    if path == "/api/monitors/poll" and method == "POST":
        project = str(data.get("project_name") or "")
        project_path = str(data.get("project_path") or "")
        store.touch_host(GUI_HOST, os.getpid(), project, project_path)
        result: dict[str, Any] = {
            "open": None,
            "wake": None,
            "unseen_events": store.unseen_events(),
            "events": store.recent_events(10),
        }
        opened = store.take_open_request()
        if opened:
            result["open"] = opened
            if focus_window is not None:
                try:
                    focus_window()
                except Exception:
                    pass
        if project and data.get("idle") and not turn_active:
            store.fail_unrunnable_wakes(GUI_HOST, os.getpid())
            for wake in store.list_wakes((st.WAKE_PENDING,), project=project):
                if wake.get("new_chat"):
                    continue  # run by the daemon, out of the user's way
                if store.lease_holder(project, wake["chat_id"]):
                    continue
                if store.claim_wake(wake["id"], GUI_HOST, os.getpid()):
                    result["wake"] = wake
                    break
        return 200, result

    if path == "/api/monitors/events/seen" and method == "POST":
        store.mark_events_seen()
        return 200, {"ok": True}

    if path == "/api/monitors/events/clear" and method == "POST":
        return 200, {"ok": True, "cleared": store.clear_events()}

    if path == "/api/monitors/wake/finish" and method == "POST":
        wake_id = int(data.get("id") or 0)
        status = str(data.get("status") or "")
        if status == "released":
            ok = store.release_wake(wake_id, GUI_HOST)
        elif status in (st.WAKE_DONE, st.WAKE_FAILED, st.WAKE_INTERRUPTED):
            # The window ran it in front of the user: already seen.
            ok = store.finish_wake(wake_id, status, str(data.get("error") or ""), seen=True)
        else:
            return 400, {"error": f"Unknown status '{status}'."}
        return 200, {"ok": ok}

    return 404, {"error": f"Unknown monitors endpoint {method} {path}."}
