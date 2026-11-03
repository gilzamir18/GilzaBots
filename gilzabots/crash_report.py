"""Native-crash visibility for GilzaBots.

A Python exception always leaves a traceback behind. A *native* crash leaves
nothing: the process dies inside a DLL, the window disappears, and the terminal
stays empty — which is exactly what a user sees when the embedded QtWebEngine
browser faults (its GPU service runs inside this very process on Windows, so a
graphics-driver fault takes the whole application with it). Nothing in the
application could report that, because nothing in the application was still
running to report it.

Two mechanisms close that gap, and they are deliberately independent:

* ``faulthandler`` writes the fatal signal and every thread's Python stack to
  ``<GILZABOTS_HOME>/logs/crash.log`` as the process dies. It is installed as a
  Windows vectored handler, so it also records *survivable* first-chance faults
  (``ctypes`` reading a bad pointer, for instance, which the caller then turns
  into ``OSError``). An entry in the log is therefore evidence of a fault, not
  proof that the fault was fatal — which is why it is not used to decide
  whether the previous run died.

* A **run marker** decides that. Every run writes ``logs/runs/<pid>-<start>.json``
  and deletes it on the way out; a marker still on disk whose process is gone
  is a run that never reached its exit path. The marker is refreshed by a
  heartbeat so a stale one cannot be mistaken for a live process that happens
  to have inherited the same pid, and so a *second* instance of GilzaBots — the
  application allows one — is never reported as a crash of the first.

Exit paths that bypass ``atexit`` (the GUI server ends with ``os._exit``) must
call `mark_clean_exit` themselves; a missed call costs a spurious crash notice,
never a missed one, which is the right direction for this trade.
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# How often the live run refreshes its marker, and how long a marker may go
# unrefreshed before the run behind it is assumed dead even though something
# still answers to its pid.
HEARTBEAT_SECONDS = 60
STALE_AFTER_SECONDS = 300

# The crash log is append-only across runs, so it needs a ceiling. One previous
# generation is kept: a crash the user reports days later is usually the last
# one, and the one before it is worth having for comparison.
MAX_LOG_BYTES = 2 * 1024 * 1024

# Upper bound on how much of the log a single notice carries. A dump of every
# thread in a QtWebEngine process is long, and the notice only has to say what
# happened and where to read the rest.
MAX_EXCERPT_CHARS = 8000

_SESSION_BANNER = "=== GilzaBots session "

_state: dict[str, Any] = {
    "installed": False,
    "log_file": None,
    "log_path": None,
    "marker_path": None,
    "started": None,
    "stop_heartbeat": None,
}


# ── Paths ─────────────────────────────────────────────────────────────────────

def _home(home: str | os.PathLike[str] | None = None) -> Path:
    if home is not None:
        return Path(home)
    from .config import get_gilzabots_home

    return Path(get_gilzabots_home())


def logs_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return _home(home) / "logs"


def crash_log_path(home: str | os.PathLike[str] | None = None) -> Path:
    return logs_dir(home) / "crash.log"


def runs_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return logs_dir(home) / "runs"


def reported_dir(home: str | os.PathLike[str] | None = None) -> Path:
    return runs_dir(home) / "reported"


# ── Process liveness ──────────────────────────────────────────────────────────

def process_is_alive(pid: int) -> bool:
    """Whether `pid` still belongs to a running process.

    ``os.kill(pid, 0)`` is not usable on Windows: any signal other than the two
    console events terminates the target, so probing with it would kill the
    process it is asking about.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, but a process nonetheless.
        return True
    except OSError:
        return False
    return True


# ── Installation ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rotate_log(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            previous = path.with_suffix(path.suffix + ".1")
            previous.unlink(missing_ok=True)
            path.replace(previous)
    except Exception:
        pass


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _heartbeat_loop(path: Path, stop: threading.Event, interval: float) -> None:
    # The payload is read from the shared state on every beat rather than
    # captured once, so context added after installation (the graphics mode,
    # decided later than the entry point that installs this) reaches the marker
    # a crashed run leaves behind, not only the log.
    while not stop.wait(interval):
        payload = dict(_state.get("marker_payload") or {}, last_seen=_now())
        try:
            _write_marker(path, payload)
        except Exception:
            return


