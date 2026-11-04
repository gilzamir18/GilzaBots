"""run_command / run_python_script: timeouts, process trees and long output."""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest


def _call(tool, *args, **kwargs):
    return asyncio.run((getattr(tool, "_func", None) or tool)(*args, **kwargs))


def _result(raw):
    return json.loads(json.loads(raw)["result"])


@pytest.fixture
def project(tmp_path):
    from gilzabots.tools import set_project_context

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    return tmp_path


def test_output_and_exit_code_are_reported(project):
    from gilzabots.tools import run_command

    result = _result(_call(run_command, f'"{sys.executable}" -c "import sys; print(42); sys.exit(3)"'))
    assert result == {"stdout": "42", "stderr": "", "exit_code": 3}


def test_long_output_keeps_its_end_and_saves_the_whole_text(project):
    from gilzabots.tools import run_command

    script = "for i in range(3000): print(f'line {i}')"
    stdout = _result(_call(run_command, f'"{sys.executable}" -c "{script}"'))["stdout"]

    assert stdout.startswith("line 0")
    assert stdout.endswith("line 2999")
    marker = stdout.split("[TRUNCATED: ")[1].split("]")[0]
    saved = marker.split(" is in ")[1].split(";")[0]
    full = (project / saved).read_text(encoding="utf-8")
    assert full.splitlines()[1234] == "line 1234"
    assert "read_content_pos" in marker


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_timeout_stops_every_process_the_command_started(project):
    from gilzabots.tools import run_command

    pid_file = project / "child.pid"
    # The shell starts a background child that would outlive a kill of the shell alone.
    command = f'sleep 60 & echo $! > "{pid_file}"; echo started; wait'
    started = time.monotonic()
    with pytest.raises(ValueError) as error:
        _call(run_command, command, timeout_seconds=1)
    assert time.monotonic() - started < 30
    message = str(error.value)
    assert "timed out after 1 seconds" in message
    assert "stdout before the timeout:\nstarted" in message

    child = int(pid_file.read_text().strip())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        pytest.fail("the command's child process survived the timeout")


@pytest.mark.parametrize("value,expected", [(5000, 1800), ("30", 30)])
def test_timeout_is_capped_and_accepts_numeric_text(value, expected):
    from gilzabots.tools import _command_timeout

    assert _command_timeout(value) == expected


@pytest.mark.parametrize("value", [0, -5, "soon"])
def test_an_invalid_timeout_is_refused(project, value):
    from gilzabots.tools import run_command

    with pytest.raises(ValueError) as error:
        _call(run_command, "echo hi", timeout_seconds=value)
    assert "timeout_seconds" in str(error.value)


def test_run_python_script_shares_the_runner(project):
    from gilzabots.tools import run_python_script

    (project / "hello.py").write_text("import sys\nprint('hi', sys.argv[1])\n", encoding="utf-8")
    assert _result(_call(run_python_script, "hello.py", "there"))["stdout"] == "hi there"


def _wait_gone(pid):
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    os.kill(pid, 9)
    return False


def test_a_synchronous_tool_leaves_the_event_loop_free(project):
    """The backend's HTTP server shares the turn's loop: a blocking tool froze it."""
    from gilzabots.tools import run_command

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        counting = asyncio.create_task(ticker())
        await run_command._func(f'"{sys.executable}" -c "import time; time.sleep(1)"')
        counting.cancel()
        return ticks

    assert asyncio.run(scenario()) >= 10


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_interrupting_the_turn_stops_the_running_command(project):
    from gilzabots.tools import run_command

    pid_file = project / "child.pid"
    command = f'sleep 60 & echo $! > "{pid_file}"; wait'

    async def scenario():
        call = asyncio.create_task(run_command._func(command, timeout_seconds=120))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        started = time.monotonic()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        return time.monotonic() - started

    # The cancel is acknowledged at once, not when the command would have ended.
    assert asyncio.run(scenario()) < 1
    # And the command does not keep running unattended after the interrupt.
    assert _wait_gone(int(pid_file.read_text().strip())), "the command survived the interrupt"
