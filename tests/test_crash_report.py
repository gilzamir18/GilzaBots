"""Tests for the native-crash record GilzaBots keeps about itself.

The case that motivates this module cannot be reproduced with an exception: the
process dies inside a DLL. So the end-to-end tests kill a real child process
with a real fault and then ask, from the parent, the same question the next
launch asks — "did the previous run get to shut down?".
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from gilzabots import crash_report


def _run_child(home: Path, body: str) -> subprocess.CompletedProcess:
    """Run `body` in a child that has crash reporting installed against `home`."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(crash_report.__file__).resolve().parent.parent)!r})
        from gilzabots import crash_report
        crash_report.install(home={str(home)!r}, details={{"webengine_gpu": "auto"}})
        {textwrap.indent(textwrap.dedent(body), "        ").strip()}
        """
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)


def _markers(home: Path) -> list[Path]:
    return sorted(crash_report.runs_dir(home).glob("*.json"))


def test_install_writes_a_marker_and_a_session_banner(tmp_path):
    result = _run_child(tmp_path, "print(crash_report.crash_log_path(home=%r))" % str(tmp_path))
    assert result.returncode == 0, result.stderr

    log = crash_report.crash_log_path(tmp_path).read_text(encoding="utf-8")
    assert "=== GilzaBots session pid=" in log
    assert "webengine_gpu: auto" in log

    # The child exited normally, so atexit retired its marker.
    assert _markers(tmp_path) == []


def test_clean_exit_leaves_nothing_for_the_next_launch_to_report(tmp_path):
    result = _run_child(tmp_path, "crash_report.mark_clean_exit()")
    assert result.returncode == 0, result.stderr
    assert crash_report.collect_previous_run_failures(home=tmp_path) == []


def test_a_native_fault_is_reported_with_its_log(tmp_path):
    """The whole point: a process that dies in native code still says so.

    ``faulthandler._sigsegv`` is CPython's own way of provoking a real fault —
    a bad dereference through ``ctypes`` is not equivalent, because ctypes
    catches the access violation and turns it into an ``OSError``, which lets
    the interpreter shut down normally.
    """
    result = _run_child(tmp_path, "import faulthandler; faulthandler._sigsegv()")
    assert result.returncode != 0, "the child was supposed to die natively"

    failures = crash_report.collect_previous_run_failures(home=tmp_path)
    assert len(failures) == 1
    failure = failures[0]
    assert failure["native_fault"] is True
    assert failure["webengine_gpu"] == "auto"
    assert "most recent call first" in failure["excerpt"]
    assert crash_report.describe_failure(failure).startswith("[GilzaBots] The previous run")


def test_context_recorded_after_installation_reaches_the_marker(tmp_path):
    """The entry point installs before the graphics mode is known.

    A crash notice that cannot say which mode the dead run used is a notice the
    user cannot act on, so `record` updates the marker as well as the log.
    """
    result = _run_child(
        tmp_path,
        'crash_report.record({"webengine_gpu": "off"})\n'
        "import faulthandler; faulthandler._sigsegv()",
    )
    assert result.returncode != 0

    failure = crash_report.collect_previous_run_failures(home=tmp_path)[0]
    assert failure["webengine_gpu"] == "off"


def test_consume_archives_the_marker_so_it_is_reported_once(tmp_path):
    result = _run_child(tmp_path, "import faulthandler; faulthandler._sigsegv()")
    assert result.returncode != 0

    assert len(crash_report.consume_previous_run_failures(home=tmp_path)) == 1
    assert crash_report.consume_previous_run_failures(home=tmp_path) == []
    # The evidence outlives the notice.
    assert list(crash_report.reported_dir(tmp_path).glob("*.json"))


def test_a_second_live_instance_is_not_a_crash(tmp_path):
    """GilzaBots allows two windows; neither may be reported as the other's crash."""
    marker_dir = crash_report.runs_dir(tmp_path)
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "4242-live.json").write_text(
        json.dumps({"pid": 4242, "started": "2026-09-15T00:00:00+00:00",
                    "last_seen": "2026-09-15T00:00:30+00:00"}),
        encoding="utf-8",
    )

    alive = crash_report.collect_previous_run_failures(
        home=tmp_path,
        now=_timestamp("2026-09-15T00:01:00+00:00"),
        is_alive=lambda pid: True,
    )
    assert alive == []

    gone = crash_report.collect_previous_run_failures(
        home=tmp_path,
        now=_timestamp("2026-09-15T00:01:00+00:00"),
        is_alive=lambda pid: False,
    )
    assert [f["pid"] for f in gone] == [4242]