def install(
    *,
    home: str | os.PathLike[str] | None = None,
    details: dict[str, Any] | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> dict[str, Any]:
    """Start recording native crashes for this process.

    Idempotent: a second call only appends `details` to the log, so both the
    CLI entry point and the GUI server can call it without racing over one
    marker. Every failure here is swallowed — diagnostics must never be the
    reason the application does not start.
    """
    if _state["installed"]:
        if details:
            record(details)
        return dict(_state)

    try:
        directory = logs_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        runs_dir(home).mkdir(parents=True, exist_ok=True)

        log_path = crash_log_path(home)
        _rotate_log(log_path)
        log_file = open(log_path, "a", encoding="utf-8", errors="replace")

        started = _now()
        pid = os.getpid()
        banner = {
            "started": started,
            "pid": pid,
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "argv": sys.argv[1:],
        }
        if details:
            banner.update(details)
        log_file.write(f"\n{_SESSION_BANNER}pid={pid} started={started} ===\n")
        for key, value in banner.items():
            log_file.write(f"  {key}: {value}\n")
        log_file.flush()

        faulthandler.enable(file=log_file, all_threads=True)

        marker_path = runs_dir(home) / f"{pid}-{started.replace(':', '').replace('.', '')}.json"
        marker = {**banner, "last_seen": started, "log_path": str(log_path)}
        _write_marker(marker_path, marker)

        stop = threading.Event()
        thread = threading.Thread(
            target=_heartbeat_loop,
            args=(marker_path, stop, heartbeat_seconds),
            name="gilzabots-run-marker",
            daemon=True,
        )

        _state.update(
            installed=True,
            log_file=log_file,
            log_path=log_path,
            marker_path=marker_path,
            marker_payload=marker,
            started=started,
            stop_heartbeat=stop,
        )
        thread.start()
        atexit.register(mark_clean_exit)
    except Exception as exc:  # pragma: no cover - depends on the host filesystem
        print(f"[GilzaBots] crash reporting unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)

    return dict(_state)


def record(details: dict[str, Any] | str) -> None:
    """Add context to this run's section of the crash log.

    Anything recorded here is what the log will still be able to say about a
    run that ends without warning — the window size, the graphics mode, the
    model in use — so it is written and flushed immediately rather than kept
    in memory.
    """
    log_file = _state.get("log_file")
    if log_file is None:
        return
    try:
        if isinstance(details, str):
            log_file.write(f"  {details}\n")
        else:
            for key, value in details.items():
                log_file.write(f"  {key}: {value}\n")
        log_file.flush()
    except Exception:
        pass

    # The marker is what a *later* run reads, so structured context belongs
    # there too: a crash notice that cannot say which graphics mode the dead
    # run used is a notice the user cannot act on.
    if isinstance(details, dict):
        payload = _state.get("marker_payload")
        marker_path = _state.get("marker_path")
        if payload is not None and marker_path is not None:
            payload.update(details)
            payload["last_seen"] = _now()
            try:
                _write_marker(Path(marker_path), payload)
            except Exception:
                pass


def mark_clean_exit() -> None:
    """Record that this run reached its exit path.

    Called from ``atexit`` and, explicitly, from the exit paths that bypass it
    (``os._exit``). Safe to call more than once.
    """
    stop = _state.get("stop_heartbeat")
    if stop is not None:
        stop.set()
    marker_path = _state.get("marker_path")
    if marker_path is not None:
        try:
            Path(marker_path).unlink(missing_ok=True)
        except Exception:
            pass
        _state["marker_path"] = None
    log_file = _state.get("log_file")
    if log_file is not None:
        try:
            log_file.write(f"=== session ended cleanly at {_now()} ===\n")
            log_file.flush()
        except Exception:
            pass


# ── Reading what the previous runs left behind ────────────────────────────────

def _session_section(
    pid: int,
    *,
    home: str | os.PathLike[str] | None = None,
    log_path: str | os.PathLike[str] | None = None,
) -> str:
    """The whole section of the crash log written by the run that owned `pid`."""
    path = Path(log_path) if log_path else crash_log_path(home)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    marker = f"{_SESSION_BANNER}pid={pid} "
    start = text.rfind(marker)
    if start < 0:
        return ""
    end = text.find(_SESSION_BANNER, start + len(marker))
    section = text[start:] if end < 0 else text[start:end]
    return section.strip()


def _bounded(section: str, limit: int) -> str:
    """Shorten a section for display, keeping its *head*.

    faulthandler writes the kind of fault and the stack of the thread that took
    it first, and every other thread after that. Keeping the tail — which is
    what this used to do — dropped exactly the two lines that say what happened
    once a dump grew past the limit, as the first dump with a Python thread in
    it did: a process with a dozen threads and long paths is several times the
    limit on its own.
    """
    if len(section) <= limit:
        return section
    return section[:limit] + "\n…"


def _is_native_fault(section: str) -> bool:
    """Whether a run's section records a fault faulthandler caught.

    How faulthandler announces one, on each platform: "Windows fatal exception:
    access violation" and "Fatal Python error: Segmentation fault". A dead run
    with neither ended some other way, such as being killed from outside. This
    reads the whole section, never a shortened one: classifying from the
    display copy is how a native fault was once reported as a run that merely
    "ended without shutting down".
    """
    lowered = section.lower()
    return "fatal exception" in lowered or "fatal python error" in lowered


def crash_log_excerpt(
    pid: int,
    *,
    home: str | os.PathLike[str] | None = None,
    log_path: str | os.PathLike[str] | None = None,
    limit: int = MAX_EXCERPT_CHARS,
) -> str:
    """Return the crash log written by the run that owned `pid`.

    The session banner carries the pid, so a run's own section can be sliced
    out of a log several runs share. An empty string means the run left no
    entry at all — a process killed from outside, for example, which faults
    nowhere and dumps nothing.
    """
    return _bounded(_session_section(pid, home=home, log_path=log_path), limit)


def _marker_is_dead(marker: dict[str, Any], now: float, is_alive: Callable[[int], bool]) -> bool:
    pid = marker.get("pid")
    if not isinstance(pid, int):
        return True
    if not is_alive(pid):
        return True
    # The pid answers, but this marker has not been refreshed in a long time:
    # the run that wrote it is gone and its number was handed to someone else.
    last_seen = marker.get("last_seen") or marker.get("started")
    try:
        seen_at = datetime.fromisoformat(str(last_seen)).timestamp()
    except Exception:
        return False
    return (now - seen_at) > STALE_AFTER_SECONDS


def collect_previous_run_failures(
    *,
    home: str | os.PathLike[str] | None = None,
    now: float | None = None,
    is_alive: Callable[[int], bool] | None = None,
    include_excerpt: bool = True,
) -> list[dict[str, Any]]:
    """Describe every earlier run that never reached its exit path.

    Does not modify anything; `consume_previous_run_failures` is the caller
    that also stops a failure being reported twice.
    """
    now = time.time() if now is None else now
    is_alive = process_is_alive if is_alive is None else is_alive
    own_marker = _state.get("marker_path")

    failures: list[dict[str, Any]] = []
    try:
        entries: Iterable[Path] = sorted(runs_dir(home).glob("*.json"))
    except Exception:
        return failures

    for entry in entries:
        if own_marker is not None and Path(entry) == Path(own_marker):
            continue
        try:
            marker = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            # An unreadable marker still means a run that did not clean up.
            marker = {"pid": None, "started": None, "unreadable": True}
        if not isinstance(marker, dict):
            marker = {"pid": None, "started": None, "unreadable": True}
        if not marker.get("unreadable") and not _marker_is_dead(marker, now, is_alive):
            continue

        failure = {
            "pid": marker.get("pid"),
            "started": marker.get("started"),
            "last_seen": marker.get("last_seen"),
            "platform": marker.get("platform"),
            "webengine_gpu": marker.get("webengine_gpu"),
            "log_path": marker.get("log_path") or str(crash_log_path(home)),
            "marker_path": str(entry),
        }
        section = (
            _session_section(marker["pid"], home=home, log_path=failure["log_path"])
            if isinstance(marker.get("pid"), int)
            else ""
        )
        failure["native_fault"] = _is_native_fault(section)
        failure["excerpt"] = _bounded(section, MAX_EXCERPT_CHARS) if include_excerpt else ""
        failures.append(failure)

    return failures


def consume_previous_run_failures(
    *,
    home: str | os.PathLike[str] | None = None,
    now: float | None = None,
    is_alive: Callable[[int], bool] | None = None,
) -> list[dict[str, Any]]:
    """`collect_previous_run_failures`, with each failure archived once read.

    The marker moves to ``logs/runs/reported/`` rather than being deleted: the
    evidence that a run died outlives the notice about it, and a user who
    reports the problem a week later still has the file.
    """
    failures = collect_previous_run_failures(home=home, now=now, is_alive=is_alive)
    if not failures:
        return failures
    try:
        archive = reported_dir(home)
        archive.mkdir(parents=True, exist_ok=True)
        for failure in failures:
            source = Path(failure["marker_path"])
            try:
                os.replace(source, archive / source.name)
            except Exception:
                source.unlink(missing_ok=True)
    except Exception:
        pass
    return failures


def describe_failure(failure: dict[str, Any]) -> str:
    """One line for the terminal, naming the evidence rather than guessing."""
    started = failure.get("started") or "unknown time"
    if failure.get("native_fault"):
        what = "ended in a native fault (no Python exception)"
    else:
        what = "ended without shutting down"
    return (
        f"[GilzaBots] The previous run (pid {failure.get('pid')}, started {started}) {what}. "
        f"Details: {failure.get('log_path')}"
    )
