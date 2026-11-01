"""A wake-up turn with no front-end: ``opalatex monitors run-wake <id>``.

Started by the daemon for a chat no window has open. It runs the chat's
orchestrator through the same ``agent_stdin.handle_run`` the window and the CLI
drive -- the orchestrator restores its saved working state for the chat, so it
continues where it stopped -- with the monitor's event as the turn's message.

**Nobody is present, and the chat is the only channel.** The turn is marked
unattended (``monitor_wake`` in the run data, ``agent_stdin.unattended_turn``):
tool permission in ``edit`` mode, plan approval, ``ask_question`` and interactive
commands refuse with a diagnostic the agent receives, instead of opening a
question outside the chat. The agent reports, or says what it needs, in its
reply; the user answers in that chat. The window applies the same rule to the
wake-ups it runs.

The turn holds the chat's lease (``store.acquire_lease``) for its whole length,
so the window or the CLI cannot start a turn in the same chat meanwhile.
"""
from __future__ import annotations

import os
import re

from . import store as st

HOLDER = "monitor"


def _t(key: str, **kwargs) -> str:
    from opalatex.i18n import _

    return _(key, **kwargs)


def _plain(text: str, limit: int = 240) -> str:
    text = re.sub(r"```.*?```", " ", str(text or ""), flags=re.DOTALL)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Collector:
    """The event hook: keeps what the notification needs, writes a log line."""

    def __init__(self):
        self.response = ""
        self.errors: list[str] = []

    def __call__(self, payload: dict) -> None:
        event = payload.get("event")
        if event == "agent_response" and not payload.get("intermediate"):
            self.response = str(payload.get("persisted_response") or payload.get("response") or "")
        elif event == "error":
            self.errors.append(str(payload.get("message") or ""))
        if event in ("agent_response", "error", "tool_call"):
            print(f"[wake] {event}: {str(payload)[:400]}", flush=True)


async def run_wake(wake_id: int, *, store: st.MonitorStore | None = None) -> int:
    store = store or st.MonitorStore()
    wake = store.get_wake(wake_id)
    if wake is None:
        print(f"[wake] no wake-up request {wake_id}.", flush=True)
        return 2
    if wake["status"] != st.WAKE_CLAIMED:
        print(f"[wake] request {wake_id} is {wake['status']}, not claimed.", flush=True)
        return 2
    pid = os.getpid()
    store.set_wake_pid(wake_id, pid)
    holder = store.acquire_lease(wake["project"], wake["chat_id"], pid, HOLDER)
    if holder is not None:
        # Another process started a turn in this chat since the daemon looked.
        store.release_wake(wake_id, wake["claimed_by"])
        print(f"[wake] chat busy (pid {holder['pid']}); request returned to the queue.", flush=True)
        return 0

    from opalatex import agent_stdin

    collector = _Collector()
    status, error = st.WAKE_FAILED, ""
    try:
        import litellm

        from opalatex.cli_render import terminal_background_command
        from opalatex.project import ChatNotFoundError, ProjectStore
        from opalatex.tools import set_project_context

        sessions = ProjectStore(db_path=wake["sessions_db"]) if wake["sessions_db"] else ProjectStore()
        try:
            project = sessions.load(wake["project"], chat_id=wake["chat_id"])
        except ChatNotFoundError:
            error = f"the chat {wake['chat_id']} no longer exists"
            return 1
        if project is None:
            error = f"the project '{wake['project']}' no longer exists"
            return 1
        set_project_context(project, sessions)
        agent_stdin.event_hook = collector
        litellm.event_hook = collector
        # Commands the turn starts in the background outlive it: a build the
        # agent waits on with another monitor must keep running.
        agent_stdin.set_background_command_runner(terminal_background_command)
        # No window: the turn gets neither the window's tools nor its prompt.
        agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_BACKGROUND)
        await agent_stdin.handle_run({
            "agent": "chat_orchestrator",
            "prompt": wake["prompt"],
            "display_prompt": wake["display"],
            "project_name": project.name,
            "project_path": project.project_path,
            "chat_id": wake["chat_id"],
            "db": sessions.db_path,
            "monitor_wake": wake["monitor_name"] or "monitor",
        })
        if collector.errors and not collector.response:
            error = collector.errors[-1]
        else:
            status = st.WAKE_DONE
        return 0 if status == st.WAKE_DONE else 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        agent_stdin.event_hook = None
        agent_stdin.set_reply_surface(None)
        try:
            import litellm

            litellm.event_hook = None
        except Exception:
            pass
        store.release_lease(wake["project"], wake["chat_id"], pid)
        store.finish_wake(wake_id, status, error, result=collector.response.strip())
        body = _plain(collector.response) if status == st.WAKE_DONE else (error or _t("monitor_wake_failed"))
        store.enqueue_notification(
            _t("monitor_wake_done_title", name=wake["monitor_name"]),
            body or _t("monitor_wake_no_answer"),
            urgency="normal" if status == st.WAKE_DONE else "critical",
            project=wake["project"],
            project_path=wake["project_path"],
            chat_id=wake["chat_id"],
        )