def test_a_stale_marker_survives_pid_reuse(tmp_path):
    """A pid that answers but stopped refreshing belongs to somebody else now."""
    marker_dir = crash_report.runs_dir(tmp_path)
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "77-stale.json").write_text(
        json.dumps({"pid": 77, "started": "2026-09-15T00:00:00+00:00",
                    "last_seen": "2026-09-15T00:00:00+00:00"}),
        encoding="utf-8",
    )

    failures = crash_report.collect_previous_run_failures(
        home=tmp_path,
        now=_timestamp("2026-09-15T01:00:00+00:00"),
        is_alive=lambda pid: True,
    )
    assert [f["pid"] for f in failures] == [77]


def test_excerpt_returns_only_the_requested_run(tmp_path):
    log = crash_report.crash_log_path(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "=== GilzaBots session pid=1 started=a ===\n  first run\n"
        "=== GilzaBots session pid=2 started=b ===\n  second run\n",
        encoding="utf-8",
    )

    assert "first run" in crash_report.crash_log_excerpt(1, home=tmp_path)
    assert "second run" not in crash_report.crash_log_excerpt(1, home=tmp_path)
    assert "second run" in crash_report.crash_log_excerpt(2, home=tmp_path)
    assert crash_report.crash_log_excerpt(3, home=tmp_path) == ""


def test_process_liveness_never_kills_the_process_it_asks_about(tmp_path):
    """On Windows os.kill(pid, 0) terminates the target; this must not."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert crash_report.process_is_alive(child.pid) is True
        assert child.poll() is None, "asking whether it was alive killed it"
    finally:
        child.kill()
        child.wait(timeout=10)
    assert crash_report.process_is_alive(child.pid) is False
    assert crash_report.process_is_alive(0) is False


def _timestamp(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def test_a_long_dump_is_still_classified_and_keeps_what_happened(tmp_path):
    """A dump bigger than the display limit must not lose its first lines.

    faulthandler writes the fault and the faulting thread first, then every other
    thread. The excerpt used to keep the tail, and classification read the
    excerpt — so the first crash with a busy Python process was announced as a
    run that merely "ended without shutting down".
    """
    other_threads = "".join(
        f"\nThread 0x{n:08x} (most recent call first):\n"
        + "".join(f'  File "C:\\very\\long\\path\\module_{n}_{k}.py", line {k} in fn\n' for k in range(40))
        for n in range(30)
    )
    log = crash_report.crash_log_path(tmp_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "=== GilzaBots session pid=9 started=2026-09-16T00:00:00+00:00 ===\n"
        "  webengine_gpu: auto\n"
        "Windows fatal exception: access violation\n\n"
        "Current thread 0x00004a6c (most recent call first):\n"
        '  File "onnxruntime\\capi\\_pybind_state.py", line 32 in <module>\n'
        + other_threads,
        encoding="utf-8",
    )
    runs = crash_report.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "9-x.json").write_text(
        json.dumps({"pid": 9, "started": "2026-09-16T00:00:00+00:00", "log_path": str(log)}),
        encoding="utf-8",
    )

    assert len(log.read_text(encoding="utf-8")) > crash_report.MAX_EXCERPT_CHARS * 2

    failure = crash_report.collect_previous_run_failures(
        home=tmp_path, is_alive=lambda pid: False
    )[0]
    assert failure["native_fault"] is True
    assert "Windows fatal exception: access violation" in failure["excerpt"]
    assert "Current thread 0x00004a6c" in failure["excerpt"]
    assert "_pybind_state.py" in failure["excerpt"]
    assert len(failure["excerpt"]) <= crash_report.MAX_EXCERPT_CHARS + 2

    # Classification never depends on whether the caller wanted the excerpt.
    quiet = crash_report.collect_previous_run_failures(
        home=tmp_path, is_alive=lambda pid: False, include_excerpt=False
    )[0]
    assert quiet["native_fault"] is True
    assert quiet["excerpt"] == ""
