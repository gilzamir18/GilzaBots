"""A wake-up turn with no front-end: ``opalatex monitors run-wake <id>``.

Started by the daemon for a chat no window has open. It runs the chat's
orchestrator through the same ``agent_stdin.handle_run`` the window and the CLI
drive -- the orchestrator restores its saved working state for the chat, so it
continues where it stopped -- with the monitor's event as the turn's message.

**Questions wait for the user.** A question the turn has to ask (permission for
an unsafe tool in ``edit`` mode, plan approval, ``ask_question``) is stored as a
pending input and announced with an OS notification; the turn keeps waiting
until it is answered from the window or ``opalatex monitors answer``, within
``request_user_input``'s own timeout. An interactive terminal command is the one
exception: it needs a person at a terminal, which this process does not have, so
it is declined with a diagnostic the agent receives.

The turn holds the chat's lease (``store.acquire_lease``) for its whole length,
so the window or the CLI cannot start a turn in the same chat meanwhile.
"""
from __future__ import annotations

import asyncio
import os
import re

from . import store as st

INPUT_POLL_SECONDS = 2.0
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
        if event in ("agent_response", "error", "tool_call", "input_request"):
            print(f"[wake] {event}: {str(payload)[:400]}", flush=True)


def make_input_transport(store: st.MonitorStore, wake: dict, *, poll: float = INPUT_POLL_SECONDS):
    """The input transport of a turn nobody is watching: store, notify, wait."""

    async def transport(request: dict) -> str:
        if request.get("type") == "interactive_terminal":
            from opalatex.agent_stdin import print_event

            print_event("problem", {
                "agent": "monitor",
                "severity": "warning",
                "message": (
                    "An interactive terminal command cannot run in a monitor wake-up: no terminal "
                    "is attached. Ask the user to run it, or use run_background_command."
                ),
            })
            return "cancel"
        store.add_input(
            request,
            wake_request_id=wake["id"],
            project=wake["project"],
            project_path=wake["project_path"],
            chat_id=wake["chat_id"],
            monitor_name=wake["monitor_name"],
            pid=os.getpid(),
        )
        store.enqueue_notification(
            _t("monitor_input_title", name=wake["monitor_name"]),
            _plain(request.get("prompt", ""), 300),
            urgency="critical",
            project=wake["project"],
            project_path=wake["project_path"],
            chat_id=wake["chat_id"],
        )
        try:
            while True:
                item = store.get_input(request["id"])
                if item is None or item["status"] == st.INPUT_CLOSED:
                    # Withdrawn: an empty answer, which every caller reads as
                    # "not approved" / "no answer", never as consent.
                    return ""
                if item["status"] == st.INPUT_ANSWERED:
                    return str(item.get("answer") or "")
                await asyncio.sleep(poll)
        finally:
            store.close_input(request["id"])

    return transport


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
        from opalatex.cli_render import terminal_background_command
        from opalatex.project import ProjectStore
        from opalatex.tools import set_project_context
        import litellm

        from opalatex.project import ChatNotFoundError

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
        agent_stdin.set_input_transport(make_input_transport(store, wake))
        # Commands the turn starts in the background outlive it: a build the
        # agent waits on with another monitor must keep running.
        agent_stdin.set_background_command_runner(terminal_background_command)
        await agent_stdin.handle_run({
            "agent": "chat_orchestrator",
            "prompt": wake["prompt"],
            "display_prompt": wake["display"],
            "project_name": project.name,
            "project_path": project.project_path,
            "chat_id": wake["chat_id"],
            "db": sessions.db_path,
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
        agent_stdin.set_input_transport(None)
        agent_stdin.event_hook = None
        try:
            import litellm

            litellm.event_hook = None
        except Exception:
            pass
        store.release_lease(wake["project"], wake["chat_id"], pid)
        store.finish_wake(wake_id, status, error)
        body = _plain(collector.response) if status == st.WAKE_DONE else (error or _t("monitor_wake_failed"))
        store.enqueue_notification(
            _t("monitor_wake_done_title", name=wake["monitor_name"]),
            body or _t("monitor_wake_no_answer"),
            urgency="normal" if status == st.WAKE_DONE else "critical",
            project=wake["project"],
            project_path=wake["project_path"],
            chat_id=wake["chat_id"],
        )
