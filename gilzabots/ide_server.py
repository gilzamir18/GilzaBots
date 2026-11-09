import sys
import os
import io
import base64
import time
from gilzabots.i18n import _ as get_translation

# ── Force UTF-8 on all I/O streams (critical for PyInstaller --windowed) ─────
os.environ["PYTHONUTF8"] = "1"

def _force_utf8_stream(stream):
    """Return a UTF-8 stream, or a safe fallback wrapper."""
    if stream is None:
        return stream
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return stream
    except Exception:
        pass
    try:
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            wrapper = io.TextIOWrapper(binary, encoding="utf-8", errors="replace", line_buffering=True)
            wrapper.mode = getattr(stream, "mode", "w")
            return wrapper
    except Exception:
        pass
    class _UnicodeSafeStream:
        encoding = "utf-8"
        def __init__(self, s): self._stream = s
        def write(self, text):
            try: self._stream.write(text)
            except UnicodeEncodeError:
                try: self._stream.write(text.encode("utf-8", "replace").decode("ascii", "replace"))
                except Exception: pass
            except Exception: pass
        def flush(self):
            try: self._stream.flush()
            except Exception: pass
        def __getattr__(self, name): return getattr(self._stream, name)
    return _UnicodeSafeStream(stream)

sys.stdout = _force_utf8_stream(sys.stdout)
sys.stderr = _force_utf8_stream(sys.stderr)

import asyncio
import collections
import os
import json
import urllib.parse
import mimetypes
import subprocess
from email.parser import BytesParser
from email.policy import default as email_default_policy
from gilzabots.subprocess_utils import utf8_text_kwargs


class GitContextError(ValueError):
    pass


def _is_path_within(child: str, parent: str) -> bool:
    child_abs = os.path.normcase(os.path.abspath(child))
    parent_abs = os.path.normcase(os.path.abspath(parent))
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


def _same_project_root(path: str, root: str) -> bool:
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(root))


def _normalize_rel_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _desktop_open_command(target_path: str) -> list[str]:
    """Return the argv that opens ``target_path`` with the desktop's handler."""
    return ["xdg-open", target_path]


# How long to watch a launcher before calling it a success. A desktop launcher
# hands the path over and exits within milliseconds, so this only has to outlast
# process startup.
_DESKTOP_OPEN_FAILURE_WINDOW = 1.5

# How many `stream_chunk` rows a reopened chat rehydrates. Only the per-token
# stream is capped: thoughts, reflections and errors are always returned complete
# (see ProjectStore.list_activity). Generous enough to cover the last answers the
# Stream panel can usefully show, bounded so a months-old chat does not load its
# entire token history to be displayed.
CHAT_HISTORY_STREAM_CHUNK_LIMIT = 5000

# What this launch found out about the launch before it: runs that ended without
# shutting down, and where their crash log is. Filled in by `start_gui_server`
# and served by /api/diagnostics/startup-report. It is kept for the lifetime of
# the process rather than consumed by the first reader, so reloading the window
# does not erase the only notice the user ever gets about a silent crash.
_STARTUP_DIAGNOSTICS: dict = {}


async def _desktop_open_failure(
    proc,
    window: float = _DESKTOP_OPEN_FAILURE_WINDOW,
    accept_codes: tuple[int, ...] = (0,),
) -> str | None:
    """Return a diagnostic if the launcher itself failed, or None if it handed off.

    Watching the launcher for a moment separates "refused" from "handed over"
    without ever waiting on the application it starts: a launcher still running
    at the end of the window has forked something and is reported as a success.
    """
    if proc is None:
        return None
    deadline = time.monotonic() + window
    while True:
        code = proc.poll()
        if code is not None and code in accept_codes:
            return None
        if code is not None:
            return f"The desktop file opener exited with status {code} without opening the path."
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _chat_context_usage(store, project, chat_id: str) -> dict | None:
    """Return the occupancy to report when a chat is (re)opened, best source first.

    1. The in-process measurement, when it belongs to this chat: the freshest
       value, updated during the turn itself.
    2. The provider measurement persisted with the chat's working state, which is
       what survives switching chats and restarting the server.
    3. A count of the restored context itself, for a conversation whose last turn
       predates the persisted measurement. Reading nothing here would send the
       panel back to a character estimate over the visible bubbles — an almost
       empty reading for an almost full window.

    Only the count in (3) is expensive (it tokenizes the whole saved history), so
    it runs off the event loop: this server shares one thread across every
    request, and a long chat's state is megabytes of JSON.
    """
    from gilzabots.token_usage import context_scope_key, get_context_usage

    usage = get_context_usage(
        context_scope_key(getattr(project, "project_path", "") or "", chat_id)
    )
    if usage:
        return usage
    try:
        stored = store.get_chat_context_usage(project.name, chat_id)
    except Exception:
        stored = None
    if stored:
        return stored

    from gilzabots.memgpt_runtime import derive_context_usage_from_state
    try:
        return await asyncio.to_thread(derive_context_usage_from_state, project, store)
    except Exception:
        return None


def _prefetch_local_ollama_model(model_id: str) -> None:
    """Start pulling *model_id* when it is served by the local Ollama.

    Same pre-fetch as a project model change (PROJECT_DESIGN §2.8); callers
    invoke it only when a chat's model actually changes.
    """
    if not model_id or not model_id.startswith("ollama/"):
        return
    from gilzabots.config import is_local_model
    from gilzabots.models_store import get_model

    entry = get_model(model_id) or {}
    if is_local_model(model_id, entry.get("api_base", "")):
        from gilzabots.ollama_manager import pull_model_in_background
        pull_model_in_background(model_id.split("ollama/", 1)[1])


def _ollama_tags_url_for_model_info(model_name: str, api_base: str | None = "") -> str:
    """Return the Ollama /api/tags URL for model validation."""
    from gilzabots.config import (
        OLLAMA_CLOUD_API_BASE,
        is_ollama_cloud_model,
        normalize_ollama_api_base_for_litellm,
    )

    tags_base = normalize_ollama_api_base_for_litellm(model_name, api_base)
    if not tags_base:
        tags_base = OLLAMA_CLOUD_API_BASE if is_ollama_cloud_model(model_name) else "http://127.0.0.1:11434"
    return tags_base.rstrip("/") + "/api/tags"

def _parse_multipart_form(body: bytes, content_type: str) -> tuple[dict, dict]:
    raw_message = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=email_default_policy).parsebytes(raw_message)
    fields = {}
    files = {}

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_param("filename", header="content-disposition")
        if filename is not None:
            files[name] = {"filename": filename, "content": payload}
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")

    return fields, files


_PROMPT_EVOLUTION_INTERNAL_OUTPUT_MARKERS = (
    "refine this user prompt",
    "user prompt to refine",
    "return only a json object",
    "provided response schema",
    "response schema",
    "evolved_prompt",
    "refined version of the user prompt",
)

# A one-shot rewrite does not need the chat's reasoning depth: with every
# catalog model at full effort, glm-5.3 spent 20-40 s and up to 12k characters
# of reasoning to rephrase one paragraph. "low" rather than "none": on Ollama
# `think: false` does not stop the reasoning, it only stops separating it, so it
# leaks undelimited into the answer (see `config.resolve_think_request`).
PROMPT_EVOLUTION_REASONING_EFFORT = "low"

PROMPT_EVOLUTION_SYSTEM_PROMPT = (
    "You rewrite prompts. The user message is a prompt that someone intends to "
    "send to an AI agent working in the user's project (code, documents, data). Rewrite "
    "it into a clearer, more specific and more useful prompt.\n"
    "Do not answer the prompt, carry out its task, or ask questions about it: "
    "your output is the improved prompt itself, which will replace the original "
    "in the chat box.\n"
    "Preserve the user's language, intent and every concrete reference it "
    "contains (file names, identifiers, commands, LaTeX macros, citation keys, labels, equations, "
    "numbers) exactly as written. Do not invent facts, data or requirements "
    "the user did not state.\n"
    "Answer with the rewritten prompt as plain text and nothing else: no "
    "preamble, no title, no commentary, no quotes around it, no JSON, and no "
    "code fence around the whole answer.\n"
    "Do not think out loud in your answer: reasoning that reaches the answer "
    "channel replaces the user's prompt."
)


def _normalize_prompt_evolution_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def clean_evolved_prompt(
    answer: str,
    source_prompt: str | None = None,
) -> str:
    """Return the refined prompt from the model's plain-text answer.

    Reasoning markup is separated and a code fence enclosing the whole answer is
    removed (the same presentation cleanup the snippet translator applies); the
    remaining text is rejected, never rewritten, when it is empty, identical to
    the source, or leaks the task's own instructions.
    """
    from gilzabots.translation import strip_model_reasoning, strip_plain_text_wrapper

    raw = str(answer or "")
    visible = strip_model_reasoning(raw)
    if not visible and raw.strip():
        raise ValueError("Prompt evolution answered with reasoning only and no refined prompt.")
    evolved_prompt = strip_plain_text_wrapper(visible)
    if not evolved_prompt:
        raise ValueError("Prompt evolution returned an empty refined prompt.")

    normalized_output = _normalize_prompt_evolution_text(evolved_prompt)
    normalized_source = _normalize_prompt_evolution_text(source_prompt or "")

    if normalized_source and normalized_output == normalized_source:
        raise ValueError("Prompt evolution returned the original prompt unchanged.")

    wrapped_source = _normalize_prompt_evolution_text(f"Refine this user prompt: {source_prompt or ''}")
    if normalized_source and normalized_output == wrapped_source:
        raise ValueError("Prompt evolution returned the internal task wrapper instead of the refined prompt.")

    for marker in _PROMPT_EVOLUTION_INTERNAL_OUTPUT_MARKERS:
        if marker in normalized_output and marker not in normalized_source:
            raise ValueError("Prompt evolution returned internal instructions instead of the refined prompt.")

    return evolved_prompt


async def _execute_prompt_evolution(
    prompt: str,
    iterations: int = 1,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Refine and evolve a prompt iteratively using LLMAgentBlock.

    The agent answers in **plain text**, like the snippet translator and for the
    same reason: the whole result is one string, so a JSON envelope only adds a
    way to fail. Ollama's cloud endpoint ignores ``format`` (measured: glm-5.3
    fenced or reformatted its answer on every call, and no model returned JSON
    with ``think`` off), so the envelope rested on instruction-following alone.
    When the model missed it, ``LLMAgentBlock`` silently spent a second full
    completion on its schema fallback and the user got "did not return a valid
    structured result" after twice the wait, with the refinement discarded.

    The project's user prompt prefix is deliberately not applied here, for the
    same reason as in ``gilzabots.translation``: this is a transform whose input
    is the user's prompt, not a conversation, and ``prepend_user_prompt`` would
    fuse the prefix into the very text being rewritten. The prefix already
    reaches the chat agent that runs the refined prompt.
    """
    import agenticblocks.blocks.llm.agent as _agent_mod
    from gilzabots.litellm_compat import wrap_agent_litellm_compat
    from gilzabots.config import get_agent_model, get_agent_llm_kwargs

    selected_model = str(model or "").strip()
    if not selected_model:
        selected_model = get_agent_model("orchestrator")
    model_kwargs = get_agent_llm_kwargs(
        "orchestrator",
        model_override=selected_model if model else None,
        reasoning_effort_override=PROMPT_EVOLUTION_REASONING_EFFORT,
    )
    try:
        model_kwargs["max_tokens"] = max(1, min(65536, int(max_tokens)))
    except (TypeError, ValueError):
        model_kwargs["max_tokens"] = 4096

    current = prompt
    iters = max(1, min(10, int(iterations or 1)))

    for _ in range(iters):
        agent = _agent_mod.LLMAgentBlock(
            name="prompt_evolution",
            system_prompt=PROMPT_EVOLUTION_SYSTEM_PROMPT,
            model=selected_model,
            model_kwargs=model_kwargs,
        )
        wrap_agent_litellm_compat(agent)
        res = await agent.run(
            _agent_mod.AgentInput(
                prompt=current
            )
        )
        current = clean_evolved_prompt(res.response, source_prompt=current)

    return current


GILZABOTS_HIDDEN_ARTIFACT_PREFIXES = ("gilzabots_partial_",)
GILZABOTS_GIT_EXCLUDE_PATTERNS = ("gilzabots_partial_*",)
LATEX_COMPILE_DEBUG_VERSION = "2026-07-10.1"


def _is_gilzabots_hidden_artifact(path: str) -> bool:
    return os.path.basename(path).lower().startswith(GILZABOTS_HIDDEN_ARTIFACT_PREFIXES)


def _append_git_exclude_patterns(exclude_path: str) -> None:
    existing = set()
    if os.path.exists(exclude_path):
        with open(exclude_path, "r", encoding="utf-8", errors="ignore") as f:
            existing = {line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")}

    missing = [pattern for pattern in GILZABOTS_GIT_EXCLUDE_PATTERNS if pattern not in existing]
    if not missing:
        return

    os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
    needs_newline = False
    if os.path.exists(exclude_path) and os.path.getsize(exclude_path) > 0:
        with open(exclude_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in {b"\n", b"\r"}

    with open(exclude_path, "a", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        f.write("# GilzaBots generated artifacts\n")
        for pattern in missing:
            f.write(pattern + "\n")


def _safe_relpath(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return path


def _latex_compile_debug_path(project_path: str) -> str:
    if not project_path:
        return ""
    return os.path.join(project_path, ".gilzabots", "latex_compile_debug.json")


def _write_latex_compile_debug(project_path: str, debug_payload: dict) -> str:
    """Persist the last LaTeX compile decision for user-visible diagnostics."""
    path = _latex_compile_debug_path(project_path)
    if not path:
        return ""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(debug_payload, f, indent=2, ensure_ascii=False)
        return path
    except Exception:
        return ""


def _format_latex_compile_debug(debug_payload: dict) -> str:
    """Format the important compile-routing facts for the PDF error panel."""
    lines = ["GilzaBots LaTeX Debug"]
    for key in (
        "debug_version",
        "backend_file",
        "compiler_file",
        "partial",
        "request_file_path",
        "resolved_full_path",
        "project_path",
        "project_name",
        "requested_main_file",
        "store_main_file",
        "selected_main_file",
        "selected_main_exists",
        "selected_main_reason",
        "compile_function",
        "partial_mode",
        "compiler_cwd",
        "compiler_input_tex",
        "compiler_returncode",
        "pdf_path",
        "debug_file",
    ):
        if key in debug_payload:
            lines.append(f"{key}: {debug_payload.get(key)}")
    return "\n".join(lines)


def _ensure_gilzabots_git_excludes(git_ctx: dict) -> None:
    if git_ctx.get("source") == "shadowgit":
        exclude_path = os.path.join(git_ctx["project_path"], ".gilzabots", ".gitignore")
        _append_git_exclude_patterns(exclude_path)
        subprocess.run(
            git_ctx["git_cmd"] + ["config", "core.excludesFile", exclude_path],
            cwd=git_ctx["cwd"],
            capture_output=True,
            **utf8_text_kwargs(),
        )
        return

    git_meta_path = os.path.join(git_ctx["repo_root"], ".git")
    if os.path.isdir(git_meta_path):
        _append_git_exclude_patterns(os.path.join(git_meta_path, "info", "exclude"))


def _resolve_git_context(project_path: str, use_shadow: bool = False, git_root_path: str | None = None) -> dict:
    project_abs = os.path.abspath(project_path)
    if not os.path.isdir(project_abs):
        raise GitContextError("Invalid project path")

    if use_shadow:
        return {
            "project_path": project_abs,
            "repo_root": project_abs,
            "cwd": project_abs,
            "git_cmd": [
                "git",
                f"--git-dir={os.path.join(project_abs, '.gilzabots', '.shadowgit')}",
                f"--work-tree={project_abs}",
            ],
            "repo_prefix": "",
            "source": "shadowgit",
        }

    repo_root = os.path.abspath(git_root_path) if git_root_path else project_abs
    if not os.path.isdir(repo_root):
        raise GitContextError("Invalid Git root path")
    if not _is_path_within(repo_root, project_abs):
        raise GitContextError("Git root must be inside the project path")
    if not os.path.exists(os.path.join(repo_root, ".git")):
        raise GitContextError("Selected Git root does not contain a .git repository")

    repo_prefix = os.path.relpath(repo_root, project_abs)
    if repo_prefix == ".":
        repo_prefix = ""
    return {
        "project_path": project_abs,
        "repo_root": repo_root,
        "cwd": repo_root,
        "git_cmd": ["git"],
        "repo_prefix": _normalize_rel_path(repo_prefix),
        "source": "git",
    }


def _unquote_git_path(path_str: str) -> str:
    if path_str.startswith('"') and path_str.endswith('"'):
        path_str = path_str[1:-1]
        try:
            import codecs
            raw_bytes, _ = codecs.escape_decode(bytes(path_str, "utf-8"))
            return raw_bytes.decode("utf-8")
        except Exception:
            return path_str
    return path_str


def _repo_path_to_project_path(repo_path: str, git_ctx: dict) -> str:
    repo_path = _unquote_git_path(repo_path)
    clean = _normalize_rel_path(repo_path)
    prefix = git_ctx.get("repo_prefix", "")
    return _normalize_rel_path(f"{prefix}/{clean}" if prefix and clean else prefix or clean)


def _project_path_to_repo_path(file_path: str, git_ctx: dict) -> str:
    project_abs = git_ctx["project_path"]
    repo_root = git_ctx["repo_root"]
    if os.path.isabs(file_path):
        file_abs = os.path.abspath(file_path)
        if not _is_path_within(file_abs, project_abs):
            raise GitContextError("File path is outside the project path")
    else:
        file_abs = os.path.abspath(os.path.join(project_abs, file_path))
        if not _is_path_within(file_abs, project_abs):
            raise GitContextError("File path is outside the project path")

    if not _is_path_within(file_abs, repo_root):
        raise GitContextError("File path is outside the selected Git root")
    return _normalize_rel_path(os.path.relpath(file_abs, repo_root))


def _discard_git_path(git_ctx: dict, repo_file_path: str) -> None:
    """Discard tracked/staged changes, or remove an untracked path."""
    git_cmd = git_ctx["git_cmd"]
    ls = subprocess.run(
        git_cmd + ["ls-files", "--error-unmatch", "--", repo_file_path],
        cwd=git_ctx["cwd"],
        capture_output=True,
        **utf8_text_kwargs(),
    )
    if ls.returncode == 0:
        res = subprocess.run(
            git_cmd + ["restore", "--staged", "--worktree", "--", repo_file_path],
            cwd=git_ctx["cwd"],
            capture_output=True,
            **utf8_text_kwargs(),
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr or res.stdout or "Git restore failed")
        return
    # Exit status 1 is the only answer that means "not tracked". Git exits 128
    # when it cannot answer at all (dubious ownership, a corrupt index, a broken
    # repository); taking that for "untracked" deleted a tracked file the user
    # had asked to restore.
    if ls.returncode != 1:
        raise RuntimeError(ls.stderr or ls.stdout or f"git ls-files failed with exit status {ls.returncode}")

    full_path = os.path.abspath(os.path.join(git_ctx["repo_root"], repo_file_path))
    if not _is_path_within(full_path, git_ctx["repo_root"]):
        raise GitContextError("File path is outside the selected Git root")
    if os.path.isdir(full_path):
        import shutil
        shutil.rmtree(full_path)
    elif os.path.exists(full_path):
        os.remove(full_path)


def _is_workspace_hidden_by_extension(path: str, hidden_extensions: list[str] | tuple[str, ...] | None = None) -> bool:
    from gilzabots.config import get_workspace_hidden_file_extensions

    filename = os.path.basename(path).lower()
    extensions = hidden_extensions if hidden_extensions is not None else get_workspace_hidden_file_extensions()
    return any(filename.endswith(ext) for ext in extensions)


def get_file_tree(dir_path, root_path=None, show_hidden_files=False, hidden_extensions=None):
    if root_path is None:
        root_path = dir_path
    # Resolved once per walk instead of once per file: the list comes from
    # config.yaml, loaded once at import and never changed at runtime, so every
    # file is checked against the same list it was checked against before.
    if hidden_extensions is None and not show_hidden_files:
        from gilzabots.config import get_workspace_hidden_file_extensions
        hidden_extensions = get_workspace_hidden_file_extensions()

    files = []
    # scandir, not listdir + os.path.isdir: on Windows the directory listing
    # already carries each entry's type, so the per-entry stat (the expensive
    # part on NTFS, and what antivirus scanners hook) is skipped. Same entries,
    # same order: both enumerate the directory the same way.
    try:
        with os.scandir(dir_path) as it:
            entries = list(it)
    except Exception:
        return []

    for entry in entries:
        item = entry.name
        # Skip heavy/hidden directories
        if item in ['node_modules', '.git', '.venv', '.env', '__pycache__', '.pytest_cache']:
            continue
        if _is_gilzabots_hidden_artifact(item):
            continue

        full_path = entry.path
        rel_path = os.path.relpath(full_path, root_path)

        # Follows symlinks like os.path.isdir did; a broken link is not a
        # directory. os.path.isdir swallowed every OSError, so this does too.
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            files.append({
                "name": item,
                "path": rel_path,
                "isDirectory": True,
                "children": get_file_tree(full_path, root_path, show_hidden_files, hidden_extensions)
            })
        else:
            if not show_hidden_files and _is_workspace_hidden_by_extension(item, hidden_extensions):
                continue
            files.append({
                "name": item,
                "path": rel_path,
                "isDirectory": False
            })
            
    # Sort: directories first, then alphabetical
    files.sort(key=lambda x: (not x["isDirectory"], x["name"].lower()))
    return files

from gilzabots.config import _MODEL_PARAMS_SCHEMA, sanitize_model_params  # noqa: F401

def _qt_invoke(fn):
    """Run fn() on the Qt main thread and return its result.

    QClipboard must be accessed from the main thread only.  The HTTP server
    runs on a background asyncio thread, so we marshal the call via a
    QMetaObject.invokeMethod + threading.Event round-trip.
    """
    import threading
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QObject, pyqtSlot, Qt

        app = QApplication.instance()
        if app is None:
            return None

        result_box = [None]
        done = threading.Event()

        class _Caller(QObject):
            @pyqtSlot()
            def call(self):
                try:
                    result_box[0] = fn()
                except Exception:
                    pass
                finally:
                    done.set()

        caller = _Caller()
        # Move to main thread so invokeMethod queues on the Qt event loop
        caller.moveToThread(app.thread())
        from PyQt6.QtCore import QMetaObject
        QMetaObject.invokeMethod(caller, 'call', Qt.ConnectionType.QueuedConnection)
        done.wait(timeout=3)
        return result_box[0]
    except Exception:
        return None


def _read_clipboard() -> str:
    # Windows has no Qt in the process (the window is WebView2): Win32 directly.
    if sys.platform == "win32":
        from gilzabots import win_clipboard
        try:
            return win_clipboard.read_text()
        except win_clipboard.ClipboardError as e:
            print(f"[GilzaBots] clipboard read failed: {e}")
            return ''

    # 1. In-process Qt (GNOME/Wayland): marshal to main thread so QClipboard is
    #    accessed safely from the asyncio background thread.
    result = _qt_invoke(lambda: __import__('PyQt6.QtWidgets', fromlist=['QApplication'])
                        .QApplication.instance().clipboard().text())
    if result is not None:
        return result

    # 2. Subprocess PyQt6 (Cinnamon/X11): a fresh QApplication in a subprocess
    #    can reach the X11 clipboard without needing the main-thread instance.
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, '-c',
             'from PyQt6.QtWidgets import QApplication; app=QApplication([]);'
             ' print(app.clipboard().text(), end="")'],
            capture_output=True, timeout=3, env=os.environ.copy(), **utf8_text_kwargs(),
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass

    return ''


_CLIPBOARD_IMAGE_SUBPROCESS = (
    "import base64, sys\n"
    "from PyQt6.QtWidgets import QApplication\n"
    "from PyQt6.QtCore import QBuffer, QIODevice\n"
    "app = QApplication([])\n"
    "image = app.clipboard().image()\n"
    "if image.isNull():\n"
    "    sys.exit(1)\n"
    "buffer = QBuffer()\n"
    "buffer.open(QIODevice.OpenModeFlag.WriteOnly)\n"
    "image.save(buffer, 'PNG')\n"
    "sys.stdout.write(base64.b64encode(bytes(buffer.data())).decode())\n"
)


def _clipboard_image_to_png_b64() -> str:
    """Encode the clipboard image of the running QApplication as base64 PNG."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QBuffer, QIODevice

    app = QApplication.instance()
    if app is None:
        return ''
    image = app.clipboard().image()
    if image.isNull():
        return ''
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    return base64.b64encode(bytes(buffer.data())).decode()


def _read_clipboard_image() -> str:
    """Return the clipboard image as base64 PNG, or '' when there is none.

    The embedded QtWebEngine shell does not implement the async Clipboard API,
    so a screenshot copied outside the app can be unreachable from JavaScript
    when the paste event carries no file. This mirrors the two fallbacks of
    `_read_clipboard`: in-process Qt first, then a throwaway QApplication in a
    subprocess for the session types where that fails. Windows has no Qt and
    reads the clipboard through Pillow (`win_clipboard`).
    """
    if sys.platform == "win32":
        from gilzabots import win_clipboard
        return win_clipboard.read_image_png_b64()

    result = _qt_invoke(_clipboard_image_to_png_b64)
    if result:
        return result

    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, '-c', _CLIPBOARD_IMAGE_SUBPROCESS],
            capture_output=True, timeout=5, env=os.environ.copy(), **utf8_text_kwargs(),
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout.strip()
    except Exception:
        pass

    return ''


def _write_clipboard(text: str):
    if sys.platform == "win32":
        from gilzabots import win_clipboard
        try:
            win_clipboard.write_text(text)
            return True, ''
        except win_clipboard.ClipboardError as e:
            return False, str(e)

    # 1. In-process Qt (GNOME/Wayland)
    def _do_write():
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(text)
            return True
        return False

    if _qt_invoke(_do_write):
        return True, ''

    # 2. Subprocess PyQt6 (Cinnamon/X11)
    try:
        import subprocess
        escaped = text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        r = subprocess.run(
            [sys.executable, '-c',
             f'from PyQt6.QtWidgets import QApplication; app=QApplication([]);'
             f' app.clipboard().setText("{escaped}"); app.processEvents()'],
            capture_output=True, timeout=3, env=os.environ.copy(), **utf8_text_kwargs(),
        )
        if r.returncode == 0:
            return True, ''
        return False, r.stderr or 'subprocess write failed'
    except Exception as e:
        return False, str(e)


