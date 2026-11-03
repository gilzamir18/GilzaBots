"""GilzaBots entry point — run with: python main.py [--mode auto|plan|edit] [--model ...]"""
import sys
import os
import io

# ── Force UTF-8 globally on Windows ──────────────────────────────────────────
# PyInstaller --windowed builds on Windows create stdout/stderr with cp1252
# encoding or set them to None. Any print() of emoji/unicode then crashes with
# UnicodeEncodeError. We fix this by GUARANTEEING a UTF-8 stream exists.
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

def _ensure_utf8_stream(stream, fallback_name="devnull"):
    """Guarantee a UTF-8 writable stream. Returns the fixed stream."""
    # Case 1: stream is None (PyInstaller --windowed)
    if stream is None:
        return open(os.devnull, "w", encoding="utf-8", errors="replace")

    # Case 2: try reconfigure (works on real terminals)
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        if getattr(stream, "encoding", "").lower().replace("-", "") == "utf8":
            return stream
    except Exception:
        pass

    # Case 3: wrap the binary buffer in a proper UTF-8 TextIOWrapper
    try:
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            wrapper = io.TextIOWrapper(buf, encoding="utf-8", errors="replace", line_buffering=True)
            return wrapper
    except Exception:
        pass

    # Case 4: replace with devnull (in windowed mode nobody reads these anyway)
    return open(os.devnull, "w", encoding="utf-8", errors="replace")

sys.stdout = _ensure_utf8_stream(sys.stdout)
sys.stderr = _ensure_utf8_stream(sys.stderr)


import subprocess

# PyInstaller + PythonNet workaround: explicitly set the python DLL path 
# so pythonnet can initialize correctly on other machines
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    if hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(sys._MEIPASS)
    
    import glob
    import ctypes
    python_dlls = glob.glob(os.path.join(sys._MEIPASS, 'python3*.dll'))
    if python_dlls:
        os.environ["PYTHONNET_PYDLL"] = python_dlls[0]
        try:
            ctypes.CDLL(python_dlls[0])
        except Exception:
            pass

    # Add the specific pywinpty bin directory to PATH so winpty-agent.exe can be found
    meipass = sys._MEIPASS
    winpty_dir = None
    # pywinpty is a Windows-only dependency, so other bundles never carry the
    # agent and walking them found nothing. On Windows the build places it in
    # the `winpty` package directory (build_exe.ps1: --add-binary ...;winpty and
    # --collect-all winpty), the only copy in the bundle; check there directly
    # before falling back to walking tens of thousands of bundled files.
    if sys.platform == "win32":
        for candidate in (meipass, os.path.join(meipass, "winpty")):
            if os.path.isfile(os.path.join(candidate, "winpty-agent.exe")):
                winpty_dir = candidate
                break
        else:
            for root, dirs, files in os.walk(meipass):
                if 'winpty-agent.exe' in files:
                    winpty_dir = root
                    break

    if winpty_dir:
        try:
            os.environ["PATH"] = winpty_dir + os.pathsep + os.environ.get("PATH", "")
        except ValueError:
            # Fallback if PATH exceeds Windows 32K limit
            pass



# Prevent command prompt windows from flashing when using subprocess in --windowed mode on Windows
if sys.platform == "win32":
    _orig_popen_init = subprocess.Popen.__init__
    def _patched_popen_init(self, *args, **kwargs):
        if 'creationflags' not in kwargs:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _patched_popen_init




from gilzabots.cli import main

if __name__ == "__main__":
    main()
