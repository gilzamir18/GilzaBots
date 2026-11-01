"""The monitor daemon's lifecycle: single instance, start on demand, start at login.

**Single instance.** The daemon holds an exclusive lock on
``<home>/monitors/daemon.lock`` for its whole life (``fcntl.flock`` /
``msvcrt.locking``). The OS releases it when the process dies, however it
dies, so "is the daemon running" is answered by trying the lock, not by a pid
file that a crash leaves behind.

**Start on demand.** Registering a monitor, or the window starting with active
monitors, calls :func:`ensure_daemon_running`, which starts it through the
login service when that is installed and as a detached process otherwise.

**Start at login** (:func:`autostart_backend`), per user and without elevation:

- Linux, outside the snap: a ``systemd --user`` unit, or an XDG autostart entry
  where there is no user systemd.
- Linux, inside the snap: an XDG autostart entry in ``$SNAP_USER_DATA``, which
  snapd's session agent starts through the snap's ``monitors`` app
  (``autostart:`` in ``snapcraft.yaml``). A confined snap can neither write
  ``~/.config/systemd`` nor run ``systemctl``.
- macOS: a LaunchAgent.
- Windows: the ``HKCU\\...\\Run`` key (a scheduled task with a logon trigger
  needs elevation on many systems).

Installing it is on by default (``settings.autostart``), because a monitor the
user asked for is expected to survive a restart; the Monitors panel and
``opalatex monitors service`` show it and turn it off.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import IO

from .process import opalatex_command, pid_alive, spawn_detached

SERVICE_NAME = "opalatex-monitors"
LAUNCH_AGENT_LABEL = "com.opalacoder.opalatex.monitors"
WINDOWS_RUN_VALUE = "OpalaTexMonitors"
AUTOSTART_SETTING = "autostart"
SYSTEMD_OWNER_PREFIX = "# OpalaTex data folder: "
XDG_OWNER_PREFIX = "X-OpalaTex-Data-Folder="
PLIST_OWNER_KEY = "OpalaTexDataFolder"


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _owner_from_text(text: str, marker: str, env_pattern: str) -> str | None:
    """The folder a service file records; for a file written before the record
    existed, the folder its ``OPALATEX_HOME`` (or its absence) resolves."""
    import re

    if not text:
        return None
    for line in text.splitlines():
        if line.startswith(marker):
            return line[len(marker):].strip()
    match = re.search(env_pattern, text)
    return match.group(1).strip() if match else _home_without_env()


def monitors_dir() -> str:
    from opalatex.config import get_opalatex_home

    path = os.path.join(get_opalatex_home(), "monitors")
    os.makedirs(path, exist_ok=True)
    return path


def lock_path() -> str:
    return os.path.join(monitors_dir(), "daemon.lock")


def state_path() -> str:
    return os.path.join(monitors_dir(), "daemon.json")


def log_path() -> str:
    return os.path.join(monitors_dir(), "daemon.log")


def daemon_argv() -> list[str]:
    return opalatex_command() + ["monitors", "daemon"]


def _running_in_snap() -> bool:
    return os.environ.get("SNAP_NAME") == "opalatex" and bool(os.environ.get("SNAP"))


# ── Code fingerprint ────────────────────────────────────────────────────────

def code_fingerprint() -> str:
    """What identifies the code this installation would run now.

    The daemon is long-lived: an update installed while it runs (a snap
    refresh, a reinstall, a source checkout that changed) would otherwise keep
    the old code in memory for as long as any monitor is active -- which is how
    a monitor registered with a newer option was carried out by a daemon that
    did not know it. The daemon compares this with the value it started with and
    restarts itself on a difference (``daemon.MonitorDaemon``).

    Built from the version, the newest modification time and count of the
    package's sources (a source or pip install), the executable's modification
    time (a frozen build), and the revision ``current`` points at (the snap,
    whose running revision stays mounted after a refresh).
    """
    from opalatex import __version__

    parts = [__version__]
    if getattr(sys, "frozen", False):
        try:
            parts.append(f"exe:{os.stat(sys.executable).st_mtime_ns}")
        except OSError:
            pass
    snap_name = os.environ.get("SNAP_NAME")
    if snap_name and os.environ.get("SNAP"):
        try:
            parts.append("snap:" + os.path.basename(os.path.realpath(f"/snap/{snap_name}/current")))
        except OSError:
            pass
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    newest, count = 0, 0
    for root, dirs, files in os.walk(package_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "gui")]
        for name in files:
            if name.endswith(".py"):
                try:
                    newest = max(newest, os.stat(os.path.join(root, name)).st_mtime_ns)
                except OSError:
                    continue
                count += 1
    parts.append(f"src:{count}:{newest}")
    return "|".join(parts)


# ── Lock ────────────────────────────────────────────────────────────────────

class DaemonLock:
    """An exclusive, non-blocking lock released by the OS when the process ends."""

    def __init__(self, path: str | None = None):
        self.path = path or lock_path()
        self._handle: IO | None = None

    def acquire(self) -> bool:
        handle = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()
        self._handle = None


def daemon_running() -> bool:
    lock = DaemonLock()
    if lock.acquire():
        lock.release()
        return False
    return True


def read_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def write_state(data: dict) -> None:
    from opalatex.atomic_file import write_text_atomic

    write_text_atomic(state_path(), json.dumps(data, indent=2))


def daemon_status() -> dict:
    running = daemon_running()
    state = read_state() if running else {}
    backend = autostart_backend()
    available, reason = backend.available() if backend else (False, "No login service is supported on this platform.")
    return {
        "running": running,
        "pid": state.get("pid") if running else None,
        "started_at": state.get("started_at") if running else None,
        "version": state.get("version") if running else None,
        "autostart": {
            "backend": backend.name if backend else None,
            "available": available,
            "reason": reason,
            "installed": bool(backend and backend.installed()),
            "other_folder": backend.foreign_owner() if backend else None,
        },
        "log": log_path(),
    }


def _home_environment() -> dict[str, str]:
    """Environment the login service must carry to find the same data folder."""
    home = os.environ.get("OPALATEX_HOME")
    return {"OPALATEX_HOME": home} if home else {}


# ── Login services ──────────────────────────────────────────────────────────

def current_home() -> str:
    from opalatex.config import get_opalatex_home

    return get_opalatex_home()


def _home_without_env() -> str:
    """The data folder a process started without ``OPALATEX_HOME`` resolves.

    ``config.get_opalatex_home`` minus its first step, computed rather than by
    unsetting the variable, which other threads of this process may be reading.
    """
    from opalatex.config import configured_opalatex_home, data_dir_error

    custom = configured_opalatex_home()
    if custom and not data_dir_error(custom):
        return custom
    return os.path.join(os.path.expanduser("~"), ".opalatex")


def _same_folder(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.realpath(os.path.expanduser(left)) == os.path.realpath(os.path.expanduser(right))


class AutostartBackend:
    """A per-user login service. There is one per user, but data folders can be
    several (``OPALATEX_HOME``), so each service records the folder it serves
    and is never replaced or removed on behalf of another one."""

    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "Not supported on this platform."

    def present(self) -> bool:
        """A service of this kind exists, whichever folder it serves."""
        return False

    def owner(self) -> str | None:
        """The data folder the existing service serves; None when there is none."""
        return None

    def installed(self) -> bool:
        """A service exists and serves this process's data folder."""
        return self.present() and _same_folder(self.owner(), current_home())

    def foreign_owner(self) -> str | None:
        """The other data folder an existing service serves, if any."""
        owner = self.owner() if self.present() else None
        return owner if owner and not _same_folder(owner, current_home()) else None

    def _refuse_foreign(self, doing: str) -> None:
        other = self.foreign_owner()
        if other:
            raise RuntimeError(
                f"The login service starts the monitors of another OpalaTex data folder ({other}); "
                f"it was not {doing}. Change \u201cStart at login\u201d from that data folder."
            )

    def install(self) -> None:
        raise RuntimeError("Not supported on this platform.")

    def uninstall(self) -> None:
        pass

    def start(self) -> bool:
        """Start the daemon through the service manager; False when it cannot."""
        return False