class AsyncHTTPServer:
    def __init__(self, host="127.0.0.1", port=3000, static_dir=None):
        self.host = host
        self.port = port
        self.static_dir = static_dir
        from .local_auth import LocalSession
        self.local_session = LocalSession(host, port)
        self.active_queues = []
        # The loop that serves requests and owns `active_queues`; set by start().
        self.loop = None
        # Events in publication order, from any thread, until the loop hands
        # them to `active_queues` (see publish_event).
        self._pending_events = collections.deque()
        self.active_terminal = None
        self.temp_terminals = {}
        self.active_agent_task = None
        self.active_agent_event_queue = None
        self.active_prompt_evolution_task = None
        self._exclusive_work = None

    # Requests whose slow, blocking part (a Tectonic run, `git status`, a walk of
    # the project tree) runs in a worker thread instead of on the event loop.
    # While one runs it holds the exclusive-work lock, and every other request
    # waits on that lock before it is dispatched. Requests are therefore handled
    # in the same order, never interleaved with these operations, exactly as when
    # the operation blocked the loop. What changes is only that work already in
    # progress (a streaming agent turn, terminal output) keeps flowing meanwhile
    # instead of freezing for the length of a compile.
    EXCLUSIVE_WORK_PATHS = frozenset({
        '/api/latex/compile',
        '/api/latex/compile-pdf-source',
        '/api/git/status',
        '/api/files',
        # Every other Git endpoint runs git as well: a diff makes up to a dozen
        # calls, a commit runs the repository's hooks, an archive zips a whole
        # checkpoint. See `_respond_from_thread`.
        '/api/git/file-at-head',
        '/api/git/commit',
        '/api/git/diff',
        '/api/git/archive',
        '/api/git/restore',
        '/api/git/log',
        '/api/git/stage',
        '/api/git/discard',
    })

    def _exclusive_work_lock(self) -> asyncio.Lock:
        # Created on first use so it belongs to the loop that serves requests.
        if self._exclusive_work is None:
            self._exclusive_work = asyncio.Lock()
        return self._exclusive_work

    async def _dispatch(self, path, handler):
        """Run ``handler`` in arrival order relative to exclusive work.

        ``asyncio.Lock`` wakes waiters first-in first-out and its uncontended
        acquire does not yield, so a request that arrives while nothing is in
        flight is dispatched immediately, and one that arrives during a compile
        runs after it, just as it did when the compile held the loop.
        """
        lock = self._exclusive_work_lock()
        if path in self.EXCLUSIVE_WORK_PATHS:
            async with lock:
                await handler()
            return
        async with lock:
            pass
        await handler()

    async def _respond_from_thread(self, writer, work):
        """Run ``work(respond)`` in a worker thread and send what it decided.

        ``work`` is a request handler's blocking part. It calls ``respond`` with
        the arguments of ``send_response`` (plus, optionally, the extra headers
        of ``send_response_with_headers``) instead of writing to the stream,
        which belongs to the event loop and must not be touched from a thread.
        The first response wins, as the first write did before.
        """
        decided = []

        def respond(status_code, body, content_type="text/plain", extra_headers=None):
            if not decided:
                decided.append((status_code, body, content_type, extra_headers))

        await asyncio.to_thread(work, respond)
        if not decided:
            return
        status_code, body, content_type, extra_headers = decided[0]
        if extra_headers is None:
            self.send_response(writer, status_code, body, content_type)
        else:
            self.send_response_with_headers(writer, status_code, body, content_type, extra_headers)

    @staticmethod
    def _is_project_terminal(term_id) -> bool:
        """Terminal ids owned by the bottom panel: a shell in the open project."""
        return term_id == 'main' or term_id.startswith('main-')

    @staticmethod
    def _same_path(a, b) -> bool:
        # normcase: Windows paths may differ only in case or separator.
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _project_terminal(self, term_id, project_path):
        """Return the shell session for a bottom-panel tab, in ``project_path``.

        A tab id (``main``, ``main-1``, ...) survives a project switch, so a
        session found under it may belong to the project that was open before.
        That session is closed and a fresh shell is started in the requested
        project; reusing it left the terminal in the previous project's root.
        A request without a project path keeps whatever session is running.
        Returns None when no session is running and none can be started.
        """
        current = self.active_terminal if term_id == 'main' else self.temp_terminals.get(term_id)
        if current is not None and current.is_running and (
            not project_path or self._same_path(current.project_path, project_path)
        ):
            return current
        if not project_path or not os.path.isdir(project_path):
            return None
        if current is not None:
            try:
                current.close()
            except Exception as e:
                print(f"[Terminal] failed to close session for {current.project_path}: {e}")
        from gilzabots.terminal_manager import TerminalSession
        session = TerminalSession(project_path)
        session.start_reading(asyncio.get_running_loop())
        if term_id == 'main':
            self.active_terminal = session
        else:
            self.temp_terminals[term_id] = session
        return session

    def _agent_turn_active(self) -> bool:
        """True while an agent turn's task is alive, including its cleanup."""
        task = self.active_agent_task
        return task is not None and not task.done()

    def _refresh_live_orchestrator(self) -> None:
        """Rebuild the live chat orchestrator so it holds the current tool set.

        Used after a change to the MCP connectors (PROJECT_DESIGN 2.25). A running
        turn keeps the orchestrator it was built with; the next one is built from
        the store anyway, so only an idle orchestrator is rebuilt here.
        """
        if self._agent_turn_active():
            return
        import gilzabots.agent_stdin as agent_stdin
        live = agent_stdin.current_project
        if live is None or agent_stdin.current_memgpt is None:
            return
        from .memgpt_runtime import build_chat_orchestrator
        from .tools import set_project_context
        live_store = agent_stdin.current_store
        set_project_context(live, live_store)
        agent_stdin.current_memgpt = build_chat_orchestrator(live, live_store)

    def _refuse_during_agent_turn(self, writer) -> bool:
        """Answer 409 and return True when an agent turn owns the project state.

        A turn keeps its project, store and orchestrator in module globals
        (PROJECT_DESIGN 2.6). A request that reloads them while it runs rebinds
        the running turn to whatever chat that request named.
        """
        if not self._agent_turn_active():
            return False
        stopping = getattr(self.active_agent_task, "_gilzabots_cancel_requested", False)
        self.send_response(
            writer,
            409,
            json.dumps({
                "error": (
                    "The previous agent turn is still stopping."
                    if stopping
                    else "Another agent turn is still running."
                ),
                "reason": "turn_stopping" if stopping else "turn_active",
            }).encode("utf-8"),
            "application/json",
        )
        return True

    def _emit_agent_cancelled_once(self, agent_task, event_queue):
        """Notify the stream immediately, without waiting for task cleanup."""
        if getattr(agent_task, "_gilzabots_cancel_event_emitted", False):
            return
        agent_task._gilzabots_cancel_event_emitted = True
        event_queue.put_nowait({"event": "cancelled", "message": "Agent execution was interrupted."})

    @staticmethod
    def _request_agent_cancel_once(agent_task):
        """Request work cancellation once while protected cleanup is allowed to finish."""
        if getattr(agent_task, "_gilzabots_cancel_requested", False):
            return False
        agent_task._gilzabots_cancel_requested = True
        agent_task.cancel()
        return True

    def publish_event(self, payload):
        """Hand an agent event to every open run stream, from any thread.

        Synchronous tools run in worker threads (`FunctionBlock` ->
        `asyncio.to_thread`) and some of them publish events, such as
        `latex_compiled`. `asyncio.Queue` is not thread-safe: a `put_nowait`
        from a worker thread does not wake the loop, so the event could sit in
        the queue until something else did. Off the loop's thread the delivery
        is scheduled on the loop instead.

        The event joins `_pending_events` at once, in either case, and the loop
        delivers that deque in order. Scheduling the put itself was not enough:
        the loop can resume the coroutine awaiting the tool before it runs a
        callback the tool's thread scheduled earlier (measured on Python 3.14,
        about 1 in 1,500), which let the `tool_result` that follows overtake the
        tool's own event.
        """
        self._pending_events.append(payload)
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if not on_loop:
                loop.call_soon_threadsafe(self._deliver_pending_events)
                return
        self._deliver_pending_events()

    def _deliver_pending_events(self):
        while True:
            try:
                payload = self._pending_events.popleft()
            except IndexError:
                return
            for q in self.active_queues:
                q.put_nowait(payload)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(self.handle_request, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        self.local_session.port = self.port
        from .local_auth import register_local_api
        register_local_api(self.host, self.port, self.local_session)
        print(f"[IDE Backend] Python Async server running on http://{self.host}:{self.port}")

    def stop(self):
        from .local_auth import unregister_local_api
        unregister_local_api(self.local_session)
        # Fecha o terminal principal
        if self.active_terminal:
            try:
                self.active_terminal.close()
            except:
                pass
        # Fecha todos os terminais temporários
        for term_id, term in list(self.temp_terminals.items()):
            try:
                term.close()
            except:
                pass
        self.temp_terminals.clear()

    async def handle_request(self, reader, writer):
        import os, sys, subprocess, platform, socket
        try:
            # Disable Nagle's algorithm: the chunked streaming responses below
            # (agent run, terminal, install progress) write many small chunks in
            # quick succession, and Nagle coalescing them delayed delivery until
            # enough data queued up or an ACK round-trip completed, producing the
            # "one word, pause, then a burst" stutter instead of a smooth stream.
            sock = writer.get_extra_info('socket')
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass

            # Read request line
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
                
            parts = request_line.decode('utf-8').strip().split()
            if len(parts) < 3:
                writer.close()
                return
                
            method, raw_path, _ = parts
            parsed_url = urllib.parse.urlparse(raw_path)
            path = parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)
            
            # Read headers
            headers = {}
            while True:
                line = await reader.readline()
                line = line.decode('utf-8').strip()
                if not line:
                    break
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.strip().lower()] = v.strip()
                
            if not self.local_session.trusted_request(headers):
                self.send_response(writer, 403, b'{"error":"Untrusted request origin or host."}', "application/json")
                return

            # Bootstrap is a same-origin POST with a custom header. A foreign
            # page cannot issue it without a preflight, which is refused below.
            if path == '/api/session' and method == 'POST':
                if headers.get('x-gilzabots-bootstrap') != '1':
                    self.send_response(writer, 403, b'{"error":"Invalid session bootstrap."}', "application/json")
                    return
                self.send_response_with_headers(writer, 200, b'{"success":true}', "application/json", {
                    "Set-Cookie": self.local_session.cookie(), "Cache-Control": "no-store",
                })
                return

            if path.startswith('/api/') and not self.local_session.authenticated(headers):
                self.send_response(writer, 401, b'{"error":"Local session required. Reload the application."}', "application/json")
                return
            if path == '/api/session' and method == 'GET':
                self.send_response(writer, 200, b'{"success":true}', "application/json")
                return

            # Read body if Content-Length exists
            body = b""
            if 'content-length' in headers:
                content_length = int(headers['content-length'])
                body = await reader.readexactly(content_length)
                
            # Handle OPTIONS (CORS)
            if method == 'OPTIONS':
                self.send_cors(writer)
                return
                
            # Route API paths
            if path.startswith('/api/'):
                await self._dispatch(path, lambda: self.route_api(method, path, query, headers, body, writer))
            else:
                # Serve static files
                await self._dispatch(path, lambda: self.serve_static(path, writer))
                
        except Exception as e:
            print(f"Error handling request: {e}")
            try:
                writer.close()
            except:
                pass

    def _notify_cloud_change(self, project_path):
        """Tell the cloud sync manager that this project's files changed.

        The backend has no filesystem watcher, so every endpoint that writes
        into a project announces it here. The call is deliberately cheap and
        never raises: it only sets an event, and a project without cloud sync
        enabled is not even registered. Mirroring must never be able to break an
        editor save.
        """
        if not project_path:
            return
        try:
            from gilzabots.cloud.service import MANAGER
            MANAGER.notify_local_change(project_path)
        except Exception:
            pass

    def send_response(self, writer, status_code, body, content_type="text/plain"):
        status_msg = {
            200: "OK", 206: "Partial Content", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 416: "Range Not Satisfiable",
            500: "Internal Server Error", 503: "Service Unavailable",
        }.get(status_code, "Error")
        if (content_type.startswith("text/") or 
            content_type in ("application/javascript", "application/json", "image/svg+xml")):
            if "charset=" not in content_type:
                content_type += "; charset=utf-8"
        headers = (
            f"HTTP/1.1 {status_code} {status_msg}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\n"
            f"X-Frame-Options: SAMEORIGIN\r\n"
            f"Connection: close\r\n\r\n"
        )
        writer.write(headers.encode('utf-8'))
        writer.write(body)
        writer.close()

    def send_response_with_headers(self, writer, status_code, body, content_type="text/plain", extra_headers=None):
        status_msg = {
            200: "OK", 206: "Partial Content", 400: "Bad Request",
            401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 416: "Range Not Satisfiable",
            500: "Internal Server Error", 503: "Service Unavailable",
        }.get(status_code, "Error")
        if (content_type.startswith("text/") or
            content_type in ("application/javascript", "application/json", "image/svg+xml")):
            if "charset=" not in content_type:
                content_type += "; charset=utf-8"
        headers = [
            f"HTTP/1.1 {status_code} {status_msg}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
        ]
        for name, value in (extra_headers or {}).items():
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                continue
            headers.append(f"{name}: {value}")
        headers.append("Connection: close")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode('utf-8'))
        writer.write(body)
        writer.close()

    def send_cors(self, writer):
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(headers.encode('utf-8'))
        writer.close()

    async def serve_static(self, path, writer):
        if not self.static_dir:
            self.send_response(writer, 404, b"Not Found")
            return
            
        # Clean path and prevent directory traversal
        rel_path = path.lstrip('/')
        if not rel_path or rel_path == '':
            rel_path = 'index.html'
            
        full_path = os.path.abspath(os.path.join(self.static_dir, rel_path))
        if not _is_path_within(full_path, self.static_dir):
            self.send_response(writer, 403, b"Forbidden")
            return
            
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            # For SPA router fallback to index.html
            full_path = os.path.join(self.static_dir, 'index.html')
            
        try:
            with open(full_path, 'rb') as f:
                content = f.read()
            mime_type, _ = mimetypes.guess_type(full_path)
            mime_type = mime_type or 'application/octet-stream'
            if (mime_type.startswith("text/") or 
                mime_type in ("application/javascript", "application/json", "image/svg+xml")):
                if "charset=" not in mime_type:
                    mime_type += "; charset=utf-8"
            
            headers = f"HTTP/1.1 200 OK\r\nContent-Type: {mime_type}\r\nContent-Length: {len(content)}\r\nX-Frame-Options: SAMEORIGIN\r\nContent-Security-Policy: frame-ancestors 'self'\r\nConnection: close\r\n\r\n"
            writer.write(headers.encode('utf-8'))
            writer.write(content)
            await writer.drain()
            writer.close()
        except Exception as e:
            self.send_response(writer, 500, f"Error: {e}".encode('utf-8'))

    async def route_api(self, method, path, query, headers, body, writer):
        # Parse JSON body if present
        data = {}
        if body:
            try:
                data = json.loads(body.decode('utf-8'))
            except:
                pass

        # 0. Clean LaTeX artifacts
        if path == '/api/latex/clean' and method == 'POST':
            project_path = data.get('projectPath', '')
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath is required"}', "application/json")
                return

            try:
                from gilzabots.latex_compiler import clean_latex_artifacts
                result = clean_latex_artifacts(project_path)
                if result.get("success"):
                    self.last_pdf_bytes = None
                    self.last_pdf_path = ""
                status = 200 if result.get("success") else 500
                self.send_response(writer, status, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({
                    "success": False,
                    "removed": [],
                    "errors": [str(e)],
                }).encode('utf-8'), "application/json")
            return

        # 0. Compile LaTeX
        if path == '/api/latex/compile':
            import gilzabots.latex_compiler as latex_compiler
            from gilzabots.latex_compiler import compile_latex, compile_latex_partial
            request_started = time.perf_counter()
            content = data.get('content', '')
            file_path = data.get('filePath', '')
            project_path = data.get('projectPath', '')
            project_name = str(data.get('projectName', '') or '').strip()
            requested_main_file = str(data.get('mainFile', '') or '').strip()
            partial = bool(data.get('partial', False))
            draft = bool(data.get('draft', False))
            from gilzabots.ui_settings import load_ui_settings
            ui_cfg = load_ui_settings()
            draft_synctex_enabled = bool(ui_cfg.get("draft_synctex_enabled", False))
            synctex_enabled = (not draft) or draft_synctex_enabled
            compile_debug = {
                "debug_version": LATEX_COMPILE_DEBUG_VERSION,
                "backend_file": __file__,
                "compiler_file": getattr(latex_compiler, "__file__", ""),
                "partial": partial,
                "draft": draft,
                "draft_synctex_enabled": draft_synctex_enabled,
                "synctex_enabled": synctex_enabled,
                "request_file_path": file_path,
                "project_path": project_path,
                "project_name": project_name,
                "requested_main_file": requested_main_file,
                "content_is_independent": latex_compiler.is_independent(content),
            }
            
            if not content:
                self.send_response(writer, 400, b'{"error":"content is required"}', "application/json")
                return
            
            full_path = ""
            if file_path:
                if os.path.isabs(file_path):
                    full_path = os.path.abspath(file_path)
                elif project_path:
                    full_path = os.path.abspath(os.path.join(project_path, file_path))
                    
            if not project_path and full_path:
                project_path = os.path.dirname(full_path)
                compile_debug["project_path_inferred_from_file"] = project_path
            compile_debug["project_path"] = project_path
            compile_debug["resolved_full_path"] = full_path
            compile_debug["resolved_full_exists"] = bool(full_path and os.path.exists(full_path))
                
            main_file = ""
            store_main_file = ""
            selected_main_reason = "unresolved"
            if project_path:
                if requested_main_file:
                    candidate_main = os.path.abspath(os.path.join(project_path, requested_main_file))
                    project_root = os.path.abspath(project_path)
                    try:
                        inside_project = os.path.commonpath([candidate_main, project_root]) == project_root
                    except ValueError:
                        inside_project = False
                    if inside_project and os.path.isfile(candidate_main):
                        main_file = os.path.relpath(candidate_main, project_root)
                        selected_main_reason = "request.mainFile"
                    compile_debug["requested_main_candidate"] = candidate_main
                    compile_debug["requested_main_exists"] = bool(os.path.isfile(candidate_main))

                from gilzabots.project import ProjectStore
                from gilzabots.config import DEFAULT_DB_PATH
                if not main_file:
                    store = ProjectStore(db_path=DEFAULT_DB_PATH)
                    # Prefer the stable internal project name. Cloud-backed and
                    # mapped paths may have more than one textual representation.
                    for p in store.list_projects():
                        same_name = bool(project_name and str(p.get("name", "")).casefold() == project_name.casefold())
                        same_path = bool(
                            p.get("project_path")
                            and os.path.normcase(os.path.abspath(os.path.expanduser(p["project_path"])))
                            == os.path.normcase(os.path.abspath(os.path.expanduser(project_path)))
                        )
                        if same_name or same_path:
                            store_main_file = p.get("main_file", "")
                            main_file = store_main_file
                            selected_main_reason = "project_store.name" if same_name else "project_store.path"
                            break
                
                from gilzabots.latex_compiler import determine_main_file_for_compilation
                preselected_main_file = main_file
                main_file = determine_main_file_for_compilation(full_path, content, project_path, main_file)
                if main_file != preselected_main_file:
                    selected_main_reason = "determine_main_file_for_compilation"
                compile_debug["store_main_file"] = store_main_file
                compile_debug["preselected_main_file"] = preselected_main_file
                compile_debug["selected_main_file"] = main_file
                compile_debug["selected_main_reason"] = selected_main_reason
                selected_main_abs = os.path.abspath(os.path.join(project_path, main_file)) if main_file else ""
                compile_debug["selected_main_abs"] = selected_main_abs
                compile_debug["selected_main_exists"] = bool(selected_main_abs and os.path.isfile(selected_main_abs))
                compile_debug["selected_main_rel"] = _safe_relpath(selected_main_abs, project_path) if selected_main_abs else ""
            
            # run compilation
            def _run_compilation():
                if partial:
                    compile_debug["compile_function"] = "compile_latex_partial"
                    result = compile_latex_partial(content, full_path, main_file, project_path, include_pdf_base64=False, draft=draft, synctex=synctex_enabled)
                    compile_debug["partial_result_success"] = result.get("success")
                    compile_debug["partial_mode"] = result.get("partial_mode", "")
                    if not result.get("success"):
                        partial_log = result.get("log") or "Partial compilation failed."
                        compile_debug["partial_fallback_reason"] = partial_log[:2000]
                        compile_debug["compile_function"] = "compile_latex_partial_then_compile_latex"
                        fallback = compile_latex(content, full_path, main_file, project_path, include_pdf_base64=False, draft=draft, synctex=synctex_enabled)
                        fallback["partial_fallback_reason"] = partial_log
                        if not fallback.get("success"):
                            fallback["log"] = (
                                f"Partial compilation failed:\n{partial_log}\n\n"
                                f"Full compilation fallback also failed:\n{fallback.get('log') or ''}"
                            )
                        result = fallback
                else:
                    compile_debug["compile_function"] = "compile_latex"
                    result = compile_latex(content, full_path, main_file, project_path, include_pdf_base64=False, draft=draft, synctex=synctex_enabled)
                return result

            # Tectonic runs for seconds (minutes on a first compile that fetches
            # its bundle). In a worker thread it no longer stalls the loop; the
            # exclusive-work lock held by `_dispatch` keeps every other request
            # waiting for it, as before (see EXCLUSIVE_WORK_PATHS).
            result = await asyncio.to_thread(_run_compilation)

            result["compiled_main_file"] = main_file
            compile_debug["result_success"] = result.get("success")
            compile_debug["result_returned_pdf_path"] = result.get("pdf_path", "")
            compile_debug["partial_mode"] = result.get("partial_mode", compile_debug.get("partial_mode", ""))
            compile_debug["pdf_path"] = result.get("pdf_path", "")
            compile_debug["synctex_path"] = result.get("synctex_path", "")
            compile_debug["compiler_debug"] = result.get("compiler_debug", {})
            compiler_debug = compile_debug["compiler_debug"]
            compile_debug["compiler_cwd"] = compiler_debug.get("cwd", "")
            compile_debug["compiler_input_tex"] = compiler_debug.get("input_tex", "")
            compile_debug["compiler_returncode"] = compiler_debug.get("returncode", "")
            compile_debug["timing"] = result.get("timing") or {}
            debug_file = _write_latex_compile_debug(project_path, compile_debug)
            if debug_file:
                compile_debug["debug_file"] = debug_file
                _write_latex_compile_debug(project_path, compile_debug)
            result["compile_debug"] = compile_debug
            if not result.get("success"):
                result["log"] = (
                    f"{_format_latex_compile_debug(compile_debug)}\n\n"
                    f"Compilation target: {main_file or '(unresolved)'}\n\n"
                    f"{result.get('log') or ''}"
                )
            
            if result.get("success") and result.get("pdf_path"):
                self.last_pdf_path = result["pdf_path"]
                self.last_synctex_path = result.get("synctex_path", "")
                self.last_pdf_bytes = None
                result["pdf_url"] = f"/api/latex/pdf?ts={int(time.time() * 1000)}"
            else:
                self.last_pdf_bytes = None
                self.last_pdf_path = ""
                self.last_synctex_path = ""
            timing = result.get("timing") or {}
            timing["request_seconds"] = round(time.perf_counter() - request_started, 3)
            result["timing"] = timing
                
            self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            return
        # 0.b The .tex that a standalone PDF is compiled from, if the project has
        #     one. The PDF viewer asks so it offers a recompile button only for
        #     PDFs that can actually be rebuilt.
        if path == '/api/latex/pdf-source' and method == 'GET':
            from gilzabots.latex_compiler import find_pdf_source_tex
            project_path = query.get('projectPath', [''])[0]
            pdf_path = query.get('pdfPath', [''])[0]
            if not project_path or not pdf_path:
                self.send_response(writer, 400, b'{"error":"projectPath and pdfPath are required"}', "application/json")
                return
            tex_file = find_pdf_source_tex(project_path, pdf_path)
            self.send_response(writer, 200, json.dumps({"texFile": tex_file}).encode('utf-8'), "application/json")
            return

        # 0.c Recompile a standalone PDF from its source .tex, read from disk, so
        #     the PDF can be rebuilt without opening the .tex in the editor. The
        #     output overwrites the viewed PDF (Tectonic writes name.pdf next to
        #     name.tex).
        if path == '/api/latex/compile-pdf-source' and method == 'POST':
            from gilzabots.latex_compiler import compile_latex, find_pdf_source_tex
            request_started = time.perf_counter()
            project_path = str(data.get('projectPath', '') or '').strip()
            pdf_path = str(data.get('pdfPath', '') or '').strip()
            draft = bool(data.get('draft', False))
            from gilzabots.ui_settings import load_ui_settings
            ui_cfg = load_ui_settings()
            draft_synctex_enabled = bool(ui_cfg.get("draft_synctex_enabled", False))
            synctex_enabled = (not draft) or draft_synctex_enabled

            if not project_path or not os.path.isdir(project_path) or not pdf_path:
                self.send_response(writer, 400, json.dumps({
                    "success": False,
                    "log": "compile-pdf-source requires a valid projectPath and pdfPath.",
                }).encode('utf-8'), "application/json")
                return

            tex_file = find_pdf_source_tex(project_path, pdf_path)
            if not tex_file:
                self.send_response(writer, 200, json.dumps({
                    "success": False,
                    "log": f"No root LaTeX document compiles to {pdf_path}: expected a .tex file with the same name in the same folder.",
                    "compiled_main_file": "",
                }).encode('utf-8'), "application/json")
                return

            abs_tex = os.path.abspath(os.path.join(project_path, tex_file))
            try:
                with open(abs_tex, "r", encoding="utf-8", errors="ignore", newline="") as f:
                    content = f.read()
            except OSError as e:
                self.send_response(writer, 200, json.dumps({
                    "success": False,
                    "log": f"Could not read {tex_file}: {e}",
                    "compiled_main_file": tex_file,
                }).encode('utf-8'), "application/json")
                return

            def _run_source_compilation():
                return compile_latex(content, abs_tex, tex_file, project_path,
                                     include_pdf_base64=False, draft=draft,
                                     synctex=synctex_enabled)

            # Same exclusive-work handling as /api/latex/compile (see
            # EXCLUSIVE_WORK_PATHS).
            result = await asyncio.to_thread(_run_source_compilation)
            result["compiled_main_file"] = tex_file
            if not result.get("success"):
                # The .tex is not open in the editor, so "Fix with AI" gets the
                # compiled source from here (the editor sends its buffer instead).
                result["source_content"] = content

            if result.get("success") and result.get("pdf_path"):
                self.last_pdf_path = result["pdf_path"]
                self.last_synctex_path = result.get("synctex_path", "")
                self.last_pdf_bytes = None
            timing = result.get("timing") or {}
            timing["request_seconds"] = round(time.perf_counter() - request_started, 3)
            result["timing"] = timing

            self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            return

        # 0.0.c Render a single includegraphics file (PDF/PNG/JPG) to inline
        #      PNG/SVG so the Rich Text editor can show a preview of figures
        #      that simply embed a raster/vector image. PDF inputs are rasterized
        #      to a single-page PNG via PyMuPDF; PNG/JPG/GIF are passed through
        #      (or also rasterized if a page number is requested).
        if path == '/api/latex/render-include' and method == 'GET':
            project_path = query.get('projectPath', [''])[0]
            file_path = query.get('filePath', [''])[0]
            source_tex = query.get('sourceTex', [''])[0]
            # When set, "auto" picks a sensible default per MIME type: PDF -> PNG
            # of page 1, PNG/JPG/GIF -> the file as-is.
            output = (query.get('output', ['auto'])[0] or 'auto').lower()

            if not project_path or not file_path:
                self.send_response(writer, 400, json.dumps({
                    "success": False, "error": "projectPath and filePath are required"
                }).encode('utf-8'), "application/json")
                return

            try:
                from gilzabots.latex_compiler import render_include_to_png
                result = render_include_to_png(
                    project_path=project_path,
                    file_path=file_path,
                    source_tex=source_tex or "",
                    output=output,
                )
                if not result.get("success"):
                    self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
                    return
                # base64 is imported at the top of the module — no need to
                # re-import it here (and re-importing inside a try/except was
                # the source of "cannot access local variable 'base64'" when
                # the failure path short-circuited before the import ran).
                payload = json.dumps({
                    "success": True,
                    "mime": result["mime"],
                    "data_base64": base64.b64encode(result["data"]).decode("utf-8"),
                }).encode("utf-8")
                self.send_response(writer, 200, payload, "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({
                    "success": False, "error": f"Render include failed: {e}"
                }).encode('utf-8'), "application/json")
            return

        # 0.0.b Render a single LaTeX graphic (tikzpicture / pgfplots) to SVG
        #      Used by the Rich Text editor and the LaTeX live preview to show
        #      inline graphical previews without compiling the whole document.
        if path == '/api/latex/render-graphic' and method == 'POST':
            graphic = data.get('graphic', '')
            project_path = data.get('projectPath', '')
            preamble = data.get('preamble', '')
            cache_key = data.get('cacheKey', '')
            source_tex = data.get('sourceTex', '')
            # Engine hint ("tikz", "picture", "chemfig", "pstricks", "forest").
            # The default ("tikz") keeps the original behaviour.
            graphic_engine = data.get('graphicEngine', '')

            if not graphic:
                self.send_response(writer, 400, json.dumps({
                    "success": False, "svg": "", "log": "graphic source is required"
                }).encode('utf-8'), "application/json")
                return

            try:
                from gilzabots.latex_compiler import render_graphic_to_svg
                result = render_graphic_to_svg(
                    graphic_source=graphic,
                    project_path=project_path or "",
                    preamble=preamble or "",
                    cache_key=cache_key or "",
                    graphic_engine=graphic_engine or "",
                    source_tex=source_tex or "",
                )
                self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({
                    "success": False, "svg": "", "log": f"Render failed: {e}"
                }).encode('utf-8'), "application/json")
            return

        # 0.0 Check Tectonic
        elif path == '/api/latex/check-tectonic' and method == 'GET':
            from gilzabots.latex_compiler import get_tectonic_path
            tectonic = get_tectonic_path()
            found = tectonic is not None
            self.send_response(writer, 200, json.dumps({"found": found}).encode('utf-8'), "application/json")
            return

        elif path == '/api/latex/check-pandoc' and method == 'GET':
            from gilzabots.document_exporter import get_pandoc_path
            pandoc = get_pandoc_path()
            found = pandoc is not None
            self.send_response(writer, 200, json.dumps({"found": found}).encode('utf-8'), "application/json")
            return

        elif path == '/api/latex/export-docx' and method == 'POST':
            project_path = data.get('projectPath', '')
            file_path = data.get('filePath', '')
            content = data.get('content', None)
            output_path = data.get('outputPath', '')

            if not project_path or not file_path:
                self.send_response(writer, 400, json.dumps({
                    "success": False,
                    "error": "projectPath and filePath are required",
                }).encode('utf-8'), "application/json")
                return

            try:
                if content is not None:
                    full_path = os.path.abspath(file_path if os.path.isabs(file_path) else os.path.join(project_path, file_path))
                    project_abs = os.path.abspath(os.path.expanduser(project_path))
                    if not _is_path_within(full_path, project_abs):
                        raise ValueError("filePath must stay inside the project directory")
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, "w", encoding="utf-8", newline="") as f:
                        f.write(content)

                from gilzabots.document_exporter import export_tex_to_docx
                result = export_tex_to_docx(project_path, file_path, output_path)
                self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({
                    "success": False,
                    "output_path": "",
                    "log": f"DOCX export failed: {e}",
                    "error": str(e),
                }).encode('utf-8'), "application/json")
            return

        # 0.1 Serve Latest PDF
        if path == '/api/latex/pdf':
            if hasattr(self, 'last_pdf_path') and self.last_pdf_path and os.path.exists(self.last_pdf_path):
                try:
                    with open(self.last_pdf_path, "rb") as pdf_file:
                        self.send_response(writer, 200, pdf_file.read(), "application/pdf")
                except Exception as e:
                    self.send_response(writer, 500, f"Error reading PDF: {e}".encode("utf-8"), "text/plain")
            elif hasattr(self, 'last_pdf_bytes') and self.last_pdf_bytes:
                self.send_response(writer, 200, self.last_pdf_bytes, "application/pdf")
            else:
                self.send_response(writer, 404, b'No PDF generated yet.', "text/plain")
            return
            
        # 0.2 Check for existing PDF
        elif path == '/api/latex/check-pdf' and method == 'GET':
            file_path = query.get('filePath', [''])[0]
            project_path = query.get('projectPath', [''])[0]
            if file_path and os.path.isabs(file_path) and not project_path:
                project_path = os.path.dirname(file_path)
            if not file_path or not project_path:
                self.send_response(writer, 400, b'{"error":"filePath and projectPath are required"}', "application/json")
                return
            
            try:
                # Find main_file
                main_file = ""
                from gilzabots.project import ProjectStore
                from gilzabots.config import DEFAULT_DB_PATH
                store = ProjectStore(db_path=DEFAULT_DB_PATH)
                for p in store.list_projects():
                    if p.get("project_path") and os.path.normcase(os.path.abspath(os.path.expanduser(p["project_path"]))) == os.path.normcase(os.path.abspath(os.path.expanduser(project_path))):
                        main_file = p.get("main_file", "")
                        break

                full_path = os.path.abspath(os.path.join(project_path, file_path))
                file_content = ""
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                        file_content = f.read()
                except Exception:
                    pass

                from gilzabots.latex_compiler import determine_main_file_for_compilation
                main_file = determine_main_file_for_compilation(full_path, file_content, project_path, main_file)
                
                if main_file:
                    full_path = os.path.abspath(os.path.join(project_path, main_file))
                else:
                    full_path = os.path.abspath(os.path.join(project_path, file_path))
                    
                target_pdf = os.path.splitext(full_path)[0] + ".pdf"
                if os.path.exists(target_pdf):
                    self.last_pdf_path = target_pdf
                    self.last_synctex_path = os.path.splitext(full_path)[0] + ".synctex.gz"
                    self.last_pdf_bytes = None
                    self.send_response(writer, 200, json.dumps({
                        "found": True,
                        "pdf_url": f"/api/latex/pdf?ts={int(time.time() * 1000)}"
                    }).encode('utf-8'), "application/json")
                else:
                    self.send_response(writer, 200, json.dumps({"found": False}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            return

        # 0.3 SyncTeX
        elif path == '/api/latex/synctex' and method == 'GET':
            from gilzabots.synctex_parser import (
                GeneratedSourceError, companion_synctex_path, find_source_line, find_pdf_position,
            )

            action = query.get('action', [''])[0]
            file_path = query.get('filePath', [''])[0]
            project_path = query.get('projectPath', [''])[0]
            
            if file_path and os.path.isabs(file_path) and not project_path:
                project_path = os.path.dirname(file_path)
            if not file_path or not project_path or not action:
                self.send_response(writer, 400, b'{"error":"action, filePath and projectPath are required"}', "application/json")
                return
                
            try:
                if file_path.lower().endswith('.pdf'):
                    # A PDF opened on its own is only linked to LaTeX source
                    # through its own companion SyncTeX file. Resolving the
                    # project's main file here instead would map the click
                    # through an unrelated document's SyncTeX.
                    if action != 'pdf2tex':
                        self.send_response(writer, 400, b'{"error":"only pdf2tex is supported for a PDF file"}', "application/json")
                        return
                    pdf_full_path = os.path.abspath(os.path.join(project_path, file_path))
                    synctex_path = companion_synctex_path(pdf_full_path)
                    if not synctex_path:
                        self.send_response(writer, 404, b'{"error":"synctex file not found"}', "application/json")
                        return
                    page = int(query.get('page', ['1'])[0])
                    x = float(query.get('x', ['0'])[0])
                    y = float(query.get('y', ['0'])[0])
                    result = find_source_line(synctex_path, page, x, y)
                    if result and 'file' in result:
                        abs_file = result['file']
                        if not os.path.isabs(abs_file):
                            abs_file = os.path.abspath(os.path.join(os.path.dirname(synctex_path), abs_file))
                        if not os.path.isfile(abs_file):
                            self.send_response(writer, 404, json.dumps({"error": f"synctex source file not found: {abs_file}"}).encode('utf-8'), "application/json")
                            return
                        result['file'] = abs_file
                        try:
                            result['relFile'] = os.path.relpath(abs_file, project_path).replace('\\', '/')
                        except ValueError:
                            result['relFile'] = abs_file
                    self.send_response(writer, 200, json.dumps({"result": result}).encode('utf-8'), "application/json")
                    return

                # Resolve main_file from project settings if available
                main_file = ""
                from gilzabots.project import ProjectStore
                from gilzabots.config import DEFAULT_DB_PATH
                store = ProjectStore(db_path=DEFAULT_DB_PATH)
                for p in store.list_projects():
                    if p.get("project_path") and os.path.normcase(os.path.abspath(os.path.expanduser(p["project_path"]))) == os.path.normcase(os.path.abspath(os.path.expanduser(project_path))):
                        main_file = p.get("main_file", "")
                        break

                target_full_path = os.path.abspath(os.path.join(project_path, file_path))
                file_content = ""
                try:
                    with open(target_full_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
                        file_content = f.read()
                except Exception:
                    pass

                from gilzabots.latex_compiler import determine_main_file_for_compilation
                main_file = determine_main_file_for_compilation(target_full_path, file_content, project_path, main_file)

                if main_file:
                    main_full_path = os.path.abspath(os.path.join(project_path, main_file))
                else:
                    main_full_path = os.path.abspath(os.path.join(project_path, file_path))
                
                target_full_path = os.path.abspath(os.path.join(project_path, file_path))

                default_synctex_path = os.path.splitext(main_full_path)[0] + ".synctex.gz"
                synctex_path = default_synctex_path
                if hasattr(self, "last_synctex_path") and self.last_synctex_path and os.path.exists(self.last_synctex_path):
                    synctex_path = self.last_synctex_path
                 
                if not os.path.exists(synctex_path):
                    self.send_response(writer, 404, b'{"error":"synctex file not found"}', "application/json")
                    return
                    
                if action == 'pdf2tex':
                    page = int(query.get('page', ['1'])[0])
                    x = float(query.get('x', ['0'])[0])
                    y = float(query.get('y', ['0'])[0])
                    result = find_source_line(synctex_path, page, x, y)
                    if result and 'file' in result:
                        abs_file = result['file']
                        if not os.path.isabs(abs_file):
                            abs_file = os.path.abspath(os.path.join(project_path, abs_file))
                        # Same contract as the standalone path above: a record
                        # naming no existing file is reported, not opened as an
                        # empty editor tab.
                        if not os.path.isfile(abs_file):
                            self.send_response(writer, 404, json.dumps({"error": f"synctex source file not found: {abs_file}"}).encode('utf-8'), "application/json")
                            return
                        result['file'] = abs_file
                        # Also return a project-relative path so the frontend can compare
                        # against selectedFile (which is always relative to project_path)
                        try:
                            rel_file = os.path.relpath(abs_file, project_path).replace('\\', '/')
                        except ValueError:
                            rel_file = abs_file
                        result['relFile'] = rel_file
                    self.send_response(writer, 200, json.dumps({"result": result}).encode('utf-8'), "application/json")
                elif action == 'tex2pdf':
                    line = int(query.get('line', ['1'])[0])
                    result = find_pdf_position(synctex_path, target_full_path, line)
                    self.send_response(writer, 200, json.dumps({"result": result}).encode('utf-8'), "application/json")
                else:
                    self.send_response(writer, 400, b'{"error":"invalid action"}', "application/json")
            except GeneratedSourceError as e:
                self.send_response(writer, 404, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            return

        # 1. List Files
        if path == '/api/files':
            project_path = query.get('projectPath', [None])[0]
            show_hidden_files = str(query.get('showHiddenFiles', ['false'])[0]).lower() in {"1", "true", "yes", "on"}
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            if not os.path.exists(project_path) or not os.path.isdir(project_path):
                self.send_response(writer, 404, b'{"error":"Directory not found"}', "application/json")
                return
            try:
                # Re-read every 10 s and on every window focus; a large project
                # takes long enough to walk that it runs in a worker thread
                # under the exclusive-work lock (see EXCLUSIVE_WORK_PATHS).
                tree = await asyncio.to_thread(get_file_tree, project_path, show_hidden_files=show_hidden_files)
                self.send_response(writer, 200, json.dumps({"files": tree}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 2. Read File
        elif path == '/api/file/read':
            project_path = query.get('projectPath', [None])[0]
            file_path = query.get('filePath', [None])[0]
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return
            full_path = os.path.abspath(os.path.join(project_path, file_path))
            if not _is_path_within(full_path, project_path):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            if not os.path.exists(full_path) or os.path.isdir(full_path):
                self.send_response(writer, 404, b'{"error":"File not found"}', "application/json")
                return
            try:
                with open(full_path, 'r', encoding='utf-8', newline='') as f:
                    content = f.read()
                payload = {"content": content}
                self.send_response(writer, 200, json.dumps(payload).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/file/raw':
            project_path = query.get('projectPath', [None])[0]
            file_path = query.get('filePath', [None])[0]
            # The .tex file that contains the \includegraphics{} reference.
            # When provided, `..` segments in `file_path` are resolved
            # relative to this file's directory (LaTeX semantics) instead of
            # the project root, which is what users expect when they write
            # \includegraphics{../illustrations/foo.pdf}.
            source_tex = query.get('sourceTex', [None])[0]
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return
            project_abs = os.path.abspath(project_path)
            # Anchor directory: prefer the .tex file's directory so that
            # `..` is resolved locally; fall back to the project root when
            # the source file is not known.
            anchor_dir = project_abs
            if source_tex:
                candidate = os.path.abspath(os.path.join(project_abs, source_tex))
                if os.path.isdir(candidate):
                    anchor_dir = candidate
                else:
                    anchor_dir = os.path.dirname(candidate)
            full_path = os.path.abspath(os.path.join(anchor_dir, file_path))
            # Final safety: the resolved path must still be inside the
            # project directory tree. This keeps path-traversal hardening
            # intact (e.g. `../../etc/passwd` is still rejected).
            if not full_path.startswith(project_abs + os.sep) and full_path != project_abs:
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            if not os.path.exists(full_path) or os.path.isdir(full_path):
                self.send_response(writer, 404, b'{"error":"File not found"}', "application/json")
                return
            try:
                import mimetypes
                content_type, _ = mimetypes.guess_type(full_path)
                if not content_type:
                    content_type = "application/octet-stream"
                with open(full_path, 'rb') as f:
                    content = f.read()
                self.send_response(writer, 200, content, content_type)
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 2.5 Read File from Shadow Git (or normal Git)
        elif path == '/api/git/file-at-head':
            project_path = query.get('projectPath', [None])[0]
            file_path = query.get('filePath', [None])[0]
            use_shadow = query.get('shadow', ['false'])[0].lower() == 'true'
            git_root_path = query.get('gitRootPath', [None])[0]
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return
            
            def _work(respond):
                try:
                    import subprocess
                    git_ctx = _resolve_git_context(project_path, use_shadow, git_root_path)
                    norm_file_path = _project_path_to_repo_path(file_path, git_ctx)
                    command = git_ctx["git_cmd"] + ["show", f"HEAD:{norm_file_path}"]
                    result = subprocess.run(
                        command, capture_output=True, cwd=git_ctx["cwd"],
                        **utf8_text_kwargs()
                    )
                    source = git_ctx["source"]
                    
                    if result.returncode == 0:
                        respond(200, json.dumps({"content": result.stdout, "source": source}).encode('utf-8'), "application/json")
                        return
                    else:
                        respond(200, b'{"error":"Not found in git"}', "application/json")
                        return
                except GitContextError as e:
                    respond(
                        200,
                        json.dumps({
                            "content": "",
                            "source": "none",
                            "git_available": False,
                            "error": str(e),
                        }).encode('utf-8'),
                        "application/json",
                    )
                    return
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 3. Write File
        elif path == '/api/file/write' and method == 'POST':
            project_path = data.get('projectPath')
            file_path = data.get('filePath')
            content = data.get('content', '')
            # `exclusive` is for creation: refuse an existing path rather than
            # overwrite whatever is already there.
            exclusive = bool(data.get('exclusive'))
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return
            full_path = os.path.abspath(os.path.join(project_path, file_path))
            if not _is_path_within(full_path, project_path):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            if exclusive and os.path.lexists(full_path):
                self.send_response(writer, 409, json.dumps({"error": f"Path already exists: {file_path}", "code": "exists"}).encode('utf-8'), "application/json")
                return
            try:
                dir_path = os.path.dirname(full_path)
                os.makedirs(dir_path, exist_ok=True)
                response_payload = {"success": True}
                with open(full_path, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                
                # If writing an SVG file, automatically generate a PDF copy alongside it using PyMuPDF
                if file_path.endswith('.svg'):
                    try:
                        import fitz
                        pdf_path = os.path.splitext(full_path)[0] + '.pdf'
                        svg_bytes = content.encode('utf-8')
                        doc = fitz.open(stream=svg_bytes, filetype="svg")
                        pdf_bytes = doc.convert_to_pdf()
                        pdf_doc = fitz.open("pdf", pdf_bytes)
                        pdf_doc.save(pdf_path)
                        pdf_doc.close()
                        doc.close()
                    except ImportError:
                        print("PyMuPDF (fitz) is not installed; cannot generate PDF copy of SVG illustration.")
                    except Exception as ex:
                        print(f"Error converting SVG to PDF: {ex}")
                
                self._notify_cloud_change(project_path)
                self.send_response(writer, 200, json.dumps(response_payload).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.2. Create Directory
        elif path == '/api/file/mkdir' and method == 'POST':
            project_path = data.get('projectPath')
            dir_path = data.get('dirPath')
            # `exclusive` is for creation: report an existing path instead of
            # silently treating it as created.
            exclusive = bool(data.get('exclusive'))
            if not project_path or not dir_path:
                self.send_response(writer, 400, b'{"error":"projectPath and dirPath are required"}', "application/json")
                return
            full_path = os.path.abspath(os.path.join(project_path, dir_path))
            if not _is_path_within(full_path, project_path):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            if exclusive and os.path.lexists(full_path):
                self.send_response(writer, 409, json.dumps({"error": f"Path already exists: {dir_path}", "code": "exists"}).encode('utf-8'), "application/json")
                return
            try:
                os.makedirs(full_path, exist_ok=True)
                self._notify_cloud_change(project_path)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.4.5. Copy File/Directory
        elif path == '/api/file/copy' and method == 'POST':
            project_path = data.get('projectPath')
            source_path = data.get('sourcePath')
            target_path = data.get('targetPath')
            if not project_path or not source_path or not target_path:
                self.send_response(writer, 400, b'{"error":"projectPath, sourcePath and targetPath are required"}', "application/json")
                return
            full_source = os.path.abspath(os.path.join(project_path, source_path))
            full_target = os.path.abspath(os.path.join(project_path, target_path))
            if (not _is_path_within(full_source, project_path) or not _is_path_within(full_target, project_path)
                    or _same_project_root(full_source, project_path)):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            try:
                if not os.path.exists(full_source):
                    self.send_response(writer, 404, b'{"error":"Source file not found"}', "application/json")
                    return
                import shutil
                if os.path.exists(full_target):
                    base, ext = os.path.splitext(full_target)
                    counter = 1
                    while os.path.exists(full_target):
                        full_target = f"{base}_copy{counter if counter > 1 else ''}{ext}"
                        counter += 1
                
                os.makedirs(os.path.dirname(full_target), exist_ok=True)
                if os.path.isdir(full_source):
                    shutil.copytree(full_source, full_target)
                else:
                    shutil.copy2(full_source, full_target)
                self._notify_cloud_change(project_path)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.4.6. Import External File
        elif path == '/api/file/import' and method == 'POST':
            content_type = headers.get('content-type', '')
            if not content_type.lower().startswith('multipart/form-data'):
                self.send_response(writer, 400, b'{"error":"multipart/form-data is required"}', "application/json")
                return
            try:
                form_fields, form_files = _parse_multipart_form(body, content_type)
                project_path = form_fields.get('projectPath')
                target_dir = form_fields.get('targetDir', '')
                upload = form_files.get('file')
                if not project_path or not upload:
                    self.send_response(writer, 400, b'{"error":"projectPath and file are required"}', "application/json")
                    return

                project_abs = os.path.abspath(project_path)
                if not os.path.isdir(project_abs):
                    self.send_response(writer, 404, b'{"error":"Project directory not found"}', "application/json")
                    return

                safe_name = os.path.basename(upload.get('filename') or '').strip()
                if not safe_name:
                    self.send_response(writer, 400, b'{"error":"Imported file must have a name"}', "application/json")
                    return

                target_dir_abs = os.path.abspath(os.path.join(project_abs, target_dir or ''))
                if not _is_path_within(target_dir_abs, project_abs):
                    self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                    return

                os.makedirs(target_dir_abs, exist_ok=True)
                full_target = os.path.abspath(os.path.join(target_dir_abs, safe_name))
                if not _is_path_within(full_target, project_abs):
                    self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                    return

                if os.path.exists(full_target):
                    base, ext = os.path.splitext(full_target)
                    counter = 1
                    while os.path.exists(full_target):
                        full_target = f"{base}_copy{counter if counter > 1 else ''}{ext}"
                        counter += 1

                with open(full_target, 'wb') as f:
                    f.write(upload.get('content', b''))

                imported_path = _normalize_rel_path(os.path.relpath(full_target, project_abs))
                self._notify_cloud_change(project_abs)
                self.send_response(
                    writer,
                    200,
                    json.dumps({"success": True, "filePath": imported_path}).encode('utf-8'),
                    "application/json",
                )
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.5. Delete File
        elif path == '/api/file/delete' and method == 'POST':
            project_path = data.get('projectPath')
            file_path = data.get('filePath')
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return
            full_path = os.path.abspath(os.path.join(project_path, file_path))
            # The project root itself is not a deletable entry: "." used to pass
            # the old prefix check and remove the whole project folder.
            if not _is_path_within(full_path, project_path) or _same_project_root(full_path, project_path):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            try:
                if os.path.exists(full_path):
                    if os.path.isdir(full_path):
                        import shutil
                        shutil.rmtree(full_path)
                    else:
                        os.remove(full_path)
                    self._notify_cloud_change(project_path)
                    self.send_response(writer, 200, b'{"success":true}', "application/json")
                else:
                    self.send_response(writer, 404, b'{"error":"File not found"}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.6. Rename File/Directory
        elif path == '/api/file/rename' and method == 'POST':
            project_path = data.get('projectPath')
            old_path = data.get('oldPath')
            new_path = data.get('newPath')
            if not project_path or not old_path or not new_path:
                self.send_response(writer, 400, b'{"error":"projectPath, oldPath and newPath are required"}', "application/json")
                return
            full_old_path = os.path.abspath(os.path.join(project_path, old_path))
            full_new_path = os.path.abspath(os.path.join(project_path, new_path))
            if (not _is_path_within(full_old_path, project_path) or not _is_path_within(full_new_path, project_path)
                    or _same_project_root(full_old_path, project_path)):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return
            try:
                if os.path.exists(full_old_path):
                    os.makedirs(os.path.dirname(full_new_path), exist_ok=True)
                    os.rename(full_old_path, full_new_path)
                    self._notify_cloud_change(project_path)
                    self.send_response(writer, 200, b'{"success":true}', "application/json")
                else:
                    self.send_response(writer, 404, b'{"error":"Source file not found"}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.6.5. Open OS Explorer / Default App
        elif path == '/api/file/open-explorer' and method == 'POST':
            project_path = data.get('projectPath')
            file_path = data.get('filePath') # Optional
            
            if not project_path or not os.path.exists(project_path):
                self.send_response(writer, 400, b'{"error":"Valid projectPath is required"}', "application/json")
                return
                
            target_path = project_path
            if file_path:
                target_path = os.path.abspath(os.path.join(project_path, file_path))
                if not _is_path_within(target_path, project_path):
                    self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                    return
                if not os.path.exists(target_path):
                    self.send_response(writer, 404, b'{"error":"File not found"}', "application/json")
                    return

            try:
                import subprocess, platform
                system = platform.system()

                # Strip PyInstaller's LD_LIBRARY_PATH so system apps (like nautilus or image viewers) don't crash.
                env = os.environ.copy()
                if 'LD_LIBRARY_PATH_ORIG' in env:
                    env['LD_LIBRARY_PATH'] = env['LD_LIBRARY_PATH_ORIG']
                elif 'LD_LIBRARY_PATH' in env:
                    del env['LD_LIBRARY_PATH']

                proc = None
                accept_codes = (0,)
                if system == "Windows":
                    os.startfile(target_path)
                elif system == "Darwin":
                    proc = subprocess.Popen(["open", target_path], env=env)
                else:
                    # Check if it's WSL (Windows Subsystem for Linux)
                    release = platform.uname().release.lower()
                    if "microsoft" in release or "wsl" in release:
                        if os.path.isdir(target_path):
                            import shutil
                            if shutil.which("explorer.exe"):
                                proc = subprocess.Popen(["explorer.exe", "."], cwd=target_path, env=env)
                                # Windows explorer.exe exits with code 1 when handing off to the shell.
                                accept_codes = (0, 1)
                            else:
                                proc = subprocess.Popen(_desktop_open_command(target_path), env=env)
                        else:
                            # Not ideal but explorer.exe can open files in WSL if converted to win path, 
                            # easiest is wslview if available, otherwise xdg-open might work inside some WSL distros
                            proc = subprocess.Popen(_desktop_open_command(target_path), env=env)
                    else:
                        proc = subprocess.Popen(_desktop_open_command(target_path), env=env)

                # The launcher forks the real application and returns, so a process still
                # running is a success. One that has already failed must not be reported as
                # "opened": that is how a confined xdg-open exiting 3 looked like a working
                # feature from the UI.
                failure = await _desktop_open_failure(proc, accept_codes=accept_codes)
                if failure:
                    self.send_response(writer, 500, json.dumps({"error": failure}).encode('utf-8'), "application/json")
                else:
                    self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 3.7. List subdirectories of a filesystem path
        elif path == '/api/fs/dirs':
            from gilzabots.config import get_gilzabots_home

            def _default_dir():
                return os.path.dirname(get_gilzabots_home())

            req_path = data.get('path')
            if not req_path or req_path == '~':
                req_path = _default_dir()

            req_path = os.path.abspath(os.path.expanduser(req_path))

            if not os.path.exists(req_path) or not os.path.isdir(req_path):
                req_path = _default_dir()
                if not os.path.exists(req_path) or not os.path.isdir(req_path):
                    req_path = os.path.expanduser("~")

            # Navigation entries do not depend on reading req_path, so they are
            # built first and returned even when the listing fails. Otherwise an
            # unreadable folder (e.g. the Windows "My Documents" compatibility
            # junction, whose ACL denies listing) leaves the picker with no way out.
            nav_entries = []
            parent = os.path.dirname(req_path)
            if parent != req_path:
                nav_entries.append({"name": "..", "path": parent})
            else:
                # At filesystem root. If Windows, list available drives so user can switch.
                import sys
                if sys.platform == 'win32':
                    import string
                    for d in string.ascii_uppercase:
                        drive = f"{d}:\\"
                        if drive != req_path and os.path.exists(drive):
                            nav_entries.append({"name": f".. (Unidade {d}:)", "path": drive})

            try:
                entries = list(nav_entries)
                for name in sorted(os.listdir(req_path)):
                    if name.startswith('.'):
                        continue
                    full = os.path.join(req_path, name)
                    if os.path.isdir(full):
                        entries.append({"name": name, "path": full})
                self.send_response(writer, 200, json.dumps({"current": req_path, "dirs": entries}).encode('utf-8'), "application/json")
            except PermissionError:
                self.send_response(writer, 403, json.dumps({"error": "Permission denied", "current": req_path, "dirs": nav_entries}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e), "current": req_path, "dirs": nav_entries}).encode('utf-8'), "application/json")

        # Hardware Inference Endpoints
        elif path == '/api/monitors' or path.startswith('/api/monitors/'):
            from gilzabots.monitors import api as monitors_api
            status, body = await monitors_api.handle(
                path, method, data, query,
                turn_active=self._agent_turn_active(),
                focus_window=_focus_main_window,
            )
            self.send_response(writer, status, json.dumps(body, default=str).encode('utf-8'), "application/json")

        elif path == '/api/connectors' or path.startswith('/api/connectors/'):
            from gilzabots.connectors import api as connectors_api
            status, body = await connectors_api.handle(
                path, method, data, query, refresh_agent=self._refresh_live_orchestrator,
            )
            self.send_response(writer, status, json.dumps(body, default=str).encode('utf-8'), "application/json")

        elif path.startswith('/api/transfer/'):
            from gilzabots import transfer_api
            status, body = await transfer_api.handle(
                path, method, data, refresh_agent=self._refresh_live_orchestrator,
            )
            self.send_response(writer, status, json.dumps(body, default=str).encode('utf-8'), "application/json")

        elif path == '/api/hardware' and method == 'GET':
            from gilzabots.hardware_store import get_or_detect_hardware
            info = get_or_detect_hardware()
            self.send_response(writer, 200, json.dumps(info).encode('utf-8'), "application/json")

        elif path == '/api/hardware/detect' and method == 'POST':
            from gilzabots.hardware_detect import get_hardware_info
            from gilzabots.hardware_store import save_hardware_info
            info = get_hardware_info()
            save_hardware_info(info)
            self.send_response(writer, 200, json.dumps(info).encode('utf-8'), "application/json")

        elif path == '/api/hardware/save' and method == 'POST':
            from gilzabots.hardware_store import save_hardware_info
            try:
                save_hardware_info(data)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # Onboarding / Ollama status endpoints
        elif path == '/api/onboarding/status' and method == 'GET':
            from gilzabots.onboarding import is_onboarding_completed
            self.send_response(writer, 200, json.dumps({"completed": is_onboarding_completed()}).encode('utf-8'), "application/json")
        elif path == '/api/onboarding/complete' and method == 'POST':
            from gilzabots.onboarding import complete_onboarding
            self.send_response(writer, 200, json.dumps({"success": complete_onboarding()}).encode('utf-8'), "application/json")
        elif path == '/api/ollama/status' and method == 'GET':
            from gilzabots.ollama_manager import check_ollama_status
            self.send_response(writer, 200, json.dumps(check_ollama_status()).encode('utf-8'), "application/json")
        elif path == '/api/ollama/install' and method == 'POST':
            from gilzabots.ollama_manager import install_ollama_windows
            self.send_response(writer, 200, json.dumps(install_ollama_windows()).encode('utf-8'), "application/json")

        elif path == '/api/settings/gilzabotshome' and method == 'GET':
            from gilzabots.config import configured_gilzabots_home, data_dir_error, get_gilzabots_home
            current_home = get_gilzabots_home()
            configured_home = configured_gilzabots_home()
            # A configured directory that cannot be opened is silently ignored at
            # startup, so the UI has to be told why the running app is not using
            # the path the user picked.
            home_error = data_dir_error(configured_home) if configured_home else ""
            is_custom = False
            try:
                import pathlib
                pointer_file = pathlib.Path.home() / ".gilzabotshome"
                if pointer_file.exists():
                    is_custom = True
            except Exception:
                pass
            self.send_response(writer, 200, json.dumps({
                "path": current_home,
                "is_custom": is_custom,
                "configured_path": configured_home,
                "error": home_error,
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/gilzabotshome' and method == 'POST':
            new_path = data.get("path", "").strip()
            try:
                import pathlib
                pointer_file = pathlib.Path.home() / ".gilzabotshome"
                previous_path = ""
                if pointer_file.exists():
                    try:
                        previous_path = pointer_file.read_text(encoding="utf-8").strip()
                    except OSError:
                        previous_path = ""
                # Reject a directory the app could not actually open, instead of
                # storing it and crashing on the next launch when sqlite3 fails
                # to create sessions.db there.
                from gilzabots.config import data_dir_error
                rejection = data_dir_error(new_path) if new_path else ""
                if rejection:
                    self.send_response(writer, 400, json.dumps({"error": rejection}).encode('utf-8'), "application/json")
                else:
                    if new_path:
                        pointer_file.write_text(new_path, encoding="utf-8")
                    else:
                        # Remove custom pointer if empty
                        if pointer_file.exists():
                            pointer_file.unlink()
                    # The directory is only read at startup, so a restart is needed
                    # exactly when the stored value actually changed.
                    requires_restart = new_path != previous_path
                    self.send_response(writer, 200, json.dumps({"success": True, "requiresRestart": requires_restart}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/app/environment' and method == 'GET':
            import platform
            from gilzabots import __version__
            self.send_response(writer, 200, json.dumps({
                "platform": platform.system(),
                "version": __version__,
            }).encode('utf-8'), "application/json")

        elif path == '/api/app/restart' and method == 'POST':
            # Settings such as GILZABOTS_HOME are read at startup, so the UI offers
            # a restart right after saving them. Respond first, then relaunch:
            # the exit is deferred inside schedule_app_restart so this response
            # reaches the browser before the process goes away.
            try:
                command = schedule_app_restart()
                self.send_response(
                    writer,
                    200,
                    json.dumps({"success": True, "command": command}).encode('utf-8'),
                    "application/json",
                )
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/install-tectonic' and method == 'POST':
            try:
                import sys
                import urllib.request
                import tempfile

                from gilzabots.external_tools import executable_name, install_executable_from_archive

                os_name = sys.platform
                if os_name == "win32":
                    url = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-pc-windows-msvc.zip"
                    filename = "tectonic.zip"
                elif os_name == "darwin":
                    import platform
                    if platform.machine() == "arm64":
                        url = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-aarch64-apple-darwin.tar.gz"
                    else:
                        url = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-apple-darwin.tar.gz"
                    filename = "tectonic.tar.gz"
                else:
                    url = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-gnu.tar.gz"
                    filename = "tectonic.tar.gz"

                with tempfile.TemporaryDirectory() as tmpdir:
                    archive_path = os.path.join(tmpdir, filename)
                    urllib.request.urlretrieve(url, archive_path)
                    installed_path = install_executable_from_archive(archive_path, executable_name("tectonic"))

                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "path": installed_path,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/install-pandoc' and method == 'POST':
            try:
                import tempfile
                import urllib.request

                from gilzabots.document_exporter import (
                    get_pandoc_download,
                    install_pandoc_from_archive,
                )

                url, filename = get_pandoc_download()
                with tempfile.TemporaryDirectory() as tmpdir:
                    archive_path = os.path.join(tmpdir, filename)
                    urllib.request.urlretrieve(url, archive_path)
                    installed_path = install_pandoc_from_archive(archive_path)

                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "path": installed_path,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/models/info' and method == 'GET':
            model_name = query.get('model', [''])[0]
            if not model_name:
                self.send_response(writer, 400, b'{"error":"model parameter is required"}', "application/json")
                return
            
            clean_name = model_name
            if '/' in clean_name:
                clean_name = clean_name.split('/', 1)[1]
            
            api_base = query.get('api_base', [''])[0]
            try:
                import urllib.request as urllib_req
                import json as _json
                req = urllib_req.Request(_ollama_tags_url_for_model_info(model_name, api_base))
                with urllib_req.urlopen(req, timeout=2) as response:
                    data_obj = _json.loads(response.read().decode())
                    models = data_obj.get("models", [])
                    
                    found_model = None
                    for m in models:
                        if m.get("name") == clean_name or m.get("name").startswith(clean_name + ":"):
                            found_model = m
                            break
                    
                    if found_model:
                        size_bytes = found_model.get("size", 0)
                        size_gb = size_bytes / (1024**3)
                        self.send_response(writer, 200, json.dumps({
                            "found": True,
                            "size_gb": round(size_gb, 2),
                            "details": found_model.get("details", {})
                        }).encode('utf-8'), "application/json")
                        return
            except Exception:
                pass
                
            self.send_response(writer, 200, json.dumps({"found": False}).encode('utf-8'), "application/json")

        elif path == '/api/gilzabots/list-projects':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            projects = store.list_projects()
            for p in projects:
                raw_path = p.get('project_path', '')
                if raw_path:
                    p['exists'] = os.path.exists(os.path.abspath(os.path.expanduser(raw_path)))
                else:
                    p['exists'] = False
            from gilzabots.ui_settings import load_ui_settings
            last_active = load_ui_settings().get('last_active_project', '')
            self.send_response(writer, 200, json.dumps({
                "projects": projects,
                "last_active_project": last_active if isinstance(last_active, str) else '',
            }).encode('utf-8'), "application/json")

        # 5. Create Project
        elif path == '/api/gilzabots/create-project' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH, is_local_model
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)

            project_name = data.get("project_name")
            project_path = data.get("project_path") or os.getcwd()
            description = data.get("description", "")
            # A project starts with no model configured. The user selects one from
            # the global model store; no implicit default is injected here.
            model = str(data.get("model") or "")
            worker_model = data.get("worker_model", "")
            mode = data.get("mode") or "auto"
            skills = data.get("skills", [])
            api_key = data.get("api_key")
            api_base = data.get("api_base")
            worker_api_key = data.get("worker_api_key")
            worker_api_base = data.get("worker_api_base")

            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name is required"}', "application/json")
                return
                
            from gilzabots.config import project_dir_error

            abs_path = os.path.abspath(os.path.expanduser(project_path)) if project_path else os.getcwd()
            if os.path.exists(abs_path) and not os.path.isdir(abs_path):
                self.send_response(writer, 400, json.dumps({"error": f"The path '{project_path}' exists but is not a directory."}).encode('utf-8'), "application/json")
                return
            # Probe by creating a file: os.access does not catch every case in
            # which a directory refuses new files, which otherwise surfaced only
            # when the user tried to create the first file.
            message = project_dir_error(abs_path)
            if message:
                self.send_response(writer, 400, json.dumps({"error": message}).encode('utf-8'), "application/json")
                return
                
            model_params_raw = data.get("model_params")
            model_params = sanitize_model_params(model_params_raw) if isinstance(model_params_raw, dict) else None
            worker_model_params_raw = data.get("worker_model_params")
            worker_model_params = sanitize_model_params(worker_model_params_raw) if isinstance(worker_model_params_raw, dict) else None

            existing_name = store.find_by_path(abs_path)
            if existing_name:
                existing_proj = store.load(existing_name)
                existing_project_name = existing_proj.project_name if existing_proj else existing_name
                err_msg = get_translation("project_exists_in_folder", name=existing_project_name)
                self.send_response(writer, 400, json.dumps({"error": err_msg}).encode('utf-8'), "application/json")
                return

            db_key = project_name.replace(" ", "_").lower()
            original_db_key = db_key
            counter = 1
            while store.exists(db_key):
                db_key = f"{original_db_key}_{counter}"
                counter += 1

            try:
                project = store.create(
                    name=db_key,
                    mode=mode,
                    model=model,
                    worker_model=worker_model,
                    project_name=project_name,
                    project_path=abs_path,
                    skills=skills,
                    description=description,
                    api_key=api_key,
                    api_base=api_base,
                    worker_api_key=worker_api_key,
                    worker_api_base=worker_api_base,
                    model_params=model_params,
                    worker_model_params=worker_model_params,
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return
                
            # A project created against a local Ollama model is pre-fetched so the
            # first message does not fail on a model that was never downloaded.
            # The helper skips models Ollama already serves.
            if model and model.startswith("ollama/") and is_local_model(model, api_base):
                from gilzabots.ollama_manager import pull_model_in_background
                pull_model_in_background(model.split("ollama/", 1)[1])
            
            if "piloto" in project_name.lower() or "pilot" in project_name.lower():
                from gilzabots.onboarding import PILOT_SKILL_NAME, pilot_skill_content
                from gilzabots.ui_settings import load_ui_settings
                from gilzabots.skills import write_skills_yaml

                cfg = load_ui_settings()
                lang = cfg.get("lang", "pt")
                skill_content = pilot_skill_content(lang)

                skill_dir = os.path.join(abs_path, ".gilzabots", "skills", PILOT_SKILL_NAME)
                os.makedirs(skill_dir, exist_ok=True)
                with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                    f.write(skill_content.strip() + "\n")
                
                # Activate the skill without overwriting command-line
                from gilzabots.skills import read_skills_yaml
                existing_skills = read_skills_yaml(abs_path)
                if PILOT_SKILL_NAME not in existing_skills:
                    existing_skills.append(PILOT_SKILL_NAME)
                write_skills_yaml(abs_path, existing_skills)

                if PILOT_SKILL_NAME not in project.skills:
                    project.skills.append(PILOT_SKILL_NAME)
                    store.save(project)

            from gilzabots.project import create_contextual_skills_defaults
            create_contextual_skills_defaults(abs_path)

            res_data = {
                "name": project.name,
                "project_name": project.project_name,
                "project_path": project.project_path,
                "skills": project.skills
            }
            self.send_response(writer, 200, json.dumps(res_data).encode('utf-8'), "application/json")

        elif path == '/api/gilzabots/load-contextual-skills' and method == 'POST':
            project_path = data.get("project_path")
            if not project_path:
                self.send_response(writer, 400, b'{"error":"project_path is required"}', "application/json")
                return
            
            from gilzabots.project import create_contextual_skills_defaults
            create_contextual_skills_defaults(project_path)
            self.send_response(writer, 200, b'{"status":"ok"}', "application/json")

        # 5b. Import existing project
        elif path == '/api/gilzabots/import-project' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectImportError, ProjectStore, import_project
            store = ProjectStore(db_path=DEFAULT_DB_PATH)

            project_path = data.get("project_path", "")
            if not project_path:
                self.send_response(writer, 400, b'{"error":"project_path is required"}', "application/json")
                return

            try:
                project = import_project(store, project_path)
            except ProjectImportError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            res_data = {
                "name": project.name,
                "project_name": project.project_name,
                "project_path": project.project_path,
                "skills": project.skills,
                "model": project.model,
            }
            self.send_response(writer, 200, json.dumps(res_data).encode('utf-8'), "application/json")

        # 6. Delete Project
        elif path == '/api/gilzabots/delete' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            import shutil
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            delete_dir = data.get("delete_dir", False)
            delete_cloud = bool(data.get("delete_cloud", False))
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name is required"}', "application/json")
                return
            if store.exists(project_name):
                proj = store.load(project_name)

                cloud_result = None
                if proj and proj.project_path:
                    from gilzabots.cloud.base import CloudError
                    from gilzabots.cloud.service import MANAGER as CLOUD_MANAGER
                    # A deleted project must not keep a background sync alive.
                    CLOUD_MANAGER.deactivate(proj.project_path)
                    if delete_cloud:
                        # First, and able to stop the whole deletion: if the
                        # cloud copy cannot be removed, nothing local is either,
                        # so the user can retry with the project still in place.
                        try:
                            cloud_result = await asyncio.get_running_loop().run_in_executor(
                                None, lambda: CLOUD_MANAGER.delete_remote_copy(proj.project_path)
                            )
                        except CloudError as e:
                            self.send_response(writer, 400, json.dumps({
                                "error": f"Could not delete the cloud copy, so nothing was removed: {e}",
                            }).encode('utf-8'), "application/json")
                            return

                # Ensure resources are released before deleting directory to avoid file locks
                if proj and proj.project_path:
                    try:
                        # 1. Close main terminal
                        if self.active_terminal and getattr(self.active_terminal, 'project_path', None) == proj.project_path:
                            try:
                                self.active_terminal.close()
                            except:
                                pass
                            self.active_terminal = None
                            
                        # 2. Close temp terminals
                        for tid, term in list(self.temp_terminals.items()):
                            if getattr(term, 'project_path', None) == proj.project_path:
                                try:
                                    term.close()
                                except:
                                    pass
                                del self.temp_terminals[tid]
                                
                        # 3. Close SQLite databases
                        from gilzabots.code_index import CODE_INDEX
                        if getattr(CODE_INDEX, '_root', None) == proj.project_path:
                            try:
                                CODE_INDEX.close()
                            except:
                                pass
                                
                        from gilzabots.vector_index import get_vector_index
                        v_idx = get_vector_index()
                        if v_idx and str(getattr(v_idx, '_root', '')) == proj.project_path:
                            try:
                                v_idx.close()
                            except:
                                pass
                    except Exception as e:
                        print(f"Error releasing resources before deletion: {e}")

                if delete_dir:
                    if proj and proj.project_path and os.path.exists(proj.project_path):
                        try:
                            import stat
                            def remove_readonly(func, path, excinfo):
                                try:
                                    os.chmod(path, stat.S_IWRITE)
                                    func(path)
                                except Exception:
                                    pass
                            shutil.rmtree(proj.project_path, onerror=remove_readonly)
                        except Exception as e:
                            print(f"Error deleting project directory: {e}")
                store.delete(project_name)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "cloud": cloud_result,
                }).encode('utf-8'), "application/json")
            else:
                self.send_response(writer, 404, json.dumps({"error": f"Project '{project_name}' not found"}).encode('utf-8'), "application/json")

        elif path == '/api/chat/delete' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_id = data.get("chat_id")
            if not project_name or not chat_id:
                self.send_response(writer, 400, b'{"error":"project_name and chat_id required"}', "application/json")
                return
            try:
                store.delete_chat(project_name, chat_id)
            except ValueError as exc:
                self.send_response(writer, 400, json.dumps({"error": str(exc)}).encode('utf-8'), "application/json")
                return
            from gilzabots.archival import clear_archival_chat
            clear_archival_chat(project_name, chat_id)
            self.send_response(writer, 200, b'{"status":"ok"}', "application/json")

        elif path == '/api/chat/clear' and method == 'POST':
            # Same effects as the /clear_chat command: both call chat_ops.clear_chat.
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ChatNotFoundError, ProjectStore
            from gilzabots.chat_ops import clear_chat

            project_name = data.get("project_name")
            chat_id = data.get("chat_id") or None
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name required"}', "application/json")
                return

            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            try:
                project = store.load(project_name, chat_id=chat_id)
            except ChatNotFoundError as exc:
                self.send_response(writer, 404, json.dumps({"error": str(exc)}).encode('utf-8'), "application/json")
                return
            if not project:
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return

            # ``chat_id`` may have been None: clear the chat the project resolved to.
            chat_id = project.current_chat_id
            if not chat_id:
                self.send_response(writer, 404, b'{"error":"project has no chat"}', "application/json")
                return
            clear_chat(project, store, chat_id)

            # Drop the in-memory orchestrator so a stale internal_history cannot
            # be written back over the cleared state by a later turn.
            import gilzabots.agent_stdin as _agent_stdin
            if getattr(_agent_stdin, "current_memgpt", None) is not None:
                _agent_stdin.current_memgpt = None

            self.send_response(
                writer,
                200,
                json.dumps({"status": "ok", "chat_id": chat_id}).encode('utf-8'),
                "application/json",
            )

        elif path == '/api/chat/clear-all' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            from gilzabots.archival import clear_archival

            project_name = data.get("project_name")
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name required"}', "application/json")
                return

            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            try:
                store.clear_all_chats(project_name)
            except ValueError as exc:
                self.send_response(
                    writer,
                    404,
                    json.dumps({"error": str(exc)}).encode('utf-8'),
                    "application/json",
                )
                return

            clear_archival(project_name)
            # Every chat is gone, so no stored measurement describes anything.
            from gilzabots.token_usage import reset_context_usage
            reset_context_usage()
            self.send_response(
                writer,
                200,
                json.dumps({"status": "ok"}).encode('utf-8'),
                "application/json",
            )

        elif path == '/api/chat/rename' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_id = data.get("chat_id")
            new_name = data.get("new_name")
            if not project_name or not chat_id or not new_name:
                self.send_response(writer, 400, b'{"error":"project_name, chat_id and new_name required"}', "application/json")
                return
            store.rename_chat(project_name, chat_id, new_name)
            self.send_response(writer, 200, b'{"status":"ok"}', "application/json")

        elif path == '/api/chat/reasoning-effort' and method == 'POST':
            # The reasoning effort of one chat, in place of its model's catalog
            # value. It is read from the store when the next turn builds its
            # orchestrator, so a turn already running keeps the effort it began with.
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ChatNotFoundError, ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_id = data.get("chat_id")
            if not project_name or not chat_id or "reasoning_effort" not in data:
                self.send_response(writer, 400, b'{"error":"project_name, chat_id and reasoning_effort required"}', "application/json")
                return
            try:
                stored = store.set_chat_reasoning_effort(project_name, chat_id, data.get("reasoning_effort"))
            except ChatNotFoundError as e:
                self.send_response(writer, 404, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            self.send_response(writer, 200, json.dumps({
                "status": "ok", "chat_id": chat_id, "reasoning_effort": stored,
            }).encode(), "application/json")

        elif path == '/api/chat/model' and method == 'POST':
            # The model one chat runs on, for one role ("model" = orchestrator,
            # "worker_model"). Empty clears it, so the chat follows the project's
            # default and then the catalog's global default. Read from the store
            # when the next turn builds its orchestrator, like the effort above.
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ChatNotFoundError, ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_id = data.get("chat_id")
            role = data.get("role")
            if not project_name or not chat_id or not role or "model" not in data:
                self.send_response(writer, 400, b'{"error":"project_name, chat_id, role and model required"}', "application/json")
                return
            try:
                previous = store.list_chat_models(project_name).get(chat_id, {}).get(role, "")
                stored = store.set_chat_model(project_name, chat_id, role, data.get("model"))
            except ChatNotFoundError as e:
                self.send_response(writer, 404, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            if stored != previous:
                _prefetch_local_ollama_model(stored)
            self.send_response(writer, 200, json.dumps({
                "status": "ok", "chat_id": chat_id, "role": role, "model": stored,
            }).encode(), "application/json")

        elif path == '/api/chat/branch' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            import uuid
            
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            source_chat_id = data.get("source_chat_id")
            new_chat_name = data.get("new_chat_name")
            message_index = data.get("message_index")
            message_id = data.get("message_id")
            client_message_id = data.get("client_message_id") or ""
            
            if not project_name or not source_chat_id or not new_chat_name or (message_index is None and message_id is None and not client_message_id):
                self.send_response(writer, 400, b'{"error":"project_name, source_chat_id, new_chat_name and message_index/message_id required"}', "application/json")
                return
                
            new_chat_id = str(uuid.uuid4())
            try:
                store.branch_chat(
                    project_name,
                    source_chat_id,
                    new_chat_id,
                    new_chat_name,
                    int(message_index or 0),
                    message_id=int(message_id) if message_id is not None else None,
                    client_message_id=client_message_id,
                )
                self.send_response(writer, 200, json.dumps({"status": "success", "new_chat_id": new_chat_id}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            return

        elif path == '/api/chat/truncate' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore

            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_id = data.get("chat_id")
            message_id = data.get("message_id")
            client_message_id = data.get("client_message_id") or ""
            superseded_by = data.get("superseded_by") or ""
            # Anchored by stable id only: a positional cut cannot be trusted because
            # UI indexes and stored rows do not line up.
            if not project_name or not chat_id or (message_id is None and not client_message_id):
                self.send_response(
                    writer,
                    400,
                    b'{"error":"project_name, chat_id and message_id/client_message_id required"}',
                    "application/json",
                )
                return
            try:
                superseded_ids = store.supersede_chat_history_from_message(
                    project_name,
                    chat_id,
                    message_id=int(message_id) if message_id is not None else None,
                    client_message_id=client_message_id,
                    superseded_by=superseded_by,
                )
                self.send_response(
                    writer,
                    200,
                    json.dumps({"status": "ok", "superseded_ids": superseded_ids}).encode('utf-8'),
                    "application/json",
                )
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            return

        elif path == '/api/chat/branch-edit' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            import uuid

            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            source_chat_id = data.get("source_chat_id")
            new_chat_name = data.get("new_chat_name") or "Edited branch"
            message_id = data.get("message_id")
            client_message_id = data.get("client_message_id") or ""
            if not project_name or not source_chat_id or (message_id is None and not client_message_id):
                self.send_response(
                    writer,
                    400,
                    b'{"error":"project_name, source_chat_id and message_id/client_message_id required"}',
                    "application/json",
                )
                return
            new_chat_id = str(uuid.uuid4())
            try:
                history = store.branch_chat_before_message(
                    project_name,
                    source_chat_id,
                    new_chat_id,
                    new_chat_name,
                    message_id=int(message_id) if message_id is not None else None,
                    client_message_id=client_message_id,
                )
                self.send_response(
                    writer,
                    200,
                    json.dumps({
                        "status": "success",
                        "new_chat_id": new_chat_id,
                        "name": new_chat_name,
                        "history": history,
                    }).encode('utf-8'),
                    "application/json",
                )
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            return


        elif path == '/api/chat/history' and method == 'GET':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ChatNotFoundError, ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = query.get("project_name", [""])[0]
            chat_id = query.get("chat_id", [""])[0] or None
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name required"}', "application/json")
                return
            try:
                project = store.load(project_name, chat_id=chat_id)
            except ChatNotFoundError as exc:
                # Answering with another chat's history is what made the panel
                # show one chat in the selector and a different one in the body.
                self.send_response(writer, 404, json.dumps({"error": str(exc)}).encode('utf-8'), "application/json")
                return
            if not project:
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return
            chat_id = project.current_chat_id
            # Every persisted thought belongs to the transcript, so no tail limit
            # may reach them: that is how later streamed answer tokens evicted the
            # entire thinking phase. Only `stream_chunk` is truncated -- it is
            # written once per token, and loading a long chat's entire token
            # history on every open is what an unbounded read costs.
            activity = store.list_activity(
                project_name, chat_id,
                limit=CHAT_HISTORY_STREAM_CHUNK_LIMIT,
                truncate_events=("stream_chunk",),
                exclude_events=("thought",),
            )
            # Reasoning is counted here and fetched per message from
            # /api/chat/thoughts when the user expands "AI Thoughts". It is
            # written once per streamed token, so shipping it with every open
            # cost 24 MB and seconds of parsing on a measured chat -- for text
            # that starts collapsed. Nothing is dropped: the rows stay in
            # project_activity and the expansion reads them.
            thought_windows = store.thought_windows(project_name, chat_id, project.history)
            # The measured context occupancy is rehydrated here so reopening a
            # chat reports the real number instead of dropping back to the
            # character estimate.
            context_usage = await _chat_context_usage(store, project, chat_id)
            self.send_response(writer, 200, json.dumps({
                "chat_id": chat_id,
                "history": project.history,
                "activity": activity,
                "thought_windows": thought_windows,
                "context_usage": context_usage,
            }).encode(), "application/json")

        elif path == '/api/chat/thoughts' and method == 'GET':
            # The reasoning of one assistant message, read only when its panel
            # is opened (see /api/chat/history above).
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = query.get("project_name", [""])[0]
            chat_id = query.get("chat_id", [""])[0]
            if not project_name or not chat_id:
                self.send_response(writer, 400, b'{"error":"project_name and chat_id required"}', "application/json")
                return
            try:
                from_id = int(query.get("from_id", ["0"])[0])
                to_id = int(query.get("to_id", ["0"])[0])
                max_tokens = int(query.get("max_tokens", ["32000"])[0])
            except (TypeError, ValueError):
                self.send_response(writer, 400, b'{"error":"from_id, to_id and max_tokens must be whole numbers"}', "application/json")
                return
            tail = store.thought_tail(project_name, chat_id, from_id, to_id, max_tokens)
            self.send_response(writer, 200, json.dumps(tail).encode(), "application/json")

        elif path == '/api/chat/list' and method == 'GET':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = query.get("project_name", [""])[0]
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name required"}', "application/json")
                return
            project = store.load(project_name)
            if not project:
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return
            # Chats are listed newest first and the default chat is the first
            # entry (empty when the project has no chat). The server resolves it
            # so the client never guesses an id.
            #
            # The tutorial chat is published the same way, and only when it actually
            # exists, so the front-end can recognise it after a reload (to show the
            # question menu) without reconstructing the reserved id itself.
            from gilzabots.project import tutorial_chat_id
            tutorial_id = tutorial_chat_id(project_name)
            has_tutorial = any(c.get("id") == tutorial_id for c in (project.chats or []))
            # Each chat carries its own reasoning effort ("" = follow the model
            # catalog), so the composer can show it without a request per chat.
            efforts = store.list_chat_reasoning_efforts(project_name)
            # Likewise its models ("" = follow the project's default).
            chat_models = store.list_chat_models(project_name)
            chats = [
                {
                    **chat,
                    "reasoning_effort": efforts.get(chat.get("id"), ""),
                    "model": chat_models.get(chat.get("id"), {}).get("model", ""),
                    "worker_model": chat_models.get(chat.get("id"), {}).get("worker_model", ""),
                }
                for chat in (project.chats or [])
            ]
            self.send_response(writer, 200, json.dumps({
                "chats": chats,
                "current_chat_id": project.current_chat_id,
                "tutorial_chat_id": tutorial_id if has_tutorial else "",
            }).encode(), "application/json")

        elif path == '/api/chat/search' and method == 'GET':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = query.get("project_name", [""])[0]
            q = query.get("q", [""])[0]
            if not project_name or not q:
                self.send_response(writer, 400, b'{"error":"project_name and q required"}', "application/json")
                return
            results = store.search_chat_content(project_name, q)
            self.send_response(writer, 200, json.dumps({"results": results}).encode(), "application/json")

        # 6c. Upload attachment (image or supported document) for chat
        elif path == '/api/chat/upload' and method == 'POST':
            filename = data.get("filename", "attachment")
            data_b64 = data.get("data_b64", "")
            mime = data.get("mime", "application/octet-stream")
            if not data_b64:
                self.send_response(writer, 400, b'{"error":"data_b64 is required"}', "application/json")
                return
            try:
                from gilzabots.attachments import build_attachment_descriptor
                project_name = data.get("project_name")
                document_mimes = {
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                }
                document_extensions = {".pdf", ".docx", ".pptx"}
                _, ext = os.path.splitext(filename or "")

                # Resolve optional document truncation settings from project
                max_chars = None
                if project_name and (mime in document_mimes or ext.lower() in document_extensions):
                    from gilzabots.config import DEFAULT_DB_PATH
                    from gilzabots.project import ProjectStore
                    _store = ProjectStore(db_path=DEFAULT_DB_PATH)
                    if _store.exists(project_name):
                        _proj = _store.load(project_name)
                        _mp = getattr(_proj, "model_params", {}) or {}
                        if _mp.get("pdf_truncate", True):
                            # max_chars is applied later in agent_stdin with context info;
                            # here we apply a hard cap of 200 000 chars to protect memory.
                            max_chars = 200_000
                # PDF/DOCX extraction and image re-encoding are synchronous and slow;
                # attaching several files at once would otherwise stall every other
                # request of the IDE (editor, preview, terminal) until they finish.
                descriptor = await asyncio.to_thread(
                    build_attachment_descriptor, filename, data_b64, mime, max_chars=max_chars
                )
                self.send_response(writer, 200, json.dumps(descriptor).encode(), "application/json")
            except ImportError as imp_err:
                missing = str(imp_err).replace("No module named ", "").strip("'\"")
                pkg_hint = "Pillow pymupdf4llm"
                msg = (
                    f"Missing dependency '{missing}' required for attachment processing. "
                    f"Please install it: pip install {pkg_hint}"
                )
                self.send_response(writer, 503, json.dumps({"error": msg}).encode(), "application/json")
            except Exception as exc:
                import traceback
                self.send_response(writer, 500, json.dumps({"error": str(exc), "trace": traceback.format_exc()}).encode(), "application/json")

        elif path == '/api/chat/create' and method == 'POST':

            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            import uuid
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            chat_name = data.get("chat_name")
            if not project_name or not chat_name:
                self.send_response(writer, 400, b'{"error":"project_name and chat_name required"}', "application/json")
                return
            chat_id = str(uuid.uuid4())
            # A new chat names no model unless the caller supplies one: the
            # model picked in the chat toolbar before the chat existed (the
            # first message is what creates it). Empty follows the default.
            try:
                effort = store.create_chat(
                    project_name, chat_id, chat_name,
                    model=data.get("model") or "",
                    worker_model=data.get("worker_model") or "",
                )
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            chat_models = store.list_chat_models(project_name).get(chat_id, {})
            model = chat_models.get("model", "")
            worker_model = chat_models.get("worker_model", "")
            for stored in (model, worker_model):
                _prefetch_local_ollama_model(stored)
            self.send_response(writer, 200, json.dumps({
                "id": chat_id, "name": chat_name, "reasoning_effort": effort,
                "model": model, "worker_model": worker_model,
            }).encode(), "application/json")

        # 6a-bis. Built-in tutorial chat.
        #
        # The front-end never supplies the tutorial text: it opens the chat and then
        # names a topic, and the server answers from `gilzabots/guides/tutorial.<lang>.md`.
        # That keeps the guide the single source of truth for the menu answers and for
        # the block injected into the orchestrator's system prompt, and it means the
        # tutorial works before any provider or model has been registered.
        elif path == '/api/tutorial/open' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import (
                ChatNotFoundError, ProjectStore, TUTORIAL_CHAT_NAME, tutorial_chat_id,
            )
            from gilzabots.tutorial import load_intro, topic_menu
            from gilzabots.ui_settings import load_ui_settings
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name required"}', "application/json")
                return
            if not store.exists(project_name):
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return
            lang = data.get("lang") or load_ui_settings().get("lang", "pt")
            chat_id = tutorial_chat_id(project_name)
            created = False
            try:
                project = store.load(project_name, chat_id=chat_id)
            except ChatNotFoundError:
                project = None
            if project is None:
                store.create_chat(project_name, chat_id, TUTORIAL_CHAT_NAME)
                created = True
                project = store.load(project_name, chat_id=chat_id)
            if not project:
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return
            # Seed the intro once. Reopening the tutorial must return to the same
            # conversation, not stack another welcome message on top of it.
            if not project.history:
                store.append_message(project, "assistant", load_intro(lang))
            chat_name = next(
                (c.get("name") for c in (project.chats or []) if c.get("id") == chat_id),
                TUTORIAL_CHAT_NAME,
            )
            tutorial_context_usage = await _chat_context_usage(store, project, chat_id)
            self.send_response(writer, 200, json.dumps({
                "chat_id": chat_id,
                "name": chat_name,
                "created": created,
                "history": project.history,
                "topics": topic_menu(lang),
                # This endpoint replaces the panel's transcript, so it must also
                # carry the chat's measured occupancy: the tutorial chat holds a
                # real conversation once the user asks follow-up questions there.
                "context_usage": tutorial_context_usage,
            }, ensure_ascii=False).encode('utf-8'), "application/json")

        elif path == '/api/tutorial/topics' and method == 'GET':
            # The question menu on its own, for a tutorial chat reopened from the chat
            # sidebar after a reload. Reusing /api/tutorial/open here would also return
            # the history and overwrite what the panel already rendered.
            from gilzabots.tutorial import topic_menu
            from gilzabots.ui_settings import load_ui_settings
            lang = query.get("lang", [""])[0] or load_ui_settings().get("lang", "pt")
            self.send_response(writer, 200, json.dumps({
                "topics": topic_menu(lang),
            }, ensure_ascii=False).encode('utf-8'), "application/json")

        elif path == '/api/tutorial/answer' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ChatNotFoundError, ProjectStore, tutorial_chat_id
            from gilzabots.tutorial import get_topic
            from gilzabots.ui_settings import load_ui_settings
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")
            topic_id = data.get("topic_id")
            if not project_name or not topic_id:
                self.send_response(writer, 400, b'{"error":"project_name and topic_id required"}', "application/json")
                return
            lang = data.get("lang") or load_ui_settings().get("lang", "pt")
            topic = get_topic(topic_id, lang)
            if topic is None:
                # Answering the nearest topic instead would reply to a question the
                # user never asked. Fail loudly.
                self.send_response(writer, 404, json.dumps({
                    "error": f"unknown tutorial topic '{topic_id}'"
                }).encode('utf-8'), "application/json")
                return
            chat_id = tutorial_chat_id(project_name)
            try:
                project = store.load(project_name, chat_id=chat_id)
            except ChatNotFoundError as exc:
                self.send_response(writer, 404, json.dumps({"error": str(exc)}).encode('utf-8'), "application/json")
                return
            if not project:
                self.send_response(writer, 404, b'{"error":"project not found"}', "application/json")
                return
            appended_from = len(project.history)
            store.append_message(project, "user", topic["question"])
            store.append_message(project, "assistant", topic["answer"])
            self.send_response(writer, 200, json.dumps({
                "chat_id": chat_id,
                "messages": project.history[appended_from:],
            }, ensure_ascii=False).encode('utf-8'), "application/json")

        # 6b. Update Project (patch fields without resetting history)
        elif path == '/api/gilzabots/update-project' and method == 'POST':
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            project_name = data.get("project_name")  # internal key (db name)
            if not project_name and data.get("project_path"):
                project_name = store.find_by_path(data["project_path"])
            if not project_name:
                self.send_response(writer, 400, b'{"error":"project_name is required"}', "application/json")
                return
            if not store.exists(project_name):
                found = None
                if data.get("project_path"):
                    found = store.find_by_path(data["project_path"])
                if found:
                    project_name = found
                else:
                    self.send_response(writer, 404, json.dumps({"error": f"Project '{project_name}' not found"}).encode(), "application/json")
                    return
            # This endpoint patches project fields, not chat content, so it loads
            # the default chat instead of trusting a client-supplied chat id.
            project = store.load(project_name)

            if "user_prompt_prefix" in data:
                if not isinstance(data["user_prompt_prefix"], str):
                    self.send_response(writer, 400, b'{"error":"user_prompt_prefix must be a string"}', "application/json")
                    return
                project.user_prompt_prefix = data["user_prompt_prefix"]

            # Patch only supplied fields
            if "display_name" in data:
                project.project_name = data["display_name"]
            previous_model = str(getattr(project, "model", "") or "")
            # An explicit empty value clears the selection: the project must be able
            # to go back to "no model configured".
            if "model" in data:
                project.model = str(data["model"] or "")
            # Compared against the stored value, not against `"model" in data`: the
            # GUI re-sends the current model on unrelated saves, and that must not
            # count as a change.
            model_changed = str(getattr(project, "model", "") or "") != previous_model
            if "worker_model" in data:
                project.worker_model = str(data["worker_model"] or "")
            if "description" in data:
                project.description = data["description"]
            if "mode" in data and data["mode"]:
                project.mode = data["mode"]
            if "project_path" in data and data["project_path"]:
                new_path = data["project_path"]
                if not os.path.exists(new_path) or not os.path.isdir(new_path):
                    self.send_response(writer, 400, b'{"error":"Project path does not exist or is not a directory"}', "application/json")
                    return
                project.project_path = os.path.abspath(new_path)

            # Reported back to the client when a *stored* git root stopped
            # resolving; see the unchanged-value branch below.
            git_root_warning = ""
            if "git_root_path" in data:
                new_git_root = data.get("git_root_path") or ""
                stored_git_root = str(getattr(project, "git_root_path", "") or "")
                if not new_git_root:
                    project.git_root_path = ""
                elif new_git_root == stored_git_root:
                    # The edit modal echoes the stored git root back on every save,
                    # so validating it here rejected edits that had nothing to do
                    # with Git whenever the stored value stopped resolving -- a
                    # folder that moved, or a Windows path in a database synced to
                    # another OS. Only a *changed* value is validated; an unchanged
                    # one is kept verbatim and reported as a warning, never
                    # rewritten or silently cleared.
                    if not os.path.isdir(new_git_root):
                        git_root_warning = "Git root path does not exist or is not a directory"
                    project.git_root_path = stored_git_root
                else:
                    abs_git_root = os.path.abspath(new_git_root)
                    if not os.path.isdir(abs_git_root):
                        self.send_response(writer, 400, b'{"error":"Git root path does not exist or is not a directory"}', "application/json")
                        return
                    if not _is_path_within(abs_git_root, project.project_path):
                        self.send_response(writer, 400, b'{"error":"Git root path must be inside the project path"}', "application/json")
                        return
                    if not os.path.exists(os.path.join(abs_git_root, ".git")):
                        self.send_response(writer, 400, b'{"error":"Git root path must contain a .git repository"}', "application/json")
                        return
                    project.git_root_path = abs_git_root
            
            if "main_file" in data:
                project.main_file = data["main_file"]

            if "compile_on_save_partial" in data:
                project.compile_on_save_partial = bool(data["compile_on_save_partial"])

            if "compile_on_save_full" in data:
                project.compile_on_save_full = bool(data["compile_on_save_full"])

            if getattr(project, "compile_on_save_full", False):
                project.compile_on_save_partial = False

            if "use_shared_memory" in data:
                project.use_shared_memory = bool(data["use_shared_memory"])

            if "model_params" in data:
                params = data["model_params"]
                if not isinstance(params, dict):
                    self.send_response(writer, 400, b'{"error":"model_params must be a JSON object"}', "application/json")
                    return
                # Reject keys with invalid characters (not letters/digits/underscores/hyphens).
                import re as _re
                for k in params.keys():
                    if not k or not _re.fullmatch(r'[A-Za-z0-9_-]+', k):
                        self.send_response(writer, 400, f'{{"error":"invalid parameter name: {k}"}}'.encode('utf-8'), "application/json")
                        return
                project.model_params = sanitize_model_params(params)

            if "worker_model_params" in data:
                params = data["worker_model_params"]
                if not isinstance(params, dict):
                    self.send_response(writer, 400, b'{"error":"worker_model_params must be a JSON object"}', "application/json")
                    return
                import re as _re
                for k in params.keys():
                    if not k or not _re.fullmatch(r'[A-Za-z0-9_-]+', k):
                        self.send_response(writer, 400, f'{{"error":"invalid parameter name: {k}"}}'.encode('utf-8'), "application/json")
                        return
                project.worker_model_params = sanitize_model_params(params)

            if "api_key" in data:
                project.api_key = data["api_key"]
            if "api_base" in data:
                project.api_base = data["api_base"]
            if "worker_api_key" in data:
                project.worker_api_key = data["worker_api_key"]
            if "worker_api_base" in data:
                project.worker_api_base = data["worker_api_base"]

            store.save(project)
            
            # Only a real model *change* pre-fetches. This endpoint patches any
            # project field -- main file, git root, compile flags -- and pulling on
            # every call downloaded gigabytes behind the user's back for edits that
            # had nothing to do with the model.
            from gilzabots.config import is_local_model
            if (
                model_changed
                and project.model
                and project.model.startswith("ollama/")
                and is_local_model(project.model, getattr(project, "api_base", ""))
            ):
                from gilzabots.ollama_manager import pull_model_in_background
                pull_model_in_background(project.model.split("ollama/", 1)[1])
            
            # Propagate the edited settings to the in-memory project. They are
            # copied onto the live object, never swapped for `project`: this
            # endpoint loaded the *default* chat, while the live project is bound
            # to the chat its last turn ran in -- possibly a turn still running.
            # Swapping it rebound that turn mid-flight, and its reply, activity and
            # working memory were written into the default chat, overwriting that
            # chat's own memory. Observed on a real "Continue" issued in a
            # secondary chat.
            import gilzabots.agent_stdin as agent_stdin
            live = agent_stdin.current_project
            if live and live.name == project.name:
                live.apply_settings_from(project)
                # A running turn keeps the orchestrator and tool context it was
                # built with (the next turn rebuilds both from the store), so they
                # are rebuilt only between turns.
                if not self._agent_turn_active():
                    live_store = agent_stdin.current_store or store
                    from .tools import set_project_context
                    set_project_context(live, live_store)
                    from .memgpt_runtime import build_chat_orchestrator
                    agent_stdin.current_memgpt = build_chat_orchestrator(live, live_store)

            from gilzabots.config import resolve_display_num_ctx

            res_data = {
                "name": project.name,
                "project_name": project.project_name,
                "project_path": project.project_path,
                "model": project.model,
                "worker_model": project.worker_model,
                "mode": project.mode,
                "description": project.description,
                "user_prompt_prefix": project.user_prompt_prefix,
                "model_params": project.model_params,
                "worker_model_params": project.worker_model_params,
                "effective_num_ctx": resolve_display_num_ctx(project.model, project.model_params),
                "api_key": getattr(project, "api_key", ""),
                "api_base": getattr(project, "api_base", ""),
                "worker_api_key": getattr(project, "worker_api_key", ""),
                "worker_api_base": getattr(project, "worker_api_base", ""),
                "current_chat_id": project.current_chat_id,
                "git_root_path": getattr(project, "git_root_path", ""),
                "git_root_warning": git_root_warning,
                "compile_on_save_partial": getattr(project, "compile_on_save_partial", True),
                "compile_on_save_full": getattr(project, "compile_on_save_full", False),
            }
            self.send_response(writer, 200, json.dumps(res_data).encode(), "application/json")

        # 6b'. Slash Command catalog (the chat composer's `/` completion menu)
        elif path == '/api/gilzabots/slash-commands' and method == 'GET':
            from gilzabots.agent_stdin import list_slash_commands
            payload = {"commands": list_slash_commands(query.get('projectPath', [''])[0])}
            self.send_response(writer, 200, json.dumps(payload).encode('utf-8'), "application/json")

        # 6c. Slash Command
        elif path == '/api/gilzabots/slash-command' and method == 'POST':
            # A slash command reloads the project for the chat it names, which is
            # the same global state a running turn persists through.
            if self._refuse_during_agent_turn(writer):
                return
            from gilzabots.agent_stdin import handle_slash_command
            try:
                result = await handle_slash_command(data)
                self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 6d. Slash Command Continue (after confirm)
        elif path == '/api/gilzabots/slash-command/continue' and method == 'POST':
            if self._refuse_during_agent_turn(writer):
                return
            from gilzabots.agent_stdin import handle_slash_command_continue
            try:
                result = await handle_slash_command_continue(data)
                self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7a. Input Response (resolves a pending GUI confirm/ask request)

        elif path == '/api/gilzabots/input_response' and method == 'POST':
            req_id = data.get("id", "")
            value = data.get("value", "")
            from gilzabots.agent_stdin import _gui_input_pending
            fut = _gui_input_pending.get(req_id)
            if fut and not fut.done():
                fut.get_loop().call_soon_threadsafe(fut.set_result, value)
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            else:
                self.send_response(writer, 404, b'{"error":"No pending request with that id"}', "application/json")
            return

        # 7a1. IDE command result (the window's answer to an agent's open_file/close_file)
        #
        # Not input_response either: that one carries a person's answer, this
        # one the application's (gilzabots/ide_control.py).
        elif path == '/api/gilzabots/ide_command_result' and method == 'POST':
            from gilzabots.ide_control import resolve_ide_command
            if resolve_ide_command(str(data.get("id") or ""), data):
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            else:
                self.send_response(writer, 404, b'{"error":"No pending IDE command with that id"}', "application/json")
            return

        # 7a2. Message to a running agent turn
        #
        # This is NOT /api/gilzabots/input_response. That endpoint answers a
        # question the agent asked and resolves a pending future; this one hands
        # the agent a message it never asked for. Routing chat text into a
        # pending `ask_question` would answer one thing with another
        # (PROJECT_DESIGN 2.7), so the two channels stay separate.
        elif path == '/api/gilzabots/message' and method == 'POST':
            from gilzabots.agent_stdin import (
                InboxScopeError,
                submit_chat_message,
            )
            from agenticblocks.blocks.llm.inbox import InboxClosedError, InboxFullError
            try:
                result = submit_chat_message(
                    data.get("content") or data.get("prompt") or "",
                    project_path=data.get("project_path") or data.get("projectPath") or "",
                    chat_id=data.get("chat_id") or "",
                    attachments=data.get("attachments") or [],
                    client_message_id=data.get("client_message_id") or "",
                    display_text=data.get("display_text") or "",
                )
                self.send_response(writer, 200, json.dumps({"status": "queued", **result}).encode(), "application/json")
            except InboxClosedError as e:
                # 409, not 200-with-a-flag: the caller must fall back to starting
                # a normal turn, and a success shape would hide that.
                self.send_response(writer, 409, json.dumps({"error": str(e), "reason": "no_active_run"}).encode(), "application/json")
            except InboxScopeError as e:
                self.send_response(writer, 409, json.dumps({"error": str(e), "reason": "scope_mismatch"}).encode(), "application/json")
            except InboxFullError as e:
                self.send_response(writer, 429, json.dumps({"error": str(e), "reason": "inbox_full"}).encode(), "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        # 7a3. Cancel a message still waiting for delivery
        elif path == '/api/gilzabots/message/cancel' and method == 'POST':
            from gilzabots.agent_stdin import cancel_chat_message
            cancelled = cancel_chat_message(data.get("item_id") or "")
            self.send_response(
                writer,
                200 if cancelled else 404,
                json.dumps({"cancelled": cancelled}).encode(),
                "application/json",
            )
            return

        # 7b. Run Agent (Streaming)
        elif path == '/api/gilzabots/run' and method == 'POST':
            # One agent turn at a time (PROJECT_DESIGN 2.6). A turn keeps its
            # project, store and GUI input hooks in module globals, so a turn
            # started while another is still alive corrupts both: the older
            # turn's cleanup removed the newer turn's hooks, and the newer
            # turn's questions went to the server console instead of the GUI.
            # A client reaches this after losing the stream of a turn that is
            # still running or still cleaning up.
            if self._refuse_during_agent_turn(writer):
                return

            from gilzabots.ui_settings import load_ui_settings
            from gilzabots.i18n import set_lang

            ui_cfg = load_ui_settings()
            request_lang = data.get("lang") or ui_cfg.get("lang", "")
            backend_lang = "pt" if (request_lang or "").startswith("pt") else "en"
            set_lang(backend_lang)


            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n\r\n"
            )
            writer.write(headers.encode('utf-8'))
            await writer.drain()

            event_queue = asyncio.Queue()
            self.active_queues.append(event_queue)

            def send_chunk(text: str):
                chunk = text.encode('utf-8')
                if not chunk:
                    return
                writer.write(f"{len(chunk):X}\r\n".encode('utf-8'))
                writer.write(chunk)
                writer.write(b"\r\n")

            from gilzabots.agent_stdin import handle_run
            from gilzabots import keep_awake

            async def run_agent():
                try:
                    await handle_run(data)
                except asyncio.CancelledError:
                    self._emit_agent_cancelled_once(agent_task, event_queue)
                except Exception as e:
                    import traceback
                    err_msg = traceback.format_exc()
                    event_queue.put_nowait({"event": "error", "message": str(e), "trace": err_msg})
                finally:
                    event_queue.put_nowait(None)

            # An agent turn is unattended work the user is waiting on, so the
            # machine must not idle-sleep through it: a suspend kills every
            # socket, and the window comes back having lost the stream of a turn
            # the server still owns. Released in `release_turn` below, which
            # runs for every way a turn can end. See gilzabots/keep_awake.py.
            keep_awake.inhibitor.acquire("GilzaBots is running an agent turn")
            agent_task = asyncio.create_task(run_agent())
            self.active_agent_task = agent_task
            self.active_agent_event_queue = event_queue
            run_project_path = data.get('projectPath') or data.get('project_path')

            def release_turn(finished_task):
                # Let the system idle again: the work the hold existed for is
                # over, however it ended (finished, failed, or interrupted).
                keep_awake.inhibitor.release()
                # The turn, not its HTTP stream, owns the active slot: a turn
                # whose client went away keeps running and releases it here.
                if self.active_agent_task is finished_task:
                    self.active_agent_task = None
                    self.active_agent_event_queue = None
                # An agent turn writes through its own file tools rather than
                # the endpoints above, so the end of the turn is the one place
                # that covers every file it touched — including a turn that was
                # interrupted, which still leaves its edits on disk.
                self._notify_cloud_change(run_project_path)

            agent_task.add_done_callback(release_turn)

            cancelled_by_event = False
            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    send_chunk(json.dumps(event) + "\n")
                    await writer.drain()
                    if event.get("event") == "cancelled":
                        # The acknowledgement reaches the UI immediately, but the
                        # stream stays alive until run_agent publishes its terminal
                        # sentinel after cancellation-safe checkpoint cleanup.
                        cancelled_by_event = True
            except asyncio.CancelledError:
                cancelled_by_event = True
                if not agent_task.done():
                    self._request_agent_cancel_once(agent_task)
            except Exception as e:
                print(f"Streaming error: {e}")
            finally:
                if event_queue in self.active_queues:
                    self.active_queues.remove(event_queue)
                # Close the HTTP chunked stream so the frontend reader unblocks.
                try:
                    writer.write(b"0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                except Exception:
                    pass
                if not agent_task.done():
                    if cancelled_by_event:
                        # Interrupted: wait for the cancellation-safe checkpoint
                        # cleanup before this request ends.
                        self._request_agent_cancel_once(agent_task)
                        try:
                            await asyncio.shield(agent_task)
                        except (asyncio.CancelledError, Exception):
                            pass
                    # Otherwise the client stream was lost (for example, the
                    # system went to sleep) while the turn is still going. That
                    # is not a reason to end the turn: it keeps waiting for a
                    # pending answer, which still arrives through
                    # /api/gilzabots/input_response, or keeps working, and it
                    # releases the active slot when it finishes.
                else:
                    try:
                        await agent_task
                    except (asyncio.CancelledError, Exception):
                        pass

        # 7b2. Interrupt Agent
        elif path == '/api/gilzabots/interrupt' and method == 'POST':
            if self.active_agent_task and not self.active_agent_task.done():
                # Emit the cancelled event to the stream queue so the frontend
                # sees the interruption message before the stream closes.
                if self.active_agent_event_queue is not None:
                    self._emit_agent_cancelled_once(
                        self.active_agent_task,
                        self.active_agent_event_queue,
                    )
                self._request_agent_cancel_once(self.active_agent_task)
                self.send_response(writer, 200, b'{"success":true,"message":"Agent execution interrupted"}', "application/json")
            else:
                self.send_response(writer, 200, b'{"success":false,"message":"No active agent running"}', "application/json")
            return

        # 7c. Terminal stream
        elif path == '/api/terminal/stream':
            term_id = query.get('term_id', ['main'])[0]
            project_path = query.get('projectPath', [None])[0]
            # Read before the local `headers` below shadows the request headers.
            # EventSource resends the id of the last event it saw; we use it to
            # replay only the scrollback the client is missing.
            try:
                last_event_id = int(headers.get('last-event-id', ''))
            except (TypeError, ValueError):
                last_event_id = None
            
            if self._is_project_terminal(term_id):
                try:
                    active_term = self._project_terminal(term_id, project_path)
                except Exception as e:
                    import traceback
                    print(f"Failed to start terminal: {e}\n{traceback.format_exc()}")
                    with open("terminal_error.log", "a", encoding="utf-8") as f:
                        f.write(f"Failed to start terminal: {e}\n{traceback.format_exc()}\n")
                    self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                    return
                if active_term is None:
                    self.send_response(writer, 400, b'{"error":"Project path required"}', "application/json")
                    return
            else:
                active_term = self.temp_terminals.get(term_id)
                if active_term is None:
                    self.send_response(writer, 404, b'{"error":"Temp terminal not found"}', "application/json")
                    return

            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n\r\n"
            )
            writer.write(headers.encode('utf-8'))
            await writer.drain()

            # Subscribe first, then snapshot the scrollback with no await in
            # between: everything up to `sent_offset` comes from the backlog and
            # everything after it from the queue, so nothing is lost or doubled.
            term_queue = asyncio.Queue()
            active_term.queues.append(term_queue)
            backlog, sent_offset = active_term.backlog_since(last_event_id)

            def send_data(data_bytes: bytes):
                # base64 is imported at the top of the module — no need to
                # re-import it here. Re-importing inside a function would
                # shadow the module-level name and cause
                # "cannot access local variable 'base64'" errors elsewhere.
                payload = (
                    f"id: {sent_offset}\n"
                    f"data: {base64.b64encode(data_bytes).decode('utf-8')}\n\n"
                )
                writer.write(payload.encode('utf-8'))

            try:
                if backlog:
                    send_data(backlog)
                    await writer.drain()
                while True:
                    data = await term_queue.get()
                    if data is None:
                        break
                    sent_offset += len(data)
                    send_data(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass  # normal client disconnect
            except OSError as e:
                if getattr(e, "winerror", None) != 10053:
                    print(f"Terminal stream error: {e}")
            except Exception as e:
                print(f"Terminal stream error: {e}")
            finally:
                if active_term and term_queue in active_term.queues:
                    active_term.queues.remove(term_queue)
                try:
                    writer.close()
                except:
                    pass

        # 7d. Terminal input (keys, resize)
        elif path == '/api/terminal/input' and method == 'POST':
            term_id = data.get("term_id", "main")
            project_path = data.get("projectPath")
            
            if self._is_project_terminal(term_id):
                try:
                    active_term = self._project_terminal(term_id, project_path)
                except Exception as e:
                    self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                    return
                if active_term is None:
                    self.send_response(writer, 400, b'{"error":"No active terminal session"}', "application/json")
                    return
            else:
                active_term = self.temp_terminals.get(term_id)
                if active_term is None:
                    self.send_response(writer, 404, b'{"error":"Temp terminal not found"}', "application/json")
                    return

            action = data.get("action", "input")
            if action == "input":
                text = data.get("text", "")
                active_term.write(text)
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            elif action == "resize":
                cols = data.get("cols", 80)
                rows = data.get("rows", 24)
                active_term.resize(cols, rows)
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            else:
                self.send_response(writer, 400, b'{"error":"Invalid action"}', "application/json")

        # 7d1. What the main terminal shows, as plain text (the agent's
        # read_terminal). Read-only: a terminal that is not running is reported,
        # never started, and one belonging to another project is not read.
        elif path == '/api/terminal/output' and method == 'GET':
            from gilzabots.terminal_manager import screen_text
            project_path = query.get('projectPath', [''])[0]
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            session = self.active_terminal
            if session is None or not session.is_running:
                self.send_response(writer, 404, b'{"error":"The main terminal is not running."}', "application/json")
                return
            if not self._same_path(session.project_path, project_path):
                self.send_response(writer, 409, json.dumps({
                    "error": f"The main terminal belongs to another project ({session.project_path})."
                }).encode('utf-8'), "application/json")
                return
            backlog, _ = session.backlog_since(None)
            self.send_response(writer, 200, json.dumps({
                "text": screen_text(backlog),
                "truncated": session.total_bytes > len(backlog),
            }).encode('utf-8'), "application/json")

        elif path == '/api/terminal/start' and method == 'POST':
            term_id = data.get("term_id")
            project_path = data.get("projectPath")
            
            if not term_id or not project_path:
                self.send_response(writer, 400, b'{"error":"term_id and projectPath required"}', "application/json")
                return
                
            from gilzabots.terminal_manager import TerminalSession
            try:
                term = TerminalSession(project_path)
                term.start_reading(asyncio.get_running_loop())
                previous = self.temp_terminals.get(term_id)
                self.temp_terminals[term_id] = term
                if previous is not None:
                    previous.close()
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, f'{{"error": "{str(e)}"}}'.encode('utf-8'), "application/json")
                
        elif path == '/api/terminal/temp/start' and method == 'POST':
            term_id = data.get("term_id")
            command = data.get("command")
            project_path = data.get("projectPath")
            
            if not term_id or not command or not project_path:
                self.send_response(writer, 400, b'{"error":"term_id, command and projectPath required"}', "application/json")
                return
                
            from gilzabots.terminal_manager import TerminalSession
            try:
                term = TerminalSession(project_path)
                term.start_reading(asyncio.get_running_loop())
                self.temp_terminals[term_id] = term
                
                # Write command to start it
                term.write(command + "\r")
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, f'{{"error": "{str(e)}"}}'.encode('utf-8'), "application/json")
                
        elif path == '/api/terminal/temp/kill' and method == 'POST':
            term_id = data.get("term_id")
            if term_id in self.temp_terminals:
                try:
                    self.temp_terminals[term_id].close()
                except:
                    pass
                del self.temp_terminals[term_id]
            self.send_response(writer, 200, b'{"ok":true}', "application/json")

        # 7e. Git status
        elif path == '/api/git/status':
            project_path = query.get('projectPath', [None])[0]
            is_shadow = query.get('shadow', ['false'])[0].lower() == 'true'
            git_root_path = query.get('gitRootPath', [None])[0]
            if not project_path or not os.path.exists(project_path):
                self.send_response(writer, 400, b'{"error":"Invalid project path"}', "application/json")
                return
            import subprocess

            # Polled every few seconds; on Windows a `git status` can take
            # seconds, so it runs in a worker thread under the exclusive-work
            # lock (see EXCLUSIVE_WORK_PATHS).
            def _collect_git_status():
                git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                _ensure_gilzabots_git_excludes(git_ctx)
                res = subprocess.run(
                    git_ctx["git_cmd"] + ["status", "--porcelain"],
                    cwd=git_ctx["cwd"],
                    capture_output=True,
                    **utf8_text_kwargs()
                )
                files = []
                for line in res.stdout.splitlines():
                    if len(line) > 3:
                        index_status = line[0]    # staged status (X in XY)
                        worktree_status = line[1]  # unstaged status (Y in XY)
                        filepath = line[3:].strip()
                        if " -> " in filepath:
                            filepath = filepath.split(" -> ")[1].strip()
                        filepath = _repo_path_to_project_path(filepath, git_ctx)
                        if _is_gilzabots_hidden_artifact(filepath):
                            continue
                        # Determine effective display status and staged flag
                        if index_status == '?' and worktree_status == '?':
                            # Untracked
                            status = '??'
                            staged = False
                        elif index_status != ' ' and index_status != '?':
                            # File has a staged change (index modified)
                            status = index_status
                            staged = True
                        else:
                            # Only unstaged changes
                            status = worktree_status
                            staged = False
                        files.append({"path": filepath, "status": status, "staged": staged})
                return files

            try:
                files = await asyncio.to_thread(_collect_git_status)
                self.send_response(writer, 200, json.dumps({"files": files, "git_available": True}).encode('utf-8'), "application/json")
            except GitContextError as e:
                self.send_response(writer, 200, json.dumps({
                    "files": [],
                    "git_available": False,
                    "error": str(e),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7f. Git commit
        elif path == '/api/git/commit' and method == 'POST':
            project_path = data.get("projectPath")
            message = data.get("message", "update")
            is_shadow = data.get("shadow", False)
            git_root_path = data.get("gitRootPath")
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath is required"}', "application/json")
                return
            def _work(respond):
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    # Commit only what is already staged (no implicit git add .)
                    res = subprocess.run(
                        git_ctx["git_cmd"] + ["commit", "-m", message],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                        **utf8_text_kwargs(),
                    )
                    if res.returncode == 0:
                        respond(200, b'{"success":true}', "application/json")
                    elif "nothing to commit" in res.stdout or "nothing added to commit" in res.stdout:
                        from gilzabots.i18n import _
                        respond(400, json.dumps({"error": _("commit_nothing")}).encode('utf-8'), "application/json")
                    else:
                        raise Exception(res.stderr or res.stdout or "Git commit failed")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)
        # 7g. Git diff (single file or full)
        elif path == '/api/git/diff':
            project_path = query.get('projectPath', [None])[0]
            file_path_param = query.get('filePath', [None])[0]
            commit_hash = query.get('commit', [None])[0]
            is_shadow = query.get('shadow', ['false'])[0].lower() == 'true'
            git_root_path = query.get('gitRootPath', [None])[0]
            if not project_path or not os.path.exists(project_path):
                self.send_response(writer, 400, b'{"error":"Invalid project path"}', "application/json")
                return
            def _work(respond):
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    git_cmd = git_ctx["git_cmd"]
                    diff = ""
                    if commit_hash:
                        verify = subprocess.run(
                            git_cmd + ["rev-parse", "--verify", f"{commit_hash}^{{commit}}"],
                            cwd=git_ctx["cwd"],
                            capture_output=True,
                            **utf8_text_kwargs(),
                        )
                        if verify.returncode != 0:
                            respond(400, b'{"error":"Invalid commit"}', "application/json")
                            return
                        end_commit_hash = query.get('endCommit', [None])[0]
                        if end_commit_hash:
                            # Range diff: net change from commit (exclusive) to endCommit (inclusive).
                            # Used to show the full diff of an agent turn (start → end).
                            verify_end = subprocess.run(
                                git_cmd + ["rev-parse", "--verify", f"{end_commit_hash}^{{commit}}"],
                                cwd=git_ctx["cwd"],
                                capture_output=True,
                                **utf8_text_kwargs(),
                            )
                            if verify_end.returncode != 0:
                                respond(400, b'{"error":"Invalid endCommit"}', "application/json")
                                return
                            diff_args = ["diff", commit_hash, end_commit_hash]
                            if file_path_param:
                                repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                                diff_args += ["--", repo_file_path]
                            res = subprocess.run(
                                git_cmd + diff_args,
                                cwd=git_ctx["cwd"],
                                capture_output=True,
                                **utf8_text_kwargs(),
                            )
                        else:
                            parent = subprocess.run(
                                git_cmd + ["rev-parse", "--verify", f"{commit_hash}^"],
                                cwd=git_ctx["cwd"],
                                capture_output=True,
                                **utf8_text_kwargs(),
                            )
                            if parent.returncode == 0:
                                diff_args = ["diff", f"{commit_hash}^", commit_hash]
                                if file_path_param:
                                    repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                                    diff_args += ["--", repo_file_path]
                                res = subprocess.run(
                                    git_cmd + diff_args,
                                    cwd=git_ctx["cwd"],
                                    capture_output=True,
                                    **utf8_text_kwargs(),
                                )
                            else:
                                show_args = ["show", "--format=", "--find-renames", commit_hash]
                                if file_path_param:
                                    repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                                    show_args += ["--", repo_file_path]
                                res = subprocess.run(
                                    git_cmd + show_args,
                                    cwd=git_ctx["cwd"],
                                    capture_output=True,
                                    **utf8_text_kwargs(),
                                )
                        diff = res.stdout

                    elif file_path_param:
                        repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                        # Check if file is untracked
                        ls = subprocess.run(
                            git_cmd + ["ls-files", "--", repo_file_path],
                            cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs()
                        )
                        full_path = os.path.join(project_path, file_path_param)
                        if not ls.stdout.strip() and os.path.isfile(full_path):
                            # Untracked file: show as new file diff
                            res = subprocess.run(
                                git_cmd + ["diff", "--no-index", "/dev/null", repo_file_path],
                                cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs()
                            )
                            diff = res.stdout
                        elif not ls.stdout.strip() and os.path.isdir(full_path):
                            diff = f"(diretório não rastreado: {file_path_param})"
                        else:
                            res = subprocess.run(git_cmd + ["diff", "--", repo_file_path], cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs())
                            res_staged = subprocess.run(git_cmd + ["diff", "--cached", "--", repo_file_path], cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs())
                            diff = res.stdout + res_staged.stdout
                    else:
                        res = subprocess.run(git_cmd + ["diff"], cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs())
                        res_staged = subprocess.run(git_cmd + ["diff", "--cached"], cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs())
                        diff = res.stdout + res_staged.stdout
                    respond(200, json.dumps({"diff": diff}).encode('utf-8'), "application/json")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 7g.0 Git checkpoint archive
        elif path == '/api/git/archive':
            project_path = query.get('projectPath', [None])[0]
            commit_hash = query.get('commit', [None])[0]
            is_shadow = query.get('shadow', ['false'])[0].lower() == 'true'
            git_root_path = query.get('gitRootPath', [None])[0]
            if not project_path or not os.path.exists(project_path):
                self.send_response(writer, 400, b'{"error":"Invalid project path"}', "application/json")
                return
            if not commit_hash:
                self.send_response(writer, 400, b'{"error":"commit is required"}', "application/json")
                return
            def _work(respond):
                import re
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    git_cmd = git_ctx["git_cmd"]
                    verify = subprocess.run(
                        git_cmd + ["rev-parse", "--verify", f"{commit_hash}^{{commit}}"],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                        **utf8_text_kwargs(),
                    )
                    if verify.returncode != 0:
                        respond(400, b'{"error":"Invalid commit"}', "application/json")
                        return
                    full_commit = verify.stdout.strip()
                    archive = subprocess.run(
                        git_cmd + ["archive", "--format=zip", full_commit],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                    )
                    if archive.returncode != 0:
                        error_text = (archive.stderr or archive.stdout or b"Git archive failed")
                        if isinstance(error_text, bytes):
                            error_text = error_text.decode("utf-8", errors="replace")
                        raise Exception(error_text)
                    project_name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.abspath(project_path))).strip("-")
                    if not project_name:
                        project_name = "project"
                    filename = f"{project_name}-checkpoint-{full_commit[:12]}.zip"
                    respond(
                        200,
                        archive.stdout,
                        "application/zip",
                        {
                            "Content-Disposition": f'attachment; filename="{filename}"',
                            "Cache-Control": "no-store",
                        },
                    )
                except GitContextError as e:
                    respond(400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 7g.1 Git restore checkpoint
        elif path == '/api/git/restore' and method == 'POST':
            project_path = data.get("projectPath")
            commit_hash = data.get("commit")
            is_shadow = data.get("shadow", False)
            git_root_path = data.get("gitRootPath")
            if not project_path or not os.path.exists(project_path):
                self.send_response(writer, 400, b'{"error":"Invalid project path"}', "application/json")
                return
            if not commit_hash:
                self.send_response(writer, 400, b'{"error":"commit is required"}', "application/json")
                return
            def _work(respond):
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    git_cmd = git_ctx["git_cmd"]
                    verify = subprocess.run(
                        git_cmd + ["rev-parse", "--verify", f"{commit_hash}^{{commit}}"],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                        **utf8_text_kwargs(),
                    )
                    if verify.returncode != 0:
                        respond(400, b'{"error":"Invalid commit"}', "application/json")
                        return
                    reset = subprocess.run(
                        git_cmd + ["reset", "--hard", commit_hash],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                        **utf8_text_kwargs(),
                    )
                    if reset.returncode != 0:
                        raise Exception(reset.stderr or reset.stdout or "Git restore failed")
                    clean = subprocess.run(
                        git_cmd + ["clean", "-fd"],
                        cwd=git_ctx["cwd"],
                        capture_output=True,
                        **utf8_text_kwargs(),
                    )
                    if clean.returncode != 0:
                        raise Exception(clean.stderr or clean.stdout or "Git clean failed")
                    respond(200, b'{"success":true}', "application/json")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 7h. Git log
        elif path == '/api/git/log':
            project_path = query.get('projectPath', [None])[0]
            def _work(respond):
                try:
                    limit = max(1, min(200, int(query.get('limit', ['20'])[0])))
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                except (TypeError, ValueError):
                    respond(400, b'{"error":"Invalid pagination parameters"}', "application/json")
                    return
                is_shadow = query.get('shadow', ['false'])[0].lower() == 'true'
                git_root_path = query.get('gitRootPath', [None])[0]
                if not project_path or not os.path.exists(project_path):
                    respond(400, b'{"error":"Invalid project path"}', "application/json")
                    return
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    res = subprocess.run(
                        git_ctx["git_cmd"] + ["log", f"--max-count={limit + 1}", f"--skip={offset}", "--pretty=format:%H|%h|%an|%ar|%s"],
                        cwd=git_ctx["cwd"], capture_output=True, **utf8_text_kwargs()
                    )
                    commits = []
                    for line in res.stdout.splitlines():
                        parts = line.split("|", 4)
                        if len(parts) == 5:
                            commits.append({"hash": parts[0], "short": parts[1], "author": parts[2], "date": parts[3], "message": parts[4]})
                    has_more = len(commits) > limit
                    respond(200, json.dumps({
                        "commits": commits[:limit],
                        "limit": limit,
                        "offset": offset,
                        "has_more": has_more,
                    }).encode('utf-8'), "application/json")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 7h.1 Shadow-git blind spots
        #      A sub-directory with its own .git is stored by the shadow
        #      repository as a gitlink, so agent edits inside it never reach a
        #      turn checkpoint and the turn is discarded as a no-op. Reported so
        #      the Review UI and the log can say why entries are missing.
        elif path == '/api/git/nested-repos':
            project_path = query.get('projectPath', [None])[0]
            if not project_path or not os.path.isdir(project_path):
                self.send_response(writer, 400, b'{"error":"Invalid project path"}', "application/json")
                return
            try:
                from gilzabots.vcs import find_nested_git_repos
                nested = await asyncio.to_thread(find_nested_git_repos, project_path)
                self.send_response(writer, 200, json.dumps({
                    "nested_repos": nested,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i. Git stage / unstage
        elif path == '/api/git/stage' and method == 'POST':
            project_path = data.get("projectPath")
            file_path_param = data.get("filePath")
            action = data.get("action", "stage")  # "stage" or "unstage"
            is_shadow = data.get("shadow", False)
            git_root_path = data.get("gitRootPath")
            if not project_path or not file_path_param:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath required"}', "application/json")
                return
            def _work(respond):
                import subprocess
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    git_cmd = git_ctx["git_cmd"]
                    if file_path_param == "__all__":
                        if action == "stage":
                            subprocess.run(git_cmd + ["add", "--all", "--", "."], cwd=git_ctx["cwd"], check=True)
                        else:
                            subprocess.run(git_cmd + ["restore", "--staged", "--", "."], cwd=git_ctx["cwd"], check=True)
                        respond(200, b'{"success":true}', "application/json")
                        return
                    repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                    if action == "stage":
                        subprocess.run(git_cmd + ["add", "--", repo_file_path], cwd=git_ctx["cwd"], check=True)
                    else:
                        subprocess.run(git_cmd + ["restore", "--staged", "--", repo_file_path], cwd=git_ctx["cwd"], check=True)
                    respond(200, b'{"success":true}', "application/json")
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

            await self._respond_from_thread(writer, _work)

        # 7j. Git discard changes
        elif path == '/api/git/discard' and method == 'POST':
            project_path = data.get("projectPath")
            file_path_param = data.get("filePath")
            is_shadow = data.get("shadow", False)
            git_root_path = data.get("gitRootPath")
            if not project_path or not file_path_param:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath required"}', "application/json")
                return
            def _work(respond):
                try:
                    git_ctx = _resolve_git_context(project_path, is_shadow, git_root_path)
                    repo_file_path = _project_path_to_repo_path(file_path_param, git_ctx)
                    _discard_git_path(git_ctx, repo_file_path)
                    respond(200, b'{"success":true}', "application/json")
                    return
                except Exception as e:
                    respond(500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                    return

            await self._respond_from_thread(writer, _work)

        # 7k. Install Optional Dependencies (Streaming)
        elif path == '/api/settings/install-dependencies' and method == 'POST':
            headers = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n\r\n"
            )
            writer.write(headers.encode('utf-8'))
            await writer.drain()

            def send_chunk(text: str):
                if len(text) < 4096:
                    text = text.rstrip('\n') + (" " * (4096 - len(text))) + "\n"
                chunk = text.encode('utf-8')
                if not chunk:
                    return
                writer.write(f"{len(chunk):X}\r\n".encode('utf-8'))
                writer.write(chunk)
                writer.write(b"\r\n")

            import sys
            import subprocess

            cmd = [sys.executable, "-m", "pip", "install", "sentence-transformers"]
            
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT
                )
                
                send_chunk(json.dumps({"status": "running", "output": f"Starting: {' '.join(cmd)}\n"}) + "\n")
                await writer.drain()
                
                while True:
                    line_bytes = await proc.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode('utf-8', errors='replace')
                    send_chunk(json.dumps({"status": "running", "output": line}) + "\n")
                    await writer.drain()
                
                await proc.wait()
                if proc.returncode == 0:
                    send_chunk(json.dumps({"status": "success", "output": "\nInstallation completed successfully!\n"}) + "\n")
                else:
                    send_chunk(json.dumps({"status": "error", "output": f"\nInstallation failed with code {proc.returncode}\n"}) + "\n")
            except Exception as e:
                send_chunk(json.dumps({"status": "error", "output": f"\nError starting installation: {e}\n"}) + "\n")
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except:
                        pass
                writer.write(b"0\r\n\r\n")
                await writer.drain()
                writer.close()
        # 7h. Check Optional Dependencies Status
        elif path == '/api/settings/check-dependencies' and method == 'GET':
            try:
                from sentence_transformers import SentenceTransformer
                installed = True
            except ImportError:
                installed = False
            self.send_response(writer, 200, json.dumps({"installed": installed}).encode('utf-8'), "application/json")
            
        # 7h2. Models Store Endpoints
        elif path == '/api/settings/models' and method == 'GET':
            from gilzabots.models_store import load_models
            from gilzabots.config import auto_num_ctx
            try:
                models = load_models()
                # What "Auto" means for each entry, so the list can say it: a
                # fixed local default, the provider's window, or unknown. Read
                # from the catalog only -- a listing never waits on a provider.
                for model in models:
                    window, source = auto_num_ctx(model.get("id"), allow_network=False)
                    model["auto_num_ctx"] = window
                    model["auto_num_ctx_source"] = source
                self.send_response(writer, 200, json.dumps({"models": models}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                
        elif path == '/api/settings/models/default' and method == 'POST':
            # Marks the global default model: what a chat runs on when neither
            # the chat nor its project names a model.
            from gilzabots.models_store import set_default_model
            try:
                model = set_default_model(str(data.get("id") or ""))
                self.send_response(writer, 200, json.dumps({"success": True, "model": model}).encode('utf-8'), "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/models/load-local-ollama' and method == 'POST':
            from gilzabots.models_store import (
                LocalOllamaNotInstalledError,
                LocalOllamaUnavailableError,
                load_local_ollama_models,
            )
            try:
                added_models = load_local_ollama_models()
                self.send_response(writer, 200, json.dumps({
                    "models": added_models,
                    "added_count": len(added_models),
                }).encode('utf-8'), "application/json")
            except LocalOllamaNotInstalledError:
                self.send_response(writer, 409, b'{"error":"ollama_not_installed"}', "application/json")
            except LocalOllamaUnavailableError:
                self.send_response(writer, 503, b'{"error":"ollama_unavailable"}', "application/json")
            except Exception:
                self.send_response(writer, 500, b'{"error":"local_ollama_load_failed"}', "application/json")
        elif path == '/api/settings/models' and method == 'POST':
            from gilzabots.models_store import add_or_update_model
            from gilzabots.context_discovery import ensure_provider_context_window
            try:
                add_or_update_model(data)
                # An "Auto" entry learns its window from the provider now, so the
                # list shows it before the model's first turn. Off the event loop:
                # the provider may take a few seconds to answer.
                await asyncio.to_thread(ensure_provider_context_window, str(data.get("id") or ""))
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                
        elif path == '/api/settings/models' and method == 'DELETE':
            from gilzabots.models_store import delete_model
            try:
                model_id = data.get("id")
                if not model_id:
                    self.send_response(writer, 400, b'{"error":"id is required"}', "application/json")
                    return
                success = delete_model(model_id)
                self.send_response(writer, 200, json.dumps({"success": success}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7h3. Provider Connection Endpoints
        elif path == '/api/settings/providers' and method == 'GET':
            from gilzabots.models_store import load_connections
            try:
                connections = load_connections()
                self.send_response(writer, 200, json.dumps({"connections": connections}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/providers' and method == 'POST':
            from gilzabots.models_store import (
                add_or_update_connection,
                ensure_default_openrouter_free_model,
                get_model,
            )
            from gilzabots.context_discovery import ensure_provider_context_window
            try:
                add_or_update_connection(data)
                added_model = None
                if str(data.get("provider") or "").strip().lower() == "openrouter":
                    added_model = ensure_default_openrouter_free_model(str(data.get("id") or ""))
                    if added_model and added_model.get("id"):
                        await asyncio.to_thread(ensure_provider_context_window, added_model["id"])
                        added_model = get_model(added_model["id"]) or added_model
                self.send_response(
                    writer,
                    200,
                    json.dumps({"success": True, "added_model": added_model}).encode('utf-8'),
                    "application/json",
                )
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/providers' and method == 'DELETE':
            from gilzabots.models_store import delete_connection
            try:
                connection_id = data.get("id")
                if not connection_id:
                    self.send_response(writer, 400, b'{"error":"id is required"}', "application/json")
                    return
                success = delete_connection(connection_id)
                self.send_response(writer, 200, json.dumps({"success": success}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/providers/openrouter/oauth' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.openrouter_oauth import begin_openrouter_authorization
            try:
                challenge = begin_openrouter_authorization(
                    label=str(data.get("label") or ""),
                    connection_id=str(data.get("connection_id") or data.get("id") or ""),
                )
                self.send_response(writer, 200, json.dumps({
                    "authorization_url": challenge.authorization_url,
                    "session": challenge.session,
                    "instructions": challenge.instructions,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/providers/openrouter/oauth/complete' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.openrouter_oauth import complete_openrouter_authorization
            session = data.get("session") or {}
            if not session:
                self.send_response(writer, 400, b'{"error":"session is required"}', "application/json")
                return
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: complete_openrouter_authorization(
                        session,
                        connection_id=str(data.get("connection_id") or data.get("id") or ""),
                        label=str(data.get("label") or ""),
                        response=data.get("response") or None,
                    ),
                )
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    **result,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/providers/openrouter/oauth/cancel' and method == 'POST':
            from gilzabots.openrouter_oauth import cancel_openrouter_authorization
            try:
                cancel_openrouter_authorization(data.get("session") or {})
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7h3. Asset Store Endpoints
        elif path == '/api/assets' and method == 'GET':
            from gilzabots.assetstore import (
                list_assets, resolve_asset_icon_path, template_conflicts,
                template_is_installed, VALID_TYPES,
            )
            asset_type = query.get('type', [None])[0]
            project_path = query.get('projectPath', [''])[0]
            if asset_type and asset_type not in VALID_TYPES:
                self.send_response(writer, 400, b'{"error":"invalid type"}', "application/json")
                return
            try:
                assets = list_assets(asset_type)
                result = []
                for a in assets:
                    entry = {
                        "id": a.get("id", ""),
                        "type": a.get("type", ""),
                        "name": a.get("name", a.get("id", "")),
                        "desc": a.get("desc", ""),
                        "version": a.get("version", ""),
                        "hasIcon": resolve_asset_icon_path(a) is not None,
                    }
                    if a.get("type") == "template" and project_path:
                        # Templates unpack at the project root, so the store has
                        # to say up front which files an install would overwrite.
                        conflicts = template_conflicts(a, project_path)
                        entry["installed"] = template_is_installed(a, project_path)
                        entry["conflicts"] = sorted(conflicts)
                    result.append(entry)
                self.send_response(writer, 200, json.dumps({"assets": result}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/assets/icon' and method == 'GET':
            from gilzabots.assetstore import list_assets, resolve_asset_icon_path
            asset_id = query.get('id', [None])[0]
            if not asset_id:
                self.send_response(writer, 400, b'{"error":"id is required"}', "application/json")
                return
            match = next((a for a in list_assets() if a.get('id') == asset_id), None)
            icon_path = resolve_asset_icon_path(match) if match else None
            if not icon_path:
                self.send_response(writer, 404, b'{"error":"icon not found"}', "application/json")
                return
            try:
                import mimetypes
                content_type, _ = mimetypes.guess_type(str(icon_path))
                with open(icon_path, 'rb') as f:
                    content = f.read()
                self.send_response(writer, 200, content, content_type or "application/octet-stream")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/assets/install' and method == 'POST':
            from gilzabots.assetstore import (
                list_assets, install_asset, template_conflicts, template_entries,
            )
            from gilzabots.skills import read_skills_yaml, write_skills_yaml, MANDATORY_SKILLS
            asset_id = data.get('id')
            asset_type = data.get('type')
            project_path = data.get('projectPath') or data.get('project_path')
            overwrite = bool(data.get('overwrite'))
            if not asset_id or not project_path:
                self.send_response(writer, 400, b'{"error":"id and projectPath are required"}', "application/json")
                return
            match = next((
                a for a in list_assets(asset_type)
                if a.get('id') == asset_id and (not asset_type or a.get('type') == asset_type)
            ), None)
            if not match:
                self.send_response(writer, 404, b'{"error":"asset not found"}', "application/json")
                return
            try:
                if match.get('type') == 'template':
                    conflicts = template_conflicts(match, project_path)
                    if conflicts and not overwrite:
                        # Refuse rather than clobber: the caller has to ask for
                        # the overwrite once it knows what it would replace.
                        self.send_response(writer, 409, json.dumps({
                            "error": "template files already exist in the project",
                            "conflicts": sorted(conflicts),
                        }).encode('utf-8'), "application/json")
                        return
                    summary = install_asset(match, project_path, overwrite=overwrite)
                    # The file count is what the UI shows as the install
                    # confirmation, so it has to be the entries actually written
                    # (archive junk excluded), not the raw zip entry count.
                    self.send_response(writer, 200, json.dumps({
                        "success": True,
                        "message": summary,
                        "files": len(template_entries(match)),
                    }).encode('utf-8'), "application/json")
                    return
                # The skills it requires come with it and are activated with it
                # (`requires` in the asset's metadata): a skill that loads one
                # the project lacks would fail only when the user runs it.
                from gilzabots.assetstore import install_skill_with_requirements, skill_requirements
                installed = install_skill_with_requirements(match, project_path)
                skill_name = match.get('name', asset_id)
                declared = read_skills_yaml(project_path) or []
                # Every requirement is activated, including one the project
                # already had and that was therefore not installed again.
                names = [str(r.get('name') or r.get('id')) for r in skill_requirements(match)] + [skill_name]
                for name in names:
                    if name not in declared and name not in MANDATORY_SKILLS:
                        declared.append(name)
                write_skills_yaml(project_path, declared)
                summary = "; ".join(message for _, message in installed)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "message": summary,
                    "installed": [name for name, _ in installed],
                    "skill": skill_name,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/skills' and method == 'GET':
            from gilzabots.skills import (
                discover_skills, active_skills, resolve_skill_icon_path, MANDATORY_SKILLS,
                local_skill_dir, shadowed_skill_dirs,
            )
            from gilzabots.assetstore import list_assets, asset_matches_install
            project_path = query.get('projectPath', [''])[0]
            try:
                discovered = discover_skills(project_path)
                active_names = {s['name'] for s in active_skills(project_path)}
                assets_by_name = {a.get('name'): a for a in list_assets('skill') if a.get('name')}
                result = []
                for s in discovered:
                    name = s["name"]
                    # A project-local copy shadows every other search dir, so it is
                    # the one running and the only one an update can refresh.
                    installed_locally = bool(project_path) and local_skill_dir(name, project_path) is not None
                    asset = assets_by_name.get(name) if installed_locally else None
                    result.append({
                        "name": name,
                        "description": s["description"],
                        "active": name in active_names,
                        "mandatory": name in MANDATORY_SKILLS,
                        "hasIcon": resolve_skill_icon_path(s) is not None,
                        "installedLocally": installed_locally,
                        "updatable": asset is not None,
                        "outdated": asset is not None and not asset_matches_install(asset, project_path),
                        "shadowsBundled": installed_locally and bool(shadowed_skill_dirs(name, project_path)),
                    })
                self.send_response(writer, 200, json.dumps({"skills": result}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/skills/update' and method == 'POST':
            # Refresh a project-local copy from the catalog asset it came from.
            from gilzabots.skills import local_skill_dir
            from gilzabots.assetstore import list_assets, install_asset
            project_path = data.get('projectPath') or data.get('project_path')
            skill_name = data.get('name')
            if not project_path or not skill_name:
                self.send_response(writer, 400, b'{"error":"projectPath and name are required"}', "application/json")
                return
            if local_skill_dir(skill_name, project_path) is None:
                self.send_response(writer, 404, json.dumps({
                    "error": f"skill '{skill_name}' has no project-local copy to update."
                }).encode('utf-8'), "application/json")
                return
            asset = next((a for a in list_assets('skill') if a.get('name') == skill_name), None)
            if asset is None:
                # No catalog source: refusing is honest, and restore-bundled is the
                # action that actually applies to this skill.
                self.send_response(writer, 404, json.dumps({
                    "error": f"skill '{skill_name}' is not in the catalog, so there is nothing to update it from."
                }).encode('utf-8'), "application/json")
                return
            try:
                message = install_asset(asset, project_path, replace=True)
                self.send_response(writer, 200, json.dumps({"success": True, "message": message}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/skills/restore-bundled' and method == 'POST':
            # Drop a project-local copy so the bundled skill it shadows runs again.
            import shutil
            from gilzabots.skills import local_skill_dir, shadowed_skill_dirs
            project_path = data.get('projectPath') or data.get('project_path')
            skill_name = data.get('name')
            if not project_path or not skill_name:
                self.send_response(writer, 400, b'{"error":"projectPath and name are required"}', "application/json")
                return
            local_dir = local_skill_dir(skill_name, project_path)
            if local_dir is None:
                self.send_response(writer, 404, json.dumps({
                    "error": f"skill '{skill_name}' has no project-local copy."
                }).encode('utf-8'), "application/json")
                return
            shadowed = shadowed_skill_dirs(skill_name, project_path)
            if not shadowed:
                self.send_response(writer, 409, json.dumps({
                    "error": (
                        f"skill '{skill_name}' exists only as this local copy; removing it "
                        "would delete the skill instead of restoring a bundled version."
                    )
                }).encode('utf-8'), "application/json")
                return
            try:
                shutil.rmtree(local_dir)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "message": f"local copy of '{skill_name}' removed; now running from {shadowed[0]}",
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/skills/icon' and method == 'GET':
            from gilzabots.skills import discover_skills, resolve_skill_icon_path
            project_path = query.get('projectPath', [''])[0]
            skill_name = query.get('name', [None])[0]
            if not skill_name:
                self.send_response(writer, 400, b'{"error":"name is required"}', "application/json")
                return
            match = next((s for s in discover_skills(project_path) if s['name'] == skill_name), None)
            icon_path = resolve_skill_icon_path(match) if match else None
            if not icon_path:
                self.send_response(writer, 404, b'{"error":"icon not found"}', "application/json")
                return
            try:
                import mimetypes
                content_type, _ = mimetypes.guess_type(icon_path)
                with open(icon_path, 'rb') as f:
                    content = f.read()
                self.send_response(writer, 200, content, content_type or "application/octet-stream")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/skills/activate' and method == 'POST':
            from gilzabots.skills import add_skill_to_project
            project_path = data.get('projectPath') or data.get('project_path')
            skill_name = data.get('name')
            if not project_path or not skill_name:
                self.send_response(writer, 400, b'{"error":"projectPath and name are required"}', "application/json")
                return
            changed, message = add_skill_to_project(project_path, skill_name)
            self.send_response(writer, 200, json.dumps({"success": changed, "message": message}).encode('utf-8'), "application/json")

        elif path == '/api/skills/deactivate' and method == 'POST':
            from gilzabots.skills import remove_skill_from_project
            project_path = data.get('projectPath') or data.get('project_path')
            skill_name = data.get('name')
            if not project_path or not skill_name:
                self.send_response(writer, 400, b'{"error":"projectPath and name are required"}', "application/json")
                return
            changed, message = remove_skill_from_project(project_path, skill_name)
            self.send_response(writer, 200, json.dumps({"success": changed, "message": message}).encode('utf-8'), "application/json")

        # 7i. Problems scan
        elif path == '/api/gilzabots/problems' and method == 'GET':
            project_path = query.get('projectPath', [None])[0]
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            if not os.path.exists(project_path) or not os.path.isdir(project_path):
                self.send_response(writer, 404, b'{"error":"Directory not found"}', "application/json")
                return
            try:
                self.send_response(writer, 200, json.dumps({"problems": []}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")


        # 7i2. Cloud sync — status for one project (providers, connection, last pass)
        elif path == '/api/cloud/status' and method == 'GET':
            from gilzabots.cloud.service import MANAGER, status_for
            project_path = query.get('projectPath', [None])[0]
            project_name = query.get('project', [''])[0]
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            try:
                status = status_for(project_path, project_name)
                # Opening a project is what starts (or stops) its background
                # sync task: the front-end polls this endpoint when a project
                # becomes active, so no separate lifecycle hook is needed.
                if project_name:
                    MANAGER.activate(project_name, project_path)
                status["syncing"] = MANAGER.is_running(project_path)
                last = MANAGER.last_outcome(project_path)
                status["last_outcome"] = last.to_dict() if last else None
                self.send_response(writer, 200, json.dumps(status).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i2a. Cloud sync — does this project have a copy in the cloud? Asked
        # by the delete dialog; reads local state only, never the network.
        elif path == '/api/cloud/link' and method == 'GET':
            from gilzabots.cloud.service import cloud_link
            project_path = query.get('projectPath', [None])[0]
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            try:
                self.send_response(writer, 200, json.dumps(cloud_link(project_path)).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i2b. Cloud sync — per-file state for the explorer's badges
        elif path == '/api/cloud/file-states' and method == 'GET':
            from gilzabots.cloud.service import MANAGER, file_states
            project_path = query.get('projectPath', [None])[0]
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath parameter is required"}', "application/json")
                return
            try:
                last = MANAGER.last_outcome(project_path)
                conflicts = [
                    conflict.rel_path
                    for conflict in ((last.report.conflicts if last and last.report else []) or [])
                ]
                # Scanning the tree is disk work, so it stays off the event loop.
                payload = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: file_states(project_path, conflicts)
                )
                self.send_response(writer, 200, json.dumps(payload).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i3. Cloud sync — update a project's settings
        elif path == '/api/cloud/settings' and method == 'POST':
            from gilzabots.cloud.service import MANAGER, status_for, update_settings
            project_path = data.get('projectPath')
            project_name = data.get('project', '')
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath is required"}', "application/json")
                return
            try:
                update_settings(project_path, data.get('settings') or {})
                if project_name:
                    MANAGER.activate(project_name, project_path)
                else:
                    MANAGER.deactivate(project_path)
                self.send_response(writer, 200, json.dumps(status_for(project_path, project_name)).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i4. Cloud sync — list backends
        elif path == '/api/cloud/providers' and method == 'GET':
            from gilzabots.cloud.registry import list_providers
            try:
                payload = [
                    {
                        "id": info.id,
                        "display_name": info.display_name,
                        "requires_authorization": info.requires_authorization,
                        "available": info.available,
                        "unavailable_reason": info.unavailable_reason,
                    }
                    for info in list_providers()
                ]
                self.send_response(writer, 200, json.dumps({"providers": payload}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i5. Cloud sync — which Google OAuth client this installation connects
        # with. Normally the one shipped with the build, so the user only ever
        # presses Connect; the custom fields are the advanced override.
        elif path == '/api/cloud/google-client' and method == 'GET':
            from gilzabots.cloud.providers.google_drive import (
                describe_client_config,
                load_bundled_client_config,
                load_user_client_config,
            )
            try:
                effective = describe_client_config()
                custom = load_user_client_config()
                # No secret is ever returned. They are write-only from the UI's
                # point of view: showing one back would put a credential in the
                # DOM of every settings screen for no benefit.
                self.send_response(writer, 200, json.dumps({
                    "client_id": effective.get("client_id", ""),
                    "has_client_secret": bool(effective.get("client_secret")),
                    "source": effective.get("source", "none"),
                    "configured": bool(effective.get("client_id")),
                    "bundled_available": bool(load_bundled_client_config().get("client_id")),
                    "custom_client_id": custom.get("client_id", ""),
                    "has_custom_client_secret": bool(custom.get("client_secret")),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/cloud/google-client' and method == 'POST':
            from gilzabots.cloud.providers.google_drive import (
                load_user_client_config,
                save_client_config,
            )
            try:
                client_id = str(data.get('client_id', '') or '').strip()
                if not client_id:
                    self.send_response(writer, 400, json.dumps({
                        "error": "client_id is required. Use DELETE to go back to the client shipped with GilzaBots."
                    }).encode('utf-8'), "application/json")
                    return
                # An empty secret keeps whatever is stored, so the user can
                # correct the client id without re-entering the secret they
                # cannot read back.
                client_secret = str(data.get('client_secret', '') or '')
                if not client_secret:
                    client_secret = load_user_client_config().get('client_secret', '')
                save_client_config(client_id, client_secret)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # Drop the override and go back to the client the build ships with.
        elif path == '/api/cloud/google-client' and method == 'DELETE':
            from gilzabots.cloud.providers.google_drive import clear_client_config
            try:
                clear_client_config()
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i6. Cloud sync — start an authorization (opens the system browser)
        elif path == '/api/cloud/connect' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.registry import get_cloud_provider
            provider_id = str(data.get('provider', '') or '')
            if not provider_id:
                self.send_response(writer, 400, b'{"error":"provider is required"}', "application/json")
                return
            try:
                provider = get_cloud_provider(provider_id, data.get('config') or {})
                challenge = provider.begin_authorization()
                self.send_response(writer, 200, json.dumps({
                    "authorization_url": challenge.authorization_url,
                    "session": challenge.session,
                    "instructions": challenge.instructions,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i7. Cloud sync — wait for the redirect and exchange the code
        elif path == '/api/cloud/connect/complete' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.registry import get_cloud_provider
            provider_id = str(data.get('provider', '') or '')
            session = data.get('session') or {}
            if not provider_id or not session:
                self.send_response(writer, 400, b'{"error":"provider and session are required"}', "application/json")
                return
            try:
                provider = get_cloud_provider(provider_id, data.get('config') or {})
                # The exchange blocks until the user finishes in the browser, so
                # it runs off the event loop: everything else in the IDE — the
                # editor, the compiler, a running agent — has to stay responsive
                # while that tab is open.
                auth = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: provider.complete_authorization(session, data.get('response') or None)
                )
                self.send_response(writer, 200, json.dumps({
                    "connected": auth.connected,
                    "account": auth.account,
                    "error": auth.error,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i8. Cloud sync — abandon a pending authorization
        elif path == '/api/cloud/connect/cancel' and method == 'POST':
            from gilzabots.cloud import oauth as cloud_oauth
            try:
                cloud_oauth.cancel_authorization(data.get('session') or {})
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i9. Cloud sync — forget stored credentials
        elif path == '/api/cloud/disconnect' and method == 'POST':
            from gilzabots.cloud.registry import get_cloud_provider
            provider_id = str(data.get('provider', '') or '')
            if not provider_id:
                self.send_response(writer, 400, b'{"error":"provider is required"}', "application/json")
                return
            try:
                get_cloud_provider(provider_id, data.get('config') or {}).revoke()
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i9b. Cloud sync — settle one conflicted file the way the user chose
        elif path == '/api/cloud/resolve-conflict' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.service import MANAGER, resolve_conflict, status_for
            project_path = data.get('projectPath')
            project_name = data.get('project', '')
            rel_path = str(data.get('path', '') or '')
            resolution = str(data.get('resolution', '') or '')
            if not project_path or not rel_path:
                self.send_response(writer, 400, b'{"error":"projectPath and path are required"}', "application/json")
                return
            try:
                # Provider I/O blocks, so it stays off the event loop like a pass.
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: resolve_conflict(
                        project_path,
                        rel_path,
                        resolution,
                        conflict_copy=str(data.get('conflict_copy', '') or ''),
                    ),
                )
                status = status_for(project_path, project_name)
                status["syncing"] = MANAGER.is_running(project_path)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "resolved": result,
                    "status": status,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except OSError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i10. Cloud sync — run a pass now
        elif path == '/api/cloud/sync' and method == 'POST':
            from gilzabots.cloud.engine import PULL, PUSH, TWO_WAY
            from gilzabots.cloud.service import MANAGER
            project_path = data.get('projectPath')
            project_name = data.get('project', '')
            if not project_path or not project_name:
                self.send_response(writer, 400, b'{"error":"projectPath and project are required"}', "application/json")
                return
            direction = str(data.get('direction', TWO_WAY) or TWO_WAY)
            if direction not in (TWO_WAY, PUSH, PULL):
                self.send_response(writer, 400, json.dumps({
                    "error": f"Unknown direction {direction!r}. Use one of: {TWO_WAY}, {PUSH}, {PULL}."
                }).encode('utf-8'), "application/json")
                return
            try:
                outcome = await MANAGER.sync_now(
                    project_name,
                    project_path,
                    direction=direction,
                    dry_run=bool(data.get('dryRun')),
                    # Deleting a large share of the working copy needs an
                    # explicit confirmation from the user, relayed here.
                    allow_bulk_delete=bool(data.get('allowBulkDelete')),
                )
                self.send_response(writer, 200, json.dumps(outcome.to_dict()).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i10b. Cloud sync — stop the pass running for a path. Covers a sync,
        # a preview and a download alike: each reports progress under the path
        # it writes to, which is what the caller names here.
        elif path == '/api/cloud/cancel' and method == 'POST':
            from gilzabots.cloud.service import request_cancel
            project_path = data.get('projectPath')
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath is required"}', "application/json")
                return
            try:
                requested = request_cancel(project_path)
                self.send_response(writer, 200, json.dumps({
                    "cancel_requested": requested,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i11. Cloud sync — drop the baseline and reconcile from scratch
        elif path == '/api/cloud/reset' and method == 'POST':
            from gilzabots.cloud.state import reset_baseline
            project_path = data.get('projectPath')
            if not project_path:
                self.send_response(writer, 400, b'{"error":"projectPath is required"}', "application/json")
                return
            try:
                reset_baseline(project_path)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i11b. Cloud sync — is this backend usable right now? Project-scoped
        # status needs a project; downloading one is exactly the case where
        # there is not one yet.
        elif path == '/api/cloud/auth-status' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.registry import get_cloud_provider
            provider_id = str(data.get('provider', '') or '')
            if not provider_id:
                self.send_response(writer, 400, b'{"error":"provider is required"}', "application/json")
                return
            try:
                backend = get_cloud_provider(provider_id, data.get('config') or {})
                # Refreshing a token is a network round-trip.
                auth = await asyncio.get_running_loop().run_in_executor(
                    None, backend.auth_status
                )
                self.send_response(writer, 200, json.dumps({
                    "connected": auth.connected,
                    "account": auth.account,
                    "error": auth.error,
                }).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 200, json.dumps({
                    "connected": False, "account": "", "error": str(e),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i12. Cloud sync — list the projects already mirrored in an account,
        # so a second machine can find a project it has never seen.
        elif path == '/api/cloud/remote-projects' and method == 'POST':
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.service import list_remote_projects
            from gilzabots.cloud.state import load_state
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore
            provider_id = str(data.get('provider', '') or '')
            if not provider_id:
                self.send_response(writer, 400, b'{"error":"provider is required"}', "application/json")
                return
            try:
                config = data.get('config') or {}
                # Listing a Drive account is a network round-trip.
                remote_projects = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: list_remote_projects(provider_id, config)
                )

                # A project already mirrored on this machine must not be offered
                # as a download: cloning it a second time would give the same
                # remote folder two local working copies fighting over it.
                mirrored = {}
                for entry in ProjectStore(db_path=DEFAULT_DB_PATH).list_projects():
                    local_path = entry.get("project_path") or ""
                    if not local_path or not os.path.isdir(local_path):
                        continue
                    settings = load_state(local_path).settings
                    if settings.provider != provider_id or not settings.remote_folder:
                        continue
                    mirrored[settings.remote_folder] = {
                        "name": entry.get("name", ""),
                        "project_name": entry.get("project_name", "") or entry.get("name", ""),
                        "project_path": local_path,
                    }

                payload = [
                    {
                        "name": project.name,
                        "root": project.root,
                        "modified_at": project.modified_at,
                        "local": mirrored.get(project.name),
                    }
                    for project in remote_projects
                ]
                self.send_response(writer, 200, json.dumps({"projects": payload}).encode('utf-8'), "application/json")
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7i13. Cloud sync — download a mirrored project onto this machine and
        # register it, so the same project can be continued from here.
        elif path == '/api/cloud/clone' and method == 'POST':
            from gilzabots.cloud import chats as cloud_chats
            from gilzabots.cloud.base import CloudError
            from gilzabots.cloud.service import MANAGER, clone_project, prepare_destination
            from gilzabots.config import DEFAULT_DB_PATH
            from gilzabots.project import ProjectStore

            provider_id = str(data.get('provider', '') or '')
            remote_name = str(data.get('name', '') or '').strip()
            parent_path = str(data.get('parentPath', '') or '').strip()
            folder_name = str(data.get('folderName', '') or '').strip() or remote_name
            if not provider_id or not remote_name or not parent_path:
                self.send_response(writer, 400, json.dumps({
                    "error": "provider, name and parentPath are required"
                }).encode('utf-8'), "application/json")
                return
            if (os.sep in folder_name or (os.altsep and os.altsep in folder_name)
                    or folder_name in {".", ".."}):
                self.send_response(writer, 400, json.dumps({
                    "error": "folderName must be a single folder name, not a path."
                }).encode('utf-8'), "application/json")
                return

            store = ProjectStore(db_path=DEFAULT_DB_PATH)
            destination = os.path.join(os.path.abspath(os.path.expanduser(parent_path)), folder_name)

            # Both checks happen before anything is transferred: a download that
            # lands somewhere it cannot be registered is wasted work.
            existing_name = store.find_by_path(destination)
            if existing_name:
                existing_proj = store.load(existing_name)
                existing_project_name = existing_proj.project_name if existing_proj else existing_name
                err_msg = get_translation("project_exists_in_folder", name=existing_project_name)
                self.send_response(writer, 400, json.dumps({"error": err_msg}).encode('utf-8'), "application/json")
                return
            # Recorded before validation creates the folder, so a cancelled
            # download removes a folder it created and keeps one the user made.
            destination_is_new = not os.path.exists(destination)
            try:
                prepare_destination(destination)
            except CloudError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            try:
                outcome = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: clone_project(
                        provider_id,
                        remote_name,
                        destination,
                        provider_config=data.get('config') or {},
                        remote_root=str(data.get('root', '') or ''),
                        settings_overrides=data.get('settings') or {},
                        destination_is_new=destination_is_new,
                    ),
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            if outcome.cancelled and not outcome.error:
                # The user stopped it; the partial copy is already gone, so
                # this is not an error and there is nothing to register.
                self.send_response(writer, 200, json.dumps({
                    "cancelled": True,
                    "project_path": outcome.project_path,
                    "partial_copy_removed": outcome.partial_copy_removed,
                }).encode('utf-8'), "application/json")
                return

            if not outcome.ok:
                # Nothing is registered when the download fails. Whatever
                # arrived is left on disk to be inspected rather than deleted,
                # which does mean the folder is no longer empty — so the message
                # says where a retry has to go instead of letting the user hit
                # "not empty" on the second attempt without knowing why.
                report = outcome.report.to_dict() if outcome.report else None
                message = outcome.error or (outcome.report.aborted if outcome.report else "")
                if not message and outcome.report and outcome.report.errors:
                    message = outcome.report.errors[0][1]
                message = message or "The download did not complete."
                if outcome.report is not None and not outcome.cancelled:
                    message += (
                        f" A partial copy is in {outcome.project_path}; remove that "
                        f"folder or choose another name before trying again."
                    )
                self.send_response(writer, 400, json.dumps({
                    "error": message,
                    "project_path": outcome.project_path,
                    "report": report,
                }).encode('utf-8'), "application/json")
                return

            # The project's own skill set travelled with its files; the row has
            # to agree with the skills.yaml that was just downloaded.
            skills = ["gilzabots"]
            try:
                from gilzabots.skills import read_skills_yaml
                found_skills = read_skills_yaml(destination)
                if found_skills:
                    skills = found_skills if "gilzabots" in found_skills else ["gilzabots"] + found_skills
            except Exception:
                pass

            # Reuse the internal name the project has on the machine it came
            # from when it is free here. Reserved chat ids are derived from it
            # (`tutorial_<name>`), so under an arbitrary new key the tutorial
            # button would open a second tutorial beside the imported one.
            export = cloud_chats.read_export(destination) or {}
            preferred = str(export.get("project", "") or "").strip()
            db_key = preferred if preferred and not store.exists(preferred) else ""
            if not db_key:
                db_key = folder_name.replace(" ", "_").lower()
                original_db_key = db_key
                counter = 1
                while store.exists(db_key):
                    db_key = f"{original_db_key}_{counter}"
                    counter += 1

            try:
                project = store.create(
                    name=db_key,
                    mode="auto",
                    model="",
                    project_name=folder_name,
                    project_path=destination,
                    skills=skills,
                    description="",
                    preserve_existing_skills=True,
                )
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            # The conversations arrived as a file; this folds them into the
            # local database. `adopt_settings` is right here and nowhere else:
            # the row was created seconds ago, so last-writer-wins would always
            # prefer the empty local settings over the ones that were synced.
            merge = None
            try:
                merge = cloud_chats.apply_export(db_key, destination, adopt_settings=True)
                # Conversations held only in another device's journal (the
                # mobile app) that no computer has folded into the export yet.
                merge.absorb(cloud_chats.apply_device_journals(db_key, destination))
            except Exception as e:
                print(f"[cloud] Failed to import the project's conversations: {e}")

            MANAGER.activate(db_key, destination)

            reloaded = store.load(db_key) or project
            # The model came from the other machine's settings, so this is the
            # same "the project's model changed" moment create-project handles:
            # without the pre-fetch the first message fails on a model that was
            # never downloaded here. The helper skips models Ollama already has.
            if reloaded.model and reloaded.model.startswith("ollama/"):
                from gilzabots.config import is_local_model
                if is_local_model(reloaded.model, reloaded.api_base):
                    from gilzabots.ollama_manager import pull_model_in_background
                    pull_model_in_background(reloaded.model.split("ollama/", 1)[1])

            self.send_response(writer, 200, json.dumps({
                "name": reloaded.name,
                "project_name": reloaded.project_name,
                "project_path": reloaded.project_path,
                "skills": reloaded.skills,
                "model": reloaded.model,
                "report": outcome.report.to_dict() if outcome.report else None,
                "merge": merge.to_dict() if merge else None,
            }).encode('utf-8'), "application/json")

        # 7i14. Open a URL in the desktop's default browser.
        # The UI runs in a pywebview window with no createWindow handler, where
        # a `target="_blank"` link does nothing at all — so an authorization URL
        # the user is asked to follow has to be opened from here.
        elif path == '/api/system/open-url' and method == 'POST':
            import webbrowser
            from urllib.parse import urlparse
            url = str(data.get('url', '') or '').strip()
            scheme = urlparse(url).scheme.lower() if url else ""
            # Only web URLs. Handing an arbitrary scheme to the desktop opener
            # would turn this into "run whatever the page asks for".
            if scheme not in {"http", "https"}:
                self.send_response(writer, 400, json.dumps({
                    "error": "Only http and https URLs can be opened."
                }).encode('utf-8'), "application/json")
                return
            try:
                # Launching a browser can block while the desktop portal answers.
                opened = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: bool(webbrowser.open(url, new=1, autoraise=True))
                )
                self.send_response(writer, 200, json.dumps({"opened": opened}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 200, json.dumps({
                    "opened": False, "error": str(e),
                }).encode('utf-8'), "application/json")

        # 7j. Web search config — GET
        elif path == '/api/settings/web-search' and method == 'GET':
            from gilzabots.web_search_config import load_config
            try:
                cfg = load_config()
                self.send_response(writer, 200, json.dumps(cfg).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k. Web search config — POST (save)
        elif path == '/api/settings/web-search' and method == 'POST':
            from gilzabots.web_search_config import save_config
            try:
                save_config(data)
                self.send_response(writer, 200, b'{"success":true}', "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k2. Image generation config — GET
        elif path == '/api/settings/image-generation' and method == 'GET':
            from gilzabots.image_gen_config import load_config
            from gilzabots.models_store import IMAGE_ROUTES, list_image_generation_models
            try:
                cfg = dict(load_config())
                cfg["models"] = [
                    {
                        "id": m.get("id", ""),
                        "name": m.get("name", ""),
                        "provider": m.get("provider", ""),
                        "image_route": m.get("image_route", ""),
                        "connection_label": m.get("connection_label", ""),
                    }
                    for m in list_image_generation_models()
                ]
                cfg["routes"] = list(IMAGE_ROUTES)
                self.send_response(writer, 200, json.dumps(cfg).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k3. Image generation config — POST (save)
        elif path == '/api/settings/image-generation' and method == 'POST':
            from gilzabots.image_gen_config import load_config, save_config
            try:
                save_config(data)
                self.send_response(writer, 200, json.dumps({"success": True, **load_config()}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k4. Speech synthesis config — GET
        elif path == '/api/settings/speech' and method == 'GET':
            from gilzabots.speech_config import (
                ENGINES, SUPPORTED_FORMATS, configuration_problem, load_config,
            )
            from gilzabots.models_store import SPEECH_ROUTES, list_speech_synthesis_models
            try:
                cfg = dict(load_config())
                cfg["models"] = [
                    {
                        "id": m.get("id", ""),
                        "name": m.get("name", ""),
                        "provider": m.get("provider", ""),
                        "speech_route": m.get("speech_route", ""),
                        "connection_label": m.get("connection_label", ""),
                    }
                    for m in list_speech_synthesis_models()
                ]
                cfg["routes"] = list(SPEECH_ROUTES)
                cfg["formats"] = list(SUPPORTED_FORMATS)
                cfg["engines"] = list(ENGINES)
                # The viewers read this to decide whether to offer "Pronounce"
                # at all. An empty string means it is ready; anything else is
                # the reason, shown on the disabled item rather than discovered
                # by pressing it and getting nothing.
                cfg["problem"] = configuration_problem()
                self.send_response(writer, 200, json.dumps(cfg).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k5. Speech synthesis config — POST (save)
        elif path == '/api/settings/speech' and method == 'POST':
            from gilzabots.speech_config import configuration_problem, load_config, save_config
            try:
                save_config(data)
                self.send_response(writer, 200, json.dumps({
                    "success": True, **load_config(), "problem": configuration_problem(),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k6. Downloadable local voices (Piper) — list / download / remove
        elif path.startswith('/api/speech/voices'):
            from gilzabots import speech_config, voice_store
            from gilzabots.voice_store import VoiceStoreError

            def _voice_error(exc):
                status = {
                    "bad_request": 400, "not_found": 404,
                    "connection": 503, "corrupt": 502,
                }.get(getattr(exc, "kind", "unknown"), 500)
                self.send_response(writer, status, json.dumps({
                    "error": str(exc), "kind": getattr(exc, "kind", "unknown"),
                }).encode('utf-8'), "application/json")

            if path == '/api/speech/voices' and method == 'GET':
                from agenticblocks.blocks.speech import availability_problem
                refresh = query.get('refresh', ['0'])[0] in ('1', 'true')
                installed = voice_store.installed_voices()
                try:
                    catalog = voice_store.list_catalog(refresh=refresh)
                    catalog_error = ""
                except VoiceStoreError as e:
                    # Being offline must not empty the list of what is already
                    # installed — the local engine works without a network.
                    catalog, catalog_error = [], str(e)
                self.send_response(writer, 200, json.dumps({
                    "catalog": catalog,
                    "catalog_error": catalog_error,
                    "installed": installed,
                    "selected": str(speech_config.load_config().get("local_voice", "") or ""),
                    "downloads": voice_store.download_progress(),
                    # The phonemizer is the one piece the app cannot install for
                    # the user (GPL-3.0, used from the system, never bundled).
                    "phonemizer_problem": availability_problem(),
                }).encode('utf-8'), "application/json")
                return

            if path == '/api/speech/voices/download' and method == 'POST':
                key = str(data.get("key") or "").strip()
                if voice_store.is_downloading(key):
                    self.send_response(writer, 409, json.dumps({
                        "error": f"'{key}' is already downloading.", "kind": "busy",
                    }).encode('utf-8'), "application/json")
                    return
                try:
                    # 63 MB off the event loop: this server runs one loop for
                    # everything, so a blocking read here would freeze the IDE.
                    summary = await asyncio.to_thread(voice_store.download_voice, key)
                    self.send_response(writer, 200, json.dumps({
                        "success": True, "voice": summary,
                        "installed": voice_store.installed_voices(),
                    }).encode('utf-8'), "application/json")
                except VoiceStoreError as e:
                    _voice_error(e)
                except Exception as e:
                    self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            if path == '/api/speech/voices/remove' and method == 'POST':
                try:
                    key = str(data.get("key") or "").strip()
                    voice_store.remove_voice(key)
                    voice_store.clear_download_progress(key)
                    # A configuration pointing at a voice that is gone would
                    # fail at synthesis time; clear it here instead.
                    cfg = speech_config.load_config()
                    if str(cfg.get("local_voice", "") or "") == key:
                        speech_config.save_config({**cfg, "local_voice": ""})
                    self.send_response(writer, 200, json.dumps({
                        "success": True, "installed": voice_store.installed_voices(),
                    }).encode('utf-8'), "application/json")
                except VoiceStoreError as e:
                    _voice_error(e)
                except Exception as e:
                    self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
                return

            self.send_response(writer, 404, b'{"error":"unknown voices endpoint"}', "application/json")
            return

        # 7k7. Dictation settings — GET / POST
        elif path == '/api/settings/transcription' and method == 'GET':
            from gilzabots import transcription_config as tc
            try:
                cfg = dict(tc.load_config())
                cfg["models"] = tc.catalog()
                cfg["compute_types"] = list(tc.COMPUTE_TYPES)
                cfg["downloads"] = tc.download_progress()
                # The composer reads this to decide whether to offer the
                # microphone at all; "" means ready.
                cfg["problem"] = tc.configuration_problem()
                self.send_response(writer, 200, json.dumps(cfg).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/transcription' and method == 'POST':
            from gilzabots import transcription_config as tc
            try:
                tc.save_config(data)
                self.send_response(writer, 200, json.dumps({
                    "success": True, **tc.load_config(),
                    "problem": tc.configuration_problem(),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k8. Transcription models — download / remove
        elif path == '/api/transcription/models' and method == 'POST':
            from gilzabots import transcription_config as tc
            from gilzabots.transcription_config import ModelStoreError

            action = str(data.get("action") or "download").strip()
            size = str(data.get("size") or "").strip()
            try:
                if action == "remove":
                    tc.remove_model(size)
                else:
                    if tc.is_downloading(size):
                        self.send_response(writer, 409, json.dumps({
                            "error": f"'{size}' is already downloading.", "kind": "busy",
                        }).encode('utf-8'), "application/json")
                        return
                    # Hundreds of megabytes off the event loop: this server runs
                    # one loop for everything, so a blocking read would freeze
                    # the IDE for the whole transfer.
                    await asyncio.to_thread(tc.download_model, size)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "models": tc.catalog(),
                    "problem": tc.configuration_problem(),
                }).encode('utf-8'), "application/json")
            except ModelStoreError as e:
                status = {
                    "bad_request": 400, "not_found": 404, "not_configured": 503,
                    "connection": 503, "corrupt": 502,
                }.get(getattr(e, "kind", "unknown"), 500)
                self.send_response(writer, status, json.dumps({
                    "error": str(e), "kind": getattr(e, "kind", "unknown"),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        # 7k9. Dictation — the recording arrives as a raw 16 kHz mono WAV body
        elif path == '/api/transcribe' and method == 'POST':
            from agenticblocks.blocks.transcription import TranscriptionError
            from gilzabots import transcription_config as tc
            from gilzabots.transcription import execute_transcription

            problem = tc.configuration_problem()
            if problem:
                self.send_response(writer, 503, json.dumps({
                    "error": problem, "kind": "not_configured",
                }).encode('utf-8'), "application/json")
                return

            # The body is the audio, not JSON: base64 in an envelope would cost
            # a third of the payload again for no benefit.
            audio = body or b""
            language = (query.get('lang', [''])[0] or '').strip()
            try:
                result = await execute_transcription(audio, language=language)
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "text": result.text,
                    "language": result.language,
                    "duration": result.duration,
                }).encode('utf-8'), "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e), "kind": "bad_request"}).encode('utf-8'), "application/json")
            except TranscriptionError as e:
                status = {
                    "auth": 401, "bad_request": 400, "too_long": 400,
                    "not_configured": 503, "connection": 503,
                    "unknown_route": 500,
                }.get(getattr(e, "kind", "unknown"), 500)
                self.send_response(writer, status, json.dumps({
                    "error": str(e), "kind": getattr(e, "kind", "unknown"),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e), "kind": "unknown"}).encode('utf-8'), "application/json")

        # 7m. Language — GET
        elif path == '/api/settings/language' and method == 'GET':
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            self.send_response(writer, 200, json.dumps({"lang": cfg.get("lang", "")}).encode('utf-8'), "application/json")

        # 7n. Language — POST (set)
        elif path == '/api/settings/language' and method == 'POST':
            from gilzabots.i18n import set_lang
            from gilzabots.ui_settings import save_ui_settings
            lang = data.get("lang", "")
            save_ui_settings({"lang": lang})
            # map frontend locale to backend lang key
            backend_lang = "pt" if (lang or "").startswith("pt") else "en"
            set_lang(backend_lang)
            self.send_response(writer, 200, b'{"success":true}', "application/json")

        # 7n2. Graphics mode for the embedded browser window — GET
        elif path == '/api/settings/graphics' and method == 'GET':
            from gilzabots.ui_settings import load_ui_settings
            from gilzabots import webengine_env
            cfg = load_ui_settings()
            # The field keeps its historic name; the setting behind it is the one
            # of this platform's engine (webengine_env.gpu_setting_key).
            saved = webengine_env.saved_gpu_mode(cfg)
            effective, source = webengine_env.resolve_gpu_mode(settings=cfg)
            flags_var = (webengine_env.WEBVIEW2_ARGUMENTS_VAR if webengine_env.uses_webview2()
                         else webengine_env.CHROMIUM_FLAGS_VAR)
            self.send_response(writer, 200, json.dumps({
                "engine": webengine_env.engine_name(),
                "webengine_gpu": saved,
                # What this running process actually did, which differs from the
                # saved value while an environment override is in force or while
                # a change is waiting for the next launch.
                "effective_webengine_gpu": _STARTUP_DIAGNOSTICS.get("webengine_gpu", effective),
                "source": source,
                "chromium_flags": os.environ.get(flags_var, ""),
            }).encode('utf-8'), "application/json")

        # 7n3. Graphics mode — POST (set; applies at the next launch)
        elif path == '/api/settings/graphics' and method == 'POST':
            from gilzabots.ui_settings import load_ui_settings, save_ui_settings
            from gilzabots import webengine_env
            requested = webengine_env.normalize_gpu_mode(
                data.get("webengine_gpu"), webengine_env.default_gpu_mode())
            previous = webengine_env.saved_gpu_mode(load_ui_settings())
            save_ui_settings({webengine_env.gpu_setting_key(): requested})
            self.send_response(writer, 200, json.dumps({
                "success": True,
                "webengine_gpu": requested,
                # Both engines read their flags once, before the browser process
                # exists, so a change only reaches the browser on the next launch.
                "requiresRestart": requested != _STARTUP_DIAGNOSTICS.get("webengine_gpu", previous),
            }).encode('utf-8'), "application/json")

        # 7n4. What the previous run left behind (native crashes have no traceback)
        elif path == '/api/diagnostics/startup-report' and method == 'GET':
            self.send_response(writer, 200, json.dumps({
                "previous_run_failures": _STARTUP_DIAGNOSTICS.get("previous_run_failures", []),
                "crash_log_path": _STARTUP_DIAGNOSTICS.get("crash_log_path", ""),
                "webengine_gpu": _STARTUP_DIAGNOSTICS.get("webengine_gpu", "off"),
                "engine": _STARTUP_DIAGNOSTICS.get("engine", ""),
            }).encode('utf-8'), "application/json")

        # 7q. LaTeX settings — GET
        elif path == '/api/settings/latex' and method == 'GET':
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            self.send_response(writer, 200, json.dumps({
                "draft_synctex_enabled": bool(cfg.get("draft_synctex_enabled", False)),
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/latex' and method == 'POST':
            from gilzabots.ui_settings import save_ui_settings
            draft_synctex_enabled = bool(data.get("draft_synctex_enabled", False))
            save_ui_settings({"draft_synctex_enabled": draft_synctex_enabled})
            self.send_response(writer, 200, json.dumps({
                "success": True,
                "draft_synctex_enabled": draft_synctex_enabled,
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/workspace' and method == 'GET':
            from gilzabots.config import get_workspace_hidden_file_extensions
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            self.send_response(writer, 200, json.dumps({
                "show_hidden_workspace_files": bool(cfg.get("show_hidden_workspace_files", False)),
                "hidden_file_extensions": get_workspace_hidden_file_extensions(),
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/workspace' and method == 'POST':
            from gilzabots.config import get_workspace_hidden_file_extensions
            from gilzabots.ui_settings import save_ui_settings
            show_hidden_workspace_files = bool(data.get("show_hidden_workspace_files", False))
            save_ui_settings({"show_hidden_workspace_files": show_hidden_workspace_files})
            self.send_response(writer, 200, json.dumps({
                "success": True,
                "show_hidden_workspace_files": show_hidden_workspace_files,
                "hidden_file_extensions": get_workspace_hidden_file_extensions(),
            }).encode('utf-8'), "application/json")

        # 7r. Appearance (accessibility interface scale) — GET
        elif path == '/api/settings/appearance' and method == 'GET':
            from gilzabots.ui_settings import UI_SCALE_MAX, UI_SCALE_MIN, clamp_ui_scale, load_ui_settings
            cfg = load_ui_settings()
            self.send_response(writer, 200, json.dumps({
                "ui_scale": clamp_ui_scale(cfg.get("ui_scale", 1.0)),
                "ui_scale_min": UI_SCALE_MIN,
                "ui_scale_max": UI_SCALE_MAX,
            }).encode('utf-8'), "application/json")

        # 7s. Appearance — POST (set)
        elif path == '/api/settings/appearance' and method == 'POST':
            from gilzabots.ui_settings import clamp_ui_scale, save_ui_settings
            ui_scale = clamp_ui_scale(data.get("ui_scale", 1.0))
            save_ui_settings({"ui_scale": ui_scale})
            self.send_response(writer, 200, json.dumps({
                "success": True,
                "ui_scale": ui_scale,
            }).encode('utf-8'), "application/json")

        # 7t. Last active project — POST (set). Read back through list-projects.
        elif path == '/api/settings/last-project' and method == 'POST':
            from gilzabots.ui_settings import save_ui_settings
            project_name = data.get("project_name")
            if not isinstance(project_name, str) or not project_name.strip():
                self.send_response(writer, 400, json.dumps({
                    "error": "project_name must be a non-empty string",
                }).encode('utf-8'), "application/json")
            else:
                save_ui_settings({"last_active_project": project_name})
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "last_active_project": project_name,
                }).encode('utf-8'), "application/json")

        # 7u. Interface preferences (formerly localStorage) — GET
        elif path == '/api/settings/client-prefs' and method == 'GET':
            from gilzabots.ui_settings import load_client_prefs
            self.send_response(writer, 200, json.dumps(load_client_prefs()).encode('utf-8'), "application/json")

        # 7v. Interface preferences — POST {"set": {...}, "remove": [...], "legacy_import": bool}
        elif path == '/api/settings/client-prefs' and method == 'POST':
            from gilzabots.ui_settings import update_client_prefs
            try:
                applied = update_client_prefs(
                    data.get("set", {}), data.get("remove", []),
                    legacy_import=bool(data.get("legacy_import", False)),
                )
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            else:
                self.send_response(writer, 200, json.dumps({
                    "success": True, "applied": applied,
                }).encode('utf-8'), "application/json")

        # 7l. Web search MCP test
        elif path == '/api/settings/web-search/test' and method == 'POST':
            from gilzabots.web_search_config import test_mcp
            mcp_url = data.get("mcp_url", "").strip()
            mcp_tool = data.get("mcp_tool", "web_search") or "web_search"
            mcp_api_key = data.get("mcp_api_key", "").strip()
            try:
                result = await test_mcp(mcp_url, mcp_tool, mcp_api_key)
                self.send_response(writer, 200, json.dumps(result).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"ok": False, "error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/clipboard/read' and method == 'GET':
            text = _read_clipboard()
            self.send_response(writer, 200, json.dumps({'text': text}).encode('utf-8'), "application/json")

        elif path == '/api/clipboard/read-image' and method == 'GET':
            # Reading the clipboard can spawn a subprocess; keep the event loop free.
            image_b64 = await asyncio.to_thread(_read_clipboard_image)
            payload = {"data_b64": image_b64 or None, "mime": "image/png" if image_b64 else None}
            self.send_response(writer, 200, json.dumps(payload).encode(), "application/json")

        elif path == '/api/clipboard/write' and method == 'POST':
            text_to_write = data.get('text', '') if isinstance(data, dict) else ''
            ok, err = _write_clipboard(text_to_write)
            if ok:
                self.send_response(writer, 200, b'{"ok":true}', "application/json")
            else:
                self.send_response(writer, 500, json.dumps({'ok': False, 'error': err}).encode(), "application/json")
        elif path == '/api/settings/prompt-evolution' and method == 'GET':
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            iters = max(1, int(cfg.get("prompt_evolution_iterations", 1)))
            max_tokens = max(1, min(65536, int(cfg.get("prompt_evolution_max_tokens", 4096))))
            self.send_response(
                writer,
                200,
                json.dumps({
                    "prompt_evolution_iterations": iters,
                    "prompt_evolution_max_tokens": max_tokens,
                }).encode('utf-8'),
                "application/json",
            )

        elif path == '/api/settings/prompt-evolution' and method == 'POST':
            from gilzabots.ui_settings import save_ui_settings
            try:
                iters = max(1, int(data.get("prompt_evolution_iterations", 1)))
            except (ValueError, TypeError):
                iters = 1
            try:
                max_tokens = max(1, min(65536, int(data.get("prompt_evolution_max_tokens", 4096))))
            except (ValueError, TypeError):
                max_tokens = 4096
            save_ui_settings({
                "prompt_evolution_iterations": iters,
                "prompt_evolution_max_tokens": max_tokens,
            })
            self.send_response(
                writer,
                200,
                json.dumps({
                    "success": True,
                    "prompt_evolution_iterations": iters,
                    "prompt_evolution_max_tokens": max_tokens,
                }).encode('utf-8'),
                "application/json",
            )

        elif path == '/api/settings/thoughts' and method == 'GET':
            from gilzabots.ui_settings import (
                clamp_chat_thought_preview_tokens, clamp_thought_context_tokens, load_ui_settings,
            )
            settings = load_ui_settings()
            self.send_response(
                writer, 200, json.dumps({
                    "thought_context_tokens": clamp_thought_context_tokens(
                        settings.get("thought_context_tokens")),
                    "chat_thought_preview_tokens": clamp_chat_thought_preview_tokens(
                        settings.get("chat_thought_preview_tokens")),
                }).encode('utf-8'), "application/json"
            )

        elif path == '/api/settings/thoughts' and method == 'POST':
            from gilzabots.ui_settings import (
                clamp_chat_thought_preview_tokens, clamp_thought_context_tokens,
                load_ui_settings, save_ui_settings,
            )
            # Either size may be sent on its own; the other keeps its value.
            current = load_ui_settings()
            saved = {}
            if "thought_context_tokens" in data:
                saved["thought_context_tokens"] = clamp_thought_context_tokens(
                    data.get("thought_context_tokens"))
            if "chat_thought_preview_tokens" in data:
                saved["chat_thought_preview_tokens"] = clamp_chat_thought_preview_tokens(
                    data.get("chat_thought_preview_tokens"))
            if saved:
                save_ui_settings(saved)
            self.send_response(
                writer, 200,
                json.dumps({
                    "success": True,
                    "thought_context_tokens": saved.get(
                        "thought_context_tokens",
                        clamp_thought_context_tokens(current.get("thought_context_tokens"))),
                    "chat_thought_preview_tokens": saved.get(
                        "chat_thought_preview_tokens",
                        clamp_chat_thought_preview_tokens(current.get("chat_thought_preview_tokens"))),
                }).encode('utf-8'),
                "application/json",
            )

        elif path == '/api/chat/evolve-prompt' and method == 'POST':
            prompt_text = (data.get("prompt") or "").strip()
            if not prompt_text:
                self.send_response(writer, 400, b'{"error":"prompt is required"}', "application/json")
                return
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            default_iters = max(1, int(cfg.get("prompt_evolution_iterations", 1)))
            iterations = data.get("iterations")
            if iterations is None:
                iterations = default_iters
            try:
                iterations = max(1, int(iterations))
            except (ValueError, TypeError):
                iterations = default_iters
            task = asyncio.current_task()
            self.active_prompt_evolution_task = task
            try:
                selected_model = str(data.get("model") or "").strip()
                evolved = await _execute_prompt_evolution(
                    prompt_text,
                    iterations=iterations,
                    model=selected_model or None,
                    max_tokens=max(1, min(65536, int(cfg.get("prompt_evolution_max_tokens", 4096)))),
                )
                self.send_response(writer, 200, json.dumps({"success": True, "prompt": evolved}).encode('utf-8'), "application/json")
            except asyncio.CancelledError:
                try:
                    self.send_response(writer, 499, b'{"error":"Prompt evolution cancelled"}', "application/json")
                except Exception:
                    pass
                raise
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            finally:
                if self.active_prompt_evolution_task is task:
                    self.active_prompt_evolution_task = None

        elif path in ('/api/chat/cancel-evolve-prompt', '/api/chat/evolve-prompt/cancel') and method == 'POST':
            if self.active_prompt_evolution_task and not self.active_prompt_evolution_task.done():
                self.active_prompt_evolution_task.cancel()
                self.send_response(writer, 200, b'{"success":true,"message":"Prompt evolution cancelled"}', "application/json")
            else:
                self.send_response(writer, 200, b'{"success":false,"message":"No active prompt evolution running"}', "application/json")
            return

        elif path == '/api/settings/translation' and method == 'GET':
            from gilzabots.translation import TRANSLATION_LANGUAGE_NAMES
            from gilzabots.ui_settings import load_ui_settings
            cfg = load_ui_settings()
            self.send_response(writer, 200, json.dumps({
                "translate_target_lang": str(cfg.get("translate_target_lang", "") or ""),
                "known_languages": sorted(TRANSLATION_LANGUAGE_NAMES.keys()),
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/translation' and method == 'POST':
            from gilzabots.ui_settings import save_ui_settings
            target_lang = str(data.get("translate_target_lang", "") or "").strip()
            save_ui_settings({"translate_target_lang": target_lang})
            self.send_response(writer, 200, json.dumps({
                "success": True,
                "translate_target_lang": target_lang,
            }).encode('utf-8'), "application/json")

        elif path == '/api/settings/sandbox' and method == 'GET':
            from gilzabots.sandbox_config import load_config
            from gilzabots.docker_sandbox import verify_docker
            try:
                cfg = load_config()
                status = await asyncio.to_thread(verify_docker)
                self.send_response(writer, 200, json.dumps({**cfg, "status": status}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/sandbox' and method == 'POST':
            from gilzabots.sandbox_config import load_config, save_config
            from gilzabots.docker_sandbox import verify_docker
            try:
                save_config(data)
                cfg = load_config()
                status = await asyncio.to_thread(verify_docker)
                self.send_response(writer, 200, json.dumps({"success": True, **cfg, "status": status}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/sandbox/verify' and method == 'POST':
            from gilzabots.docker_sandbox import verify_docker
            try:
                status = await asyncio.to_thread(verify_docker)
                self.send_response(writer, 200, json.dumps({"success": True, "status": status}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/sandbox/pull' and method == 'POST':
            from gilzabots.docker_sandbox import pull_sandbox_image, verify_docker
            image = data.get("image")
            try:
                ok, msg = await asyncio.to_thread(pull_sandbox_image, image)
                status = await asyncio.to_thread(verify_docker)
                self.send_response(writer, 200 if ok else 400, json.dumps({
                    "success": ok,
                    "message": msg,
                    "status": status,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/settings/sandbox/recreate' and method == 'POST':
            from gilzabots.docker_sandbox import recreate_project_container
            project_path = data.get("projectPath") or ""
            try:
                ok, msg = await asyncio.to_thread(recreate_project_container, project_path)
                self.send_response(writer, 200 if ok else 400, json.dumps({
                    "success": ok,
                    "message": msg,
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/translate' and method == 'POST':
            from gilzabots.translation import execute_translation, resolve_target_language
            from gilzabots.ui_settings import load_ui_settings
            snippet = str(data.get("text") or "")
            if not snippet.strip():
                self.send_response(writer, 400, b'{"error":"text is required"}', "application/json")
                return
            cfg = load_ui_settings()
            target_language = resolve_target_language(
                data.get("target_lang"),
                cfg.get("translate_target_lang"),
                cfg.get("lang"),
            )
            try:
                translated_text = await execute_translation(
                    snippet,
                    target_language,
                    model=str(data.get("model") or "").strip() or None,
                )
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "target_language": target_language,
                    "translated_text": translated_text,
                }).encode('utf-8'), "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/snippet-query' and method == 'POST':
            from gilzabots.snippet_query import SnippetQuerySource, execute_snippet_query
            excerpt = str(data.get("excerpt") or "")
            question = str(data.get("question") or "")
            if not excerpt.strip() or not question.strip():
                self.send_response(writer, 400, b'{"error":"excerpt and question are required"}', "application/json")
                return
            try:
                result = await execute_snippet_query(
                    excerpt,
                    question,
                    source=SnippetQuerySource.from_payload(data.get("source")),
                    project_path=str(data.get("projectPath") or "").strip() or None,
                    model=str(data.get("model") or "").strip() or None,
                )
                self.send_response(writer, 200, json.dumps({
                    "success": True,
                    "answer": result.answer,
                    "truncated": result.truncated,
                    "files_read": result.files_read,
                }).encode('utf-8'), "application/json")
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        elif path == '/api/speech' and method == 'POST':
            from agenticblocks.blocks.speech import SpeechSynthesisError
            from gilzabots.speech import execute_speech_synthesis, resolve_language_tag
            from gilzabots.ui_settings import load_ui_settings
            from gilzabots import speech_config

            snippet = str(data.get("text") or "")
            if not snippet.strip():
                self.send_response(writer, 400, b'{"error":"text is required"}', "application/json")
                return

            # Refused before a request is made rather than after: the feature is
            # opt-in and needs an endpoint nothing else in the app requires, so
            # "not configured" is a first-class answer with an instruction in it.
            problem = speech_config.configuration_problem()
            if problem:
                self.send_response(writer, 503, json.dumps({
                    "error": problem, "kind": "not_configured",
                }).encode('utf-8'), "application/json")
                return

            cfg = load_ui_settings()
            language = resolve_language_tag(
                data.get("lang"),
                cfg.get("translate_target_lang"),
                cfg.get("lang"),
            )
            try:
                audio, mime = await execute_speech_synthesis(
                    snippet,
                    voice=str(data.get("voice") or "").strip(),
                    language=language,
                    model=str(data.get("model") or "").strip() or None,
                    speed=data.get("speed"),
                    response_format=str(data.get("format") or "").strip(),
                )
                # The bytes are the response. Wrapping them in base64 inside a
                # JSON envelope would cost a third of the payload again and buy
                # nothing: an <audio> element takes a URL.
                self.send_response(writer, 200, audio, mime)
            except ValueError as e:
                self.send_response(writer, 400, json.dumps({"error": str(e), "kind": "bad_request"}).encode('utf-8'), "application/json")
            except SpeechSynthesisError as e:
                # The classified kind travels with the message so the UI can be
                # specific about what to fix, the way image generation is
                # (PROJECT_DESIGN 2.12). Nothing falls back to another provider.
                status = {
                    "auth": 401,
                    "bad_request": 400,
                    "too_long": 400,
                    "not_configured": 503,
                    "connection": 503,
                    "unsupported": 503,
                    "unknown_route": 500,
                }.get(getattr(e, "kind", "unknown"), 500)
                self.send_response(writer, status, json.dumps({
                    "error": str(e), "kind": getattr(e, "kind", "unknown"),
                }).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e), "kind": "unknown"}).encode('utf-8'), "application/json")

        # ── PDF annotations (standalone PDFs) ──────────────────────────────
        # Stored inside the PDF itself, so they interoperate with Zotero,
        # Acrobat and any other reader. See gilzabots/pdf_annotations.py.
        elif path.startswith('/api/pdf/annotations'):
            from gilzabots.pdf_annotations import (
                DEFAULT_AUTHOR,
                DEFAULT_COLOR,
                PdfAnnotationError,
                add_annotation,
                delete_annotation,
                list_annotations,
                move_annotation_marker,
                read_without_annotations,
                update_annotation,
            )

            project_path = (query.get('projectPath', [None])[0] if method == 'GET'
                            else data.get('projectPath'))
            file_path = (query.get('filePath', [None])[0] if method == 'GET'
                         else data.get('filePath'))
            if not project_path or not file_path:
                self.send_response(writer, 400, b'{"error":"projectPath and filePath are required"}', "application/json")
                return

            project_abs = os.path.abspath(project_path)
            full_path = os.path.abspath(os.path.join(project_abs, file_path))
            if not _is_path_within(full_path, project_abs):
                self.send_response(writer, 403, b'{"error":"Forbidden: Path traversal detected"}', "application/json")
                return

            try:
                if path == '/api/pdf/annotations/document' and method == 'GET':
                    # The viewer's "hide annotations" toggle: pdf.js paints marks
                    # into the page canvas and react-pdf gives no way to turn that
                    # off, so the stripped copy is produced here. The file on disk
                    # is untouched.
                    self.send_response(
                        writer, 200, read_without_annotations(full_path), "application/pdf",
                    )
                    return
                if path == '/api/pdf/annotations' and method == 'GET':
                    payload = {"success": True, "annotations": list_annotations(full_path)}
                elif path == '/api/pdf/annotations' and method == 'POST':
                    payload = {"success": True, "annotation": add_annotation(
                        full_path,
                        page=int(data.get('page') or 0),
                        kind=str(data.get('kind') or ''),
                        rects=data.get('rects') or [],
                        color=str(data.get('color') or '') or DEFAULT_COLOR,
                        content=str(data.get('content') or ''),
                        author=str(data.get('author') or '') or DEFAULT_AUTHOR,
                    )}
                elif path == '/api/pdf/annotations/update' and method == 'POST':
                    payload = {"success": True, "annotation": update_annotation(
                        full_path,
                        int(data.get('id') or 0),
                        content=data.get('content'),
                        color=data.get('color'),
                    )}
                elif path == '/api/pdf/annotations/marker' and method == 'POST':
                    payload = {"success": True, "annotation": move_annotation_marker(
                        full_path,
                        int(data.get('id') or 0),
                        data.get('point') or [],
                    )}
                elif path == '/api/pdf/annotations/delete' and method == 'POST':
                    payload = {"success": True, "deleted": delete_annotation(
                        full_path, int(data.get('id') or 0)
                    )}
                else:
                    self.send_response(writer, 404, b'{"error":"Not Found"}', "application/json")
                    return
                self.send_response(writer, 200, json.dumps(payload).encode('utf-8'), "application/json")
            except PdfAnnotationError as e:
                # A PDF that cannot be annotated is a 409, not a 500: the request
                # was well-formed and the message names a condition the user can act on.
                self.send_response(writer, 409, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except (TypeError, ValueError) as e:
                self.send_response(writer, 400, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")
            except Exception as e:
                self.send_response(writer, 500, json.dumps({"error": str(e)}).encode('utf-8'), "application/json")

        else:
            self.send_response(writer, 404, b'{"error":"Not Found"}', "application/json")

def build_relaunch_command():
    """Build the argv that re-launches this GilzaBots instance.

    Frozen builds (PyInstaller) expose the app itself as ``sys.executable`` and
    must not be re-run through an interpreter. Source checkouts are re-launched
    with the same interpreter and entry script. Module launches preserve ``-m``
    so package-relative imports still work.
    When no entry script exists, invoke the CLI entry point directly.
    """
    import sys

    extra_args = list(sys.argv[1:])
    if getattr(sys, "frozen", False):
        return [sys.executable] + extra_args

    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if main_spec is not None:
        return [sys.executable, "-m", main_spec.name] + extra_args

    script = sys.argv[0] if sys.argv else ""
    if script and os.path.isfile(script):
        return [sys.executable, os.path.abspath(script)] + extra_args
    return [sys.executable, "-c", "from gilzabots.cli import main; main()"] + extra_args


def spawn_detached(command, cwd=None, env=None):
    """Start ``command`` fully detached from this process and return the Popen."""
    import subprocess
    import sys

    kwargs = {"cwd": cwd or os.getcwd(), "close_fds": True}
    if env is not None:
        kwargs["env"] = env
    if sys.platform == "win32":
        # DETACHED_PROCESS keeps the child alive after we exit, and a process with
        # no console cannot receive Ctrl+C delivered to the old one.
        # CREATE_NEW_PROCESS_GROUP must not be added: it makes Windows disable
        # Ctrl+C in the new process (an implicit SetConsoleCtrlHandler(NULL, TRUE)),
        # and every process it starts inherits that. After an in-app restart the
        # integrated terminal's PowerShell then ignored Ctrl+C entirely.
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def schedule_app_restart(delay=0.6):
    """Relaunch GilzaBots and terminate this process shortly after.

    The work is deferred to a timer thread so the HTTP response that triggered
    the restart can be flushed to the browser first. The replacement process is
    spawned immediately before ``os._exit`` so that this process has released
    its listening port by the time the child boots and picks one.
    """
    import threading

    command = build_relaunch_command()
    cwd = os.getcwd()

    def _restart():
        try:
            # The replacement must decide its own rendering mode. Inheriting this
            # run's is how "applies at the next launch" failed to apply at the
            # next launch (see webengine_env.pristine_environment).
            from gilzabots.webengine_env import pristine_environment

            spawn_detached(command, cwd=cwd, env=pristine_environment())
        except Exception as e:  # pragma: no cover - depends on the host OS
            print(f"[GilzaBots] restart failed to spawn a new instance: {e}")
            return
        # ``os._exit`` skips atexit, so this exit path has to retire its own run
        # marker — otherwise every restart looks like a crash to the next launch.
        from gilzabots import crash_report
        crash_report.mark_clean_exit()
        os._exit(0)

    timer = threading.Timer(delay, _restart)
    timer.daemon = True
    timer.start()
    return command


def find_available_port(host, start_port, max_port=3050):
    """First port in [start_port, max_port] the server's listener can bind.

    The probe must use the listener's own bind semantics. asyncio's server
    sets SO_REUSEADDR on POSIX, so a port whose only remnants are TIME_WAIT
    connections from the previous run is free for it; a probe without the
    flag treats it as taken and moves to the next port. Closing and reopening
    the app then changed the port, hence the page origin and its
    localStorage. On Windows SO_REUSEADDR would let the probe bind a port
    another process is listening on, and asyncio does not set it there either.
    """
    import socket
    for p in range(start_port, max_port + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"No available ports found between {start_port} and {max_port}")

# Background of the startup window before its first paint; matches splash.html,
# so the window does not flash white (pywebview's default) before the splash.
SPLASH_BACKGROUND = "#0f1117"


def load_splash_page(static_dir, lang=""):
    """The startup splash (gui/splash.html) ready for pywebview's ``html=``.

    The page is shown before the local server exists, so nothing in it may be
    fetched: the icon is inlined as a data URI. ``lang`` is the saved UI
    language ("" lets the page ask the system). Returns None when the bundle has
    no splash page (built before it existed, or API-only).
    """
    import html as _html
    if not static_dir:
        return None
    try:
        with open(os.path.join(static_dir, "splash.html"), "r", encoding="utf-8") as f:
            page = f.read()
        with open(os.path.join(static_dir, "splash-icon.png"), "rb") as f:
            icon = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None
    page = page.replace('src="__SPLASH_ICON__"', f'src="data:image/png;base64,{icon}"', 1)
    if lang:
        page = page.replace('data-splash-lang=""', f'data-splash-lang="{_html.escape(lang, quote=True)}"', 1)
    return page


#: The desktop window, once created; a monitor notification click raises it.
_MAIN_WINDOW = None


def _focus_main_window() -> None:
    window = _MAIN_WINDOW
    if window is None:
        return
    for method in ("restore", "show"):
        try:
            getattr(window, method)()
        except Exception:
            pass


def start_gui_server(host="127.0.0.1", port=3000):
    import os
    from gilzabots import crash_report
    from gilzabots.config import DEFAULT_LANG
    from gilzabots.i18n import set_lang
    from gilzabots.ui_settings import load_ui_settings
    from gilzabots.webengine_env import apply_webengine_environment

    try:
        port = find_available_port(host, port)
    except Exception as e:
        print(f"Warning: could not find available port using fallback logic: {e}")

    ui_cfg = load_ui_settings()

    # The graphics mode has to be decided before the embedded browser
    # (QtWebEngine, or WebView2 on Windows) reads the environment, and it is
    # worth recording next to the run it applies to: a crash log that does not
    # say whether the GPU was in use cannot answer whether turning it off helped.
    webengine = apply_webengine_environment(settings=ui_cfg)
    crash_report.install(details={
        "browser_engine": webengine["engine"],
        "webengine_gpu": webengine["gpu_mode"],
        "webengine_gpu_source": webengine["source"],
        "chromium_flags": webengine["chromium_flags"],
        "qt_quick_backend": webengine["qt_quick_backend"],
    })
    if webengine["reason"]:
        quick = f", QT_QUICK_BACKEND={webengine['qt_quick_backend']}" if webengine["qt_quick_backend"] else ""
        engine_label = "WebView2" if webengine["engine"] == "webview2" else "QtWebEngine"
        print(f"[GilzaBots] {engine_label}: {webengine['reason']} "
              f"({webengine['flags_var']}={webengine['chromium_flags']}{quick})")

    # A run that died leaves its marker behind; this is the only moment the
    # application can say so, since the run itself had no chance to.
    global _STARTUP_DIAGNOSTICS
    previous_failures = crash_report.consume_previous_run_failures()
    for failure in previous_failures:
        print(crash_report.describe_failure(failure))
    _STARTUP_DIAGNOSTICS = {
        "previous_run_failures": previous_failures,
        "crash_log_path": str(crash_report.crash_log_path()),
        "webengine_gpu": webengine["gpu_mode"],
        "engine": webengine["engine"],
    }

    saved_lang = ui_cfg.get("lang", "")
    if saved_lang:
        backend_lang = "pt" if saved_lang.startswith("pt") else "en"
        set_lang(backend_lang)
    else:
        set_lang(DEFAULT_LANG)
    # Path to gui directory inside gilzabots package
    package_dir = os.path.dirname(os.path.abspath(__file__))
    static_dir = os.path.join(package_dir, "gui")

    if not os.path.exists(static_dir):
        print(f"Warning: GUI static assets directory not found at {static_dir}. Server will run API-only.")
        static_dir = None

    server = AsyncHTTPServer(host=host, port=port, static_dir=static_dir)
    url = f"http://{host}:{port}"

    import threading

    server_ready = threading.Event()
    backend = {"thread": None}
    backend_lock = threading.Lock()

    def _run_asyncio_server():
        async def _inner():
            await server.start()
            server_ready.set()        # signal: server is accepting connections
            while True:
                await asyncio.sleep(3600)

        asyncio.run(_inner())

    def _start_backend(report=lambda _phase: None):
        """Load the agent runtime and start serving; safe to call twice.

        This is where launching spends its time: the agent runtime pulls in
        litellm, about 4 s on a warm disk. It therefore runs once the window is
        already showing the splash screen, in pywebview's worker thread, or on
        the main thread when no window could be opened (browser fallback).
        `report` names the phase for the splash, which starts in "loading": it
        is not reported, because evaluating JavaScript waits for the page to
        finish loading and would hold the imports back by that long.
        """
        with backend_lock:
            if backend["thread"] is None:
                import gilzabots.agent_stdin as agent_stdin

                def web_event_hook(payload):
                    server.publish_event(payload)

                agent_stdin.event_hook = web_event_hook
                import litellm
                litellm.event_hook = web_event_hook
                # litellm prints a "Give Feedback / Get Help" banner with every
                # provider error. The packaged entry point (main.py) silenced it
                # by importing litellm first thing, which kept the window, and
                # its splash screen, from appearing for ~4 s.
                litellm.suppress_debug_info = True

                # TEMPORARY DIAGNOSTIC — remove together with agenticblocks'
                # _await_reporting_cancellation. Stamps the "never awaited" warning with the
                # time so it can be matched to a "[LLM CALL CANCELLED ...]" line.
                import sys as _sys
                import warnings as _warnings
                from datetime import datetime as _datetime
                _show_warning = _warnings.showwarning

                def _show_warning_with_time(message, category, filename, lineno, file=None, line=None):
                    if "never awaited" in str(message):
                        print(f"[WARNING TIME {_datetime.now():%H:%M:%S.%f}]", file=_sys.stderr, flush=True)
                    _show_warning(message, category, filename, lineno, file, line)

                _warnings.showwarning = _show_warning_with_time

                # --- Run asyncio server in a background daemon thread so the main thread
                # is free for the desktop window toolkit (GTK/pywebview requires main thread).
                report("starting")
                thread = threading.Thread(target=_run_asyncio_server, daemon=True)
                thread.start()
                backend["thread"] = thread
                # Monitors outlive the window; one left active before a reboot
                # restarts its daemon here if the login service did not.
                from gilzabots.monitors.api import on_server_start
                threading.Thread(target=on_server_start, daemon=True).start()
        # Nothing may load the URL before the server accepts connections.
        server_ready.wait(timeout=10)
        return backend["thread"]

    try:
        import os as _os
        import sys as _sys
        # The window is WebView2 on Windows (gilzabots/windows_webview.py) and
        # QtWebEngine on macOS and Linux (PROJECT_DESIGN.md §2.16.1).
        _use_webview2 = _sys.platform == 'win32'
        if _use_webview2:
            from gilzabots import windows_webview
            # Without the runtime pywebview falls back to MSHTML (the IE engine)
            # with only a log warning; here it is a failure to open the window,
            # which the handler at the end of this block reports.
            windows_webview.require_runtime()
        elif 'PYWEBVIEW_GUI' not in _os.environ:
            # Force the Qt backend on macOS and Linux.
            _os.environ['PYWEBVIEW_GUI'] = 'qt'

        import webview  # pywebview
        
        # Allow file downloads (like export markdown/pdf)
        webview.settings['ALLOW_DOWNLOADS'] = True

        # Patches of pywebview's Qt backend. Windows has no Qt: WebView2 handles
        # permissions, the context menu, printing and downloads itself, and
        # windows_webview.install_handlers covers the rest.
        if not _use_webview2:
            # Monkey-patch pywebview's Qt backend to use proper Qt enum values
            # instead of raw ints for setFeaturePermission. Modern Qt wrappers (PyQt6/PySide6)
            # no longer accept int in place of PermissionPolicy, causing a TypeError + crash.
            try:
                from webview.platforms import qt as _wv_qt
                _QWP = _wv_qt.QWebPage

                if hasattr(_QWP, "PermissionPolicy"):
                    _granted = _QWP.PermissionPolicy.PermissionGrantedByUser
                    _denied = _QWP.PermissionPolicy.PermissionDeniedByUser
                else:
                    _granted = 1
                    _denied = 2

                def _onFeaturePermissionRequested(self, url, feature):
                    if feature in (
                        _QWP.Feature.MediaAudioCapture,
                        _QWP.Feature.MediaVideoCapture,
                        _QWP.Feature.MediaAudioVideoCapture,
                    ):
                        self.setFeaturePermission(url, feature, _granted)
                    else:
                        self.setFeaturePermission(url, feature, _denied)

                # Patch only if the broken version (using int literals) is present
                import inspect as _inspect
                _src = _inspect.getsource(_wv_qt)
                if "setFeaturePermission(url, feature, 2)" in _src:
                    if hasattr(_wv_qt, "BrowserView") and hasattr(_wv_qt.BrowserView, "WebPage"):
                        _wv_qt.BrowserView.WebPage.onFeaturePermissionRequested = _onFeaturePermissionRequested

            except Exception:
                pass

            # Suppress Qt's native context menu by patching BrowserView.__init__
            # to add a second loadFinished connection that sets NoContextMenu policy.
            try:
                from webview.platforms import qt as _wv_qt2
                from qtpy.QtCore import Qt as _Qt

                if hasattr(_wv_qt2, "BrowserView"):
                    _orig_init = _wv_qt2.BrowserView.__init__

                    def _patched_init(self, window):
                        _orig_init(self, window)
                        page = self.webview.page()

                        def _disable_context_menu(_ok=None):
                            self.webview.setContextMenuPolicy(_Qt.ContextMenuPolicy.NoContextMenu)
                        page.loadFinished.connect(_disable_context_menu)

                        # Handle window.print() triggered from JavaScript: the
                        # embedded webview has no print dialog of its own, so the
                        # page is saved as a PDF the user picks a path for.
                        try:
                            from PyQt6.QtWidgets import QFileDialog

                            def _handle_print():
                                try:
                                    file_path, _ = QFileDialog.getSaveFileName(
                                        self.webview, "Save PDF", "", "PDF Files (*.pdf)"
                                    )
                                    if not file_path:
                                        return
                                    if not file_path.lower().endswith('.pdf'):
                                        file_path += '.pdf'
                                    self.webview.page().printToPdf(file_path)
                                except Exception as e:
                                    print(f"[GilzaBots] print to PDF failed: {type(e).__name__}: {e}")

                            page.printRequested.connect(_handle_print)
                        except Exception as pe:
                            print(f"[GilzaBots] could not install the print handler: "
                                  f"{type(pe).__name__}: {pe}")

                    _wv_qt2.BrowserView.__init__ = _patched_init
                
                    # Patch on_download_requested for PyQt6 compatibility
                    _orig_on_download_requested = getattr(_wv_qt2.BrowserView, "on_download_requested", None)
                    if _orig_on_download_requested:
                        def _patched_on_download_requested(self, download):
                            try:
                                from PyQt6.QtWidgets import QFileDialog
                                import os
                                old_path = download.url().path() if hasattr(download, 'url') else ""
                                if hasattr(download, 'suggestedFileName'):
                                    default_name = download.suggestedFileName()
                                else:
                                    default_name = os.path.basename(old_path)
                                path, _ = QFileDialog.getSaveFileName(
                                    self, getattr(self, 'localization', {}).get('global.saveFile', 'Salvar Arquivo'), default_name
                                )
                                if path:
                                    if hasattr(download, 'setDownloadDirectory') and hasattr(download, 'setDownloadFileName'):
                                        download.setDownloadDirectory(os.path.dirname(path))
                                        download.setDownloadFileName(os.path.basename(path))
                                    elif hasattr(download, 'setPath'):
                                        download.setPath(path)
                                    download.accept()
                            except Exception as e:
                                print('[patch download] FAILED:', e)
                        _wv_qt2.BrowserView.on_download_requested = _patched_on_download_requested
            except Exception as e:
                print('[patch init] FAILED:', e)

        # Ensure WebKit2GTK GObject introspection is available on Linux
        try:
            import gi
            gi.require_version("WebKit2", "4.1")
        except Exception:
            pass

        # Determine screen dimensions dynamically if possible
        if not _use_webview2:
            os.environ["QT_LOGGING_RULES"] = "*.debug=false;qt.qpa.*=false"
        width = 1000
        height = 650
        try:
            screens = webview.screens
            if screens:
                primary = screens[0]
                width = max(800, int(primary.width * 0.80))
                height = max(600, int(primary.height * 0.75))
        except Exception:
            pass

        # The window opens on the splash screen at once; the agent runtime and
        # the server come up behind it (_boot below). A bundle built before the
        # splash existed has no page to show, and then the window waits for the
        # server exactly as it always did.
        splash = load_splash_page(static_dir, saved_lang)
        if splash is None:
            print("[GilzaBots] gui/splash.html not found in the front-end bundle; starting without a splash screen.")
            _start_backend()
        global _MAIN_WINDOW
        window = _MAIN_WINDOW = webview.create_window(
            title="GilzaBots",
            url=None if splash else url,
            html=splash,
            width=width,
            height=height,
            resizable=True,
            text_select=True,
            zoomable=True,
            background_color=SPLASH_BACKGROUND,
        )
        if _use_webview2:
            # The restart is what brings a window back whose browser process
            # died (windows_webview.failure_action); the same relaunch Settings uses.
            windows_webview.install_handlers(window, restart=lambda: schedule_app_restart(delay=0.2))

        def _boot():
            def report(phase):
                try:
                    window.evaluate_js(f"window.gilzabotsSplash && window.gilzabotsSplash.status({json.dumps(phase)})")
                except Exception:
                    pass

            try:
                _start_backend(report)
            except Exception as exc:
                # Off the main thread a failure would otherwise leave the splash
                # spinning forever: say what broke, on screen and in the log.
                import traceback
                traceback.print_exc()
                detail = json.dumps(f"{type(exc).__name__}: {exc}")
                try:
                    window.evaluate_js(f"window.gilzabotsSplash && window.gilzabotsSplash.fail({detail})")
                except Exception:
                    pass
                return
            report("opening")
            window.load_url(url)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(os.getcwd(), "icon.png")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")
        if not os.path.exists(icon_path):
            icon_path = None

        import sys
        if icon_path and sys.platform == "win32" and not icon_path.lower().endswith(".ico"):
            icon_path = None

        # webview.start() blocks the main thread until the window is closed.
        storage_path = os.path.expanduser("~/.gilzabots/webview")
        os.makedirs(storage_path, exist_ok=True)
        
        gui_engine = 'edgechromium' if _use_webview2 else None
        webview.start(
            func=_boot if splash else None,
            gui=gui_engine, debug=False, icon=icon_path, private_mode=False, storage_path=storage_path,
        )

    except (ImportError, Exception) as e:
        import traceback
        with open("pywebview_error.log", "w", encoding="utf-8") as f:
            f.write(f"Error type: {type(e).__name__}\n")
            f.write(f"Error msg: {e}\n")
            f.write(traceback.format_exc())

        # Graceful fallback: open in the default web browser
        import webbrowser
        print(f"[GilzaBots] pywebview failed to launch ({type(e).__name__}: {e}) — opening browser at {url}")
        backend_thread = _start_backend()
        webbrowser.open(url)
        # Keep the server alive
        try:
            backend_thread.join()
        except KeyboardInterrupt:
            pass

    print("\nStopping GilzaBots Server...")
    try:
        server.stop()
    except Exception as e:
        print(f"Error stopping server: {e}")
    # Closing the window is a normal end, and ``os._exit`` skips atexit: say so
    # explicitly, or the next launch reports this run as one that died.
    crash_report.mark_clean_exit()
    os._exit(0)
