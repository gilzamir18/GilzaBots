"""Native desktop notifications, with a click that opens the chat.

- **Linux (GNOME, KDE, any Freedesktop server)**: the
  ``org.freedesktop.Notifications`` D-Bus interface, called through ``gdbus``
  (GLib's command-line client, present wherever GNOME is). The daemon keeps a
  ``gdbus monitor`` on the same interface for ``ActionInvoked``: clicking the
  notification or its *Open chat* button opens the chat.
- **Windows 10/11**: a WinRT toast, shown from Windows PowerShell under the
  application's own AppUserModelID (registered in ``HKCU`` so the toast carries
  the GilzaBots name and icon). The toast is protocol-activated: clicking it
  opens an ``gilzabots-monitor:`` URI, a scheme registered for the current user
  to run ``gilzabots monitors open <uri>``.
- **macOS**: ``osascript``'s ``display notification``. Notification Center
  does not route a click to a script, so there is no click action.

A backend that cannot run raises :class:`NotifyError` with what is missing;
the caller records it against the notification instead of dropping it
silently.
"""
from __future__ import annotations

import base64
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
from typing import Callable

APP_NAME = "GilzaBots"
WINDOWS_APP_ID = "io.github.gilzamir18.GilzaBots"
URI_SCHEME = "gilzabots-monitor"
URGENCIES = {"low": 0, "normal": 1, "critical": 2}


class NotifyError(RuntimeError):
    """A notification that could not be shown."""


def open_uri(project: str, chat_id: str, project_path: str = "") -> str:
    query = urllib.parse.urlencode(
        {key: value for key, value in (("project", project), ("chat", chat_id), ("path", project_path)) if value}
    )
    return f"{URI_SCHEME}:open?{query}"


def parse_open_uri(uri: str) -> dict:
    """``{"project", "chat_id", "project_path"}`` from an ``gilzabots-monitor:`` URI.

    Raises ValueError for anything else: the scheme is registered with the OS,
    so any program can hand us a URI, and only this one shape is honoured.
    """
    parsed = urllib.parse.urlsplit(str(uri or "").strip())
    if parsed.scheme != URI_SCHEME:
        raise ValueError(f"Not an {URI_SCHEME}: URI: {uri!r}")
    action = (parsed.path or parsed.netloc).strip("/")
    if action != "open":
        raise ValueError(f"Unsupported {URI_SCHEME}: action {action!r}")
    query = urllib.parse.parse_qs(parsed.query)
    return {
        "project": (query.get("project") or [""])[0],
        "chat_id": (query.get("chat") or [""])[0],
        "project_path": (query.get("path") or [""])[0],
    }


def icon_path() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")
    return path if os.path.isfile(path) else ""


def _hidden_window_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


class Notifier:
    backend = "none"
    supports_click = False

    def send(self, title: str, body: str, *, urgency: str = "normal", uri: str = "",
             action_label: str = "Open") -> str:
        """Show a notification; returns a backend id. Raises NotifyError."""
        raise NotifyError("No desktop notification backend is available on this system.")

    def start_listener(self, on_open: Callable[[str], None]) -> None:
        """Begin delivering clicks to *on_open*. A no-op where clicks cannot be received."""

    def stop(self) -> None:
        """Stop the click listener, if any."""


# ── Linux / Freedesktop ─────────────────────────────────────────────────────

def _gvariant_string(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")
    )
    return f"'{escaped}'"


_ACTION_SIGNAL = re.compile(r"ActionInvoked \(uint32 (\d+), '((?:[^'\\]|\\.)*)'\)")
_CLOSED_SIGNAL = re.compile(r"NotificationClosed \(uint32 (\d+), uint32 \d+\)")
_NOTIFY_REPLY = re.compile(r"\(uint32 (\d+),\)")


