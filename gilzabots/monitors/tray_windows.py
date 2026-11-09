"""The tray icon on Windows: a WinForms ``NotifyIcon`` through pythonnet.

macOS and Linux draw the icon with Qt (``tray._run_qt``). Windows has no Qt
(PROJECT_DESIGN.md §2.16.1), and pythonnet with WinForms is already part of the
desktop install there, since it is what pywebview's WebView2 window runs on.
Everything else is shared with the Qt icon: the menu model, the tooltip, the
actions, the exit conditions and the refresh period (``tray.py``).

WinForms needs a single-threaded apartment, which Python's main thread is not
guaranteed to be, so the icon runs its message loop on a .NET thread created as
STA, the way pywebview runs its window. The Python thread only waits for it, in
short joins, so a Ctrl+C in a console still reaches Python.
"""
from __future__ import annotations

import os
import sys
import threading
import time

from . import service
from . import store as st
from .process import pid_alive

#: How often the icon re-reads ``monitors.db`` (the Qt icon uses the same period).
REFRESH_MILLISECONDS = 2000


def toolkit_available() -> tuple[bool, str]:
    """Whether pythonnet (``clr``) is importable here, as ``(ok, reason)``."""
    import importlib.util

    try:
        found = importlib.util.find_spec("clr") is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return True, ""
    return False, "pythonnet is not installed (it comes with the desktop app's 'gui' extra)."


def _load_dotnet():
    try:
        import clr
    except Exception:
        # pywebview's own fallback (webview/platforms/winforms.py).
        os.environ["PYTHONNET_RUNTIME"] = "coreclr"
        import clr

    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    clr.AddReference("System.Threading")


def run(daemon_pid: int) -> int:
    """Show the icon until the daemon ends or the code changes. Returns the exit code."""
    from . import tray
    from .daemon import CODE_CHECK_SECONDS, _apply_ui_language

    _load_dotnet()
    import System.Windows.Forms as WinForms
    from System import EventHandler
    from System.Drawing import Bitmap, Color, Graphics, Icon, SolidBrush, Size
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Threading import ApartmentState, Thread, ThreadStart

    _apply_ui_language()
    store = st.MonitorStore()
    started_with = service.code_fingerprint()
    outcome = {"code": 0, "error": None}

    def ui_thread() -> None:
        try:
            WinForms.Application.EnableVisualStyles()
            base = Bitmap(Bitmap(tray._icon_path()), Size(64, 64))
            normal_icon = Icon.FromHandle(base.GetHicon())
            marked = Bitmap(base)
            painter = Graphics.FromImage(marked)
            painter.SmoothingMode = SmoothingMode.AntiAlias
            painter.FillEllipse(SolidBrush(Color.FromArgb(0xE5, 0x53, 0x4B)), 40, 2, 22, 22)
            painter.Dispose()
            attention_icon = Icon.FromHandle(marked.GetHicon())

            icon = WinForms.NotifyIcon()
            icon.Icon = normal_icon
            menu = WinForms.ContextMenuStrip()
            icon.ContextMenuStrip = menu
            timing = {"code": time.monotonic(), "lang": time.monotonic()}
            current = {"info": None}
            loop = {"running": False, "quit": False}

            def quit_loop() -> None:
                icon.Visible = False
                loop["quit"] = True
                # ExitThread only ends a loop that is running; before Run() the
                # flag is what keeps the loop from starting.
                if loop["running"]:
                    WinForms.Application.ExitThread()

            def act(item_id: str) -> None:
                try:
                    if item_id == "open":
                        tray.open_window(store)
                    elif item_id == "monitors":
                        tray.open_window(store, view="monitors")
                    elif item_id == "pause":
                        tray.pause_all(store)
                    elif item_id == "resume":
                        tray.resume_all(store)
                    elif item_id == "stop":
                        info = tray.summary(store)
                        if info["active"]:
                            answer = WinForms.MessageBox.Show(
                                tray._t("tray_stop_confirm_body", n=info["active"]),
                                tray._t("tray_stop_confirm_title"),
                                WinForms.MessageBoxButtons.YesNo,
                                WinForms.MessageBoxIcon.Question,
                            )
                            if answer != WinForms.DialogResult.Yes:
                                return
                        # The daemon ends this process when it stops; do not wait for it here.
                        threading.Thread(target=service.stop_daemon, daemon=True).start()
                except Exception as exc:
                    print(f"[monitors] tray action '{item_id}' failed: {exc}", file=sys.stderr, flush=True)
                refresh(force=True)

            def rebuild(info: dict) -> None:
                menu.Items.Clear()
                for item in tray.menu_model(info):
                    if item["id"] == "-":
                        menu.Items.Add(WinForms.ToolStripSeparator())
                        continue
                    entry = WinForms.ToolStripMenuItem(item["label"])
                    entry.Enabled = item["enabled"]
                    entry.Click += EventHandler(lambda _s, _e, item_id=item["id"]: act(item_id))
                    menu.Items.Add(entry)

            def refresh(force: bool = False) -> None:
                if not pid_alive(daemon_pid):
                    quit_loop()
                    return
                now = time.monotonic()
                if now - timing["code"] >= CODE_CHECK_SECONDS:
                    timing["code"] = now
                    if service.code_fingerprint() != started_with:
                        quit_loop()  # the daemon restarts on the new code and starts a new icon
                        return
                if now - timing["lang"] >= 60:
                    timing["lang"] = now
                    _apply_ui_language()
                    force = True
                try:
                    info = tray.summary(store)
                except Exception as exc:
                    print(f"[monitors] tray refresh failed: {exc}", file=sys.stderr, flush=True)
                    return
                if force or info != current["info"]:
                    current["info"] = info
                    icon.Icon = attention_icon if info["unseen"] else normal_icon
                    icon.Text = notify_text(tray.tooltip(info))
                    rebuild(info)

            def on_mouse_click(_sender, args) -> None:
                if args.Button == WinForms.MouseButtons.Left:
                    act("open")

            icon.MouseClick += WinForms.MouseEventHandler(on_mouse_click)
            refresh(force=True)
            if loop["quit"]:  # the daemon was already gone, or the code changed
                icon.Dispose()
                return
            icon.Visible = True
            timer = WinForms.Timer()
            timer.Interval = REFRESH_MILLISECONDS
            timer.Tick += EventHandler(lambda _s, _e: refresh())
            timer.Start()
            print(f"[monitors] tray icon {os.getpid()} shown for daemon {daemon_pid}.", flush=True)
            loop["running"] = True
            WinForms.Application.Run()
            timer.Stop()
            icon.Visible = False
            icon.Dispose()
        except Exception as exc:  # reported by the Python thread below
            outcome["code"] = 1
            outcome["error"] = exc

    thread = Thread(ThreadStart(ui_thread))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    while thread.IsAlive:
        thread.Join(500)
    if outcome["error"] is not None:
        error = outcome["error"]
        print(f"[monitors] the tray icon failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return outcome["code"]


#: ``NotifyIcon.Text`` raises above this length (the Win32 NOTIFYICONDATA tip
#: holds 128 characters including the terminator; .NET Framework enforces 63).
NOTIFY_TEXT_LIMIT = 63


def notify_text(text: str) -> str:
    """`text` cut to what ``NotifyIcon.Text`` accepts."""
    if len(text) <= NOTIFY_TEXT_LIMIT:
        return text
    return text[: NOTIFY_TEXT_LIMIT - 1] + "…"
