"""The orchestrator's monitor tools: register, list, pause and cancel.

``register_monitor`` serves both uses of a monitor:

- **waiting** (``action="resume_chat"``): the orchestrator is waiting for
  something (a build, a download, a deploy) and ends its turn instead of
  spinning; the monitor wakes this chat when the condition holds, or when its
  checks or time run out, with the reason;
- **watching for the user** (``action="notify"``): a watchdog the user asked
  for in the chat ("tell me when the dollar rises 10 cents"), which shows an OS
  notification each time the condition fires.

Registration runs the check once before storing anything (except a timer, and
an ``llm`` check, which would spend a model call): a URL that does not answer,
a ``json_path`` that does not match or a command that cannot start is refused
with the diagnostic, rather than stored to fail every few minutes unseen.

``register_monitor`` is not a safe tool: a monitor keeps reaching the network or
running a command with nobody watching, so ``edit`` mode asks the user and
``plan`` mode blocks it like any other execution.
"""
from __future__ import annotations

import asyncio
import datetime

from . import store as st
from .spec import RESUMING_ACTIONS, WAKE_CHAT_NEW, WAKE_CHAT_ORIGIN, MonitorSpecError, display_value, normalize_spec

#: The tool's names for where a wake-up runs, and the specification's.
WAKE_IN_CHOICES = {"same_chat": WAKE_CHAT_ORIGIN, "new_chat": WAKE_CHAT_NEW}

MAX_LISTED = 50


def _session():
    from opalatex import tools

    return tools._PROJECT_SESSION, tools._PROJECT_STORE


def _store() -> st.MonitorStore:
    return st.MonitorStore()


