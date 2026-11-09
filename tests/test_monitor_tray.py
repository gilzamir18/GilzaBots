"""The monitor daemon's tray icon (gilzabots/monitors/tray.py, PROJECT_DESIGN 2.24)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from gilzabots.monitors import service, tray
from gilzabots.monitors import store as st
from gilzabots.monitors.daemon import MonitorDaemon
from gilzabots.monitors.spec import normalize_spec


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mstore(clock):
    return st.MonitorStore(clock=clock)


def _spec(name="price", **overrides) -> dict:
    raw = {
        "name": name,
        "check": {"type": "timer"},
        "action": {"type": "notify", "message": "hi"},
        "interval_seconds": 60,
    }
    raw.update(overrides)
    return normalize_spec(raw)


class FakeProcess:
    _next_pid = 4000

    def __init__(self, argv, **kwargs):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.argv = argv
        self.kwargs = kwargs
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class Spawner:
    def __init__(self):
        self.processes: list[FakeProcess] = []

    def __call__(self, argv, **kwargs):
        process = FakeProcess(argv, **kwargs)
        self.processes.append(process)
        return process


def _supervisor(mstore, clock, tmp_path, *, environment=lambda: {"DISPLAY": ":0"}, qt=lambda: (True, "")):
    spawner = Spawner()
    supervisor = tray.TraySupervisor(
        mstore, argv=lambda pid: ["tray", str(pid)], clock=clock, spawn=spawner,
        environment=environment, qt_probe=qt, status_path=str(tmp_path / "tray.json"),
    )
    return supervisor, spawner


def _status(tmp_path) -> dict:
    return json.loads((tmp_path / "tray.json").read_text())


# ── What the icon shows and does ────────────────────────────────────────────

class TestActions:
    def test_pause_all_and_resume_only_what_it_paused(self, mstore):
        first = mstore.create_monitor(_spec("a"))
        second = mstore.create_monitor(_spec("b"))
        mine = mstore.create_monitor(_spec("paused by the user"))
        mstore.set_status(mine["id"], st.STATUS_PAUSED, "paused in the Monitors panel")

        assert tray.pause_all(mstore) == 2
        assert mstore.count_active() == 0
        assert tray.summary(mstore)["paused"] == 2

        assert tray.resume_all(mstore) == 2
        assert {m["id"] for m in mstore.list_monitors((st.STATUS_ACTIVE,))} == {first["id"], second["id"]}
        assert mstore.get_monitor(mine["id"])["status"] == st.STATUS_PAUSED

    def test_the_menu_offers_resume_while_monitors_are_paused_from_it(self, mstore):
        assert [i["id"] for i in tray.menu_model(tray.summary(mstore)) if i["id"] != "-"] == [
            "summary", "open", "monitors", "pause", "stop"]
        assert next(i for i in tray.menu_model(tray.summary(mstore)) if i["id"] == "pause")["enabled"] is False

        mstore.create_monitor(_spec())
        assert next(i for i in tray.menu_model(tray.summary(mstore)) if i["id"] == "pause")["enabled"] is True
        tray.pause_all(mstore)
        ids = [i["id"] for i in tray.menu_model(tray.summary(mstore))]
        assert "resume" in ids and "pause" not in ids

    def test_tooltip_counts_active_monitors_and_new_events(self):
        text = tray.tooltip({"active": 2, "paused": 0, "unseen": 3, "running": 1})
        assert text.splitlines()[0] == "GilzaBots"
        assert "2" in text and "3" in text and "1" in text

    def test_open_raises_the_window_or_starts_the_app(self, mstore, _isolated_monitors):
        result = tray.open_window(mstore, view="monitors")
        assert result == {"launched": True} and len(_isolated_monitors) == 1
        request = mstore.take_open_request()
        assert (request["project"], request["chat_id"], request["view"]) == ("", "", "monitors")

        mstore.touch_host("gui", os.getpid(), "proj", "/proj")
        assert tray.open_window(mstore) == {"launched": False} and len(_isolated_monitors) == 1
        assert mstore.take_open_request()["view"] == ""


# ── Status the panel shows ──────────────────────────────────────────────────

class TestStatus:
    def test_status_follows_the_setting_and_the_daemon(self, mstore):
        assert tray.tray_status(mstore, daemon_running=False)["state"] == tray.STATE_STOPPED
        tray.set_tray_enabled(mstore, False)
        assert tray.tray_status(mstore, daemon_running=True) == {
            "enabled": False, "state": tray.STATE_DISABLED, "reason": ""}

    def test_daemon_status_carries_the_tray(self):
        assert service.daemon_status()["tray"]["state"] == tray.STATE_STOPPED


# ── Reaching the desktop ────────────────────────────────────────────────────

class TestGraphicalEnvironment:
    def test_an_environment_with_a_display_is_used_as_is(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        env = tray.graphical_environment({"WAYLAND_DISPLAY": "wayland-0"}, manager_env=lambda: pytest.fail("probed"))
        assert env == {"WAYLAND_DISPLAY": "wayland-0"}

    def test_a_service_started_before_the_desktop_reads_the_user_manager(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        env = tray.graphical_environment(
            {"HOME": "/home/u"},
            manager_env=lambda: {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000", "PATH": "/x"},
        )
        assert env == {"HOME": "/home/u", "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}

    def test_no_desktop_yet(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert tray.graphical_environment({"HOME": "/home/u"}, manager_env=dict) is None

    def test_windows_always_has_a_desktop(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert tray.graphical_environment({"USERPROFILE": "C:\\u"}, manager_env=lambda: pytest.fail("probed")) == {
            "USERPROFILE": "C:\\u"}


# ── Supervision ─────────────────────────────────────────────────────────────

class TestSupervisor:
    def test_starts_the_icon_and_restarts_it_after_a_clean_exit(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        supervisor.step()
        assert len(spawner.processes) == 1
        first = spawner.processes[0]
        assert first.argv == ["tray", str(os.getpid())] and first.kwargs["env"] == {"DISPLAY": ":0"}
        assert _status(tmp_path)["state"] == tray.STATE_RUNNING and _status(tmp_path)["pid"] == first.pid

        clock.advance(1)
        supervisor.step()  # throttled
        first.code = 0
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 2

    def test_a_crash_is_retried_with_a_growing_delay(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        supervisor.step()
        spawner.processes[-1].code = 1
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 1 and _status(tmp_path)["state"] == tray.STATE_FAILED
        clock.advance(10)
        supervisor.step()
        assert len(spawner.processes) == 2
        spawner.processes[-1].code = 1
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        clock.advance(10)
        supervisor.step()
        assert len(spawner.processes) == 2  # the second delay is 20 s
        clock.advance(10)
        supervisor.step()
        assert len(spawner.processes) == 3

    def test_a_desktop_without_a_tray_is_reported_and_retried_later(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        supervisor.step()
        spawner.processes[-1].code = tray.EXIT_NO_TRAY
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        status = _status(tmp_path)
        assert status["state"] == tray.STATE_NO_TRAY and "AppIndicator" in status["reason"]
        clock.advance(tray.RETRY_NO_TRAY_SECONDS - tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 1
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 2

    def test_waits_for_the_desktop(self, mstore, clock, tmp_path):
        env = {"value": None}
        supervisor, spawner = _supervisor(mstore, clock, tmp_path, environment=lambda: env["value"])
        supervisor.step()
        assert spawner.processes == [] and _status(tmp_path)["state"] == tray.STATE_NO_SESSION
        env["value"] = {"WAYLAND_DISPLAY": "wayland-0"}
        clock.advance(tray.RETRY_NO_SESSION_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 1

    def test_without_qt_nothing_is_started(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path, qt=lambda: (False, "PyQt6 is not installed"))
        supervisor.step()
        assert spawner.processes == []
        status = _status(tmp_path)
        assert (status["state"], status["reason"]) == (tray.STATE_NO_QT, "PyQt6 is not installed")

    def test_turning_the_setting_off_ends_the_icon(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        supervisor.step()
        tray.set_tray_enabled(mstore, False)
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert spawner.processes[0].terminated
        assert _status(tmp_path)["state"] == tray.STATE_DISABLED
        clock.advance(tray.SUPERVISE_SECONDS)
        supervisor.step()
        assert len(spawner.processes) == 1

    def test_close_ends_the_icon_and_records_it(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        supervisor.step()
        supervisor.close()
        assert spawner.processes[0].terminated and _status(tmp_path)["state"] == tray.STATE_STOPPED


# ── The daemon and its icon ─────────────────────────────────────────────────

class _Notifier:
    backend = "test"

    def send(self, *args, **kwargs):
        pass

    def start_listener(self, callback):
        pass

    def stop(self):
        pass


class _Engine:
    async def tick(self):
        return None


class TestDaemon:
    def test_the_daemon_supervises_and_closes_its_icon(self, mstore, clock, tmp_path):
        supervisor, spawner = _supervisor(mstore, clock, tmp_path)
        daemon = MonitorDaemon(mstore, notifier=_Notifier(), engine=_Engine(), clock=clock,
                               idle_exit_seconds=0, fingerprint=lambda: "same", tray=supervisor)
        asyncio.run(daemon.step())
        assert len(spawner.processes) == 1
        asyncio.run(daemon.run())  # idle: exits at once
        assert spawner.processes[0].terminated

    def test_monitors_paused_from_the_icon_keep_the_daemon_and_its_resume(self, mstore, clock):
        daemon = MonitorDaemon(mstore, notifier=_Notifier(), engine=_Engine(), clock=clock,
                               fingerprint=lambda: "same")
        mstore.create_monitor(_spec())
        tray.pause_all(mstore)
        assert daemon.busy()
        tray.resume_all(mstore)
        assert mstore.count_active() == 1
        mstore.set_status(mstore.list_monitors()[0]["id"], st.STATUS_PAUSED, "paused in the Monitors panel")
        assert not daemon.busy()


# ── The real icon process ───────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="the Windows icon is WinForms, not Qt")
def test_the_icon_reports_a_desktop_without_a_tray(tmp_path):
    """Qt's offscreen platform has no system tray: the icon exits with EXIT_NO_TRAY."""
    pytest.importorskip("PyQt6.QtWidgets")
    code = (
        "import sys\n"
        "from gilzabots.monitors import service, tray\n"
        f"service.monitors_dir = lambda: {str(tmp_path)!r}\n"
        "tray.TRAY_WAIT_SECONDS = 0\n"
        "sys.exit(tray.run_tray(1))\n"
    )
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "GILZABOTS_HOME": str(tmp_path / "home")}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                            env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert result.returncode == tray.EXIT_NO_TRAY, result.stdout + result.stderr
    assert "no system tray" in result.stdout


