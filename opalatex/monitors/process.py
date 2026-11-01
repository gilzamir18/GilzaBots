"""Process helpers shared by the monitor daemon and its clients."""
from __future__ import annotations

import os
import subprocess
import sys


def pid_alive(pid: int) -> bool:
    """True when a process with *pid* exists on this machine."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def opalatex_command() -> list[str]:
    """The argv that starts this OpalaTex installation.

    A frozen build is its own executable. A source checkout is not installed
    as a package, so it runs its ``main.py`` by absolute path (a service starts
    in the home directory, where ``-m`` would not find the package); an
    installed package, the snap included, runs ``-m opalatex.cli`` as the snap
    launcher does. On Windows a source install uses ``pythonw.exe`` so a
    background process does not open a console window.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    executable = sys.executable
    if sys.platform == "win32":
        candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.isfile(candidate):
            executable = candidate
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(os.path.dirname(package_dir), "main.py")
    if os.path.isfile(main_py):
        return [executable, main_py]
    return [executable, "-m", "opalatex.cli"]


def spawn_detached(argv: list[str], *, log_path: str | None = None, env: dict | None = None) -> subprocess.Popen:
    """Start *argv* so that it outlives the calling process and its terminal."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    kwargs["stdout"] = log
    kwargs["stderr"] = subprocess.STDOUT if log_path else subprocess.DEVNULL
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, **kwargs)
    finally:
        if log_path:
            log.close()