def _when(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _condition_from_args(condition: str, value: str, threshold: float, direction: str) -> dict:
    raw: dict = {"type": condition}
    if condition == "regex":
        raw["pattern"] = value
    elif condition == "delta":
        raw["threshold"] = threshold
        raw["direction"] = direction or "any"
    elif condition:
        raw["value"] = value if value != "" else None
    return raw


def build_spec(
    *,
    name: str,
    interval_seconds: int,
    check_type: str,
    url: str = "",
    json_path: str = "",
    regex: str = "",
    command: str = "",
    path: str = "",
    watch: str = "mtime",
    question: str = "",
    condition: str = "",
    value: str = "",
    threshold: float = 0.0,
    direction: str = "any",
    action: str = "resume_chat",
    message: str = "",
    resume_prompt: str = "",
    first_check_seconds: int = -1,
    max_checks: int = 0,
    ttl_seconds: int = 0,
    once: bool | None = None,
    wake_in: str = "same_chat",
    description: str = "",
    project=None,
    sessions_db: str = "",
) -> dict:
    """Assemble and validate a specification from the tool's flat arguments."""
    check: dict = {"type": check_type}
    if check_type == "http":
        check.update(url=url, json_path=json_path, regex=regex)
    elif check_type == "command":
        check["command"] = command
    elif check_type == "file":
        check.update(path=path, watch=watch or "mtime")
    elif check_type == "llm":
        check["question"] = question
    if wake_in not in WAKE_IN_CHOICES:
        raise MonitorSpecError(f"'wake_in' must be 'same_chat' or 'new_chat', got '{wake_in}'.")
    raw = {
        "name": name,
        "description": description,
        "check": check,
        "condition": _condition_from_args(condition, value, threshold, direction) if condition else {},
        "action": {"type": action, "message": message, "prompt": resume_prompt, "chat": WAKE_IN_CHOICES[wake_in]},
        "interval_seconds": interval_seconds,
        "first_check_seconds": None if first_check_seconds is None or first_check_seconds < 0 else first_check_seconds,
        "max_checks": max_checks or None,
        "ttl_seconds": ttl_seconds or None,
        # Unset, the specification's default applies (once for a wake-up of
        # this chat, recurring otherwise); a choice is never overridden.
        "repeat": None if once is None else not once,
        "project": getattr(project, "name", "") or "",
        "project_path": getattr(project, "project_path", "") or "",
        "chat_id": getattr(project, "current_chat_id", "") or "",
        "sessions_db": sessions_db,
    }
    return normalize_spec(raw)


async def probe(spec: dict) -> dict:
    """Run the check once for a new monitor. Raises CheckError / ConditionError."""
    from .engine import MonitorEngine

    engine = MonitorEngine(_store())
    monitor = {**spec, "id": "probe", "has_value": False, "last_value": None, "last_met": None,
               "reference": None, "check_count": 0}
    value = await engine.observe(monitor)
    evaluation = engine.judge(monitor, value)
    return {"value": value, "evaluation": evaluation}


async def register(spec: dict, *, run_probe: bool = True) -> tuple[dict, dict | None, dict]:
    """Probe, store and make sure the daemon runs. Returns (monitor, probe, daemon)."""
    from .checks import CheckError
    from .service import ensure_daemon_running
    from .spec import ConditionError

    observed = None
    if run_probe and spec["check"]["type"] not in ("timer", "llm"):
        try:
            observed = await probe(spec)
        except (CheckError, ConditionError) as exc:
            raise MonitorSpecError(f"The first check failed, so the monitor was not registered: {exc}") from None
    store = _store()
    kwargs: dict = {}
    if observed is not None:
        evaluation = observed["evaluation"]
        kwargs = {
            "initial_value": observed["value"],
            "has_initial_value": True,
            # Not ``initial_met``: a level condition that already holds is
            # reported on the first scheduled check, which is what was asked.
            "initial_reference": evaluation.reference,
        }
    monitor = store.create_monitor(spec, **kwargs)
    # Runs the service manager: off the event loop of the caller (the window's
    # server, or the agent turn).
    daemon = await asyncio.to_thread(ensure_daemon_running, store)
    return monitor, observed, daemon


def _active_waking_monitor(store: st.MonitorStore, project) -> dict | None:
    """When this turn was woken by a monitor that is still active, return it.

    A recurring monitor stays ``active`` after firing and schedules its next
    check itself; a wake-up turn that registers another monitor to "keep it
    going" creates duplicates on every cycle.
    """
    from opalatex.agent_stdin import unattended_monitor_id, unattended_turn

    wake_name = unattended_turn()
    if not wake_name:
        return None
    wake_id = unattended_monitor_id()
    if wake_id:
        monitor = store.get_monitor(wake_id)
        if monitor is not None:
            return monitor if monitor["status"] == st.STATUS_ACTIVE else None
    project_name = getattr(project, "name", "") or ""
    chat_id = getattr(project, "current_chat_id", "") or ""
    if project_name and chat_id:
        for wake in reversed(store.list_wakes((st.WAKE_CLAIMED,), project=project_name)):
            if wake.get("chat_id") == chat_id and wake.get("monitor_id"):
                monitor = store.get_monitor(wake["monitor_id"])
                if monitor is not None:
                    return monitor if monitor["status"] == st.STATUS_ACTIVE else None
    wanted = wake_name.casefold()
    for monitor in store.list_monitors((st.STATUS_ACTIVE,), project=project_name or None):
        if monitor["name"].casefold() == wanted:
            return monitor
    return None


def _find_duplicate_active(store: st.MonitorStore, spec: dict) -> dict | None:
    """An already-active monitor in the same project with the same name, check, condition and target."""
    wanted_name = spec["name"].casefold()
    wanted_chat = spec["action"].get("chat")
    for existing in store.list_monitors((st.STATUS_ACTIVE,), project=spec.get("project") or None):
        if existing.get("project", "") != spec.get("project", ""):
            continue
        if existing["name"].casefold() != wanted_name:
            continue
        if existing["check"] != spec["check"] or existing["condition"] != spec["condition"]:
            continue
        if existing["action"].get("chat") != wanted_chat:
            continue
        if wanted_chat == WAKE_CHAT_ORIGIN and existing.get("chat_id") != spec.get("chat_id"):
            continue
        return existing
    return None


def describe(monitor: dict) -> str:
    check = monitor["check"]
    target = {
        "timer": "timer",
        "http": f"GET {check.get('url', '')}" + (f" → {check['json_path']}" if check.get("json_path") else ""),
        "command": f"command `{check.get('command', '')}`",
        "file": f"file {check.get('path', '')} ({check.get('watch', 'mtime')})",
        "llm": f"model question: {check.get('question', '')[:80]}",
    }.get(check["type"], check["type"])
    condition = monitor["condition"]
    cond_text = condition["type"]
    if "value" in condition:
        cond_text += f" {condition['value']}"
    if "pattern" in condition:
        cond_text += f" /{condition['pattern']}/"
    if "threshold" in condition:
        cond_text += f" ±{condition['threshold']:g} ({condition.get('direction', 'any')})"
    lines = [
        f"- {monitor['id']} \"{monitor['name']}\" [{monitor['status']}]"
        + (f" ({monitor['status_reason']})" if monitor.get("status_reason") else ""),
        f"  watches: {target}; fires on: {cond_text} ({'every time' if monitor['repeat'] else 'once'});"
        f" checked every {monitor['interval_seconds']} s;"
        f" action: {monitor['action']['type']}"
        + (" (wakes a new chat)" if monitor["action"].get("chat") == WAKE_CHAT_NEW
           else " (wakes this chat)" if monitor["action"]["type"] in RESUMING_ACTIONS else ""),
        f"  project: {monitor.get('project') or '-'}; checks: {monitor['check_count']}"
        + (f"/{monitor['max_checks']}" if monitor.get("max_checks") else "")
        + f"; fired: {monitor['fire_count']}; next check: {_when(monitor.get('next_check_at')) if monitor['status'] == st.STATUS_ACTIVE else '-'}",
    ]
    if monitor.get("has_value"):
        lines.append(f"  last value: {display_value(monitor.get('last_value'), 160)}")
    if monitor.get("last_error"):
        lines.append(f"  last error: {monitor['last_error'][:200]}")
    return "\n".join(lines)


def build_monitor_tools():
    """The tools, built against ``opalatex.tools.opalatex_tool`` (mode gate)."""
    from opalatex.tools import opalatex_tool

    @opalatex_tool(
        name="register_monitor",
        description=(
            "Register a monitor: a background watcher that keeps running after your turn ends and even "
            "when OpalaTex is closed. Two uses:\n"
            "1. WAIT for something instead of polling (a build, a download, a long command): "
            "action='resume_chat', with resume_prompt saying what to do when woken. This chat is woken "
            "with a [Monitor event] message when the condition holds, or when max_checks/ttl run out. "
            "After registering, END YOUR TURN with one sentence telling the user what you are waiting for.\n"
            "The wake-up turn runs unattended: it cannot ask the user anything (no ask_question, no approvals), so "
            "resume_prompt must contain every detail needed to finish the task and report in the chat.\n"
            "wake_in: 'same_chat' (default) wakes THIS chat, with this conversation and your working memory -- "
            "use it to continue a task. 'new_chat' opens a fresh chat in this project for every wake-up and "
            "leaves this conversation untouched -- use it for recurring work triggered by a watch (e.g. write a "
            "report each time a price moves); the new chat sees only the monitor's message, so resume_prompt "
            "must say everything needed.\n"
            "2. WATCH something for the user ('tell me when the dollar rises 10 cents'): action='notify', "
            "with message as the notification text. It fires an OS notification each time.\n"
            "once: true fires a single time and ends the monitor; false keeps firing every time the condition "
            "holds. Leave it out for the default: once for wake_in='same_chat' (a task wait), recurring otherwise "
            "(watches, and wake_in='new_chat', which opens a new chat at every firing). Follow what the user asked. "
            "A recurring monitor remains active and fires again on its own schedule -- never tell resume_prompt "
            "to call register_monitor again, and never call register_monitor in a woken chat while the original "
            "monitor continues.\n"
            "check_type: 'timer' (no check, just wake after first_check_seconds/interval), 'http' (GET url; "
            "json_path like 'USDBRL.bid' or regex extracts the value), 'command' (shell command in the project; "
            "value is its output), 'file' (path; watch='mtime' or 'content'), 'llm' (a model answers question "
            "with web search; costly, interval >= 300).\n"
            "condition: 'always' (timer), 'changed', 'equals'/'not_equals'/'contains' value, 'regex' (value is "
            "the pattern), 'gt'/'ge'/'lt'/'le' value (numbers), 'delta' (moved by threshold since the last firing, "
            "direction up/down/any), 'exit_code' (command; value, default 0), 'exists'/'missing' (file), 'llm'.\n"
            "message may use {name}, {value}, {previous}, {delta}, {reference}, {checks}. "
            "Prefer a public JSON API with json_path over an llm check whenever the value can be read directly. "
            "The check runs once at registration and an error is returned instead of registering a monitor that cannot work."
        ),
    )
    async def register_monitor(
        name: str,
        interval_seconds: int,
        check_type: str = "timer",
        condition: str = "",
        action: str = "resume_chat",
        resume_prompt: str = "",
        message: str = "",
        url: str = "",
        json_path: str = "",
        regex: str = "",
        command: str = "",
        path: str = "",
        watch: str = "mtime",
        question: str = "",
        value: str = "",
        threshold: float = 0.0,
        direction: str = "any",
        first_check_seconds: int = -1,
        max_checks: int = 0,
        ttl_seconds: int = 0,
        once: bool | None = None,
        wake_in: str = "same_chat",
        description: str = "",
    ) -> str:
        project, project_store = _session()
        if project is None:
            return "register_monitor failed: no project is open."
        try:
            spec = build_spec(
                name=name, interval_seconds=interval_seconds, check_type=check_type,
                url=url, json_path=json_path, regex=regex, command=command, path=path, watch=watch,
                question=question, condition=condition, value=value, threshold=threshold,
                direction=direction, action=action, message=message, resume_prompt=resume_prompt,
                first_check_seconds=first_check_seconds, max_checks=max_checks, ttl_seconds=ttl_seconds,
                once=once, wake_in=wake_in, description=description, project=project,
                sessions_db=getattr(project_store, "db_path", "") or "",
            )
            store = _store()
            waking = _active_waking_monitor(store, project)
            if waking is not None:
                return (
                    f"register_monitor was not run: this turn was woken by monitor {waking['id']} "
                    f"\"{waking['name']}\", which is recurring and remains active "
                    f"(next check at {_when(waking.get('next_check_at'))}, every {waking['interval_seconds']} s). "
                    "Do not re-register it; finish this turn's task and reply."
                )
            duplicate = _find_duplicate_active(store, spec)
            if duplicate is not None:
                return (
                    f"register_monitor was not run: monitor {duplicate['id']} \"{duplicate['name']}\" is already active "
                    f"(next check at {_when(duplicate.get('next_check_at'))}, every {duplicate['interval_seconds']} s). "
                    "Cancel it with cancel_monitor first if you need to replace it."
                )
            monitor, observed, daemon = await register(spec)
        except MonitorSpecError as exc:
            return f"register_monitor failed: {exc}"
        lines = [
            f"Registered monitor {monitor['id']} \"{monitor['name']}\": "
            f"first check at {_when(monitor['next_check_at'])}, then every {monitor['interval_seconds']} s"
            + (f", at most {monitor['max_checks']} checks" if monitor.get("max_checks") else "")
            + (f", until {_when(monitor['expires_at'])}" if monitor.get("expires_at") else "")
            + ".",
        ]
        lines.append("It fires every time the condition holds." if monitor["repeat"]
                     else "It fires once, then ends.")
        if observed is not None:
            lines.append(
                f"Current value: {display_value(observed['value'], 200)} "
                f"(condition holds now: {'yes' if observed['evaluation'].met else 'no'})."
            )
        if daemon.get("error"):
            lines.append(f"WARNING: {daemon['error']}")
        elif daemon.get("autostart_installed"):
            lines.append("The monitor service was set to start at login.")
        if monitor["action"].get("chat") == WAKE_CHAT_NEW:
            lines.append("Each wake-up opens a new chat in this project; this conversation is left as it is.")
        elif monitor["action"]["type"] in RESUMING_ACTIONS:
            lines.append("End your turn now; this chat will be woken by the monitor.")
        return "\n".join(lines)

    @opalatex_tool(
        name="list_monitors",
        is_safe=True,
        description=(
            "List monitors with their id, status, what they watch, last value and errors. "
            "status: 'active' (default), 'paused', 'all', or a final state "
            "('completed', 'expired', 'cancelled', 'error'). Use it to find the id before cancel_monitor or pause_monitor."
        ),
    )
    def list_monitors(status: str = "active") -> str:
        wanted = str(status or "active").strip().lower()
        if wanted == "all":
            statuses = None
        elif wanted in st.ALL_STATUSES:
            statuses = (wanted,)
        else:
            return f"list_monitors failed: unknown status '{status}'. Use active, paused, all, {', '.join(st.FINAL_STATUSES)}."
        monitors = _store().list_monitors(statuses)
        if not monitors:
            return f"No monitors with status '{wanted}'."
        shown = monitors[-MAX_LISTED:]
        header = f"{len(monitors)} monitor(s)" + (f", the last {MAX_LISTED} shown" if len(monitors) > MAX_LISTED else "") + ":"
        return header + "\n" + "\n".join(describe(m) for m in shown)

    @opalatex_tool(
        name="cancel_monitor",
        is_safe=True,
        description="Cancel a monitor for good, by its id (see list_monitors). It stops checking and never fires again.",
    )
    def cancel_monitor(monitor_id: str) -> str:
        store = _store()
        monitor = store.get_monitor(str(monitor_id or "").strip())
        if monitor is None:
            return f"cancel_monitor failed: no monitor '{monitor_id}'. Use list_monitors status='all' to see the ids."
        if monitor["status"] in st.FINAL_STATUSES:
            return f"Monitor {monitor['id']} is already {monitor['status']}."
        store.set_status(monitor["id"], st.STATUS_CANCELLED, "cancelled by the agent")
        return f"Cancelled monitor {monitor['id']} \"{monitor['name']}\"."

    @opalatex_tool(
        name="pause_monitor",
        is_safe=True,
        description="Pause a monitor (paused=true) or resume a paused one (paused=false), by its id. A paused monitor does not check.",
    )
    async def pause_monitor(monitor_id: str, paused: bool = True) -> str:
        store = _store()
        monitor = store.get_monitor(str(monitor_id or "").strip())
        if monitor is None:
            return f"pause_monitor failed: no monitor '{monitor_id}'. Use list_monitors status='all' to see the ids."
        if paused:
            if monitor["status"] != st.STATUS_ACTIVE:
                return f"Monitor {monitor['id']} is {monitor['status']}, not active; only an active monitor can be paused."
            store.set_status(monitor["id"], st.STATUS_PAUSED, "paused by the agent")
            return f"Paused monitor {monitor['id']} \"{monitor['name']}\"."
        if not store.resume_monitor(monitor["id"]):
            return f"Monitor {monitor['id']} is {monitor['status']}, not paused; only a paused monitor can be resumed."
        from .service import ensure_daemon_running

        await asyncio.to_thread(ensure_daemon_running, store)
        return f"Resumed monitor {monitor['id']} \"{monitor['name']}\"."

    return [register_monitor, list_monitors, cancel_monitor, pause_monitor]


MONITORS_PROMPT_BLOCK = (
    "\n## Monitors: waiting and watching in the background\n"
    "When a task depends on something that finishes later (a long build, a download, a job you "
    "started with run_background_command), do not poll it in a loop and do not keep the turn open. "
    "Register a monitor with register_monitor (action='resume_chat', with resume_prompt saying "
    "what to do next), tell the user in one sentence what you are waiting for, and end your turn. "
    "The chat is woken with a [Monitor event] message: if the work is done, continue; if the "
    "monitor ran out of checks, either register another one or say so in your reply and let the user "
    "decide there. A monitor's turn runs with nobody present and cannot ask anything outside its "
    "chat, so settle the details with the user BEFORE registering and write a resume_prompt complete "
    "enough to act on without questions (what to look for, where, in what form to report it). The wake-up runs in this chat by default; wake_in='new_chat' "
    "opens a fresh chat for each wake-up instead, for recurring work that should not crowd this "
    "conversation. A recurring monitor (once=False or wake_in='new_chat') stays active and schedules "
    "every cycle itself: never instruct resume_prompt to call register_monitor again, and in a woken "
    "chat never re-register a monitor that is still active.\n"
    "When the user asks to be told about something later (a price, a page, a file, a condition), "
    "register a monitor with action='notify'; it shows an OS notification and keeps working while "
    "OpalaTex is closed. Read a value from a JSON API with json_path when one exists; use an llm "
    "check only for conditions that need judgment. list_monitors, pause_monitor and cancel_monitor "
    "manage them; the user also manages them in the Monitors panel and with `opalatex monitors`.\n"
)