def test_a_second_icon_does_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "tray_lock_path", lambda: str(tmp_path / "tray.lock"))
    held = service.DaemonLock(str(tmp_path / "tray.lock"))
    assert held.acquire()
    try:
        assert tray.run_tray(os.getpid()) == tray.EXIT_DUPLICATE
    finally:
        held.release()


# ── Windows: the icon is WinForms, not Qt ───────────────────────────────────

def test_the_toolkit_probe_follows_the_platform(monkeypatch):
    from gilzabots.monitors import tray_windows

    monkeypatch.setattr(tray_windows, "toolkit_available", lambda: (False, "pythonnet is not installed"))
    monkeypatch.setattr(tray, "qt_available", lambda: (True, ""))
    monkeypatch.setattr(sys, "platform", "win32")
    assert tray.toolkit_available() == (False, "pythonnet is not installed")
    monkeypatch.setattr(sys, "platform", "linux")
    assert tray.toolkit_available() == (True, "")


def test_the_supervisor_probes_the_platform_toolkit_by_default(mstore, tmp_path):
    supervisor = tray.TraySupervisor(mstore, status_path=str(tmp_path / "tray.json"))
    assert supervisor.qt_probe is tray.toolkit_available


def test_the_windows_tooltip_is_cut_to_what_notifyicon_accepts():
    from gilzabots.monitors.tray_windows import NOTIFY_TEXT_LIMIT, notify_text

    assert notify_text("GilzaBots\n2 active") == "GilzaBots\n2 active"
    long = "x" * 200
    assert len(notify_text(long)) == NOTIFY_TEXT_LIMIT
    assert notify_text(long).endswith("…")


def _run_windows_icon(tmp_path, daemon_seconds: float) -> subprocess.CompletedProcess:
    code = (
        "import subprocess, sys\n"
        "from gilzabots.monitors import service, tray\n"
        f"service.monitors_dir = lambda: {str(tmp_path)!r}\n"
        f"daemon = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({daemon_seconds})'])\n"
        "if not daemon_seconds_positive:\n"
        "    daemon.wait()\n"
        "sys.exit(tray.run_tray(daemon.pid))\n"
    ).replace("daemon_seconds_positive", str(daemon_seconds > 0))
    env = {**os.environ, "GILZABOTS_HOME": str(tmp_path / "home")}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=90,
                          env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.skipif(sys.platform != "win32", reason="the WinForms icon exists only on Windows")
def test_the_windows_icon_is_shown_and_ends_with_its_daemon(tmp_path):
    pytest.importorskip("clr")
    result = _run_windows_icon(tmp_path, 3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tray icon" in result.stdout and "shown for daemon" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="the WinForms icon exists only on Windows")
def test_the_windows_icon_does_not_start_for_a_daemon_already_gone(tmp_path):
    pytest.importorskip("clr")
    result = _run_windows_icon(tmp_path, 0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shown for daemon" not in result.stdout
