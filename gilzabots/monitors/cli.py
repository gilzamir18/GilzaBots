"""``gilzabots monitors ...``: manage monitors without opening the agent.

User commands::

    gilzabots monitors list [--all | --status S]
    gilzabots monitors info <id>
    gilzabots monitors add --json '<spec>' | --file spec.json
    gilzabots monitors pause|resume|cancel|delete|test <id>
    gilzabots monitors daemon [--status | --stop]
    gilzabots monitors service status|install|uninstall
    gilzabots monitors tray status|on|off
    gilzabots monitors notify-test

Internal commands (started by the daemon or the OS)::

    gilzabots monitors run-wake <wake-id>
    gilzabots monitors llm-check <monitor-id>
    gilzabots monitors open <gilzabots-monitor:...>
    gilzabots monitors tray run --daemon-pid <pid>
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys

from . import store as st


def _when(epoch) -> str:
    if not epoch:
        return "-"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gilzabots monitors", description="Manage GilzaBots monitor agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="List monitors (active and paused by default)")
    listing.add_argument("--all", action="store_true", help="Include completed, expired, cancelled and failed monitors")
    listing.add_argument("--status", choices=st.ALL_STATUSES)

    for name, text in (
        ("info", "Show a monitor and its recent history"),
        ("pause", "Pause a monitor"),
        ("resume", "Resume a paused monitor"),
        ("cancel", "Cancel a monitor for good"),
        ("delete", "Delete a monitor and its history"),
        ("test", "Check a monitor now and show what would happen, changing nothing"),
        ("llm-check", "(internal) evaluate a monitor's model check"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("monitor_id")

    add = sub.add_parser("add", help="Register a monitor from a JSON specification")
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument("--json", help="The specification as JSON")
    source.add_argument("--file", help="A file holding the specification as JSON")
    add.add_argument("--no-probe", action="store_true", help="Do not run the check once before registering")


    daemon = sub.add_parser("daemon", help="Run the monitor daemon in the foreground")
    group = daemon.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="Report whether the daemon runs")
    group.add_argument("--stop", action="store_true", help="Stop the running daemon")

    service = sub.add_parser("service", help="The login service that starts the daemon")
    service.add_argument("action", choices=("status", "install", "uninstall"))

    tray = sub.add_parser("tray", help="The daemon's tray icon: status, on, off ('run' is internal)")
    tray.add_argument("action", choices=("status", "on", "off", "run"))
    tray.add_argument("--daemon-pid", type=int, default=0, help="(internal, with 'run') the daemon to follow")

    sub.add_parser("notify-test", help="Show a test desktop notification")

    wake = sub.add_parser("run-wake", help="(internal) run a wake-up turn")
    wake.add_argument("wake_id", type=int)

    opener = sub.add_parser("open", help="(internal) open the chat named by an gilzabots-monitor: URI")
    opener.add_argument("uri")
    return parser


def _print_monitor(monitor: dict) -> None:
    from .agent_tools import describe

    print(describe(monitor))


def _cmd_list(store: st.MonitorStore, args) -> int:
    if args.status:
        statuses = (args.status,)
    elif args.all:
        statuses = None
    else:
        statuses = (st.STATUS_ACTIVE, st.STATUS_PAUSED)
    monitors = store.list_monitors(statuses)
    if not monitors:
        print("No monitors.")
        return 0
    for monitor in monitors:
        _print_monitor(monitor)
    return 0


def _get(store: st.MonitorStore, monitor_id: str) -> dict | None:
    monitor = store.get_monitor(monitor_id)
    if monitor is None:
        print(f"No monitor '{monitor_id}'. Run 'gilzabots monitors list --all' to see the ids.", file=sys.stderr)
    return monitor


def _cmd_info(store: st.MonitorStore, args) -> int:
    monitor = _get(store, args.monitor_id)
    if monitor is None:
        return 1
    _print_monitor(monitor)
    print(f"  created: {_when(monitor['created_at'])}; last check: {_when(monitor.get('last_check_at'))};"
          f" last fired: {_when(monitor.get('last_fired_at'))}")
    print("  check: " + json.dumps(monitor["check"], ensure_ascii=False))
    print("  condition: " + json.dumps(monitor["condition"], ensure_ascii=False))
    print("  action: " + json.dumps(monitor["action"], ensure_ascii=False))
    events = store.list_events(monitor["id"], limit=20)
    if events:
        print("  history (newest first):")
        for event in events:
            print(f"    {_when(event['at'])}  {event['kind']:<12} {event['message'][:120]}")
    return 0


def _cmd_status_change(store: st.MonitorStore, args) -> int:
    monitor = _get(store, args.monitor_id)
    if monitor is None:
        return 1
    if args.command == "pause":
        if monitor["status"] != st.STATUS_ACTIVE:
            print(f"Monitor {monitor['id']} is {monitor['status']}; only an active monitor can be paused.", file=sys.stderr)
            return 1
        store.set_status(monitor["id"], st.STATUS_PAUSED, "paused from the command line")
    elif args.command == "resume":
        if not store.resume_monitor(monitor["id"]):
            print(f"Monitor {monitor['id']} is {monitor['status']}; only a paused monitor can be resumed.", file=sys.stderr)
            return 1
        from .service import ensure_daemon_running

        ensure_daemon_running(store)
    elif args.command == "cancel":
        if monitor["status"] in st.FINAL_STATUSES:
            print(f"Monitor {monitor['id']} is already {monitor['status']}.")
            return 0
        store.set_status(monitor["id"], st.STATUS_CANCELLED, "cancelled from the command line")
    elif args.command == "delete":
        store.delete_monitor(monitor["id"])
        print(f"Deleted monitor {monitor['id']}.")
        return 0
    print(f"Monitor {monitor['id']} is now {store.get_monitor(monitor['id'])['status']}.")
    return 0


def _llm_runner():
    from .llm_check import run_llm_check_subprocess

    return run_llm_check_subprocess


def _cmd_test(store: st.MonitorStore, args) -> int:
    from .engine import MonitorEngine

    monitor = _get(store, args.monitor_id)
    if monitor is None:
        return 1
    result = asyncio.run(MonitorEngine(store, llm_runner=_llm_runner()).dry_run(monitor))
    if not result["ok"]:
        print(f"Check failed: {result['error']}")
        return 1
    print(f"Value: {result['value']}")
    print(f"Condition holds: {'yes' if result['met'] else 'no'}; would fire now: {'yes' if result['would_fire'] else 'no'}")
    return 0


def _cmd_add(store: st.MonitorStore, args) -> int:
    from .agent_tools import register
    from .spec import MonitorSpecError, normalize_spec

    try:
        text = args.json if args.json is not None else open(args.file, encoding="utf-8").read()
        raw = json.loads(text)
    except (OSError, ValueError) as exc:
        print(f"Could not read the specification: {exc}", file=sys.stderr)
        return 2
    try:
        spec = normalize_spec(raw)
        monitor, observed, daemon = asyncio.run(register(spec, run_probe=not args.no_probe))
    except MonitorSpecError as exc:
        print(f"Not registered: {exc}", file=sys.stderr)
        return 1
    print(f"Registered {monitor['id']}. First check at {_when(monitor['next_check_at'])}.")
    if observed is not None:
        from .spec import display_value

        print(f"Current value: {display_value(observed['value'])}")
    if daemon.get("error"):
        print(f"Warning: {daemon['error']}", file=sys.stderr)
    return 0


def _cmd_daemon(store: st.MonitorStore, args) -> int:
    from . import service

    if args.status:
        print(json.dumps(service.daemon_status(), indent=2))
        return 0
    if args.stop:
        stopped = service.stop_daemon()
        print("Monitor daemon stopped." if stopped else "The monitor daemon did not stop.")
        return 0 if stopped else 1
    from .daemon import run_daemon

    return run_daemon()


def _cmd_tray(store: st.MonitorStore, args) -> int:
    from . import service, tray

    if args.action == "run":
        pid = args.daemon_pid or int(service.read_state().get("pid") or 0)
        if not pid or not service.daemon_running():
            print("The monitor daemon is not running; the tray icon belongs to it.", file=sys.stderr)
            return 1
        return tray.run_tray(pid)
    if args.action in ("on", "off"):
        tray.set_tray_enabled(store, args.action == "on")
    print(json.dumps(tray.tray_status(store, service.daemon_running()), indent=2))
    return 0


def _cmd_service(store: st.MonitorStore, args) -> int:
    from . import service

    if args.action == "status":
        print(json.dumps(service.daemon_status()["autostart"], indent=2))
        return 0
    result = service.set_autostart(store, args.action == "install")
    if not result["ok"]:
        print(f"Failed: {result['error']}", file=sys.stderr)
        return 1
    print(f"Login service ({result['backend']}): {'installed' if result['installed'] else 'removed'}.")
    return 0


def _cmd_notify_test(_store: st.MonitorStore, _args) -> int:
    from .notify import NotifyError, default_notifier

    notifier = default_notifier()
    try:
        notifier.send("GilzaBots", "Monitor notifications work.", urgency="normal")
    except NotifyError as exc:
        print(f"Notification failed ({notifier.backend}): {exc}", file=sys.stderr)
        return 1
    print(f"Notification sent through {notifier.backend}.")
    return 0


def _cmd_run_wake(store: st.MonitorStore, args) -> int:
    from .wake_runner import run_wake

    return asyncio.run(run_wake(args.wake_id, store=store))


def _cmd_llm_check(store: st.MonitorStore, args) -> int:
    """Print the verdict as one JSON line; the daemon reads the last line."""
    from .checks import CheckError
    from .llm_check import evaluate_in_process

    monitor = store.get_monitor(args.monitor_id)
    if monitor is None:
        print(json.dumps({"ok": False, "error": f"no monitor '{args.monitor_id}'"}))
        return 1
    try:
        value = asyncio.run(evaluate_in_process(monitor))
    except CheckError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"ok": True, "value": value}, ensure_ascii=False))
    return 0


def _cmd_open(store: st.MonitorStore, args) -> int:
    from .notify import parse_open_uri
    from .service import open_chat

    try:
        target = parse_open_uri(args.uri)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    open_chat(store, target["project"], target["chat_id"], target["project_path"])
    return 0


_COMMANDS = {
    "list": _cmd_list,
    "info": _cmd_info,
    "pause": _cmd_status_change,
    "resume": _cmd_status_change,
    "cancel": _cmd_status_change,
    "delete": _cmd_status_change,
    "test": _cmd_test,
    "add": _cmd_add,
    "daemon": _cmd_daemon,
    "service": _cmd_service,
    "tray": _cmd_tray,
    "notify-test": _cmd_notify_test,
    "run-wake": _cmd_run_wake,
    "llm-check": _cmd_llm_check,
    "open": _cmd_open,
}


def main(argv: list[str]) -> int:
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        from gilzabots.ui_settings import load_ui_settings
        from gilzabots.i18n import set_lang

        lang = str(load_ui_settings().get("lang") or "")
        set_lang("pt" if lang.startswith("pt") else "en")
    except Exception:
        pass
    store = st.MonitorStore()
    return _COMMANDS[args.command](store, args)
