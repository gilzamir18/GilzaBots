"""Keep the operating system awake while an agent turn is running.

An agent turn is unattended work: the user starts it and looks away, which is
exactly the situation the idle-sleep timer was written for. When the machine
suspends mid-turn every socket dies with it, so on resume the GUI has lost the
stream of a turn the server still owns and the user is met with the *"An agent
turn is still running, but this window lost its connection to it"* dialog
(`App.jsx`, `utils/agentRunConflict.js`) — being asked to kill work they were
waiting for. The turn-outlives-its-stream contract (PROJECT_DESIGN 2.6) makes
that recoverable rather than fatal, but the suspend itself is what should not
have happened: nothing that runs while the user waits for it should let the
system decide the wait is idleness.

This asks the OS not to *idle*-sleep. It deliberately does not block a sleep the
user asks for (closing the lid, choosing Sleep) and does not keep the display on:
the screen may go dark while the turn keeps running, which is the behaviour a
long compile or download already has.

Inhibition is reference counted, so nested holders (a turn plus a worker
delegation) cannot release each other's hold, and every backend failure is
non-fatal: a machine with no inhibitor available still runs its turns, it just
sleeps as it did before.
"""

from __future__ import annotations

import subprocess
import sys
import threading

__all__ = ["SleepInhibitor", "inhibitor", "backend_for_platform"]


class _NullBackend:
    """Used where no inhibitor is available; the turn still runs."""

    name = "none"

    def start(self, reason: str) -> None:
        raise RuntimeError("no sleep inhibitor is available on this platform")

    def stop(self) -> None:
        pass


class _WindowsBackend:
    """`SetThreadExecutionState`, held by a thread that outlives the call.

    The flag belongs to the *thread* that sets it and is cleared when that
    thread exits, so it cannot be set from whichever worker happens to start
    the turn. A dedicated thread holds it and parks until release.
    """

    name = "windows"

    _ES_CONTINUOUS = 0x80000000
    _ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None

    def start(self, reason: str) -> None:
        import ctypes

        stop_event = threading.Event()
        self._stop_event = stop_event
        self._started.clear()
        self._error = None

        def _hold() -> None:
            try:
                set_state = ctypes.windll.kernel32.SetThreadExecutionState
                if not set_state(self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED):
                    raise OSError("SetThreadExecutionState refused the request")
            except BaseException as exc:  # reported to the caller below
                self._error = exc
                self._started.set()
                return
            self._started.set()
            stop_event.wait()
            try:
                set_state(self._ES_CONTINUOUS)
            except Exception:
                pass

        self._thread = threading.Thread(
            target=_hold, name="gilzabots-keep-awake", daemon=True
        )
        self._thread.start()
        self._started.wait(timeout=5)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self._thread = None
        self._stop_event = None


class _SubprocessBackend:
    """A child process that holds the inhibition for as long as it lives."""

    def __init__(self, name: str, command: list[str]) -> None:
        self.name = name
        self._command = command
        self._process: subprocess.Popen | None = None

    def start(self, reason: str) -> None:
        command = [part.replace("{reason}", reason) for part in self._command]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def backend_for_platform(platform: str | None = None):
    """Return the inhibitor backend for *platform* (defaults to this machine)."""
    platform = platform or sys.platform
    if platform == "win32":
        return _WindowsBackend()
    if platform == "darwin":
        # -i prevents idle *system* sleep and leaves display sleep alone.
        return _SubprocessBackend("macos", ["caffeinate", "-i"])
    if platform.startswith("linux"):
        return _SubprocessBackend(
            "linux",
            [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=GilzaBots",
                "--why={reason}",
                "--mode=block",
                "sleep",
                "infinity",
            ],
        )
    return _NullBackend()


class SleepInhibitor:
    """Reference-counted hold on the system's idle-sleep timer."""

    def __init__(self, backend=None) -> None:
        self._backend = backend if backend is not None else backend_for_platform()
        self._lock = threading.Lock()
        self._holders = 0
        self._active = False
        self._reported_failure = False

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "name", "unknown")

    @property
    def active(self) -> bool:
        return self._active

    @property
    def holders(self) -> int:
        return self._holders

    def acquire(self, reason: str = "GilzaBots is running an agent turn") -> bool:
        """Hold off idle sleep. Returns whether the hold is actually in place."""
        with self._lock:
            self._holders += 1
            if self._holders > 1 or self._active:
                return self._active
            try:
                self._backend.start(reason)
                self._active = True
            except Exception as exc:
                # A machine that cannot inhibit sleep still runs its turns; say
                # so once rather than on every turn.
                if not self._reported_failure:
                    self._reported_failure = True
                    print(
                        "[GilzaBots] Could not keep the system awake for agent turns "
                        f"({type(exc).__name__}: {exc}). Turns still run, but the "
                        "system may sleep during one.",
                        file=sys.stderr,
                        flush=True,
                    )
            return self._active

    def release(self) -> None:
        """Drop one hold; the last one out lets the system idle again."""
        with self._lock:
            if self._holders == 0:
                return
            self._holders -= 1
            if self._holders > 0 or not self._active:
                return
            try:
                self._backend.stop()
            finally:
                self._active = False


# The process-wide hold used by the agent turn lifecycle in `ide_server.py`.
inhibitor = SleepInhibitor()
