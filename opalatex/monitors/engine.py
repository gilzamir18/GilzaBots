"""The scheduler: runs due checks, decides firings, and carries out actions.

One ``tick`` takes every active monitor whose ``next_check_at`` has passed,
observes it (``checks.run_check``), applies its condition (``spec.evaluate``)
and records the result. A firing enqueues what the action asks for -- an OS
notification (delivered by the daemon) and/or a wake-up of the monitor's chat
(run by the window or the daemon, see ``daemon``). The engine itself never
runs an agent turn.

Lifecycle, in the order it is applied on a check:

1. past ``expires_at`` -> ``expired`` (no check is made);
2. a check error -> counted; ``MAX_CONSECUTIVE_ERRORS`` in a row -> ``error``;
3. fired and not ``repeat`` -> ``completed``;
4. ``max_checks`` used up -> ``expired``.

A task wait (a monitor that wakes the chat it was registered from) also wakes
it when it ends in 1, 2 or 4, with the reason, so the orchestrator that was
waiting can decide whether to wait longer or tell the user -- it is never left
waiting for a wake-up that will not come. Any other monitor that ends gets a
notification instead (2 already sends a critical one); no chat is opened just
to report an ending.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import store as st
from .checks import CheckError, LlmRunner, run_check
from .spec import (
    NOTIFYING_ACTIONS,
    RESUMING_ACTIONS,
    WAKE_CHAT_NEW,
    ConditionError,
    Evaluation,
    display_value,
    evaluate,
    render_template,
)

#: A check that fails this many times in a row stops its monitor: the target is
#: gone or the specification is wrong, and checking forever would only hide it.
MAX_CONSECUTIVE_ERRORS = 5

EVENT_CONDITION_MET = "condition_met"
EVENT_EXHAUSTED = "exhausted"
EVENT_EXPIRED = "expired"
EVENT_ERROR = "error"


def _t(key: str, **kwargs) -> str:
    from opalatex.i18n import _

    return _(key, **kwargs)


def create_wake_chat(monitor: dict) -> str:
    """Create the chat a ``chat: new`` monitor wakes, in its project. Returns its id.

    Raises RuntimeError when the project no longer exists: the wake-up is then
    reported, not redirected to some other project or chat.
    """
    import datetime
    import uuid

    from opalatex.project import ProjectStore

    sessions = ProjectStore(db_path=monitor["sessions_db"]) if monitor.get("sessions_db") else ProjectStore()
    if not sessions.exists(monitor["project"]):
        raise RuntimeError(f"the project '{monitor['project']}' no longer exists")
    chat_id = str(uuid.uuid4())
    name = _t("monitor_new_chat_name", name=monitor["name"],
              time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    sessions.create_chat(monitor["project"], chat_id, name)
    return chat_id


def wake_prompt(monitor: dict, event: str, value: Any, variables: dict, error: str = "",
                new_chat: bool = False, continues: bool | None = None) -> str:
    """The message the orchestrator receives when its monitor wakes the chat.

    Model-facing, so always English, like the rest of the system prompt.
    """
    if continues is None:
        max_checks = monitor.get("max_checks")
        exhausted = bool(max_checks and int(variables.get("checks") or 0) >= int(max_checks))
        continues = bool(event == EVENT_CONDITION_MET and monitor.get("repeat") and not exhausted)
    what = {
        EVENT_CONDITION_MET: "fired: its condition was met",
        EVENT_EXHAUSTED: f"used all {monitor.get('max_checks')} of its checks without its condition being met",
        EVENT_EXPIRED: "reached its time limit without its condition being met",
        EVENT_ERROR: f"stopped after {MAX_CONSECUTIVE_ERRORS} consecutive failed checks. Last error: {error}",
    }[event]
    where = "registered in another chat of this project" if new_chat else "registered in this chat"
    lines = [
        f"[Monitor event: {event}] The monitor \"{monitor['name']}\" ({monitor['id']}) {where} {what}.",
    ]
    if new_chat:
        lines.append(
            "This is a new chat the monitor opened for this wake-up: the conversation that registered "
            "it is not in your context, so everything you need is in this message and in the project's files."
        )
    shown = display_value(value)
    if shown:
        lines.append(f"Observed value: {shown}")
    if "delta" in variables:
        lines.append(f"Change since the reference: {variables['delta']:+.6g} (reference {variables.get('reference')})")
    lines.append(f"Checks made: {variables.get('checks', 0)}.")
    instructions = (monitor.get("action") or {}).get("prompt", "")
    if instructions:
        lines.append(f"What you asked to be told to do when woken: {instructions}")
    if continues:
        lines.append(
            f"Monitor status: \"{monitor['name']}\" ({monitor['id']}) is recurring and remains active "
            f"(it checks every {monitor.get('interval_seconds')} s on its own). Do NOT call register_monitor "
            "to re-create or renew this monitor, even if the instructions above ask to maintain recurrence -- "
            "the original monitor continues running, and re-registering it creates duplicate monitors."
        )
        tail = "Do not call register_monitor to renew this monitor; end your turn once you have replied."
    elif new_chat:
        tail = (
            f"The monitor \"{monitor['name']}\" ({monitor['id']}) was set to fire once and has now ended; "
            "do not re-register it."
        )
    else:
        tail = (
            f"The monitor \"{monitor['name']}\" ({monitor['id']}) has ended. "
            "To keep waiting, register a new monitor with register_monitor and end your turn."
        )
    lines.append(
        "This message was written by the monitor, not typed by the user, and nobody is watching this "
        "turn. " + ("Do what the instructions above ask. " if new_chat else "Continue the task from where you stopped. ")
        + "Work autonomously: do not ask questions (ask_question, plan approval and actions that need "
        "the user's approval are unavailable in this turn). Where the instructions leave a choice open, "
        "make the reasonable choice and say which one you made. Your reply in this chat is how the user "
        "receives the result; if something blocks the task, say in that reply what you need and end the "
        f"turn -- the user answers in this chat. {tail}"
    )
    return "\n".join(lines)


class MonitorEngine:
    def __init__(self, store: st.MonitorStore, *, llm_runner: LlmRunner | None = None,
                 clock=time.time, max_parallel: int = 4, chat_creator=create_wake_chat):
        self.store = store
        self.llm_runner = llm_runner
        self.chat_creator = chat_creator
        self.clock = clock
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._in_flight: set[str] = set()

    async def tick(self) -> int:
        """Process every due monitor; returns how many were checked."""
        due = [m for m in self.store.due_monitors(self.clock()) if m["id"] not in self._in_flight]
        if not due:
            return 0
        await asyncio.gather(*(self._guarded(monitor) for monitor in due))
        return len(due)

    async def _guarded(self, monitor: dict) -> None:
        self._in_flight.add(monitor["id"])
        try:
            async with self._semaphore:
                # Re-read: it may have been paused or cancelled since it was listed.
                current = self.store.get_monitor(monitor["id"])
                if current is None or current["status"] != st.STATUS_ACTIVE:
                    return
                await self.process(current)
        finally:
            self._in_flight.discard(monitor["id"])

    async def observe(self, monitor: dict) -> Any:
        """One observation. Raises CheckError."""
        return await run_check(monitor, llm_runner=self.llm_runner)

    def judge(self, monitor: dict, value: Any) -> Evaluation:
        """Apply the condition to *value* against the stored state. Raises ConditionError."""
        return evaluate(
            monitor["check"], monitor["condition"], value,
            previous=monitor.get("last_value"),
            has_previous=bool(monitor.get("has_value")),
            previous_met=monitor.get("last_met"),
            reference=monitor.get("reference"),
        )

    async def process(self, monitor: dict) -> None:
        now = self.clock()
        expires_at = monitor.get("expires_at")
        if expires_at and now >= expires_at:
            self._end(monitor, st.STATUS_EXPIRED, EVENT_EXPIRED, monitor.get("last_value"))
            return

        next_check = now + monitor["interval_seconds"]
        try:
            value = await self.observe(monitor)
            evaluation = self.judge(monitor, value)
        except (CheckError, ConditionError) as exc:
            self._record_error(monitor, str(exc), next_check)
            return
        except Exception as exc:  # a bug in a check must not stop the scheduler
            self._record_error(monitor, f"{type(exc).__name__}: {exc}", next_check)
            return

        checks = int(monitor.get("check_count", 0)) + 1
        self.store.record_check(
            monitor["id"], value=value, met=evaluation.met,
            reference=evaluation.reference if evaluation.reference is not None else monitor.get("reference"),
            next_check_at=next_check, fired=evaluation.fire,
        )
        detail = {"met": evaluation.met, "fired": evaluation.fire, **(evaluation.details or {})}
        self.store.add_event(monitor["id"], "check", display_value(value), detail)

        max_checks = monitor.get("max_checks")
        exhausted = bool(max_checks and checks >= int(max_checks))
        if evaluation.fire:
            continues = bool(monitor.get("repeat") and not exhausted)
            # Finalize status before queueing the wake-up so the turn it starts
            # sees whether this monitor is still active or has ended.
            if not monitor.get("repeat"):
                self.store.set_status(monitor["id"], st.STATUS_COMPLETED, "fired")
            elif exhausted:
                refreshed = self.store.get_monitor(monitor["id"]) or monitor
                self._end(refreshed, st.STATUS_EXPIRED, EVENT_EXHAUSTED, value, checks=checks, wake=False)
            variables = self._variables(monitor, value, evaluation, checks)
            self.fire(monitor, EVENT_CONDITION_MET, value, variables, continues=continues)
            return
        if exhausted:
            refreshed = self.store.get_monitor(monitor["id"]) or monitor
            self._end(refreshed, st.STATUS_EXPIRED, EVENT_EXHAUSTED, value, checks=checks, wake=True)

    def _record_error(self, monitor: dict, error: str, next_check: float) -> None:
        failures = self.store.record_check_error(monitor["id"], error, next_check)
        self.store.add_event(monitor["id"], "error", error, {"consecutive": failures})
        if failures >= MAX_CONSECUTIVE_ERRORS:
            refreshed = self.store.get_monitor(monitor["id"]) or monitor
            self._end(refreshed, st.STATUS_ERROR, EVENT_ERROR, refreshed.get("last_value"), error=error)

    def _end(self, monitor: dict, status: str, event: str, value: Any, *, checks: int | None = None,
             error: str = "", wake: bool = True) -> None:
        self.store.set_status(monitor["id"], status, event if not error else f"{event}: {error}")
        variables = {
            "name": monitor["name"],
            "value": value,
            "checks": checks if checks is not None else monitor.get("check_count", 0),
        }
        action = monitor.get("action") or {}
        task_wait = action.get("type") in RESUMING_ACTIONS and action.get("chat") != WAKE_CHAT_NEW
        if wake and task_wait:
            # The chat that is waiting must learn the wait is over, or it waits forever.
            self._enqueue_wake(monitor, event, value, variables, error)
        elif event != EVENT_ERROR:
            # A watch that ends (out of checks or time) says so, without opening
            # a chat only to report it.
            self.store.enqueue_notification(
                _t("monitor_wake_done_title", name=monitor["name"]),
                _t(f"monitor_wake_display_{event}", name=monitor["name"]),
                urgency="low",
                project=monitor.get("project", ""),
                project_path=monitor.get("project_path", ""),
                chat_id=monitor.get("chat_id", ""),
            )
        if event == EVENT_ERROR:
            self.store.enqueue_notification(
                _t("monitor_error_title", name=monitor["name"]),
                error,
                urgency="critical",
                project=monitor.get("project", ""),
                project_path=monitor.get("project_path", ""),
                chat_id=monitor.get("chat_id", ""),
            )

    @staticmethod
    def _variables(monitor: dict, value: Any, evaluation: Evaluation, checks: int) -> dict:
        variables: dict[str, Any] = {
            "name": monitor["name"],
            "value": value,
            "previous": monitor.get("last_value"),
            "checks": checks,
        }
        details = evaluation.details or {}
        if "delta" in details:
            variables["delta"] = details["delta"]
            variables["reference"] = details.get("reference")
        if isinstance(value, dict) and "summary" in value:
            variables["summary"] = value.get("summary", "")
        return variables

    def fire(self, monitor: dict, event: str, value: Any, variables: dict, *,
             continues: bool | None = None) -> None:
        action = monitor.get("action") or {}
        kind = action.get("type")
        message = ""
        if kind in NOTIFYING_ACTIONS:
            template = action.get("message") or _t("monitor_fired_default_body")
            message = render_template(template, variables)
            title = render_template(action.get("title") or "", variables) or _t(
                "monitor_fired_title", name=monitor["name"]
            )
            self.store.enqueue_notification(
                title, message,
                project=monitor.get("project", ""),
                project_path=monitor.get("project_path", ""),
                chat_id=monitor.get("chat_id", ""),
            )
        if kind in RESUMING_ACTIONS:
            self._enqueue_wake(monitor, event, value, variables, continues=continues)
        self.store.add_event(monitor["id"], "fired", message or display_value(value), {"event": event})

    def _enqueue_wake(self, monitor: dict, event: str, value: Any, variables: dict, error: str = "",
                      *, continues: bool | None = None) -> None:
        new_chat = (monitor.get("action") or {}).get("chat") == WAKE_CHAT_NEW
        chat_id = monitor.get("chat_id", "")
        if new_chat:
            try:
                chat_id = self.chat_creator(monitor)
            except Exception as exc:
                reason = f"Could not open a new chat for the wake-up: {exc}"
                self.store.add_event(monitor["id"], "error", reason, {"event": event})
                self.store.enqueue_notification(
                    _t("monitor_wake_done_title", name=monitor["name"]), reason, urgency="critical",
                    project=monitor.get("project", ""), project_path=monitor.get("project_path", ""),
                )
                return
        if not chat_id:
            # Validation requires a chat for `origin`; a monitor stored before
            # that rule must still not wake whatever chat happens to be open.
            reason = "The monitor names no chat to wake; register it again from a chat, or with a new chat each time."
            self.store.add_event(monitor["id"], "error", reason, {"event": event})
            self.store.enqueue_notification(
                _t("monitor_wake_done_title", name=monitor["name"]), reason, urgency="critical",
                project=monitor.get("project", ""), project_path=monitor.get("project_path", ""),
            )
            return
        prompt = wake_prompt(monitor, event, value, variables, error, new_chat=new_chat, continues=continues)
        display = _t(f"monitor_wake_display_{event}", name=monitor["name"])
        wake_id = self.store.enqueue_wake(monitor, prompt, display, chat_id=chat_id, new_chat=new_chat)
        self.store.add_event(monitor["id"], "wake_queued", display,
                             {"wake_request_id": wake_id, "event": event, "chat_id": chat_id, "new_chat": new_chat})

    async def dry_run(self, monitor: dict) -> dict:
        """Check now and report what would happen, changing nothing ("Test")."""
        try:
            value = await self.observe(monitor)
        except CheckError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            evaluation = self.judge(monitor, value)
        except ConditionError as exc:
            return {"ok": False, "error": str(exc), "value": display_value(value)}
        return {
            "ok": True,
            "value": display_value(value),
            "raw_value": value,
            "met": evaluation.met,
            "would_fire": evaluation.fire,
            "details": evaluation.details or {},
        }