def _run(argv: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, **kwargs)


def _quote_systemd(arg: str) -> str:
    if arg and all(ch.isalnum() or ch in "/._-:=+@" for ch in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


class SystemdUserAutostart(AutostartBackend):
    name = "systemd-user"

    def __init__(self, config_home: str | None = None):
        base = config_home or os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        self.unit_path = os.path.join(base, "systemd", "user", f"{SERVICE_NAME}.service")

    #: (time, answer) of the last probe. Probing runs ``systemctl``; the panel
    #: asks for the status every few seconds, and the answer does not change
    #: within a session.
    _probe: tuple[float, tuple[bool, str]] | None = None
    PROBE_TTL_SECONDS = 300.0

    def available(self) -> tuple[bool, str]:
        cached = SystemdUserAutostart._probe
        if cached and time.monotonic() - cached[0] < self.PROBE_TTL_SECONDS:
            return cached[1]
        answer = self._probe_systemd()
        SystemdUserAutostart._probe = (time.monotonic(), answer)
        return answer

    @staticmethod
    def _probe_systemd() -> tuple[bool, str]:
        if not shutil.which("systemctl"):
            return False, "systemctl was not found."
        try:
            probe = _run(["systemctl", "--user", "show-environment"], timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"systemd --user is not reachable: {exc}"
        if probe.returncode != 0:
            return False, "systemd --user is not running for this session."
        return True, ""

    def unit_text(self) -> str:
        exec_start = " ".join(_quote_systemd(arg) for arg in daemon_argv())
        env_lines = "".join(
            f"Environment={_quote_systemd(f'{key}={value}')}\n" for key, value in _home_environment().items()
        )
        return (
            f"{SYSTEMD_OWNER_PREFIX}{current_home()}\n"
            "[Unit]\n"
            "Description=OpalaTex monitor agents\n"
            "After=graphical-session.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_start}\n"
            f"{env_lines}"
            "Restart=on-failure\n"
            "RestartSec=10\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def present(self) -> bool:
        return os.path.isfile(self.unit_path)

    def owner(self) -> str | None:
        return _owner_from_text(_read_text(self.unit_path), SYSTEMD_OWNER_PREFIX,
                                r'Environment="?OPALATEX_HOME=([^"\n]+)"?')

    def install(self) -> None:
        self._refuse_foreign("replaced")
        os.makedirs(os.path.dirname(self.unit_path), exist_ok=True)
        with open(self.unit_path, "w", encoding="utf-8") as handle:
            handle.write(self.unit_text())
        for argv in (["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", f"{SERVICE_NAME}.service"]):
            result = _run(argv)
            if result.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr.strip()}")

    def uninstall(self) -> None:
        self._refuse_foreign("removed")
        if os.path.isfile(self.unit_path):
            _run(["systemctl", "--user", "disable", f"{SERVICE_NAME}.service"])
            os.remove(self.unit_path)
            _run(["systemctl", "--user", "daemon-reload"])

    def start(self) -> bool:
        if not self.installed():
            return False
        result = _run(["systemctl", "--user", "start", f"{SERVICE_NAME}.service"])
        return result.returncode == 0


class XdgAutostart(AutostartBackend):
    """A desktop-session autostart entry (GNOME, KDE, XFCE...).

    In the snap, ``$HOME`` is ``$SNAP_USER_DATA``, and snapd's session agent
    starts the app whose ``autostart:`` names this file.
    """

    name = "xdg-autostart"

    def __init__(self, config_home: str | None = None, snap: bool | None = None):
        self.snap = _running_in_snap() if snap is None else snap
        base = config_home or (
            os.path.join(os.path.expanduser("~"), ".config") if self.snap
            else os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        )
        self.entry_path = os.path.join(base, "autostart", f"{SERVICE_NAME}.desktop")

    def available(self) -> tuple[bool, str]:
        return True, ""

    def desktop_text(self) -> str:
        if self.snap:
            exec_line = "opalatex.monitors"
        else:
            env = " ".join(f"{key}={value}" for key, value in _home_environment().items())
            command = subprocess.list2cmdline(daemon_argv())
            exec_line = f"env {env} {command}" if env else command
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=OpalaTex monitors\n"
            "Comment=Runs OpalaTex monitor agents in the background\n"
            f"Exec={exec_line}\n"
            "NoDisplay=true\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            f"{XDG_OWNER_PREFIX}{current_home()}\n"
        )

    def present(self) -> bool:
        return os.path.isfile(self.entry_path)

    def owner(self) -> str | None:
        return _owner_from_text(_read_text(self.entry_path), XDG_OWNER_PREFIX,
                                r"env (?:[^\n]* )?OPALATEX_HOME=(\S+)")

    def install(self) -> None:
        self._refuse_foreign("replaced")
        os.makedirs(os.path.dirname(self.entry_path), exist_ok=True)
        with open(self.entry_path, "w", encoding="utf-8") as handle:
            handle.write(self.desktop_text())

    def uninstall(self) -> None:
        self._refuse_foreign("removed")
        if os.path.isfile(self.entry_path):
            os.remove(self.entry_path)


class LaunchAgentAutostart(AutostartBackend):
    name = "launch-agent"

    def __init__(self):
        self.plist_path = os.path.join(
            os.path.expanduser("~"), "Library", "LaunchAgents", f"{LAUNCH_AGENT_LABEL}.plist"
        )

    def available(self) -> tuple[bool, str]:
        return (True, "") if shutil.which("launchctl") else (False, "launchctl was not found.")

    def plist_bytes(self) -> bytes:
        import plistlib

        data = {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": daemon_argv(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "StandardOutPath": log_path(),
            "StandardErrorPath": log_path(),
            # Ignored by launchd; records the data folder this agent serves.
            PLIST_OWNER_KEY: current_home(),
        }
        env = _home_environment()
        if env:
            data["EnvironmentVariables"] = env
        return plistlib.dumps(data)

    def present(self) -> bool:
        return os.path.isfile(self.plist_path)

    def owner(self) -> str | None:
        import plistlib

        try:
            with open(self.plist_path, "rb") as handle:
                data = plistlib.load(handle)
        except (OSError, ValueError):
            return None
        return (data.get(PLIST_OWNER_KEY)
                or (data.get("EnvironmentVariables") or {}).get("OPALATEX_HOME")
                or _home_without_env())

    def install(self) -> None:
        self._refuse_foreign("replaced")
        os.makedirs(os.path.dirname(self.plist_path), exist_ok=True)
        with open(self.plist_path, "wb") as handle:
            handle.write(self.plist_bytes())

    def uninstall(self) -> None:
        self._refuse_foreign("removed")
        if os.path.isfile(self.plist_path):
            _run(["launchctl", "unload", "-w", self.plist_path])
            os.remove(self.plist_path)

    def start(self) -> bool:
        if not self.installed():
            return False
        return _run(["launchctl", "load", "-w", self.plist_path]).returncode == 0


class WindowsRunKeyAutostart(AutostartBackend):
    name = "windows-run-key"
    _KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def command(self) -> str:
        return subprocess.list2cmdline(daemon_argv())

    #: Not in the Run key itself: every value there is executed at login.
    _OWNER_KEY = r"Software\OpalaTex"
    _OWNER_VALUE = "MonitorsDataFolder"

    def present(self) -> bool:
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._KEY) as key:
                winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
            return True
        except OSError:
            return False

    def owner(self) -> str | None:
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._OWNER_KEY) as key:
                return winreg.QueryValueEx(key, self._OWNER_VALUE)[0]
        except OSError:
            # Recorded before ownership was; a Run command carries no environment.
            return _home_without_env()

    def install(self) -> None:
        import winreg  # type: ignore[import-not-found]

        self._refuse_foreign("replaced")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._KEY) as key:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, self.command())
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._OWNER_KEY) as key:
            winreg.SetValueEx(key, self._OWNER_VALUE, 0, winreg.REG_SZ, current_home())

    def uninstall(self) -> None:
        import winreg  # type: ignore[import-not-found]

        self._refuse_foreign("removed")
        for path, value in ((self._KEY, WINDOWS_RUN_VALUE), (self._OWNER_KEY, self._OWNER_VALUE)):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, value)
            except OSError:
                pass


