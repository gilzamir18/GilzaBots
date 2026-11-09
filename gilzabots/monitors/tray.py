"""The tray icon of the monitor daemon: ``gilzabots monitors tray run``.

The icon shows that GilzaBots is working in the background and gives a short
menu: open the window, open the Monitors panel, pause or resume every monitor,
stop the background service. It belongs to the daemon, not to the window:
closing the window still closes the window, and the icon is there exactly while
the daemon is (``docs/specs/monitors.md``).

It is a process of its own, started and supervised by the daemon
(``TraySupervisor``), for two reasons:

- the daemon must keep working without a display (an SSH login, a login
  service started before the desktop), and the toolkit needs one;
- a native failure in the toolkit or in the desktop's tray must not take the
  monitors down with it.

The toolkit is Qt on macOS and Linux and WinForms (through pythonnet) on
Windows, which has no Qt (``tray_windows.py``).

The icon reads everything from ``monitors.db`` and acts through it, like every
other process (``store.py``); it needs no channel to the daemon. It exits when
the daemon's process ends or the installed code changes, and the daemon starts
a fresh one.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from typing import Callable

from . import service
from . import store as st
from .process import pid_alive

#: ``store`` setting: whether the daemon shows the icon (on by default).
TRAY_SETTING = "tray_icon"
#: ``status_reason`` of a monitor paused from the icon; *Resume* resumes only those.
PAUSED_FROM_TRAY = "paused from the tray icon"

#: Exit codes of ``run_tray`` the supervisor tells apart.
EXIT_NO_TRAY = 3
EXIT_DUPLICATE = 4

#: How often the supervisor looks at the setting and the icon's process.
SUPERVISE_SECONDS = 5.0
#: How long the icon waits for the desktop's tray to appear (a login session
#: registers it a moment after the login service starts the daemon).
TRAY_WAIT_SECONDS = 30.0
#: Retry delays after the icon could not be shown.
RETRY_NO_SESSION_SECONDS = 10.0
RETRY_NO_TRAY_SECONDS = 5 * 60.0
RETRY_DUPLICATE_SECONDS = 60.0
RETRY_NO_QT_SECONDS = 60 * 60.0
MAX_CRASH_BACKOFF_SECONDS = 10 * 60.0

#: Status of the icon as the panel shows it (``daemon_status()['tray']``).
STATE_RUNNING = "running"
STATE_DISABLED = "disabled"
STATE_STOPPED = "stopped"
STATE_NO_QT = "no_qt"
STATE_NO_SESSION = "no_session"
STATE_NO_TRAY = "no_tray"
STATE_FAILED = "failed"

NO_TRAY_REASON = (
    "The desktop has no system tray. On GNOME, enable the AppIndicator extension "
    "(Ubuntu ships it as 'Ubuntu AppIndicators')."
)
NO_SESSION_REASON = "No graphical session was found for the monitor service yet."

#: Variables a process needs to reach the user's graphical session on Linux.
GRAPHICAL_VARIABLES = (
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION",
)


def _t(key: str, **kwargs) -> str:
    from gilzabots.i18n import _

    return _(key, **kwargs)


# ── Paths and settings ──────────────────────────────────────────────────────

def tray_lock_path() -> str:
    return os.path.join(service.monitors_dir(), "tray.lock")


def tray_status_path() -> str:
    return os.path.join(service.monitors_dir(), "tray.json")


def tray_log_path() -> str:
    return os.path.join(service.monitors_dir(), "tray.log")


def tray_enabled(store: st.MonitorStore) -> bool:
    return bool(store.get_setting(TRAY_SETTING, True))


def set_tray_enabled(store: st.MonitorStore, enabled: bool) -> dict:
    store.set_setting(TRAY_SETTING, bool(enabled))
    return {"ok": True, "enabled": bool(enabled)}


def read_tray_status() -> dict:
    try:
        with open(tray_status_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def tray_status(store: st.MonitorStore, daemon_running: bool) -> dict:
    """What the panel shows about the icon. Only a running daemon has one."""
    enabled = tray_enabled(store)
    if not enabled:
        return {"enabled": False, "state": STATE_DISABLED, "reason": ""}
    if not daemon_running:
        return {"enabled": True, "state": STATE_STOPPED, "reason": ""}
    data = read_tray_status()
    return {"enabled": True, "state": str(data.get("state") or STATE_STOPPED), "reason": str(data.get("reason") or "")}


# ── What the icon shows and does ────────────────────────────────────────────

def paused_from_tray(store: st.MonitorStore) -> list[dict]:
    return [m for m in store.list_monitors((st.STATUS_PAUSED,)) if m.get("status_reason") == PAUSED_FROM_TRAY]


def summary(store: st.MonitorStore) -> dict:
    return {
        "active": store.count_active(),
        "paused": len(paused_from_tray(store)),
        "unseen": store.unseen_events(),
        "running": len(store.list_wakes((st.WAKE_CLAIMED,))),
    }


def pause_all(store: st.MonitorStore) -> int:
    """Pause every active monitor, marked so that *Resume* resumes only these."""
    paused = 0
    for monitor in store.list_monitors((st.STATUS_ACTIVE,)):
        if store.set_status(monitor["id"], st.STATUS_PAUSED, PAUSED_FROM_TRAY):
            paused += 1
    return paused


def resume_all(store: st.MonitorStore) -> int:
    """Resume the monitors *Pause all* paused; one the user paused alone stays paused."""
    return sum(1 for monitor in paused_from_tray(store) if store.resume_monitor(monitor["id"]))


def open_window(store: st.MonitorStore, view: str = "") -> dict:
    """Raise the window (starting the app when none runs), optionally on a view."""
    return service.open_chat(store, "", "", "", view=view)


def tooltip(info: dict) -> str:
    lines = ["GilzaBots", _t("tray_summary_active", n=info["active"])]
    if info["running"]:
        lines.append(_t("tray_summary_running", n=info["running"]))
    if info["paused"]:
        lines.append(_t("tray_summary_paused", n=info["paused"]))
    if info["unseen"]:
        lines.append(_t("tray_summary_unseen", n=info["unseen"]))
    return "\n".join(lines)


def menu_model(info: dict) -> list[dict]:
    """The menu, top to bottom: ``{"id", "label", "enabled"}`` or a ``{"id": "-"}`` separator."""
    header = _t("tray_summary_active", n=info["active"])
    if info["unseen"]:
        header += " · " + _t("tray_summary_unseen", n=info["unseen"])
    items = [
        {"id": "summary", "label": header, "enabled": False},
        {"id": "-"},
        {"id": "open", "label": _t("tray_open"), "enabled": True},
        {"id": "monitors", "label": _t("tray_open_monitors"), "enabled": True},
        {"id": "-"},
    ]
    if info["paused"]:
        items.append({"id": "resume", "label": _t("tray_resume_all", n=info["paused"]), "enabled": True})
    else:
        items.append({"id": "pause", "label": _t("tray_pause_all"), "enabled": info["active"] > 0})
    items += [{"id": "-"}, {"id": "stop", "label": _t("tray_stop"), "enabled": True}]
    return items


# ── Reaching the graphical session ──────────────────────────────────────────

def _systemd_environment() -> dict[str, str]:
    """The user manager's environment, which the desktop updates at login."""
    try:
        result = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                                text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in GRAPHICAL_VARIABLES and not value.startswith("$'"):
            env[key] = value
    return env