class FreedesktopNotifier(Notifier):
    backend = "freedesktop"
    supports_click = True

    def __init__(self, gdbus: str):
        self.gdbus = gdbus
        self._uris: dict[int, str] = {}
        self._lock = threading.Lock()
        self._monitor: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def _bus_args(self) -> list[str]:
        return [
            self.gdbus, "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
        ]

    def send(self, title, body, *, urgency="normal", uri="", action_label="Open") -> str:
        actions = f"[{_gvariant_string('default')}, {_gvariant_string(action_label)}]" if uri else "@as []"
        hints = f"{{'urgency': <byte {URGENCIES.get(urgency, 1)}>}}"
        argv = self._bus_args() + [
            "--method", "org.freedesktop.Notifications.Notify",
            # Every non-string argument must carry its explicit GVariant type
            # so the signature is (susssasa{sv}i) even when D-Bus introspection
            # is unavailable (an AppArmor profile that denies Introspect on
            # /org/freedesktop/Notifications makes gdbus parse an untyped "0"
            # as int32 instead of uint32).
            _gvariant_string(APP_NAME), "uint32 0", _gvariant_string(icon_path()),
            _gvariant_string(title),
            # body-markup servers read the body as markup, so it is escaped.
            _gvariant_string(html.escape(body, quote=False)),
            actions, hints,
            # -1 is the server's default timeout (GNOME then keeps it in the
            # tray). Written as typed GVariant text: a bare "-1" is read by
            # gdbus as a command-line option and the call fails with its usage.
            "int32 -1",
        ]
        try:
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotifyError(f"gdbus failed: {exc}") from None
        if completed.returncode != 0:
            raise NotifyError(
                "The desktop notification service refused the notification: "
                + (completed.stderr.strip() or f"exit code {completed.returncode}")
            )
        match = _NOTIFY_REPLY.search(completed.stdout)
        notification_id = int(match.group(1)) if match else 0
        if uri and notification_id:
            with self._lock:
                self._uris[notification_id] = uri
        return str(notification_id)

    def start_listener(self, on_open):
        if self._thread is not None:
            return
        argv = [
            self.gdbus, "monitor", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
        ]
        try:
            self._monitor = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            self._monitor = None
            return

        def _read() -> None:
            assert self._monitor is not None and self._monitor.stdout is not None
            for line in self._monitor.stdout:
                self.handle_signal_line(line, on_open)

        self._thread = threading.Thread(target=_read, name="gilzabots-notify-clicks", daemon=True)
        self._thread.start()

    def handle_signal_line(self, line: str, on_open) -> None:
        action = _ACTION_SIGNAL.search(line)
        if action:
            with self._lock:
                uri = self._uris.get(int(action.group(1)))
            if uri:
                try:
                    on_open(uri)
                except Exception:
                    pass
            return
        closed = _CLOSED_SIGNAL.search(line)
        if closed:
            # Closed by the user or expired into the tray: GNOME still lists it
            # there and emits ActionInvoked when it is clicked, so the mapping is
            # kept; it is bounded instead.
            with self._lock:
                if len(self._uris) > 500:
                    for key in sorted(self._uris)[:250]:
                        self._uris.pop(key, None)

    def stop(self):
        if self._monitor is not None and self._monitor.poll() is None:
            try:
                self._monitor.terminate()
            except OSError:
                pass
        self._monitor = None
        self._thread = None


class NotifySendNotifier(Notifier):
    """``notify-send`` where ``gdbus`` is absent. It cannot report clicks."""

    backend = "notify-send"

    def __init__(self, binary: str):
        self.binary = binary

    def send(self, title, body, *, urgency="normal", uri="", action_label="Open") -> str:
        argv = [self.binary, "--app-name", APP_NAME, "--urgency", urgency]
        icon = icon_path()
        if icon:
            argv += ["--icon", icon]
        argv += [title, body]
        try:
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotifyError(f"notify-send failed: {exc}") from None
        if completed.returncode != 0:
            raise NotifyError(completed.stderr.strip() or f"notify-send exited with {completed.returncode}")
        return ""


# ── Windows ─────────────────────────────────────────────────────────────────

def _xml(text: str) -> str:
    return html.escape(text, quote=True)


def toast_xml(title: str, body: str, uri: str, action_label: str) -> str:
    activation = f' activationType="protocol" launch="{_xml(uri)}"' if uri else ""
    actions = (
        f'<actions><action content="{_xml(action_label)}" activationType="protocol" arguments="{_xml(uri)}"/></actions>'
        if uri else ""
    )
    return (
        f"<toast{activation}><visual><binding template=\"ToastGeneric\">"
        f"<text>{_xml(title)}</text><text>{_xml(body)}</text>"
        f"</binding></visual>{actions}</toast>"
    )