def autostart_backend() -> AutostartBackend | None:
    if sys.platform == "win32":
        return WindowsRunKeyAutostart()
    if sys.platform == "darwin":
        return LaunchAgentAutostart()
    if sys.platform.startswith("linux"):
        if _running_in_snap():
            return XdgAutostart(snap=True)
        systemd = SystemdUserAutostart()
        if systemd.available()[0]:
            return systemd
        return XdgAutostart(snap=False)
    return None


def autostart_enabled(store) -> bool:
    return bool(store.get_setting(AUTOSTART_SETTING, True))


def set_autostart(store, enabled: bool) -> dict:
    """Turn the login service on or off and remember the choice."""
    store.set_setting(AUTOSTART_SETTING, bool(enabled))
    backend = autostart_backend()
    if backend is None:
        return {"ok": False, "error": "No login service is supported on this platform."}
    try:
        if enabled:
            ok, reason = backend.available()
            if not ok:
                return {"ok": False, "error": reason}
            backend.install()
        else:
            backend.uninstall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "backend": backend.name, "installed": backend.installed()}


def ensure_daemon_running(store=None, *, wait: float = 0.0) -> dict:
    """Start the daemon if it is not running; install the login service if enabled.

    Returns ``{"running", "started", "autostart_installed", "error"}``.
    """
    result = {"running": False, "started": False, "autostart_installed": False, "error": ""}
    backend = autostart_backend()
    if store is not None and autostart_enabled(store) and backend is not None:
        try:
            if not backend.installed() and backend.available()[0]:
                backend.install()
                result["autostart_installed"] = True
        except Exception as exc:
            result["error"] = f"Could not install the login service: {exc}"
    if daemon_running():
        result["running"] = True
        return result
    started = False
    if backend is not None and backend.installed():
        try:
            started = backend.start()
        except Exception:
            started = False
    if not started:
        try:
            spawn_detached(daemon_argv(), log_path=log_path())
            started = True
        except OSError as exc:
            result["error"] = (result["error"] + " " if result["error"] else "") + f"Could not start the monitor daemon: {exc}"
            return result
    result["started"] = True
    deadline = time.monotonic() + wait
    while wait and time.monotonic() < deadline:
        if daemon_running():
            break
        time.sleep(0.1)
    result["running"] = daemon_running() if wait else True
    return result


