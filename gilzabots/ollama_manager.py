import subprocess
import json
import threading
import urllib.request
import platform
import re
from .subprocess_utils import utf8_text_kwargs

def check_ollama_status():
    """Check if Ollama is installed and running, and get its version."""
    installed = False
    running = False
    version = None
    is_supported = False

    # Check via CLI
    try:
        result = subprocess.run(["ollama", "--version"], capture_output=True, check=True, **utf8_text_kwargs())
        # Usually outputs: "ollama version is 0.3.14" or "ollama version 0.3.14"
        installed = True
        output = result.stdout.strip()
        parts = output.split()
        for p in parts:
            if p[0].isdigit():
                version = p
                break
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Check if running (port 11434) and fetch exact version
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as response:
            if response.status == 200:
                running = True
                installed = True
                data = json.loads(response.read().decode())
                if data and "version" in data:
                    version = data["version"]
    except Exception:
        pass

    # Parse version to check if >= 0.3.5
    if version:
        try:
            match = re.search(r'(\d+)\.(\d+)\.(\d+)', version)
            if match:
                major, minor, patch = map(int, match.groups())
                if major > 0:
                    is_supported = True
                elif major == 0 and minor > 30:
                    is_supported = True
                elif major == 0 and minor == 30 and patch >= 5:
                    is_supported = True
        except Exception:
            pass

    return {
        "installed": installed,
        "running": running,
        "version": version,
        "is_supported": is_supported
    }

def install_ollama_windows():
    """Triggers the Ollama installation script for Windows."""
    if platform.system() != "Windows":
        return {"success": False, "error": "Not running on Windows."}
    
    try:
        cmd = 'irm https://ollama.com/install.ps1 | iex'
        subprocess.Popen(["powershell", "-Command", cmd], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {"success": True, "message": "Installation started in a new PowerShell window."}
    except Exception as e:
        return {"success": False, "error": str(e)}


# Guards `ollama pull` against being started twice for the same tag: the IDE can
# fire several project updates in a row, and each one used to spawn its own
# download thread for the very same model.
_PULL_LOCK = threading.Lock()
_PULLS_IN_FLIGHT: set[str] = set()


def _report(message):
    print(f"[Ollama] {message}")


def list_local_model_names():
    """Return the model tags served by the local Ollama, or None if unreachable.

    None means "unknown" (server down, not installed, malformed answer) and is
    deliberately distinct from an empty set ("reachable, nothing installed"),
    so callers can tell a missing model from a missing server.
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

    entries = payload.get("models", []) if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None

    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "") or "").strip()
        if not name:
            continue
        names.add(name)
        # Ollama always reports an explicit tag ("gemma4:latest"), while a model
        # id may carry the implicit one ("gemma4"). Register both spellings so
        # an already-installed model is never re-pulled over a naming detail.
        if name.endswith(":latest"):
            names.add(name.rsplit(":", 1)[0])
    return names


def pull_model_in_background(model_name, report=None):
    """Download an Ollama model in a daemon thread, skipping what is already local.

    Returns True when a download thread was started, False when the request was
    rejected up front (blank name, or a pull of the same tag already running).
    The "already installed" check happens inside the thread, because it costs an
    HTTP round-trip that must not block the caller's request handler.
    """
    name = str(model_name or "").strip()
    if not name:
        return False

    log = report or _report

    with _PULL_LOCK:
        if name in _PULLS_IN_FLIGHT:
            return False
        _PULLS_IN_FLIGHT.add(name)

    def _run():
        try:
            local_models = list_local_model_names()
            if local_models is not None and name in local_models:
                return
            log(f"Downloading model '{name}' in the background...")
            result = subprocess.run(
                ["ollama", "pull", name],
                capture_output=True,
                **utf8_text_kwargs(),
            )
            if result.returncode == 0:
                log(f"Model '{name}' is ready.")
            else:
                detail = (result.stderr or result.stdout or "").strip().splitlines()
                reason = detail[-1] if detail else f"exit code {result.returncode}"
                log(f"Download of model '{name}' failed: {reason}")
        except FileNotFoundError:
            log(f"Cannot download model '{name}': the 'ollama' command was not found.")
        except Exception as e:
            log(f"Download of model '{name}' failed: {type(e).__name__}: {e}")
        finally:
            with _PULL_LOCK:
                _PULLS_IN_FLIGHT.discard(name)

    threading.Thread(target=_run, daemon=True, name=f"ollama-pull-{name}").start()
    return True