def toast_script(xml: str, app_id: str = WINDOWS_APP_ID) -> str:
    # The XML travels base64-encoded so no character in a title or body can
    # break out of the PowerShell string it is placed in.
    payload = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        f"$text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{payload}'))\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$xml.LoadXml($text)\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)\n"
    )


def _powershell() -> str | None:
    # WinRT projection is a Windows PowerShell (5.1) feature; pwsh 7 dropped it.
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("powershell.exe") or shutil.which("powershell")


def register_windows_integration(open_command: list[str] | None = None) -> None:
    """Register the AppUserModelID, notification settings and ``gilzabots-monitor:`` scheme in HKCU.

    All live under the current user's hive, need no elevation, and are
    removed by :func:`unregister_windows_integration`.
    """
    import winreg  # type: ignore[import-not-found]

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{WINDOWS_APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        icon = icon_path()
        if icon:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon)
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER,
        rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{WINDOWS_APP_ID}",
    ) as key:
        winreg.SetValueEx(key, "Enabled", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ShowInActionCenter", 0, winreg.REG_DWORD, 1)
    if open_command:
        command = subprocess.list2cmdline(open_command) + ' "%1"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{URI_SCHEME}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:GilzaBots monitor")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{URI_SCHEME}\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def unregister_windows_integration() -> None:
    import winreg  # type: ignore[import-not-found]

    for path in (
        rf"Software\Classes\{URI_SCHEME}\shell\open\command",
        rf"Software\Classes\{URI_SCHEME}\shell\open",
        rf"Software\Classes\{URI_SCHEME}\shell",
        rf"Software\Classes\{URI_SCHEME}",
        rf"Software\Classes\AppUserModelId\{WINDOWS_APP_ID}",
        rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{WINDOWS_APP_ID}",
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass


class WindowsToastNotifier(Notifier):
    backend = "windows-toast"
    supports_click = True

    def __init__(self, powershell: str, open_command: list[str] | None = None):
        self.powershell = powershell
        self.open_command = open_command
        self._registered = False

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        try:
            register_windows_integration(self.open_command)
        except OSError as exc:
            raise NotifyError(f"Could not register GilzaBots for notifications: {exc}") from None
        self._registered = True

    def send(self, title, body, *, urgency="normal", uri="", action_label="Open") -> str:
        self._ensure_registered()
        script = toast_script(toast_xml(title, body, uri, action_label))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        argv = [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded]
        try:
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                **_hidden_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotifyError(f"PowerShell failed: {exc}") from None
        if completed.returncode != 0:
            raise NotifyError("Windows refused the toast: " + (completed.stderr.strip()[:500] or f"exit code {completed.returncode}"))
        return ""


# ── macOS ───────────────────────────────────────────────────────────────────

def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacNotifier(Notifier):
    backend = "osascript"

    def send(self, title, body, *, urgency="normal", uri="", action_label="Open") -> str:
        script = f"display notification {_applescript_string(body)} with title {_applescript_string(title)}"
        try:
            completed = subprocess.run(
                ["osascript", "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NotifyError(f"osascript failed: {exc}") from None
        if completed.returncode != 0:
            raise NotifyError(completed.stderr.strip() or "osascript refused the notification")
        return ""


def default_notifier(open_command: list[str] | None = None) -> Notifier:
    """The backend for this platform, or a Notifier that reports why there is none."""
    if sys.platform == "win32":
        powershell = _powershell()
        if powershell:
            if open_command is None:
                from .process import gilzabots_command

                open_command = gilzabots_command() + ["monitors", "open"]
            return WindowsToastNotifier(powershell, open_command)
        return Notifier()
    if sys.platform == "darwin":
        return MacNotifier() if shutil.which("osascript") else Notifier()
    gdbus = shutil.which("gdbus")
    if gdbus:
        return FreedesktopNotifier(gdbus)
    notify_send = shutil.which("notify-send")
    if notify_send:
        return NotifySendNotifier(notify_send)
    return Notifier()