def graphical_environment(base: dict | None = None,
                          manager_env: Callable[[], dict[str, str]] = _systemd_environment) -> dict | None:
    """The environment to start the icon with, or None while there is no desktop.

    Windows and macOS always have one. On Linux a daemon the login service
    started before the desktop was up has no ``DISPLAY``/``WAYLAND_DISPLAY``;
    the desktop then exports them to the user manager, which is where they are
    read from.
    """
    env = dict(os.environ if base is None else base)
    if not sys.platform.startswith("linux"):
        return env
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    for key, value in manager_env().items():
        if value and key in GRAPHICAL_VARIABLES:
            env[key] = value
    return env if (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")) else None


def qt_available() -> tuple[bool, str]:
    try:
        found = importlib.util.find_spec("PyQt6") is not None and importlib.util.find_spec("PyQt6.QtWidgets") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return True, ""
    return False, "PyQt6 is not installed (it comes with the desktop app's 'gui' extra)."


def toolkit_available() -> tuple[bool, str]:
    """Whether the toolkit that draws the icon here can be loaded, as ``(ok, reason)``.

    Qt on macOS and Linux; WinForms through pythonnet on Windows, which has no
    Qt (``tray_windows``). A missing toolkit is reported as ``STATE_NO_QT``,
    the state the panel shows as "unavailable", with the reason naming it.
    """
    if sys.platform == "win32":
        from .tray_windows import toolkit_available as windows_toolkit_available

        return windows_toolkit_available()
    return qt_available()


# ── Supervision, inside the daemon ──────────────────────────────────────────

class TraySupervisor:
    """Keeps the icon's process running while the setting asks for it.

    Every outcome that is not a running icon is recorded with its reason
    (``tray.json``) and retried after a delay suited to it: a desktop that is
    still starting soon, a desktop without a tray in minutes, a crash with a
    growing back-off. Nothing is retried in a tight loop.
    """

    def __init__(self, store: st.MonitorStore, *, argv: Callable[[int], list[str]] | None = None,
                 clock=time.time, spawn=subprocess.Popen,
                 environment: Callable[[], dict | None] = graphical_environment,
                 qt_probe: Callable[[], tuple[bool, str]] = toolkit_available,
                 status_path: str | None = None):
        from .process import gilzabots_command

        self.store = store
        self.argv = argv or (lambda pid: gilzabots_command() + ["monitors", "tray", "run", "--daemon-pid", str(pid)])
        self.clock = clock
        self.spawn = spawn
        self.environment = environment
        self.qt_probe = qt_probe
        self.status_path = status_path or tray_status_path()
        self.process: subprocess.Popen | None = None
        self._next_check = 0.0
        self._retry_at = 0.0
        self._crashes = 0
        self._status: dict = {}

    def _set_status(self, state: str, reason: str = "") -> None:
        status = {"state": state, "reason": reason,
                  "pid": self.process.pid if self.process is not None and state == STATE_RUNNING else None}
        if status == self._status:
            return
        self._status = status
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({**status, "daemon_pid": os.getpid(), "updated_at": self.clock()}, handle)
            os.replace(tmp, self.status_path)
        except OSError as exc:
            print(f"[monitors] could not record the tray icon status: {exc}", file=sys.stderr, flush=True)

    def _reap(self, now: float) -> None:
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        self.process = None
        if code == EXIT_NO_TRAY:
            self._set_status(STATE_NO_TRAY, NO_TRAY_REASON)
            self._retry_at = now + RETRY_NO_TRAY_SECONDS
        elif code == EXIT_DUPLICATE:
            self._set_status(STATE_FAILED, "Another tray icon process is running.")
            self._retry_at = now + RETRY_DUPLICATE_SECONDS
        elif code == 0:
            # It ended on purpose (new code, the daemon it watched changed):
            # a fresh one is started right away.
            self._retry_at = now
        else:
            self._crashes += 1
            delay = min(MAX_CRASH_BACKOFF_SECONDS, 10.0 * 2 ** (self._crashes - 1))
            self._set_status(STATE_FAILED, f"The tray icon process exited with code {code}; see {tray_log_path()}.")
            self._retry_at = now + delay
            print(f"[monitors] the tray icon exited with code {code}; retrying in {delay:.0f} s.",
                  file=sys.stderr, flush=True)

    def step(self) -> None:
        now = self.clock()
        if now < self._next_check:
            return
        self._next_check = now + SUPERVISE_SECONDS
        self._reap(now)
        if not tray_enabled(self.store):
            self.stop()
            self._set_status(STATE_DISABLED)
            return
        if self.process is not None:
            return
        if now < self._retry_at:
            return
        ok, reason = self.qt_probe()
        if not ok:
            self._set_status(STATE_NO_QT, reason)
            self._retry_at = now + RETRY_NO_QT_SECONDS
            return
        env = self.environment()
        if env is None:
            self._set_status(STATE_NO_SESSION, NO_SESSION_REASON)
            self._retry_at = now + RETRY_NO_SESSION_SECONDS
            return
        try:
            with open(tray_log_path(), "ab") as log:
                self.process = self.spawn(
                    self.argv(os.getpid()),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                    **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
                )
        except OSError as exc:
            self.process = None
            self._crashes += 1
            self._set_status(STATE_FAILED, f"The tray icon could not be started: {exc}")
            self._retry_at = now + min(MAX_CRASH_BACKOFF_SECONDS, 10.0 * 2 ** (self._crashes - 1))
            return
        self._set_status(STATE_RUNNING)

    def stop(self, timeout: float = 3.0) -> None:
        """End the icon's process (the daemon stops, or the setting turned it off)."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def close(self) -> None:
        self.stop()
        self._set_status(STATE_STOPPED)


# ── The icon itself (Qt) ────────────────────────────────────────────────────

def _icon_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(here, "icon.png"), os.path.join(os.path.dirname(here), "icon.png")):
        if os.path.isfile(candidate):
            return candidate
    return ""


def run_tray(daemon_pid: int) -> int:
    """Entry point of ``gilzabots monitors tray run``. Returns the exit code."""
    lock = service.DaemonLock(tray_lock_path())
    if not lock.acquire():
        print("[monitors] another tray icon is already running.", flush=True)
        return EXIT_DUPLICATE
    try:
        if sys.platform == "win32":
            from .tray_windows import run as run_windows

            return run_windows(daemon_pid)
        return _run_qt(daemon_pid)
    finally:
        lock.release()


def _run_qt(daemon_pid: int) -> int:
    import signal
    import threading

    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

    from .daemon import CODE_CHECK_SECONDS, _apply_ui_language

    _apply_ui_language()
    app = QApplication(["gilzabots-monitors"])
    app.setApplicationName("GilzaBots")
    app.setDesktopFileName("gilzabots")
    app.setQuitOnLastWindowClosed(False)

    deadline = time.monotonic() + TRAY_WAIT_SECONDS
    while not QSystemTrayIcon.isSystemTrayAvailable():
        if time.monotonic() >= deadline:
            print(f"[monitors] {NO_TRAY_REASON}", flush=True)
            return EXIT_NO_TRAY
        app.processEvents()
        time.sleep(1.0)

    store = st.MonitorStore()
    base = QPixmap(_icon_path()).scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
    normal_icon = QIcon(base)
    marked = QPixmap(base)
    painter = QPainter(marked)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#e5534b"))
    painter.drawEllipse(40, 2, 22, 22)
    painter.end()
    attention_icon = QIcon(marked)

    icon = QSystemTrayIcon(normal_icon)
    menu = QMenu()
    icon.setContextMenu(menu)
    started_with = service.code_fingerprint()
    timing = {"code": time.monotonic(), "lang": time.monotonic()}
    current = {"info": None}

    def act(item_id: str) -> None:
        try:
            if item_id == "open":
                open_window(store)
            elif item_id == "monitors":
                open_window(store, view="monitors")
            elif item_id == "pause":
                pause_all(store)
            elif item_id == "resume":
                resume_all(store)
            elif item_id == "stop":
                info = summary(store)
                if info["active"]:
                    box = QMessageBox(QMessageBox.Icon.Question, _t("tray_stop_confirm_title"),
                                      _t("tray_stop_confirm_body", n=info["active"]))
                    yes = box.addButton(_t("tray_stop_confirm_yes"), QMessageBox.ButtonRole.AcceptRole)
                    box.addButton(_t("tray_cancel"), QMessageBox.ButtonRole.RejectRole)
                    box.exec()
                    if box.clickedButton() is not yes:
                        return
                # The daemon ends this process when it stops; do not wait for it here.
                threading.Thread(target=service.stop_daemon, daemon=True).start()
        except Exception as exc:
            print(f"[monitors] tray action '{item_id}' failed: {exc}", file=sys.stderr, flush=True)
        refresh(force=True)

    def rebuild(info: dict) -> None:
        menu.clear()
        for item in menu_model(info):
            if item["id"] == "-":
                menu.addSeparator()
                continue
            action = QAction(item["label"], menu)
            action.setEnabled(item["enabled"])
            action.triggered.connect(lambda _checked=False, item_id=item["id"]: act(item_id))
            menu.addAction(action)

    def refresh(force: bool = False) -> None:
        if not pid_alive(daemon_pid):
            app.quit()
            return
        now = time.monotonic()
        if now - timing["code"] >= CODE_CHECK_SECONDS:
            timing["code"] = now
            if service.code_fingerprint() != started_with:
                app.quit()  # the daemon restarts on the new code and starts a new icon
                return
        if now - timing["lang"] >= 60:
            timing["lang"] = now
            _apply_ui_language()
            force = True
        try:
            info = summary(store)
        except Exception as exc:
            print(f"[monitors] tray refresh failed: {exc}", file=sys.stderr, flush=True)
            return
        if force or info != current["info"]:
            current["info"] = info
            icon.setIcon(attention_icon if info["unseen"] else normal_icon)
            icon.setToolTip(tooltip(info))
            rebuild(info)

    def on_activated(reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            act("open")

    icon.activated.connect(on_activated)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_args: app.quit())
            except (ValueError, OSError):
                pass
    refresh(force=True)
    icon.show()
    timer = QTimer()
    # Also what lets Python run the signal handlers while Qt's loop waits.
    timer.timeout.connect(refresh)
    timer.start(2000)
    print(f"[monitors] tray icon {os.getpid()} shown for daemon {daemon_pid}.", flush=True)
    app.exec()
    icon.hide()
    return 0