# ── Opening a chat from a notification ──────────────────────────────────────

#: A window that has not polled for this long is not running a front-end.
GUI_ALIVE_SECONDS = 20.0


def open_chat(store, project: str, chat_id: str, project_path: str = "") -> dict:
    """Show a chat: in the running window, or by starting one.

    The request is queued for the window's poll in both cases, so a window
    that is still starting picks it up as soon as it is ready.
    """
    store.add_open_request(project, project_path, chat_id)
    if store.live_hosts("gui", GUI_ALIVE_SECONDS):
        return {"launched": False}
    if project:
        try:
            from opalatex.ui_settings import save_ui_settings

            save_ui_settings({"last_active_project": project})
        except Exception:
            pass
    spawn_detached(opalatex_command(), log_path=os.path.join(monitors_dir(), "window.log"))
    return {"launched": True}


def stop_daemon(timeout: float = 10.0) -> bool:
    """Ask a running daemon to exit (used by tests and ``monitors daemon --stop``)."""
    state = read_state()
    pid = int(state.get("pid") or 0)
    if not pid or not pid_alive(pid):
        return not daemon_running()
    try:
        if sys.platform == "win32":
            _run(["taskkill", "/PID", str(pid), "/F"])
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not daemon_running():
            return True
        time.sleep(0.1)
    return False
