"""The monitor daemon: ``opalatex monitors daemon``.

One per user (``service.DaemonLock``). Every ``POLL_SECONDS`` it

1. runs the checks that are due (``engine.MonitorEngine.tick``);
2. delivers queued OS notifications -- it is the long-lived process, so it is
   the one that can receive the click (``notify``);
3. dispatches wake-ups. A wake-up of a project the window has open is left to
   the window, which runs it as an ordinary, visible turn; any other is run
   here, in a child process (``opalatex monitors run-wake <id>``), never in
   this process, because a turn binds the project, store and orchestrator in
   module globals and the daemon serves every project;
4. reconciles: a claimed wake-up or a pending question whose process died is
   closed, so nothing waits on a process that no longer exists.

It exits on its own after ``IDLE_EXIT_SECONDS`` with nothing to do -- no active
monitor, no wake-up, no question, no child, no recent notification whose
click it should still be able to receive. Registering a monitor starts it
again (``service.ensure_daemon_running``).
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

from opalatex import __version__

from . import service
from . import store as st
from .engine import MonitorEngine
from .notify import Notifier, NotifyError, default_notifier, open_uri, parse_open_uri
from .process import opalatex_command, pid_alive

POLL_SECONDS = 1.0
IDLE_EXIT_SECONDS = 15 * 60
#: Keep listening for clicks this long after the last notification.
CLICK_WINDOW_SECONDS = 60 * 60
MAX_WAKE_CHILDREN = 2
#: How often the daemon checks whether the installed code changed under it.
CODE_CHECK_SECONDS = 30.0
HOST_KIND = "daemon"


def _t(key: str, **kwargs) -> str:
    from opalatex.i18n import _

    return _(key, **kwargs)


def _apply_ui_language() -> None:
    try:
        from opalatex.i18n import set_lang
        from opalatex.ui_settings import load_ui_settings

        lang = str(load_ui_settings().get("lang") or "")
        set_lang("pt" if lang.startswith("pt") else "en")
    except Exception:
        pass


class MonitorDaemon:
    def __init__(self, store: st.MonitorStore, *, notifier: Notifier | None = None,
                 engine: MonitorEngine | None = None, clock=time.time,
                 child_argv=None, idle_exit_seconds: float = IDLE_EXIT_SECONDS,
                 fingerprint=service.code_fingerprint):
        self.store = store
        self.clock = clock
        self.notifier = notifier or default_notifier(opalatex_command() + ["monitors", "open"])
        if engine is None:
            from .llm_check import run_llm_check_subprocess

            engine = MonitorEngine(store, llm_runner=run_llm_check_subprocess, clock=clock)
        self.engine = engine
        self.child_argv = child_argv or (lambda wake_id: opalatex_command() + ["monitors", "run-wake", str(wake_id)])
        self.children: dict[int, tuple[subprocess.Popen, tuple[str, str]]] = {}
        self.idle_exit_seconds = idle_exit_seconds
        self._last_busy = clock()
        self._last_notification = 0.0
        self._stopping = False
        self.fingerprint = fingerprint
        self._started_with = fingerprint()
        self._last_code_check = clock()
        #: Set when the installed code changed: run_daemon re-executes itself.
        self.restart_requested = False

    # ── Notifications ───────────────────────────────────────────────────────

    def deliver_notifications(self) -> int:
        delivered = 0
        for item in self.store.undelivered_notifications():
            uri = open_uri(item["project"], item["chat_id"], item["project_path"]) if item["chat_id"] else ""
            try:
                self.notifier.send(
                    item["title"], item["body"], urgency=item["urgency"] or "normal",
                    uri=uri, action_label=_t("monitor_notification_open"),
                )
                self.store.mark_notification(item["id"], self.notifier.backend)
            except NotifyError as exc:
                self.store.mark_notification(item["id"], self.notifier.backend, str(exc))
                print(f"[monitors] notification not shown: {exc}", file=sys.stderr, flush=True)
            delivered += 1
            self._last_notification = self.clock()
        return delivered

    def open_from_notification(self, uri: str) -> None:
        try:
            target = parse_open_uri(uri)
        except ValueError:
            return
        try:
            service.open_chat(self.store, target["project"], target["chat_id"], target["project_path"])
        except Exception as exc:
            print(f"[monitors] could not open the chat: {exc}", file=sys.stderr, flush=True)

    # ── Wake-ups ────────────────────────────────────────────────────────────

    def _window_has(self, wake: dict, gui_hosts: list[dict]) -> bool:
        return any(
            (host.get("project") and host["project"] == wake["project"])
            or (host.get("project_path") and host["project_path"] == wake["project_path"])
            for host in gui_hosts
        )

    def dispatch_wakes(self) -> int:
        started = 0
        self.store.fail_unrunnable_wakes(HOST_KIND, os.getpid())
        gui_hosts = self.store.live_hosts("gui", service.GUI_ALIVE_SECONDS)
        running_chats = {chat for _proc, chat in self.children.values()}
        for wake in self.store.list_wakes((st.WAKE_PENDING,)):
            chat = (wake["project"], wake["chat_id"])
            if self._window_has(wake, gui_hosts):
                continue  # the window runs it, visibly
            if chat in running_chats or self.store.lease_holder(*chat):
                continue  # one turn per chat; it waits for the current one
            if len(self.children) >= MAX_WAKE_CHILDREN:
                break
            if not self.store.claim_wake(wake["id"], HOST_KIND, os.getpid()):
                continue
            try:
                with open(os.path.join(service.monitors_dir(), "wake.log"), "ab") as log:
                    process = subprocess.Popen(
                        self.child_argv(wake["id"]),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
                    )
            except OSError as exc:
                self.store.finish_wake(wake["id"], st.WAKE_FAILED, f"could not start the turn: {exc}")
                continue
            self.children[wake["id"]] = (process, chat)
            running_chats.add(chat)
            started += 1
        return started

    def reap_children(self) -> None:
        for wake_id, (process, _chat) in list(self.children.items()):
            code = process.poll()
            if code is None:
                continue
            del self.children[wake_id]
            # The child finishes its own request; one still claimed died early.
            self.store.finish_wake(wake_id, st.WAKE_FAILED, f"the turn process exited with code {code}")

    def reconcile(self) -> None:
        for wake in self.store.list_wakes((st.WAKE_CLAIMED,)):
            if wake["id"] in self.children:
                continue
            pid = int(wake.get("claimed_pid") or 0)
            if pid and not pid_alive(pid):
                self.store.finish_wake(wake["id"], st.WAKE_INTERRUPTED, "the process running the turn ended")
        for item in self.store.list_inputs(st.INPUT_WAITING):
            pid = int(item.get("holder_pid") or 0)
            if pid and not pid_alive(pid):
                self.store.close_input(item["id"])

    # ── Loop ────────────────────────────────────────────────────────────────

    def busy(self) -> bool:
        return bool(
            self.store.count_active()
            or self.children
            or self.store.list_wakes((st.WAKE_PENDING, st.WAKE_CLAIMED))
            or self.store.list_inputs(st.INPUT_WAITING)
            or self.store.undelivered_notifications()
            or (self._last_notification and self.clock() - self._last_notification < CLICK_WINDOW_SECONDS)
        )

    async def step(self) -> None:
        # Before anything is checked or dispatched: work done by stale code is
        # what this guards against.
        if self.check_code():
            return
        self.store.touch_host(HOST_KIND, os.getpid())
        await self.engine.tick()
        self.deliver_notifications()
        self.reap_children()
        self.dispatch_wakes()
        self.reconcile()
        if self.busy():
            self._last_busy = self.clock()

    def check_code(self) -> bool:
        """Stop for a restart when the installed code is no longer what runs."""
        if self.clock() - self._last_code_check < CODE_CHECK_SECONDS:
            return False
        self._last_code_check = self.clock()
        if self.fingerprint() == self._started_with:
            return False
        print("[monitors] the installed OpalaTex code changed; restarting the daemon to load it.", flush=True)
        self.restart_requested = True
        self._stopping = True
        return True

    def should_exit(self) -> bool:
        return self._stopping or (self.clock() - self._last_busy >= self.idle_exit_seconds)

    def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        self.notifier.start_listener(self.open_from_notification)
        last_lang = 0.0
        try:
            while not self.should_exit():
                if time.monotonic() - last_lang > 60:
                    _apply_ui_language()
                    last_lang = time.monotonic()
                try:
                    await self.step()
                except Exception as exc:  # keep serving the other monitors
                    import traceback

                    traceback.print_exc()
                    print(f"[monitors] step failed: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(POLL_SECONDS)
        finally:
            self.notifier.stop()
            self.store.remove_host(HOST_KIND, os.getpid())


def run_daemon() -> int:
    """Entry point of ``opalatex monitors daemon``. Returns the exit code."""
    lock = service.DaemonLock()
    if not lock.acquire():
        print("[monitors] another monitor daemon is already running.", flush=True)
        return 0
    store = st.MonitorStore()
    daemon = MonitorDaemon(store)
    service.write_state({"pid": os.getpid(), "started_at": time.time(), "version": __version__})

    def _terminate(*_args) -> None:
        daemon.stop()

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _terminate)
            except (ValueError, OSError):
                pass
    print(f"[monitors] daemon {os.getpid()} started (notifications: {daemon.notifier.backend}).", flush=True)
    try:
        asyncio.run(daemon.run())
    finally:
        lock.release()
    if daemon.restart_requested:
        _restart_with_new_code()
    print("[monitors] daemon stopped.", flush=True)
    return 0


def _restart_with_new_code() -> None:
    """Replace this process with a daemon running the installed code.

    ``exec`` keeps the PID, so a systemd unit or LaunchAgent sees the same
    service. Windows has no real exec: a new detached daemon is started and this
    one exits. The argv is rebuilt from the installation (``daemon_argv``), not
    copied from ``sys.argv``, which under ``-m`` names a module file.
    """
    argv = service.daemon_argv()
    sys.stdout.flush()
    sys.stderr.flush()
    if sys.platform == "win32":
        from .process import spawn_detached

        spawn_detached(argv, log_path=service.log_path())
        return
    os.execv(argv[0], argv)
