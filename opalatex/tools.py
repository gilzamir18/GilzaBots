"""Tools for the OpalaTex Autonomous Agent (MemGPT pattern + HITL)."""
import os
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import functools
import asyncio
import concurrent.futures
from agenticblocks.core.function_block import as_tool

from . import terminal as T

# ─── Shared progress state ───────────────────────────────────────────────────
# The orchestrator reads these to render a live progress panel.

TURN_ACHIEVEMENTS = ""



class _AgentProgress:
    def __init__(self):
        self.heartbeat: int = 0
        self.max_heartbeats: int = 50
        self.tasks_done: int = 0
        self.tasks_total: int = 0
        self.last_tool: str = "—"
        self.last_args: str = ""
        self.start_time: float = time.monotonic()
        self.live_context = None
        # Error / failure tracking
        self.task_failures: int = 0          # reviewer said task NOT DONE
        self.worker_errors: int = 0          # worker returned [ERROR ...]
        self.lint_errors: int = 0            # lint check failures from write/edit
        self.plan_retries: int = 0           # oracle reflection retries
        self.recent_events: list[str] = []   # last N notable events (errors, retries)

    def _push_event(self, msg: str) -> None:
        self.recent_events.append(msg)
        if len(self.recent_events) > 6:
            self.recent_events.pop(0)

    def record_lint_error(self, path: str) -> None:
        self.lint_errors += 1
        short = os.path.basename(path)
        self._push_event(f"[red]✗ lint[/red] {short}")

    def record_worker_error(self, task_id: str, snippet: str = "") -> None:
        self.worker_errors += 1
        snip = snippet[:60].replace("\n", " ") if snippet else ""
        self._push_event(f"[red]✗ worker[/red] {task_id}" + (f": {snip}" if snip else ""))

    def record_task_failure(self, task_id: str, reason: str = "") -> None:
        self.task_failures += 1
        snip = reason[:60].replace("\n", " ") if reason else ""
        self._push_event(f"[yellow]⚠ review[/yellow] {task_id}" + (f": {snip}" if snip else ""))

    def record_plan_retry(self, attempt: int) -> None:
        self.plan_retries += 1
        self._push_event(f"[yellow]⚠ plan retry[/yellow] #{attempt}")

    def update(self, tool_name: str, args_preview: str = "") -> None:
        self.heartbeat += 1
        self.last_tool = tool_name
        self.last_args = args_preview[:80] if args_preview else ""



    def elapsed(self) -> str:
        secs = int(time.monotonic() - self.start_time)
        m, s = divmod(secs, 60)
        return f"{m}m{s:02d}s"


AGENT_PROGRESS = _AgentProgress()
_RECENT_FILE_ATTACHMENTS: dict[str, dict] = {}


def set_recent_file_attachments(attachments_by_alias: dict[str, dict]) -> None:
    """Register chat attachments that tools may reference by synthetic alias."""
    _RECENT_FILE_ATTACHMENTS.clear()
    _RECENT_FILE_ATTACHMENTS.update(attachments_by_alias or {})


def add_recent_file_attachments(attachments_by_alias: dict[str, dict]) -> None:
    """Register more attachments without dropping the ones already registered.

    A turn can gain attachments after it started: a message sent while the agent
    is working is delivered mid-turn and brings its own files. Replacing the map
    would unregister the aliases the model was already told about, so those calls
    would start failing halfway through the turn.
    """
    _RECENT_FILE_ATTACHMENTS.update(attachments_by_alias or {})


def recent_file_attachment_count() -> int:
    """Number of aliases registered for the current turn."""
    return len(_RECENT_FILE_ATTACHMENTS)


def set_recent_image_attachments(attachments_by_alias: dict[str, dict]) -> None:
    """Backward-compatible wrapper for callers that only register images."""
    set_recent_file_attachments(attachments_by_alias)

# Set once at session start via set_project_path(); all tools use this as their workspace.
_PROJECT_PATH: str = ""
_PROJECT_SESSION = None
_PROJECT_STORE = None

# Toggled by run_skill() around a helper agent's execution (PROJECT_DESIGN
# 2.23). A helper has no run_skill tool, so recovery advice must not tell it to
# delegate; it does not change project files, so the write tools confine it to
# the scratch area; and it sees images through its own model, not the main
# agent's.
_IN_SKILL_WORKER: bool = False
_HELPER_MODEL: str = ""

# Whether the main agent currently holds the command-execution tools. It always
# does, under both policies, except in plan mode, where the mode withholds them.
# Kept next to _IN_SKILL_WORKER because both exist for the same reason --
# recovery advice must name a route the caller can actually take
# (PROJECT_DESIGN 2.6).
_ORCHESTRATOR_HAS_TERMINAL: bool = True

#: Where a helper may write: notes, plans and intermediate files it needs for
#: its job. Everything else in the project is the main agent's to change.
HELPER_SCRATCH_DIR = ".opalatex"


def set_worker_context(active: bool, model: str = "") -> None:
    """Mark the window in which a helper agent runs, and the model it runs on."""
    global _IN_SKILL_WORKER, _HELPER_MODEL
    _IN_SKILL_WORKER = bool(active)
    _HELPER_MODEL = str(model or "") if active else ""


def in_helper_context() -> bool:
    return _IN_SKILL_WORKER


def set_orchestrator_terminal_access(active: bool) -> None:
    """Record whether the main agent got the command-execution tools."""
    global _ORCHESTRATOR_HAS_TERMINAL
    _ORCHESTRATOR_HAS_TERMINAL = bool(active)


def caller_has_terminal() -> bool:
    """True when whoever is running right now can execute commands itself.

    A helper always can; the main agent can in every mode but plan.
    """
    return True if _IN_SKILL_WORKER else _ORCHESTRATOR_HAS_TERMINAL


def _refuse_helper_project_write(resolved: str) -> None:
    """Fail fast when a helper tries to change a project file.

    A helper returns data; the main agent makes the changes and sees each of
    their results (PROJECT_DESIGN 2.23). Its own notes go under the scratch area.
    """
    if not _IN_SKILL_WORKER:
        return
    scratch = os.path.realpath(os.path.join(get_project_path(), HELPER_SCRATCH_DIR))
    target = os.path.realpath(resolved)
    try:
        inside = os.path.commonpath([scratch, target]) == scratch
    except ValueError:
        inside = False
    if inside:
        return
    raise ValueError(
        f"Error: helpers do not change project files, and '{_preview(resolved)}' is outside "
        f"the scratch area. Put notes and intermediate files under '{HELPER_SCRATCH_DIR}/' "
        "inside the project. If the task needs this file changed, say so in your report "
        "and give the exact change (path, passage, new text): the main agent applies it."
    )

def set_project_context(session, store=None) -> None:
    global _PROJECT_PATH, _PROJECT_SESSION, _PROJECT_STORE
    _PROJECT_SESSION = session
    _PROJECT_STORE = store
    if session:
        _PROJECT_PATH = os.path.abspath(session.project_path) if getattr(session, "project_path", "") else os.getcwd()
        # Snapshot the mode at context-set time so create_plan() can reference it
        # in audit messages even after the mode has already changed.
        session._initial_mode = getattr(session, "mode", "auto")

        # Ensure .opalatex/ is ignored by the user's own git so internal
        # files (editor state, skill requests) never appear in their git status.
        try:
            proj_gitignore = os.path.join(_PROJECT_PATH, ".gitignore")
            entry = ".opalatex/"
            existing = ""
            if os.path.isfile(proj_gitignore):
                with open(proj_gitignore, "r", encoding="utf-8") as _f:
                    existing = _f.read()
            if entry not in existing:
                with open(proj_gitignore, "a", encoding="utf-8") as _f:
                    if existing and not existing.endswith("\n"):
                        _f.write("\n")
                    _f.write(f"{entry}\n")
                # Untrack any .opalatex/ files already committed to the user's git index.
                subprocess.run(
                    ["git", "rm", "--cached", "-r", "--ignore-unmatch", ".opalatex/"],
                    cwd=_PROJECT_PATH, capture_output=True
                )
        except Exception:
            pass

        # Load project-specific .env file if it exists
        env_path = os.path.join(_PROJECT_PATH, ".env")
        if os.path.isfile(env_path):
            from dotenv import load_dotenv
            try:
                load_dotenv(dotenv_path=env_path, override=True)
            except Exception:
                pass
                
        # Also explicitly propagate api_key and api_base from session if present
        model_name = getattr(session, "model", None)
        alt_model_name = getattr(session, "worker_model", None)
        
        from .api_keys import get_env_var_for_model
        orch_env_var = get_env_var_for_model(model_name) if model_name else None
        worker_env_var = get_env_var_for_model(alt_model_name) if alt_model_name else None

        def env_file_has_var(var_name):
            if os.path.isfile(env_path):
                try:
                    with open(env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.strip().startswith(f"{var_name}="):
                                return True
                except Exception:
                    pass
            return False

        # 1. Orchestrator credentials
        if getattr(session, "api_key", None):
            os.environ["OPENAI_API_KEY"] = session.api_key
            if orch_env_var:
                os.environ[orch_env_var] = session.api_key
        else:
            if not env_file_has_var("OPENAI_API_KEY"):
                os.environ.pop("OPENAI_API_KEY", None)
            if orch_env_var and not env_file_has_var(orch_env_var):
                if not (orch_env_var == worker_env_var and getattr(session, "worker_api_key", None)):
                    os.environ.pop(orch_env_var, None)

        if getattr(session, "api_base", None):
            os.environ["OPENAI_API_BASE"] = session.api_base
        else:
            if not env_file_has_var("OPENAI_API_BASE"):
                os.environ.pop("OPENAI_API_BASE", None)

        # 2. Worker credentials
        if getattr(session, "worker_api_key", None):
            os.environ["WORKER_API_KEY"] = session.worker_api_key
            if worker_env_var:
                os.environ[worker_env_var] = session.worker_api_key
        else:
            if not env_file_has_var("WORKER_API_KEY"):
                os.environ.pop("WORKER_API_KEY", None)
            if worker_env_var:
                if not (worker_env_var == orch_env_var and getattr(session, "api_key", None)) and not env_file_has_var(worker_env_var):
                    os.environ.pop(worker_env_var, None)

        if getattr(session, "worker_api_base", None):
            os.environ["WORKER_API_BASE"] = session.worker_api_base
        else:
            if not env_file_has_var("WORKER_API_BASE"):
                os.environ.pop("WORKER_API_BASE", None)

        # Dynamically inject/register the configured context window (num_ctx) in LiteLLM's model mapping
        # so it doesn't fail with a BadRequestError (request exceeds available context window).
        try:
            import litellm
            from .config import resolve_effective_num_ctx
            model_name = getattr(session, "model", None)
            num_ctx = resolve_effective_num_ctx("memgpt", model_name, getattr(session, "api_base", None))
            if model_name and num_ctx:
                # Direct registration for model in litellm's dynamic model database
                if model_name not in litellm.model_prices_and_context_window:
                    litellm.model_prices_and_context_window[model_name] = {
                        "max_tokens": num_ctx,
                        "max_input_tokens": num_ctx,
                        "max_output_tokens": num_ctx,
                        "context_window": num_ctx,
                    }
                else:
                    litellm.model_prices_and_context_window[model_name]["context_window"] = num_ctx
                    litellm.model_prices_and_context_window[model_name]["max_tokens"] = num_ctx
                    litellm.model_prices_and_context_window[model_name]["max_input_tokens"] = num_ctx
        except Exception:
            pass
    else:
        _PROJECT_PATH = os.getcwd()


def get_project_path() -> str:
    return _PROJECT_PATH or os.getcwd()


def _collect_python_files(root: str, base_dir: str | None = None) -> list[str]:
    """Collect Python files below root while skipping OpalaTex internals."""
    base = os.path.abspath(base_dir or root)
    start = os.path.abspath(root)
    skipped_dirs = {"opalatex", "tests", "skills", "debug", "__pycache__", ".git", ".venv"}
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [d for d in dirnames if d not in skipped_dirs]
        rel_dir = os.path.relpath(dirpath, base)
        if rel_dir != "." and any(part in skipped_dirs for part in rel_dir.split(os.sep)):
            continue
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(os.path.join(dirpath, filename))
    return files


def _resolve_path(path: str) -> str:
    """Make path absolute, rooted at the project directory if relative."""
    if os.path.isabs(path):
        return path
    return os.path.join(get_project_path(), path)


_SAME_NAME_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build"}


def _same_name_candidates(missing: str, limit: int = 5, max_entries: int = 20000) -> list[str]:
    """Project-relative paths of files named like *missing*, for a not-found diagnostic.

    Models routinely guess a path from a filename they saw ("aula03_slides.tex" at
    the project root when it lives in AULA_03/). Naming where the file does exist
    turns the failure into a route; the caller still picks the path itself.
    Bounded by *max_entries* so a huge tree cannot stall the error path.
    """
    name = os.path.basename(missing.rstrip("/\\"))
    root = get_project_path()
    if not name or not os.path.isdir(root):
        return []
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SAME_NAME_SKIP_DIRS)
        seen += len(filenames) + len(dirnames)
        if name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
            if len(found) >= limit:
                break
        if seen >= max_entries:
            break
    return found


def _preview(value: object, max_len: int = 60) -> str:
    """Return a short, single-line preview of any argument value."""
    s = str(value).replace("\n", " ")
    return s[:max_len] + "…" if len(s) > max_len else s

_TEXT_FILE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _read_text_file(path: str) -> str:
    """Read text files with common encoding fallbacks used by LaTeX tools."""
    with open(path, "rb") as f:
        data = f.read()

    if not data:
        return ""

    encodings = list(_TEXT_FILE_ENCODINGS)
    if data.startswith(b"\xff\xfe"):
        encodings.insert(0, "utf-16-le")
    elif data.startswith(b"\xfe\xff"):
        encodings.insert(0, "utf-16-be")

    tried = set()
    for encoding in encodings:
        if encoding in tried:
            continue
        tried.add(encoding)
        try:
            text = data.decode(encoding)
            return text.replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue

    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_logical_text_file(path: str) -> str:
    """Read a text file, or the JSON document inside a packaged JPT."""
    if Path(path).suffix.lower() == ".jpt":
        from .jpt import read_jpt

        return read_jpt(path).text
    return _read_text_file(path)


def _decode_escape_sequences(s: str) -> str:
    """Turn literal \\n \\t \\r \\\\ sequences into the characters they name.

    Used only by `_load_tool_json`, and only after the argument already failed
    to parse as JSON. It must never run over text a tool writes as given: a
    single line of LaTeX or code is full of these sequences (`\\textbf`,
    `\\renewcommand`, `\\newline`, `"a\\nb"`), and the file tools used to run it
    over every single-line edit. In the saved tool calls of a real installation
    it fired 26 times, every one of them on valid LaTeX (`\\textit` became a tab
    and "extit"), and never on the double-escaped content it was written for.
    """
    if "\n" in s:
        return s
    if r"\n" not in s and r"\t" not in s and r"\r" not in s:
        return s
    return (
        s
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\r", "\r")
        .replace(r"\\", "\\")
    )

SAFE_SHELL_COMMANDS = {
    # Linux/macOS
    "ls", "pwd", "cat", "grep", "find", "whoami", "head", "tail", "less", "more", "tree", "cd", "echo",
    # Windows (CMD / PowerShell)
    "dir", "type", "findstr", "Get-ChildItem", "Get-Location", "Get-Content", "Select-String", "tasklist"
}

_DENIED_TOOLS = set()

class UserCancelException(BaseException):
    """Raised to immediately abort the agent turn when a user denies permission."""
    pass


def opalatex_tool(name: str, description: str, is_safe: bool = False):
    """
    Decorator that wraps the agenticblocks tool.
    Enforces 'plan', 'edit', and 'auto' mode safety rules.
    """
    def decorator(func):
        is_async = asyncio.iscoroutinefunction(func)

        def _check_permission(*args, **kwargs):
            mode = getattr(_PROJECT_SESSION, "mode", "auto") if _PROJECT_SESSION else "auto"
            
            # Auto mode or safe tool -> always execute
            if mode == "auto" or is_safe:
                return True

            # Planning never grants execution authority, including permissions
            # remembered from an earlier edit-mode turn. Memory tools are safe
            # and intentionally remain available above.
            if mode == "plan":
                return "Execution blocked: In 'plan' mode, only reading, memory and communication tools are available."
                
            # Check if this tool was set to 'always allow'
            if _PROJECT_SESSION and hasattr(_PROJECT_SESSION, "results"):
                allowed_tools = _PROJECT_SESSION.results.get("allowed_tools", [])
                if name in allowed_tools:
                    return True

            if name in _DENIED_TOOLS:
                return f"Execution blocked: You already asked to use '{name}' and the user DENIED it. Do not try again."

            # Now we are in 'edit' or 'plan' mode, and the tool is NOT safe.
            is_run_command_safe = False
            from .i18n import _ as _translate
            question = _translate("tool_permission_question", tool=name)
            
            if name == "run_command":
                cmd = kwargs.get("command") or (args[0] if args else "")
                base_cmd = cmd.strip().split()[0] if cmd else ""
                is_writing = ">" in cmd or "|" in cmd
                if base_cmd in SAFE_SHELL_COMMANDS and not is_writing:
                    is_run_command_safe = True
                else:
                    question = _translate("command_permission_question", command=cmd)
            
            if is_run_command_safe:
                return True
                
            if mode == "plan":
                return "Execution blocked: In 'plan' mode, you are not allowed to execute operations that modify the system."
                
            if mode == "edit":
                # A monitor's turn has nobody to ask (PROJECT_DESIGN 2.24).
                from .agent_stdin import unattended_refusal, unattended_turn
                if unattended_turn() is not None:
                    return "Execution blocked: " + unattended_refusal(
                        f"'{name}' needs the user's approval in 'edit' mode, which cannot be asked now"
                    )
                return question
                
            return True

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            res = _check_permission(*args, **kwargs)
            if isinstance(res, str):
                if res.startswith("Execution blocked"):
                    return res
                
                # Ask the user for permission. Every front-end answers this the
                # same way (agent_stdin.request_user_input): the GUI over HTTP,
                # the stdin protocol with an `input_response` command, the CLI
                # with a terminal prompt.
                import asyncio
                from opalatex.agent_stdin import request_user_input
                try:
                    raw = await request_user_input(
                        res,
                        options=["yes", "no", "always"],
                        default="yes",
                        input_type="confirm",
                    )
                    ans = str(raw).strip().lower()
                except asyncio.TimeoutError:
                    # An unanswered permission request is not permission.
                    ans = "no"

                if ans not in ("yes", "y", "s", "sim", "true", "1", "always", "a"):
                    _DENIED_TOOLS.add(name)
                    return f"Execution blocked: The user denied permission to use '{name}'. Choose a safe alternative or explain what is needed."
                    
                if ans in ("always", "a"):
                    if _PROJECT_SESSION and hasattr(_PROJECT_SESSION, "results"):
                        if "allowed_tools" not in _PROJECT_SESSION.results:
                            _PROJECT_SESSION.results["allowed_tools"] = []
                        if name not in _PROJECT_SESSION.results["allowed_tools"]:
                            _PROJECT_SESSION.results["allowed_tools"].append(name)

            try:
                if is_async:
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
            except Exception:
                raise

            return result
                
        return as_tool(name=name, description=description)(async_wrapper)
            
    return decorator

# ─── Tools ───────────────────────────────────────────────────────────────────
_BINARY_EXTS: set[str] = {
    ".xlsx", ".xls", ".ods", ".numbers",
    ".pdf", ".docx", ".pptx", ".odt", ".odp", ".doc", ".ppt", ".rtf",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".tar", ".snap", ".whl", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd",
    ".mp3", ".mp4", ".wav", ".ogg", ".avi", ".mov", ".mkv", ".flac",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc", ".pyd", ".class",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".mdb",
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".psd"}

# Binary formats whose text OpalaTex can extract by path (attachments.py). These
# never reach the binary refusal below: read_file extracts them and then treats
# the result like any other text, staged read included.
_DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}

# Spreadsheet formats with no reader at all -- .xlsx is handled above.
_SHEET_EXTS = {".xls", ".ods", ".numbers"}


def _binary_read_error(resolved: str) -> str | None:
    """Return a diagnostic when *resolved* is not text, else None.

    `_read_text_file` falls back to latin-1, which decodes *any* byte sequence
    without raising — so without this check a .xlsx or .pdf read by path returns
    mojibake that looks like content and lands in the context window. Failing
    with a route the caller can actually take is the contract (PROJECT_DESIGN
    2.6, no silent substitution).
    """
    ext = os.path.splitext(resolved)[1].lower()

    binary = ext in _BINARY_EXTS
    if not binary:
        try:
            with open(resolved, "rb") as f:
                head = f.read(8192)
        except OSError:
            return None
        # UTF-16/32 text is full of NUL bytes and _read_text_file handles it via
        # the BOM, so a BOM rules out the NUL sniff.
        if not head.startswith((b"\xff\xfe", b"\xfe\xff")) and b"\x00" in head:
            binary = True

    if not binary:
        return None

    preview = _preview(resolved)

    if ext in _IMAGE_EXTS:
        route = f"Use analyze_image('{preview}', prompt) to inspect it."
    else:
        if ext in _SHEET_EXTS:
            what = f"OpalaTex reads .xlsx but has no reader for {ext}."
            conversion = "an .xlsx or .csv export"
        else:
            what = "This is not a text file."
            conversion = "a conversion to text"

        # Naming a route the caller cannot take is a dead end that pushes models
        # into improvising (PROJECT_DESIGN 2.6). A helper and the main agent both
        # have run_command, except the main agent in plan mode, where commands and
        # helpers are both blocked -- leaving asking the user as its only move.
        if caller_has_terminal():
            how = (
                f"You have run_command: convert it yourself with {conversion} into a text file "
                "inside the project, then read that file."
            )
        else:
            how = (
                "Commands are unavailable in plan mode, so it cannot be converted now: ask the "
                "user to attach the file to the chat or to provide the converted text instead of "
                "planning around content you cannot read."
            )
        route = f"{what} {how} Do not retry read_file on this path."

    return f"Error: '{preview}' is a binary file ({ext or 'no extension'}), not text. {route}"


@opalatex_tool(
    name="read_file",
    is_safe=True,
    description=(
        "Read the contents of a file in the project workspace. Relative paths are resolved "
        "from the project directory. PDF, DOCX, PPTX and XLSX files are supported: their text "
        "is extracted automatically, so read them with this tool instead of asking the user to "
        "convert them by hand. For a packaged .jpt, this returns its logical deck.json rather "
        "than ZIP bytes or internal media."
    ),
)
def read_file(path: str) -> str:
    attachment = _RECENT_FILE_ATTACHMENTS.get(path) or _RECENT_FILE_ATTACHMENTS.get(os.path.basename(path))
    if attachment:
        att_type = attachment.get("type", "")
        if att_type == "pdf_text":
            # Uploads made from this version cache the original on disk and carry
            # a path; history written before that still carries the base64 inline.
            raw_path = attachment.get("raw_path")
            raw_data = attachment.get("raw_data")
            if raw_path and os.path.exists(raw_path):
                try:
                    from opalatex.attachments import extract_document_text_from_path
                    return extract_document_text_from_path(raw_path)
                except Exception as e:
                    raise ValueError(f"Error extracting attached document '{attachment.get('name', path)}': {e}")
            if raw_data:
                try:
                    from opalatex.attachments import extract_document_text
                    return extract_document_text(
                        raw_data,
                        attachment.get("raw_mime") or attachment.get("mime", ""),
                        attachment.get("name", path),
                    )
                except Exception as e:
                    raise ValueError(f"Error extracting attached document '{attachment.get('name', path)}': {e}")
            # No original available (old chat whose cache entry is gone): the
            # extracted text is exactly what the model was shown for this turn.
            return attachment.get("data", "")
        if att_type == "image":
            raise ValueError(
                f"'{path}' is an image attachment. Use analyze_image('{path}', prompt) to inspect it."
            )

    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")

    AGENT_PROGRESS.update("read_file", f"path={_preview(resolved)}")

    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory, not a file. Use get_project_overview() to view contents.")
        if not os.path.exists(resolved):
            # Same rule as the binary-file route below: name only tools the caller
            # holds (PROJECT_DESIGN 2.6), and only the route of the current mode: a
            # small model given a conditional "if you are in plan mode, use
            # create_plan" while in auto called run_skill("create_plan") -- a tool
            # that does not exist there. Without a terminal the caller is the main
            # agent in plan mode; a helper writes only to its scratch area.
            if _IN_SKILL_WORKER:
                create = (
                    f"If you are trying to create it: helpers write only under '{HELPER_SCRATCH_DIR}/'; "
                    "for a project file, put its path and content in your report."
                )
            elif caller_has_terminal():
                create = "If you are trying to create it, use 'write_file' instead."
            else:
                create = (
                    "If you are trying to create it: nothing is written in plan mode; include the "
                    "file in the plan you propose with create_plan."
                )
            # search_code matches file *contents*, so it cannot find a file by
            # name; get_project_overview is the listing route.
            candidates = _same_name_candidates(resolved)
            if candidates:
                where = (
                    "A file with this name exists at: " + ", ".join(candidates)
                    + ". If that is the file you meant, read it by that path."
                )
            else:
                where = (
                    "No file with this name exists anywhere in the project. If you expected one, "
                    "list the project with get_project_overview instead of guessing the path."
                )
            raise ValueError(f"Error: file not found: {_preview(resolved)}. {where} {create}")
    except OSError as e:
        # Catch cases like [Errno 36] File name too long if path contains code
        raise ValueError(f"Error: invalid path argument ({e.strerror}). 'read_file' expects a file path, not file contents.")

    # A PDF/DOCX/PPTX/XLSX in the workspace is not text, but it does have text
    # inside it. Extract it here so the rest of read_file (the context budget
    # check) treats it exactly like any other document, instead of refusing a
    # file whose content OpalaTex can actually produce.
    extracted = None
    logical_text = None
    if Path(resolved).suffix.lower() == ".jpt":
        try:
            logical_text = _read_logical_text_file(resolved)
        except Exception as error:
            raise ValueError(f"Error reading JPT package '{_preview(resolved)}': {error}")
    if os.path.splitext(resolved)[1].lower() in _DOC_EXTS:
        from .attachments import extract_document_text_from_path

        AGENT_PROGRESS.update("read_file", f"extracting {_preview(resolved)}")
        try:
            extracted = extract_document_text_from_path(resolved)
        except ImportError as e:
            raise ValueError(
                f"Error: '{_preview(resolved)}' needs a document extractor that is not "
                f"installed ({e}). Ask the user to install it, or to provide a text export."
            )
        except Exception as e:
            raise ValueError(
                f"Error extracting text from '{_preview(resolved)}': {e}. The file may be "
                "corrupt, password-protected, or empty. Do not retry read_file on this path."
            )
        if not extracted.strip():
            raise ValueError(
                f"Error: '{_preview(resolved)}' was read but contains no extractable text "
                "(a scanned/image-only PDF, or an empty document). Do not retry read_file on "
                "this path; use analyze_image on the page images, or ask the user for a text version."
            )

    if extracted is None:
        if logical_text is None:
            binary_error = _binary_read_error(resolved)
            if binary_error:
                raise ValueError(binary_error)
    else:
        budget_chars = free_context_chars()
        if budget_chars is not None and budget_chars > 0 and len(extracted) > budget_chars:
            how = "Use read_document(path, offset=0, limit=10000) and follow next_offset to read the extracted text without creating files."
            raise ValueError(
                f"Error: the text extracted from '{_preview(resolved)}' is {len(extracted):,} "
                f"characters and does not fit the remaining context budget "
                f"(~{budget_chars:,} characters). Do not retry read_file on this path, and do "
                f"not call read_content_pos on it — it is a binary container. {how}"
            )
        header = (
            f"[Text extracted from '{os.path.basename(resolved)}' "
            f"({os.path.splitext(resolved)[1].lower().lstrip('.')}). Layout and formatting are "
            "not preserved.]\n"
        )
        return header + extracted

    # A whole-file read is the fastest way to destroy a context window: the
    # result is appended to history, MemGPT cannot evict the current user turn
    # to make room, and the provider then truncates the request from the front —
    # silently dropping the very question being answered. Refuse the read with a
    # diagnostic instead of returning content that cannot fit (PROJECT_DESIGN
    # 2.6, no silent substitution): the model is told how to page through the
    # file rather than having its request quietly turned into a partial one.
    budget_chars = free_context_chars()
    try:
        size = (len(logical_text.encode("utf-8")) if logical_text is not None
                else os.path.getsize(resolved))
    except OSError:
        size = 0
    if budget_chars is not None and size > budget_chars:
        window = context_window_tokens()
        used = used_context_tokens()
        if budget_chars <= 0:
            raise ValueError(
                f"Error: the context window is exhausted ({used:,} of {window:,} tokens "
                f"already used), so '{_preview(resolved)}' cannot be read. Summarize what "
                "you already know and answer, or ask the user to start a new chat or raise "
                "num_ctx in the project settings."
            )
        # Structured data/log files should be routed to a specialized skill
        # rather than paged through manually.  The redirect is strongest here
        # because weak models obey error-message guidance more reliably than
        # system-prompt instructions.
        _DATA_EXTS = {".jsonl", ".csv", ".tsv", ".log"}
        ext = os.path.splitext(resolved)[1].lower()
        if ext in _DATA_EXTS:
            if _IN_SKILL_WORKER:
                # A helper has no run_skill tool, so telling it to "delegate via
                # run_skill" is a dead end.
                routing_hint = (
                    "IMPORTANT: You are a helper and do NOT have a run_skill tool. "
                    "Use the processing script or command listed in your skill instructions "
                    "(via run_command) to inspect or condense this file instead of reading it "
                    "directly. If your skill has no such script, report back that the file is "
                    "too large to process directly."
                )
            elif caller_has_terminal():
                routing_hint = (
                    "IMPORTANT: Check your helpers list for a log/data processing helper and "
                    "delegate to it with run_skill. If none is listed, write a small streaming "
                    "Python script with write_file that prints a short digest, and run it "
                    "yourself with run_python_script."
                )
            else:
                routing_hint = (
                    "IMPORTANT: In plan mode, helpers and scripts are unavailable. Sample the "
                    "file with read_content_pos or search_code, and put the processing step "
                    "in the plan you propose with create_plan."
                )
            raise ValueError(
                f"Error: '{_preview(resolved)}' is a large data/log file "
                f"({size:,} bytes, ~{size // 4:,} tokens) and does not fit "
                f"the remaining context budget (~{budget_chars // 4:,} tokens "
                f"of the {window:,}-token window, {used:,} already used). "
                f"Do NOT retry read_file or read_content_pos on this path. "
                f"{routing_hint}"
            )
        raise ValueError(
            f"Error: '{_preview(resolved)}' is {size:,} bytes (~{size // 4:,} tokens) and does "
            f"not fit the remaining context budget (~{budget_chars // 4:,} tokens of the "
            f"{window:,}-token window, {used:,} already used). Do not retry read_file on this "
            f"path. Use search_code(pattern, path) to locate the relevant lines, then "
            f"read_content_pos(path, start_pos, end_pos) to read only that range."
        )

    try:
        text = logical_text if logical_text is not None else _read_text_file(resolved)
    except Exception as e:
        raise ValueError(f"Error reading {_preview(resolved)}: {e}")
    _note_file_seen(resolved)
    return text

@opalatex_tool(name="analyze_image", is_safe=True, description="Analyzes an image file using computer vision and returns a text description. Use this when the user asks you to look at a chart, UI mockup, or any image file.")
def analyze_image(file_path: str, prompt: str = "Describe this image in detail") -> str:
    model = ""
    try:
        import base64
        import mimetypes
        import litellm
        from .config import get_agent_llm_kwargs, get_agent_model, sanitize_litellm_kwargs_for_model
        
        attachment = _RECENT_FILE_ATTACHMENTS.get(file_path) or _RECENT_FILE_ATTACHMENTS.get(os.path.basename(file_path))
        if attachment:
            resolved = file_path
            encoded_img = attachment.get("data", "")
            content_type = attachment.get("mime") or "image/jpeg"
        else:
            resolved = _resolve_path(file_path)
        if not attachment and (not os.path.exists(resolved) or os.path.isdir(resolved)):
            return "infelizmente não consegui analisar sua imagem: arquivo não encontrado ou é um diretório."
            
        if not attachment:
            content_type, _ = mimetypes.guess_type(resolved)
            if not content_type or not content_type.startswith("image/"):
                content_type = "image/jpeg"
            
        if not attachment:
            with open(resolved, "rb") as f:
                encoded_img = base64.b64encode(f.read()).decode("utf-8")
            
        if _IN_SKILL_WORKER and _HELPER_MODEL:
            # A helper looks through its own model: that is the model whose
            # vision the main agent was told about when it chose the helper
            # (PROJECT_DESIGN 2.23), and the helper only holds this tool when
            # that model accepts images.
            kwargs = get_agent_llm_kwargs("worker")
            model = _HELPER_MODEL
        else:
            kwargs = get_agent_llm_kwargs("memgpt")

            # The main agent's model, since it is the one using the tool.
            model = get_agent_model("memgpt")
            if _PROJECT_SESSION:
                project_override = getattr(_PROJECT_SESSION, "model", None)
                if project_override:
                    model = project_override
                
        kwargs.pop("stream", None)
        
        if kwargs.get("api_base"):
            if model.startswith("ollama/") or model.startswith("ollama_chat/"):
                if kwargs["api_base"].endswith("/v1"):
                    kwargs["api_base"] = kwargs["api_base"][:-3]
                elif kwargs["api_base"].endswith("/v1/"):
                    kwargs["api_base"] = kwargs["api_base"][:-4]
        kwargs = sanitize_litellm_kwargs_for_model(model, kwargs)
        kwargs.pop("stream", None)

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{content_type};base64,{encoded_img}"}
                    }
                ]
            }
        ]
        
        AGENT_PROGRESS.update("analyze_image", f"path={_preview(resolved)}")
        response = litellm.completion(model=model, messages=messages, **kwargs)
        return response.choices[0].message.content or "infelizmente não consegui analisar sua imagem."
        
    except litellm.exceptions.BadRequestError as e:
        return f"CRITICAL ERROR: The model ({model}) rejected the request. Explicitly inform the user that this model likely does not support Computer Vision (not multimodal) or the image is too large. Technical detail: {str(e)}"
    except litellm.exceptions.AuthenticationError as e:
        return f"CRITICAL ERROR: Authentication failed for model {model}. EXPLÍCITLY tell the user that they need to specify the API Key for this model in the top bar (under 'Edit Models'). Technical detail: {str(e)}"
    except litellm.exceptions.APIConnectionError as e:
        return f"CRITICAL ERROR: Connection failed with the model's API ({model}). Tell the user to verify if the local server (e.g., Ollama) is running or if the URL is correct. Technical detail: {str(e)}"
    except Exception as e:
        return f"CRITICAL ERROR: An internal error occurred while analyzing the image using the model {model}. Tell the user the following reason: {str(e)}"


# ─── Image generation ─────────────────────────────────────────────────────────
# One tool, several providers: the transport lives in
# `agenticblocks.blocks.image`, and which provider answers is a catalog entry
# plus an api_base (Settings > Image Generation). The tool itself only knows
# "prompt in, file on disk out".

_IMAGE_ERROR_GUIDANCE = {
    "auth": (
        "Tell the user the image model's API key is missing or was rejected, and "
        "that they can set it in the top bar under 'Edit Models'."
    ),
    "connection": (
        "Tell the user the image endpoint could not be reached: if it is a local "
        "server (LocalAI, Docker Model Runner, vLLM-Omni, Ollama), it must be "
        "running and its api_base must match what is registered in 'Edit Models'."
    ),
    "unsupported": (
        "Tell the user this endpoint has no image generation route. A local "
        "Ollama only serves one on builds where image generation is available "
        "(currently macOS); on Linux/Windows use LocalAI, Docker Model Runner or "
        "vLLM-Omni, or point api_base at a machine that does serve it."
    ),
    "bad_request": (
        "The provider rejected the request. Report the technical detail to the "
        "user: usually the model id or the requested size is not supported."
    ),
    "empty": (
        "The provider accepted the request but returned no image. This is often a "
        "content filter. Report it to the user instead of retrying the same prompt."
    ),
    "unknown_route": (
        "The configured image route does not exist. Tell the user to review "
        "Settings > Image Generation."
    ),
    "not_configured": (
        "Tell the user to configure an image model in Settings > Image Generation."
    ),
}


def _image_filename_stem(filename: str, prompt: str) -> str:
    """Return a safe file stem from *filename*, or one derived from *prompt*."""
    raw = (filename or "").strip().replace("\\", "/")
    raw = raw.split("/")[-1]
    raw = os.path.splitext(raw)[0]
    if not raw:
        raw = "-".join((prompt or "image").lower().split())[:48]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return stem or "image"


def _unique_image_path(directory: str, stem: str, ext: str) -> str:
    """Return a path inside *directory* that does not overwrite an existing file."""
    candidate = os.path.join(directory, f"{stem}{ext}")
    index = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}-{index}{ext}")
        index += 1
    return candidate


def _format_byte_size(count: int) -> str:
    """Return a human-readable size; small files must not read as "0 KB"."""
    if count < 1024:
        return f"{count} bytes"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _write_generated_image(target_path: str, data: bytes) -> tuple[int, str]:
    """Write *data* to *target_path*; return (byte count, "WxH" or "")."""
    Path(target_path).parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "wb") as f:
        f.write(data)

    dimensions = ""
    try:
        from PIL import Image

        with Image.open(target_path) as img:
            dimensions = f"{img.width}x{img.height}"
    except Exception:
        pass
    return len(data), dimensions


@opalatex_tool(
    name="generate_image",
    is_safe=False,
    description=(
        "Generate an image from a text prompt and save it as a file inside the "
        "project, returning its project-relative path. Use it when the user asks "
        "for an illustration, figure or diagram that does not exist yet. The "
        "prompt should be a detailed visual description in English. Never use it "
        "to render LaTeX, plots or charts -- those belong in TikZ/pgfplots code. "
        "The returned path can be shown in chat as Markdown (![caption](path)) "
        "and included in LaTeX with \\includegraphics."
    ),
)
async def generate_image(
    prompt: str,
    filename: str = "",
    size: str = "",
    negative_prompt: str = "",
    seed: int = -1,
) -> str:
    from agenticblocks.blocks.image import (
        ImageGenerationError,
        ImageGenerationInput,
        extension_for_mime,
        resolve_image_bytes,
    )

    from .image_gen_config import build_block, configuration_problem, load_config

    if not (prompt or "").strip():
        return "CRITICAL ERROR: generate_image requires a non-empty 'prompt'."

    problem = configuration_problem()
    if problem:
        return f"CRITICAL ERROR: {problem}"

    cfg = load_config()
    AGENT_PROGRESS.update("generate_image", f"prompt={_preview(prompt)}")

    try:
        block = build_block()
        request = ImageGenerationInput(
            prompt=prompt,
            n=1,
            size=(size or cfg.get("size") or "").strip(),
            negative_prompt=(negative_prompt or "").strip(),
            seed=(int(seed) if seed is not None and int(seed) >= 0 else None),
        )
        output = await block.run(request)
        data, mime = await resolve_image_bytes(output.images[0])
    except ImageGenerationError as e:
        guidance = _IMAGE_ERROR_GUIDANCE.get(e.kind, "Report the failure to the user.")
        return f"CRITICAL ERROR: {e} {guidance}"
    except Exception as e:
        return (
            "CRITICAL ERROR: An internal error occurred while generating the image. "
            f"Tell the user the following reason: {e}"
        )

    output_dir = str(cfg.get("output_dir") or "figures")
    target_dir = _resolve_path(output_dir)
    stem = _image_filename_stem(filename, prompt)
    target_path = _unique_image_path(target_dir, stem, extension_for_mime(mime))

    try:
        byte_count, dimensions = await asyncio.to_thread(
            _write_generated_image, target_path, data
        )
    except OSError as e:
        raise ValueError(f"Error saving the generated image: {e}")

    relative = os.path.relpath(target_path, get_project_path()).replace(os.sep, "/")
    latex_path = os.path.splitext(relative)[0]
    detail = f"{dimensions}, " if dimensions else ""

    # The bytes never enter the conversation: a 1024x1024 PNG in base64 is over a
    # megabyte of context, and the file on disk is the deliverable anyway
    # (PROJECT_DESIGN 2.6, tool results are bounded by the remaining window).
    return (
        f"Image generated with {output.model} and saved to {relative} "
        f"({detail}{_format_byte_size(byte_count)}).\n"
        f"To show it to the user in chat, write the Markdown ![{stem}]({relative}).\n"
        f"To place it in a LaTeX document, use \\includegraphics[width=0.8\\textwidth]{{{latex_path}}} "
        f"inside a figure environment."
    )

@opalatex_tool(name="write_file", is_safe=False, description="Write or overwrite a file inside the project directory. Relative paths are resolved from the project directory. Creates parent directories if needed. For a .jpt, pass the complete logical deck JSON; this tool writes the validated package and internalizes all local assets. ALWAYS use this tool to save file content — never use run_command with echo/printf/cat to write files, as shell quoting will break with multi-line or HTML/JSON content.")
def write_file(path: str, content: str) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")
    _refuse_helper_project_write(resolved)

    AGENT_PROGRESS.update("write_file", f"path={_preview(resolved)}")
    
    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory. Cannot write to a directory.")
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Error: invalid path argument ({e.strerror}).")

    try:
        if Path(resolved).suffix.lower() == ".jpt":
            from .jpt import write_packaged_jpt

            result = write_packaged_jpt(
                resolved, content, project_root=get_project_path()
            )
        else:
            before = _text_before_write(resolved)
            with open(resolved, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        try:
            from .code_index import CODE_INDEX
            CODE_INDEX.rebuild_file(resolved)
        except Exception:
            pass

        if Path(resolved).suffix.lower() == ".jpt":
            return (
                f"Successfully wrote packaged JPT to {_preview(resolved)} "
                f"with {result.assets} internal asset(s)."
            )
        _note_file_seen(resolved)
        return f"Successfully wrote to {_preview(resolved)}." + _syntax_note(resolved, before)
    except Exception as e:
        raise ValueError(f"Error writing {_preview(resolved)}: {e}")


@opalatex_tool(
    name="export_tex_to_docx",
    is_safe=False,
    description=(
        "Export a .tex file in the project to .docx using Pandoc. "
        "Use this when the user asks to convert LaTeX/TeX source to Word. "
        "The input and output paths are project-relative. Pandoc must be installed."
    ),
)
def export_tex_to_docx(tex_path: str, output_path: str = "") -> str:
    resolved_project = get_project_path()
    AGENT_PROGRESS.update("export_tex_to_docx", f"path={_preview(tex_path)}")
    try:
        from .document_exporter import export_tex_to_docx as _export_tex_to_docx
        result = _export_tex_to_docx(resolved_project, tex_path, output_path)
    except Exception as e:
        raise ValueError(f"Error exporting DOCX: {e}")
    if not result.get("success"):
        raise ValueError(result.get("log") or "DOCX export failed.")
    return f"Successfully exported DOCX to {result.get('relative_output_path') or result.get('output_path')}."


def _require_extension(path: str, allowed: set[str]) -> None:
    ext = Path(path).suffix.lower()
    if ext not in allowed:
        raise ValueError(f"Output path must end with one of: {', '.join(sorted(allowed))}")


@opalatex_tool(
    name="create_docx_file",
    is_safe=False,
    description=(
        "Create a Word .docx file directly from simple Markdown-like text. "
        "Supports headings beginning with #, ##, ###, bullet lines beginning with '- ', "
        "numbered lines like '1. ', and plain paragraphs. Use this instead of writing raw binary DOCX."
    ),
)
def create_docx_file(path: str, markdown: str, title: str = "") -> str:
    try:
        resolved = _resolve_path(path)
        _require_extension(resolved, {".docx"})
    except Exception as e:
        raise ValueError(f"Error resolving DOCX path: {e}")

    AGENT_PROGRESS.update("create_docx_file", f"path={_preview(resolved)}")
    try:
        from docx import Document
    except ImportError:
        raise ValueError("python-docx is not installed. Install project dependencies before creating DOCX files directly.")

    try:
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        doc = Document()
        if title:
            doc.add_heading(title, level=1)
        for raw_line in markdown.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("### "):
                doc.add_heading(line[4:].strip(), level=3)
            elif line.startswith("## "):
                doc.add_heading(line[3:].strip(), level=2)
            elif line.startswith("# "):
                doc.add_heading(line[2:].strip(), level=1)
            elif line.startswith("- "):
                doc.add_paragraph(line[2:].strip(), style="List Bullet")
            elif len(line) > 3 and line[0].isdigit() and line[1:3] == ". ":
                doc.add_paragraph(line[3:].strip(), style="List Number")
            else:
                doc.add_paragraph(line)
        doc.save(resolved)
        return f"Successfully created DOCX file at {_preview(resolved)}."
    except Exception as e:
        raise ValueError(f"Error creating DOCX file {_preview(resolved)}: {e}")


@opalatex_tool(
    name="create_pptx_file",
    is_safe=False,
    description=(
        "Create a PowerPoint .pptx file directly from a JSON slide outline. "
        "slides_json must be an array of objects like "
        "[{\"title\":\"Slide title\",\"bullets\":[\"Point one\",\"Point two\"]}]."
    ),
)
def create_pptx_file(path: str, slides_json: str, title: str = "") -> str:
    try:
        resolved = _resolve_path(path)
        _require_extension(resolved, {".pptx"})
    except Exception as e:
        raise ValueError(f"Error resolving PPTX path: {e}")

    AGENT_PROGRESS.update("create_pptx_file", f"path={_preview(resolved)}")
    try:
        from pptx import Presentation
    except ImportError:
        raise ValueError("python-pptx is not installed. Install project dependencies before creating PPTX files directly.")

    try:
        # Parse first, repair only on failure: the escape repair fires on any
        # string containing \n, \t or \r, so running it over valid JSON turns a
        # bullet holding `C:\temp` or `\theta` into a tab. See _load_tool_json.
        slides = _load_tool_json(slides_json, "slides_json")
        if not isinstance(slides, list):
            raise ValueError("slides_json must be a JSON array")
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        prs = Presentation()
        if title:
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = title
            slide.placeholders[1].text = ""
        for item in slides:
            if not isinstance(item, dict):
                continue
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = str(item.get("title", "Untitled slide"))
            body = slide.placeholders[1].text_frame
            body.clear()
            bullets = item.get("bullets", [])
            if isinstance(bullets, str):
                bullets = [line.strip() for line in bullets.splitlines() if line.strip()]
            if not bullets:
                bullets = [str(item.get("body", ""))] if item.get("body") else []
            for index, bullet in enumerate(bullets):
                paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
                paragraph.text = str(bullet)
                paragraph.level = 0
        prs.save(resolved)
        return f"Successfully created PPTX file at {_preview(resolved)}."
    except Exception as e:
        raise ValueError(f"Error creating PPTX file {_preview(resolved)}: {e}")


# ─── Presentations (.jpt) ─────────────────────────────────────────────────────
# The native counterpart of create_pptx_file: a .pptx an agent writes is a file
# it cannot reopen, while a .jpt is the format the IDE's own slide editor edits.
# Both take a semantic outline rather than coordinates, for the same reason —
# an author that has to invent geometry produces text past the edge of its box
# and a title that moves between slides. Everything geometric is computed by
# opalatex/jpt/layout.py, and the result is checked by opalatex/jpt/lint.py
# before it reaches disk. See docs/specs/jpt_format.md §17.

_JPT_OUTLINE_HELP = (
    "outline_json is {\"title\": str, \"slides\": [...]} — or just the array of "
    "slides. Each slide is {\"layout\": ..., ...} where layout is one of: "
    "'title' (title, subtitle), 'section' (title, subtitle), 'bullets' (title, "
    "bullets: an array whose items are strings, or {\"text\": str, \"level\": 0-4} "
    "for a sub-point — NEVER type a marker character into a bullet, the box "
    "draws its own; optional 'bulletStyle': disc|dash|number), "
    "'text' (title, text), 'equation' (title, equation: "
    "LaTeX WITHOUT $ delimiters, caption), 'image' (title, image: project-relative "
    "path, caption), 'two_columns' (title, left, right — each an object holding "
    "ONE of bullets/text/equation/image), 'image_text' (title, image, bullets, "
    "side: left|right), 'quote' (quote, attribution), 'blank' (title). Any slide "
    "also takes 'notes' (speaker notes) and 'elements' (raw .jpt elements, an "
    "escape hatch for what the layouts cannot express). A slide also takes "
    "'background' (#rrggbb), 'backgroundImage' (a project-relative path or data "
    "URI, null for none), 'backgroundFit' (cover|contain|fill) and "
    "'backgroundOpacity' (0-1 — dim a photo to about 0.4 so text stays "
    "readable over it). Top level also takes 'theme' {background, "
    "backgroundImage, backgroundFit, backgroundOpacity, color, accent, "
    "fontFamily} for a deck-wide background, and 'slide_numbers': true. "
    "Pictures and video files referenced by project path are PACKAGED inside the "
    ".jpt, so the deck is one self-contained file the user can move or send; "
    "the source files on disk are left where they are. Web-player URLs such as "
    "YouTube remain links to their network services. The file on disk is a ZIP "
    "package containing deck.json and jpt:assets/... members; read_file exposes "
    "the logical JSON, and line-position tools must not be used on it. "
    "NEVER write x/y/w/h or fontSize: the layout computes them."
)


def _load_tool_json(text: str, field: str):
    """Parse a JSON tool argument without mangling what is inside it.

    `_decode_escape_sequences` exists for models that emit a literal `\\n`
    where a newline belongs, and it fires on any string containing `\\n`,
    `\\t` or `\\r`. A JSON document carrying LaTeX contains those by the
    dozen — `\\top`, `\\rm`, `\\nabla` — and running the repair over valid
    JSON turns `\\top` into a tab followed by "op". So the repair runs only
    after a real parse failure, which is the only time it was ever meant to.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as first:
        try:
            return json.loads(_decode_escape_sequences(text))
        except json.JSONDecodeError:
            raise ValueError(f"{field} is not valid JSON: {first}") from first


def _jpt_write(resolved: str, deck: dict, *, verb: str) -> str:
    """Lint and atomically write a self-contained JPT package."""
    from .jpt import format_report, has_errors, lint, write_packaged_jpt

    findings = lint(deck, project_root=get_project_path())
    if has_errors(findings):
        # Fail fast and say exactly what is wrong: a deck written with text
        # spilling off the slide is worse than no deck, because nobody finds
        # out until it is on a projector.
        raise ValueError(
            f"The presentation was NOT written — fix these and call again:\n"
            f"{format_report(findings)}"
        )

    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    result = write_packaged_jpt(
        resolved, deck, project_root=get_project_path()
    )

    slides = len(deck["slides"])
    summary = f"{verb} {_preview(resolved)} — {slides} slide{'s' if slides != 1 else ''}."
    summary += (
        f" Packaged {result.assets} internal asset"
        f"{'s' if result.assets != 1 else ''} ({result.asset_bytes:,} bytes)."
    )
    if result.external_urls:
        summary += (
            " Web-player/network URLs remain external services: "
            + ", ".join(result.external_urls)
            + "."
        )
    if findings:
        summary += f"\n{format_report(findings)}"
    return summary


@opalatex_tool(
    name="create_presentation",
    is_safe=False,
    description=(
        "Create an OpalaTex presentation (.jpt) that opens in the IDE's slide "
        "editor, from an outline. The layout — position, size and font size of "
        "every element — is computed for you from a grid, so slides are "
        "consistent and text always fits. " + _JPT_OUTLINE_HELP + " The deck is "
        "checked before it is saved: errors (text that cannot fit at a readable "
        "size, an element off the slide, a missing image, malformed LaTeX) abort "
        "the write and are reported so you can shorten the text or fix the path. "
        "Prefer this over create_pptx_file when the user will keep working on "
        "the deck; .pptx export is available from the editor."
    ),
)
def create_presentation(path: str, outline_json: str, title: str = "") -> str:
    from .jpt import JptError, compile_outline

    resolved = _resolve_path(path)
    _require_extension(resolved, {".jpt"})
    AGENT_PROGRESS.update("create_presentation", f"path={_preview(resolved)}")

    outline = _load_tool_json(outline_json, "outline_json")
    # The array form is what create_pptx_file takes, so an agent that knows one
    # tool can use the other without relearning the shape.
    if isinstance(outline, list):
        outline = {"slides": outline}
    if not isinstance(outline, dict):
        raise ValueError("outline_json must be an object or an array of slides")
    if title:
        outline["title"] = title
    outline.setdefault("title", Path(resolved).stem)

    try:
        deck = compile_outline(outline)
    except JptError as error:
        raise ValueError(f"The outline cannot be laid out: {error}")
    return _jpt_write(resolved, deck, verb="Created")


@opalatex_tool(
    name="edit_presentation",
    is_safe=False,
    description=(
        "Apply a list of edits to an existing .jpt presentation. operations_json "
        "is an array of objects: "
        "{\"op\":\"add_slide\",\"at\":<index>,\"slide\":{...outline slide...}}, "
        "{\"op\":\"delete_slide\",\"slide\":\"<id>\"}, "
        "{\"op\":\"move_slide\",\"from\":<i>,\"to\":<j>}, "
        "{\"op\":\"set_notes\",\"slide\":\"<id>\",\"notes\":\"...\"}, "
        "{\"op\":\"set_background\",\"slide\":\"<id>\",\"background\":\"#rrggbb\","
        "\"backgroundImage\":\"figures/photo.jpg\",\"backgroundFit\":\"cover\","
        "\"backgroundOpacity\":0.4} (use \"all\":true instead of \"slide\" to set the "
        "whole deck's background; backgroundImage null removes it), "
        "{\"op\":\"update_element\",\"element\":\"<id>\",\"patch\":{...}}, "
        "{\"op\":\"delete_element\",\"element\":\"<id>\"}, "
        "{\"op\":\"reorder_element\",\"element\":\"<id>\",\"direction\":\"front|forward|backward|back\"}, "
        "{\"op\":\"embed_images\"} is accepted as an idempotent compatibility "
        "operation; every save already packages all local pictures and videos. "
        "A slide added this way is laid out by the same engine create_presentation "
        "uses. Read the file first (read_file) to learn the ids. The edited deck "
        "is checked before it is saved, exactly as on creation. Every local asset "
        "added by an edit is packaged into the .jpt on save."
    ),
)
def edit_presentation(path: str, operations_json: str) -> str:
    from . import jpt as jptlib
    from .jpt import JptError

    resolved = _resolve_path(path)
    _require_extension(resolved, {".jpt"})
    AGENT_PROGRESS.update("edit_presentation", f"path={_preview(resolved)}")
    if not os.path.exists(resolved):
        raise ValueError(f"No presentation at {_preview(resolved)} — create it first.")

    operations = _load_tool_json(operations_json, "operations_json")
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations_json must be a non-empty array of operations")

    try:
        deck = jptlib.parse(jptlib.read_jpt(resolved).text)
    except (JptError, jptlib.JptPackageError) as error:
        raise ValueError(f"{_preview(resolved)} is not a usable presentation: {error}")
    grid = jptlib.Grid(deck["width"], deck["height"])
    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise ValueError(f"operation {index} is not an object")
        op = operation.get("op")
        try:
            if op == "add_slide":
                slide = jptlib.build_slide(grid, deck["theme"], operation.get("slide") or {})
                jptlib.add_slide(deck, slide, operation.get("at"))
            elif op == "delete_slide":
                jptlib.delete_slide(deck, str(operation["slide"]))
            elif op == "move_slide":
                jptlib.move_slide(deck, int(operation["from"]), int(operation["to"]))
            elif op == "set_notes":
                jptlib.find_slide(deck, str(operation["slide"]))["notes"] = str(operation.get("notes") or "")
            elif op == "set_background":
                # Colour, picture, fit and opacity in one operation, and `all`
                # writes them onto the theme so every slide follows — the same
                # two choices the editor's own control offers.
                target = (deck["theme"] if operation.get("all")
                          else jptlib.find_slide(deck, str(operation["slide"])))
                for key, field in (("background", "background"),
                                   ("backgroundImage", "backgroundImage"),
                                   ("backgroundFit", "backgroundFit"),
                                   ("backgroundOpacity", "backgroundOpacity")):
                    if key in operation:
                        target[field] = operation[key]
            elif op == "add_element":
                raw = dict(operation["element"])
                jptlib.add_element(deck, str(operation["slide"]),
                                   jptlib.create_element(raw.pop("type"), **raw))
            elif op == "update_element":
                jptlib.update_element(deck, str(operation["element"]), operation.get("patch") or {})
            elif op == "delete_element":
                jptlib.delete_element(deck, str(operation["element"]))
            elif op == "embed_images":
                # Compatibility with older callers. Packaging is unconditional.
                pass
            elif op == "reorder_element":
                jptlib.reorder_element(deck, str(operation["element"]),
                                       str(operation.get("direction") or "front"))
            else:
                raise ValueError(f"unknown op {op!r}")
        except (JptError, KeyError, TypeError, ValueError) as error:
            # Nothing has been written yet, so the file on disk is untouched:
            # the agent gets a diagnostic and the user keeps their deck.
            raise ValueError(f"operation {index} ({op}) failed: {error}")

    return _jpt_write(resolved, deck, verb="Updated")


@opalatex_tool(
    name="set_presentation_theme",
    is_safe=False,
    description=(
        "Give a .jpt presentation a theme — colours, type, an optional background "
        "picture, and the header/footer bands a Beamer-style theme draws. Pass "
        "`theme` with the name of a theme from the Asset Store (call it with an "
        "unknown name to see the catalogue), and/or `fields_json` with explicit "
        "values to set or override: {\"background\",\"color\",\"accent\","
        "\"fontFamily\",\"backgroundImage\",\"backgroundFit\",\"backgroundOpacity\","
        "\"headerHeight\",\"headerColor\",\"titleColor\",\"footerHeight\","
        "\"footerColor\",\"footerTextColor\",\"footerText\"}. A header band is "
        "drawn only behind a slide that has a title, and each slide's title is "
        "marked so it takes the theme's title colour. The deck is checked before "
        "it is saved, exactly as on creation."
    ),
)
def set_presentation_theme(path: str, theme: str = "", fields_json: str = "") -> str:
    from . import jpt as jptlib
    from .jpt import JptError
    from .assetstore import list_assets, theme_image_path

    resolved = _resolve_path(path)
    _require_extension(resolved, {".jpt"})
    AGENT_PROGRESS.update("set_presentation_theme", f"path={_preview(resolved)} theme={theme}")
    if not os.path.exists(resolved):
        raise ValueError(f"No presentation at {_preview(resolved)} — create it first.")
    if not theme and not fields_json:
        raise ValueError("pass a store theme name, explicit fields_json, or both")

    # A named theme *replaces* the deck's look: every field it does not set goes
    # back to the format's default. Merging instead would leave the previous
    # theme's header band standing behind the new theme's colours, which is a
    # deck that looks like neither. Explicit `fields_json` merges, because that
    # is a tweak rather than a choice of theme.
    values: dict = {}
    applied_name = ""
    if theme:
        catalog = list_assets("theme")
        wanted = theme.strip().lower()
        match = next(
            (a for a in catalog
             if a["id"].lower() == wanted or str(a.get("name", "")).lower() == wanted),
            None,
        )
        if match is None:
            # The catalogue is the discovery path: an unknown name answers with
            # what does exist rather than sending the agent hunting for a second
            # tool that lists them.
            names = ", ".join(f"{a['id']} ({a['name']})" for a in catalog) or "none installed"
            raise ValueError(f"no theme called {theme!r}. Available: {names}")
        values.update(jptlib.DEFAULT_THEME)
        values.update(match.get("theme") or {})
        applied_name = match.get("name") or match["id"]
        image = theme_image_path(match)
        # Embedded, like every other picture an agent puts in a deck: a path
        # into the OpalaTex installation would break the moment the deck moved.
        values["backgroundImage"] = jptlib.to_data_uri(str(image)) if image else ""

    if fields_json:
        extra = _load_tool_json(fields_json, "fields_json")
        if not isinstance(extra, dict):
            raise ValueError("fields_json must be a JSON object of theme fields")
        unknown = sorted(set(extra) - set(jptlib.THEME_FIELDS))
        if unknown:
            raise ValueError(
                f"not theme fields: {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(jptlib.THEME_FIELDS))}"
            )
        values.update(extra)

    try:
        deck = jptlib.parse(jptlib.read_jpt(resolved).text)
        jptlib.apply_theme(deck, values)
    except (JptError, jptlib.JptPackageError) as error:
        raise ValueError(f"the theme could not be applied: {error}")
    verb = f"Applied {applied_name} to" if applied_name else "Themed"
    return _jpt_write(resolved, deck, verb=verb)


@opalatex_tool(
    name="check_presentation",
    is_safe=True,
    description=(
        "Review a .jpt presentation for the defects an audience would notice: "
        "text that does not fit its box, elements off the slide or on top of one "
        "another, type too small to project, low contrast, missing images, "
        "malformed LaTeX, a title that moves between slides. Read-only. Use it "
        "after editing a deck by hand, or to check one the user wrote."
    ),
)
def check_presentation(path: str) -> str:
    from . import jpt as jptlib
    from .jpt import JptError

    resolved = _resolve_path(path)
    _require_extension(resolved, {".jpt"})
    if not os.path.exists(resolved):
        raise ValueError(f"No presentation at {_preview(resolved)}.")
    try:
        deck = jptlib.parse(jptlib.read_jpt(resolved).text)
    except (JptError, jptlib.JptPackageError) as error:
        return f"{_preview(resolved)} is not a usable presentation: {error}"

    findings = jptlib.lint(deck, project_root=get_project_path())
    slides = len(deck["slides"])
    header = f"{_preview(resolved)}: {slides} slide{'s' if slides != 1 else ''}."
    return f"{header}\n{jptlib.format_report(findings)}"


# ─── LaTeX compilation ───────────────────────────────────────────────────────
# Agents used to verify LaTeX with `run_command("pdflatex ...")`, which the
# prompts themselves suggested. OpalaTex compiles with Tectonic, which it finds
# outside PATH (external_tools.find_tool), so the shell reported no engine and
# the agent closed with "could not compile". This tool is the IDE's own compile:
# same engine, same main-file choice as the GUI's compile button, so an agent's
# "it compiles" means what the user sees.

_COMPILE_LOG_HEAD = 2500
_COMPILE_LOG_TAIL = 1000
_COMPILE_MAX_WARNINGS = 25
_UNRESOLVED_RE = re.compile(r"^LaTeX Warning: (?:Reference|Citation) .*undefined.*$", re.M)


def _compile_target(path: str) -> tuple[str, str]:
    """Return (project_dir, main file relative to it) for a compile request."""
    from .latex_compiler import determine_main_file_for_compilation, guess_main_file

    project_dir = get_project_path()
    project_main = str(getattr(_PROJECT_SESSION, "main_file", "") or "")
    if not path:
        main = project_main or guess_main_file(project_dir)
        if not main:
            raise ValueError(
                "No LaTeX root document found in the project (no .tex file with \\documentclass). "
                "Pass the path of the .tex file to compile."
            )
        if not os.path.isfile(os.path.join(project_dir, main)):
            raise ValueError(f"The project's main file {main} does not exist. Pass the .tex file to compile.")
        return project_dir, main

    resolved = os.path.abspath(_resolve_path(path))
    if not os.path.isfile(resolved):
        raise ValueError(f"File not found: {_preview(resolved)}")
    if not resolved.lower().endswith(".tex"):
        raise ValueError(f"compile_latex compiles .tex files; got {_preview(path)}.")
    try:
        inside = os.path.commonpath([resolved, os.path.abspath(project_dir)]) == os.path.abspath(project_dir)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"{_preview(path)} is outside the project directory.")
    # A chapter without \documentclass compiles through the document that
    # includes it, exactly as the GUI's compile button resolves it.
    main = determine_main_file_for_compilation(resolved, _read_text_file(resolved), project_dir, project_main)
    return project_dir, main


def _trim_compile_log(log: str) -> str:
    log = log.strip()
    if len(log) <= _COMPILE_LOG_HEAD + _COMPILE_LOG_TAIL:
        return log
    return log[:_COMPILE_LOG_HEAD] + "\n... [TRUNCATED] ...\n" + log[-_COMPILE_LOG_TAIL:]


def _compile_warnings(log: str, tex_log_path: str) -> list[str]:
    """Deduplicated engine warnings plus unresolved references from the .log.

    Tectonic prints overfull boxes and similar on its own output, but undefined
    references and citations reach only the TeX log it keeps beside the PDF.
    """
    warnings: list[str] = []
    for line in log.splitlines():
        line = line.strip()
        if line.lower().startswith("warning:") and "warnings were issued" not in line and line not in warnings:
            warnings.append(line)
    try:
        with open(tex_log_path, "r", encoding="utf-8", errors="replace") as handle:
            for match in _UNRESOLVED_RE.finditer(handle.read()):
                line = match.group(0).strip()
                if line not in warnings:
                    warnings.append(line)
    except OSError:
        pass
    return warnings


@opalatex_tool(
    name="compile_latex",
    is_safe=False,
    description=(
        "Compile a LaTeX document with Tectonic, the engine this IDE uses, and report errors and "
        "warnings with file:line locations. This is how you verify LaTeX: never look for pdflatex, "
        "xelatex or latexmk in the shell. `path` is a .tex file of the project; a chapter without "
        "\\documentclass is compiled through the document that includes it. Omit `path` to compile "
        "the project's main file. The PDF is written next to the main file, as the compile button does. "
        "`draft=true` runs a single fast pass (cross-references are not resolved); use it for a quick "
        "syntax check and a full compile before reporting that the document builds."
    ),
)
def compile_latex(path: str = "", draft: bool = False) -> str:
    from . import latex_compiler

    project_dir, main = _compile_target(path)
    AGENT_PROGRESS.update("compile_latex", main)
    if not latex_compiler.get_tectonic_path():
        raise ValueError(
            "Tectonic is not installed, so this IDE cannot compile LaTeX yet. "
            "Tell the user to install it from Settings (Install Tectonic); do not look for another engine."
        )

    result = latex_compiler.compile_latex(
        "", file_path=None, main_file=main, project_dir=project_dir,
        include_pdf_base64=False, draft=draft,
    )
    log = str(result.get("log") or "")
    # The IDE shows the outcome as if its Compile button had been pressed: the
    # PDF pane reloads, a failure fills the error card and Problems. Without it
    # the agent compiled out of the user's sight and the pane kept the old PDF.
    try:
        from opalatex.agent_stdin import print_event
        print_event("latex_compiled", {
            "project_path": project_dir,
            "main_file": main,
            "success": bool(result.get("success")),
            "draft": bool(draft),
            "log": log,
        })
    except Exception:
        pass
    seconds = (result.get("timing") or {}).get("compile_seconds")
    took = f" in {seconds:.1f}s" if isinstance(seconds, (int, float)) else ""
    mode = "draft pass" if draft else "full compile"

    if not result.get("success"):
        return (
            f"FAILED: {main} did not compile ({mode}{took}, Tectonic).\n"
            f"Compiler output:\n{_trim_compile_log(log)}"
        )

    pdf_path = result.get("pdf_path") or ""
    pdf_rel = os.path.relpath(pdf_path, project_dir).replace("\\", "/") if pdf_path else "(unknown)"
    tex_log = os.path.splitext(os.path.join(project_dir, main))[0] + ".log"
    warnings = _compile_warnings(log, tex_log)
    lines = [f"SUCCESS: {main} compiled ({mode}{took}, Tectonic). PDF: {pdf_rel}"]
    if warnings:
        lines.append(f"Warnings ({len(warnings)}):")
        lines.extend(warnings[:_COMPILE_MAX_WARNINGS])
        if len(warnings) > _COMPILE_MAX_WARNINGS:
            lines.append(f"... and {len(warnings) - _COMPILE_MAX_WARNINGS} more")
        if draft and any("undefined" in w for w in warnings):
            lines.append("Undefined references are expected after a draft pass; a full compile resolves them.")
    else:
        lines.append("No warnings.")
    return "\n".join(lines)


import platform
from .external_tools import environment_with_managed_tools
from .subprocess_utils import utf8_text_kwargs

_os_info = f"{platform.system()} ({platform.release()})"
_run_cmd_desc = f"Execute a non-interactive shell command (e.g. ls, dir, mkdir, npm install, a test suite). Runs inside the project directory. Returns stdout/stderr and the exit code. It stops after timeout_seconds (default 120, up to 1800): raise it for a long build or test run. Long output keeps its start and end, and the full text is saved to a file whose path the result gives. Use search_code for project text searches. NEVER run servers or infinite processes. NEVER use echo/printf/cat to write file content — use write_file instead. NOTE: Host OS is {_os_info}. Use the appropriate shell commands."

# Commands run in a session (POSIX) or process group (Windows) of their own, so
# a timeout stops everything the command started: killing only the shell left
# a hung test runner or build running, holding files and CPU, after the tool had
# already reported the timeout.
_COMMAND_TIMEOUT_DEFAULT = 120
_COMMAND_TIMEOUT_MAX = 1800
# Output past the limit keeps its start and, larger, its end -- where test
# summaries, tracebacks and compiler errors are -- and the whole text is saved
# under .opalatex/command_output/ for a paged read.
_COMMAND_OUTPUT_LIMIT = 4000
_COMMAND_OUTPUT_HEAD = 1500
_COMMAND_OUTPUT_TAIL = 2500
_COMMAND_OUTPUT_DIR = os.path.join(".opalatex", "command_output")
_COMMAND_OUTPUT_KEEP = 20


def _command_timeout(timeout_seconds) -> int:
    try:
        value = int(timeout_seconds)
    except (TypeError, ValueError):
        raise ValueError(f"timeout_seconds must be a whole number of seconds, not {timeout_seconds!r}.")
    if value < 1:
        raise ValueError("timeout_seconds must be at least 1.")
    return min(value, _COMMAND_TIMEOUT_MAX)


def _save_command_output(text: str, stream: str) -> str:
    """Save *text* under the project's command-output folder; return its project-relative path."""
    root = get_project_path()
    folder = os.path.join(root, _COMMAND_OUTPUT_DIR)
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    path = os.path.join(folder, f"{stamp}-{stream}.log")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        saved = sorted(name for name in os.listdir(folder) if name.endswith(".log"))
        for name in saved[:-_COMMAND_OUTPUT_KEEP]:
            os.remove(os.path.join(folder, name))
    except OSError:
        pass
    return os.path.relpath(path, root).replace(os.sep, "/")


def _fit_command_output(text: str, stream: str) -> str:
    text = (text or "").strip()
    if len(text) <= _COMMAND_OUTPUT_LIMIT:
        return text
    try:
        where = f"the full {stream} ({len(text)} characters) is in {_save_command_output(text, stream)}; page it with read_content_pos"
    except OSError:
        where = f"{len(text) - _COMMAND_OUTPUT_HEAD - _COMMAND_OUTPUT_TAIL} characters omitted"
    return (
        text[:_COMMAND_OUTPUT_HEAD]
        + f"\n... [TRUNCATED: {where}] ...\n"
        + text[-_COMMAND_OUTPUT_TAIL:]
    )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=15,
            )
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_captured(command: str, timeout_seconds) -> str:
    """Run *command* in the project through the shell and report it as the command tools do.

    With the Docker sandbox enabled the command runs in the project's container.
    """
    from .sandbox_config import is_enabled as sandbox_is_enabled

    timeout = _command_timeout(timeout_seconds)
    stop_error = None
    if sandbox_is_enabled():
        from .docker_sandbox import run_in_sandbox

        res = run_in_sandbox(command, get_project_path(), timeout)
        out, err, returncode = res["stdout"], res["stderr"], res["exit_code"]
        timed_out = res.get("error") == "TimeoutExpired"
        stop_error = res.get("stop_error")
    else:
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=get_project_path(),
            env=environment_with_managed_tools(),
            **utf8_text_kwargs(),
            **group,
        )
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", ""
        returncode = proc.returncode
    if timed_out:
        partial = ""
        if (out or "").strip():
            partial += "\nstdout before the timeout:\n" + _fit_command_output(out, "stdout")
        if (err or "").strip():
            partial += "\nstderr before the timeout:\n" + _fit_command_output(err, "stderr")
        if stop_error:
            stopped = (
                ". Its processes inside the Docker sandbox could not be stopped "
                f"({stop_error}) and may still be running; resetting the project "
                "container in Settings ends them."
            )
        else:
            stopped = " and was stopped, together with every process it started."
        raise ValueError(
            f"Error: Command timed out after {timeout} seconds{stopped} Pass a larger "
            f"timeout_seconds (up to {_COMMAND_TIMEOUT_MAX}) for a long build or test run; "
            "start a server or watcher with run_background_command instead." + partial
        )
    result = {
        "stdout": _fit_command_output(out, "stdout"),
        "stderr": _fit_command_output(err, "stderr"),
        "exit_code": returncode,
    }
    return json.dumps({"result": json.dumps(result, indent=2)})


@opalatex_tool(name="run_command", is_safe=False, description=_run_cmd_desc)
def run_command(command: str, timeout_seconds: int = _COMMAND_TIMEOUT_DEFAULT) -> str:
    AGENT_PROGRESS.update("run_command", f"$ {_preview(command)}")
    try:
        return _run_captured(command, timeout_seconds)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error running command: {e}")

@opalatex_tool(name="run_background_command", is_safe=False, description="Start a long-running background command or server (e.g., `npm run dev`) directly in the user's MAIN IDE terminal. This runs asynchronously and returns immediately, allowing the agent to continue working while the server runs. NEVER use this for commands where you need to see the output to proceed.")
async def run_background_command(command: str) -> str:
    import json
    import urllib.request
    
    AGENT_PROGRESS.update("background_cmd", f"$ {_preview(command)}")
    cwd = get_project_path()

    # A front-end without the IDE's terminal runs the command itself.
    from opalatex.agent_stdin import background_command_runner
    runner = background_command_runner()

    command_line = command
    from .sandbox_config import is_enabled as sandbox_is_enabled
    if sandbox_is_enabled():
        from .docker_sandbox import sandbox_terminal_command
        # Only the IDE's terminal is a TTY; the CLI's runner has no input at all.
        command_line, _script = await asyncio.to_thread(
            sandbox_terminal_command, command, cwd, runner is None
        )

    if runner is not None:
        return await runner(command_line, cwd)

    def send():
        from .local_auth import local_api_connection
        base_url, cookie = local_api_connection()
        req = urllib.request.Request(
            base_url + "/api/terminal/input",
            data=json.dumps({
                "action": "input", "text": f"{command_line}\r",
                "term_id": "main", "projectPath": cwd,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2.0) as response:
            return response.status

    try:
        # The HTTP server shares this event loop. Blocking urlopen here would
        # prevent it from accepting the very request the tool is waiting for.
        status = await asyncio.to_thread(send)
        if status == 200:
            return (
                f"SUCCESS: The command '{command}' has been sent to the main background terminal and is "
                "now running. Read what it prints with read_terminal."
            )
        return f"FAILED to start background command: HTTP {status}"
    except Exception as e:
        return f"FAILED to send command to background terminal: {str(e)}"



@opalatex_tool(name="run_python_script", is_safe=False, description="Execute a Python script securely. It automatically uses the correct Python interpreter for the environment. Provide the script path and any optional arguments. It stops after timeout_seconds (default 120, up to 1800).")
def run_python_script(script_path: str, args: str = "", timeout_seconds: int = _COMMAND_TIMEOUT_DEFAULT) -> str:
    try:
        resolved = _resolve_path(script_path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")

    from .sandbox_config import is_enabled as sandbox_is_enabled
    if sandbox_is_enabled():
        from .docker_sandbox import container_path
        # python3 is the name every image with Python has; Debian's has no `python`.
        cmd = f'python3 "{container_path(resolved, get_project_path())}" {args}'.strip()
    else:
        cmd = f'"{sys.executable}" "{resolved}" {args}'.strip()

    AGENT_PROGRESS.update("run_python_script", f"$ {_preview(cmd)}")
    try:
        return _run_captured(cmd, timeout_seconds)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error running script: {e}")

@opalatex_tool(name="run_interactive_command", is_safe=False, description="Run a command that requires user interaction (e.g. npm create, interactive scripts, prompts). This opens a dedicated interactive terminal popup in the user's GUI, allowing them to answer the prompts safely. Use this WHENEVER a command needs human choices, waits for input, or requires a PTY. Do NOT use standard `exec` or `run_command` for interactive tasks, as they will hang.")
async def run_interactive_command(command: str) -> str:
    import uuid
    import opalatex.terminal as T
    
    AGENT_PROGRESS.update("interactive_cmd", _preview(command))
    from .agent_stdin import unattended_refusal, unattended_turn
    if unattended_turn() is not None:
        return unattended_refusal(
            f"the interactive command {command!r} needs a person at a terminal; use run_command or "
            "run_background_command for commands that need no input"
        )
    term_id = "temp_" + str(uuid.uuid4())[:8]

    actual_command = command
    script = None
    from .sandbox_config import is_enabled as sandbox_is_enabled
    if sandbox_is_enabled():
        from .docker_sandbox import sandbox_terminal_command
        actual_command, script = await asyncio.to_thread(
            sandbox_terminal_command, command, get_project_path(), True
        )

    # Trigger the interactive terminal modal in the UI
    try:
        res = await T.a_interactive_terminal(actual_command, term_id)
    finally:
        # The script removes itself when it runs; this covers a cancelled terminal.
        if script and os.path.exists(script):
            os.remove(script)

    if res and res.lower() not in ("cancel", "no", "false"):
        return f"SUCCESS: The interactive command '{command}' finished successfully according to the user."
    else:
        return f"FAILURE: The user indicated that the interactive command '{command}' failed or was cancelled. Please ask the user for details if needed."


@opalatex_tool(
    name="search_code",
    is_safe=True,
    description=(
        "Search for text or a regular expression in project files using Python, "
        "returning relative file paths, 1-indexed line numbers, and matching lines. "
        "query is matched as literal text unless use_regex=true, so '|' and '.*' are only "
        "regex syntax with use_regex=true; in a regex, double a LaTeX backslash (\\\\label). "
        "query is mandatory; to read a known file without searching, call read_file(path) "
        "or read_content_pos(path, start_pos, end_pos) instead. "
        "Use offset to page matches, context_lines for surrounding text, include_logs for build logs, "
        "and include_hidden for hidden directories. Use this to locate sections, labels, definitions, or other markers before "
        "calling read_content_pos or replace_content_range."
    ),
)
def search_code(
    query: str,
    path: str = ".",
    use_regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    file_pattern: str = "*",
    offset: int = 0,
    context_lines: int = 0,
    include_hidden: bool = False,
    include_logs: bool = False,
) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")

    AGENT_PROGRESS.update("search_code", f"query={_preview(query)} path={_preview(resolved)}")

    if not query:
        raise ValueError("query must not be empty.")
    if max_results < 1:
        raise ValueError("max_results must be a positive integer.")
    if offset < 0 or not 0 <= context_lines <= 20:
        raise ValueError("offset must be non-negative and context_lines must be between 0 and 20.")

    # Compiled before any file is walked, so a bad pattern fails with what is
    # wrong with the pattern rather than as an error "searching" some file.
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(query if use_regex else re.escape(query), flags)
    except re.error as e:
        raise ValueError(
            f"Error: invalid regular expression (use_regex=true): {e}. In a regular expression "
            "a backslash starts an escape, so a LaTeX command needs it doubled (\\\\label matches "
            "\\label); or pass use_regex=false to match the query as literal text."
        )

    root = Path(get_project_path()).resolve()
    target = Path(resolved).resolve()
    skipped_dirs = {".git", ".opalatex", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    skipped_exts = {
        ".aux", ".bbl", ".blg", ".class", ".dll", ".exe", ".gz", ".ico", ".jar",
        ".jpg", ".jpeg", ".log", ".mp3", ".mp4", ".pdf", ".png", ".pyc", ".so",
        ".synctex.gz", ".zip",
    }
    if include_logs:
        skipped_exts.difference_update({".log", ".aux", ".bbl", ".blg"})

    try:
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = []
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = sorted(d for d in dirnames if d not in skipped_dirs and (include_hidden or not d.startswith(".")))
                for filename in sorted(filenames):
                    candidate = Path(dirpath) / filename
                    if candidate.suffix.lower() in skipped_exts:
                        continue
                    rel_candidate = os.path.relpath(candidate, target).replace(os.sep, "/")
                    if not (
                        fnmatch(filename, file_pattern)
                        or fnmatch(rel_candidate, file_pattern)
                    ):
                        continue
                    candidates.append(candidate)
        else:
            raise ValueError(f"Error: path does not exist: {_preview(resolved)}.")

        matches: list[str] = []
        matches_seen_file = None
        searched = 0
        match_index = 0
        returned = 0

        for candidate in candidates:
            searched += 1
            try:
                text = _read_logical_text_file(str(candidate))
            except Exception:
                continue
            if "\x00" in text[:4096]:
                continue
            rel_path = os.path.relpath(candidate, root)
            lines = text.splitlines()
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    if not match_index or matches_seen_file != candidate:
                        # Its line numbers are about to reach the model.
                        _note_file_seen(str(candidate))
                        matches_seen_file = candidate
                    match_index += 1
                    if match_index <= offset:
                        continue
                    for index in range(max(0, line_number - 1 - context_lines), min(len(lines), line_number + context_lines)):
                        matches.append(f"{rel_path}:{index + 1}: {lines[index].strip()}")
                    returned += 1
                    if returned >= max_results:
                        return (
                            "\n".join(matches)
                            + f"\n[search_code] Stopped after {max_results} matches. Continue with offset={offset + returned}."
                        )

        if not matches:
            result = f"No matches for {query!r} in {_preview(os.path.relpath(target, root))}."
            if not use_regex and "|" in query:
                # Observed: models write `a|b|c` without use_regex, get no
                # matches, and conclude the text is absent. Say how the query was
                # read; the search itself is not changed.
                result += (
                    " The query was matched as literal text (use_regex=false), so '|' matched "
                    "only a literal '|' character, not alternatives. To find any of several "
                    "alternatives, search again with use_regex=true."
                )
            return result
        return "\n".join(matches) + f"\n[search_code] Searched {searched} files."
    except Exception as e:
        raise ValueError(f"Error searching code in {_preview(resolved)}: {e}")

@opalatex_tool(
    name="ask_question",
    is_safe=True,
    description=(
        "Ask the user a clarifying question with optional selectable choices. "
        "Provide 'question' and optionally a list of 2-5 distinct 'options' representing common choices. "
        "The user will be presented with clickable choices in the UI plus an automatic 'Other' write-in field. "
        "Do NOT include your own 'Other', 'Others (please specify)', or 'None of the above' choice in 'options' — "
        "that catch-all is added automatically by the UI, so adding one yourself creates a duplicate. "
        "Set 'is_multi_select' to true whenever the user may choose more than one option (for example, several columns, features, or files); "
        "leave it false only when the choices are mutually exclusive. Do not replace one multi-select question with a series of single-select questions. "
        "A multi-select response is returned as a JSON array of the selected option labels and any custom write-in response. "
        "Use this proactively whenever collecting user choices, column selections, target formats, seed filters, or clarifying ambiguous preferences."
    )
)
async def ask_question(
    question: str,
    options: list[str] | None = None,
    is_multi_select: bool = False
) -> str:
    AGENT_PROGRESS.update("ask_question", _preview(question))
    from .agent_stdin import unattended_refusal, unattended_turn
    if unattended_turn() is not None:
        return unattended_refusal(f"the question {question[:120]!r} was not asked")
    
    import opalatex.terminal as T
    ans = await T.aask(question, options=options, is_multi_select=is_multi_select)
    
    if not ans:
        return "The user did not provide an answer or cancelled the prompt."
    return f"User response: {ans}"

# Backward compatibility alias
ask_human = ask_question



@opalatex_tool(
    name="get_project_overview",
    is_safe=True,
    description=(
        "Return a compact overview of the current project: directory tree (max depth defined by parameter max_depth, DEFAULT/MINIMUM 5), "
        "file count by type, and a summary of key files (README, package.json, requirements.txt, etc.). "
        "Use this when the project structure or target file is unknown. Skip it when the user or context already identifies the file to inspect or edit."
    ),
)
def get_project_overview(max_depth:int = 5) -> str:
    from .config import get_project_overview_max_depth
    AGENT_PROGRESS.update("get_project_overview")
    root = Path(get_project_path())
    
    try:
        if max_depth < 0:
            max_depth_cfg = get_project_overview_max_depth()
        else:
            max_depth_cfg = max_depth
            
        # FORCE MINIMUM DEPTH
        if max_depth_cfg < 5:
            max_depth_cfg = 5

        if not root.exists():
            return f"Project directory does not exist yet: {root}"

        # ── Directory tree (skip hidden & node_modules/__pycache__) ──
        SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build"}

        def _tree(path: Path, depth: int = 0) -> list[str]:
            if depth >= max_depth_cfg:
                return ["  " * depth + "..."]
            lines = []
            try:
                entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            except PermissionError:
                return []
            for entry in entries:
                if entry.name.startswith(".") or entry.name in SKIP:
                    continue
                indent = "  " * depth
                lines.append(indent + entry.name + ("/" if entry.is_dir() else ""))
                if entry.is_dir():
                    lines.extend(_tree(entry, depth + 1))
            return lines

        tree_lines = _tree(root)
        tree_str = f"{root.name}/\n" + "\n".join(tree_lines) if tree_lines else f"{root.name}/  (empty)"

        # ── File count by extension ──
        ext_count: dict[str, int] = {}
        total = 0
        for p in root.rglob("*"):
            if p.is_file() and not any(part in SKIP or part.startswith(".") for part in p.parts):
                ext = p.suffix.lower() or "(no ext)"
                ext_count[ext] = ext_count.get(ext, 0) + 1
                total += 1
        ext_summary = ", ".join(
            f"{ext}: {n}" for ext, n in sorted(ext_count.items(), key=lambda x: -x[1])[:8]
        )

        # ── Key file snapshots ──
        KEY_FILES = ["README.md", "package.json", "requirements.txt", "pyproject.toml",
                     "Makefile", "Dockerfile", "docker-compose.yml", ".env.example"]
        snippets = []
        for name in KEY_FILES:
            kf = root / name
            if kf.exists():
                try:
                    content = kf.read_text(encoding="utf-8", errors="replace")[:400]
                    snippets.append(f"### {name}\n{content.strip()}")
                except Exception:
                    pass

        parts = [
            f"## Project: {root.name}",
            f"Path: {root}",
            f"Total files: {total}  |  By type: {ext_summary or 'n/a'}",
            "",
            "## Directory structure",
            tree_str,
        ]
        if snippets:
            parts += ["", "## Key files"] + snippets

        return "\n".join(parts)
    except Exception as e:
        return f"Error generating project overview: {e}"

@opalatex_tool(
    name="write_content_pos",
    is_safe=False,
    description=(
        "Insert content into an EXISTING file starting at a specific line number (1-indexed). "
        "The new content will be inserted just before the specified line. "
        "The file must already exist: use write_file to create a new file, and "
        "replace_content_range when you need to replace or remove existing lines. This tool "
        "cannot edit packaged .jpt files; use edit_presentation or a full read_file/write_file "
        "round-trip for them."
    )
)
def write_content_pos(path: str, content: str, pos: int) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")
    _refuse_helper_project_write(resolved)

    AGENT_PROGRESS.update("write_content_pos", f"path={_preview(resolved)} pos={pos}")
    
    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory. Cannot write to a directory.")
        if not os.path.exists(resolved):
            raise ValueError(
                f"Error: file not found: {_preview(resolved)}. write_content_pos only "
                "inserts into a file that already exists. To create a new file, call "
                "write_file with the full content instead."
            )
    except OSError as e:
        raise ValueError(f"Error: invalid path argument ({e.strerror}).")

    if Path(resolved).suffix.lower() == ".jpt":
        raise ValueError(
            "Packaged .jpt files cannot be edited by line position. Use "
            "edit_presentation, or read_file followed by write_file for a full "
            "validated deck rewrite."
        )

    _refuse_if_changed_since_seen(resolved)
    try:
        with open(resolved, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()

        idx = max(0, min(pos - 1, len(lines)))
        
        if content and not content.endswith('\n'):
            content += '\n'
            
        before = "".join(lines)
        lines.insert(idx, content)
        
        with open(resolved, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        try:
            from .code_index import CODE_INDEX
            CODE_INDEX.rebuild_file(resolved)
        except Exception:
            pass
            
        _note_file_seen(resolved)
        return f"Successfully inserted content at line {pos} in {_preview(resolved)}." + _syntax_note(resolved, before)
    except Exception as e:
        raise ValueError(f"Error writing to {_preview(resolved)}: {e}")


@opalatex_tool(
    name="replace_content_range",
    is_safe=False,
    description=(
        "Replace an inclusive 1-indexed line range in an existing file. "
        "Use this for surgical edits to large files instead of rewriting the entire file. "
        "Pass an empty content string to delete the selected lines. This tool cannot edit "
        "packaged .jpt files; use edit_presentation or a full read_file/write_file round-trip "
        "for them."
    )
)
def replace_content_range(path: str, start_pos: int, end_pos: int, content: str) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")
    _refuse_helper_project_write(resolved)

    AGENT_PROGRESS.update(
        "replace_content_range",
        f"path={_preview(resolved)} lines={start_pos}-{end_pos}",
    )

    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory. Cannot write to a directory.")
        if not os.path.exists(resolved):
            raise ValueError(
                f"Error: file not found: {_preview(resolved)}. replace_content_range only "
                "edits a file that already exists. To create a new file, call write_file "
                "with the full content instead."
            )
    except OSError as e:
        raise ValueError(f"Error: invalid path argument ({e.strerror}).")

    if Path(resolved).suffix.lower() == ".jpt":
        raise ValueError(
            "Packaged .jpt files cannot be edited by line range. Use "
            "edit_presentation, or read_file followed by write_file for a full "
            "validated deck rewrite."
        )

    if start_pos < 1 or end_pos < 1:
        raise ValueError("start_pos and end_pos must be 1-indexed positive integers.")
    if end_pos < start_pos:
        raise ValueError("end_pos must be greater than or equal to start_pos.")
    _refuse_if_changed_since_seen(resolved)

    try:
        with open(resolved, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()

        if start_pos > len(lines):
            raise ValueError(f"Error: start_pos {start_pos} is beyond the end of the file (total lines: {len(lines)}).")

        start_idx = start_pos - 1
        end_idx = min(end_pos, len(lines))
        replacement = content
        replacement_lines = []
        if replacement:
            if not replacement.endswith("\n"):
                replacement += "\n"
            replacement_lines = replacement.splitlines(keepends=True)

        before = "".join(lines)
        lines[start_idx:end_idx] = replacement_lines

        with open(resolved, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        try:
            from .code_index import CODE_INDEX
            CODE_INDEX.rebuild_file(resolved)
        except Exception:
            pass

        _note_file_seen(resolved)
        return (
            f"Successfully replaced lines {start_pos}-{end_pos} "
            f"in {_preview(resolved)}."
        ) + _syntax_note(resolved, before)
    except Exception as e:
        raise ValueError(f"Error replacing content in {_preview(resolved)}: {e}")

# ─── What the agent last saw of a file ────────────────────────────────────────
# Line numbers are only as good as the read they came from. Each read or write
# by a file tool records the file's (mtime, size); the line-position tools then
# refuse to edit a file that changed by other means since -- the user saving it
# in the editor, a command, a sync -- because the lines the model is about to
# replace may no longer be the ones it read. A file never seen is not checked,
# and edit_file needs no check: its passage is matched by text.
_SEEN_FILES: dict[str, tuple[int, int]] = {}
_SEEN_FILES_LOCK = threading.Lock()


def _file_stamp(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _note_file_seen(path: str) -> None:
    stamp = _file_stamp(path)
    if stamp is None:
        return
    with _SEEN_FILES_LOCK:
        _SEEN_FILES[os.path.normcase(os.path.abspath(path))] = stamp


def _refuse_if_changed_since_seen(path: str) -> None:
    key = os.path.normcase(os.path.abspath(path))
    with _SEEN_FILES_LOCK:
        seen = _SEEN_FILES.get(key)
    if seen is None or _file_stamp(path) == seen:
        return
    raise ValueError(
        f"Error: {_preview(path)} changed since you last read it (saved in the editor, "
        "or changed by a command or a sync), so line numbers from that read may no longer "
        "point at the same text. Nothing was written. Read the lines again with "
        "read_content_pos, or use edit_file, which finds the passage by its text."
    )


# ─── Syntax check after a write ───────────────────────────────────────────────
# A broken edit used to surface only when something later ran the file, often
# turns later and after more edits stacked on it. The file tools now parse what
# they wrote, offline and in milliseconds, and say so when the write introduced
# a syntax error. Only a newly introduced error is reported: a file that did not
# parse before the write is left to the model's own judgement, and so is a
# format whose parse depends on project setup (JSX inside .js, YAML custom tags)
# when there is no earlier version that parsed to compare with.

def _syntax_python(text: str, suffix: str) -> str | None:
    try:
        compile(text, "<edited>", "exec", dont_inherit=True)
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"
    except ValueError as e:
        return str(e)
    return None


def _syntax_json(text: str, suffix: str) -> str | None:
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        return f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}"
    return None


def _syntax_toml(text: str, suffix: str) -> str | None:
    import tomllib

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"invalid TOML: {e}"
    return None


def _syntax_yaml(text: str, suffix: str) -> str | None:
    try:
        import yaml
    except ImportError:
        return None
    try:
        for _ in yaml.safe_load_all(text):
            pass
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        return f"invalid YAML{where}: {getattr(e, 'problem', None) or e}"
    return None


_ES_MODULE_RE = re.compile(r"^\s*(?:import\s*[\w{*'\"]|export\s)", re.MULTILINE)


def _syntax_javascript(text: str, suffix: str) -> str | None:
    import shutil
    import tempfile

    node = shutil.which("node")
    if not node:
        return None
    # With module detection, `node --check` on a .js file passes a syntax error
    # in a file that uses import/export (`export const a = ;` exits 0), so
    # ES-module text is checked as .mjs, which forces the module parser.
    if suffix == ".js" and _ES_MODULE_RE.search(text):
        suffix = ".mjs"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        res = subprocess.run(
            [node, "--check", temp_path], capture_output=True, timeout=10,
            stdin=subprocess.DEVNULL, **utf8_text_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    if res.returncode == 0:
        return None
    # node prints "<file>:<line>", the source line, a caret, then the error.
    lines = [line for line in (res.stderr or "").splitlines() if line.strip()]
    message = next((line for line in lines if "Error" in line), lines[-1] if lines else "syntax error")
    line_no = ""
    if lines and lines[0].startswith(temp_path + ":"):
        line_no = " at line " + lines[0][len(temp_path) + 1:].split(":")[0]
    return f"{message.strip()}{line_no}"


# suffix -> (language, checker, checked without an earlier version that parsed)
_SYNTAX_CHECKERS = {
    ".py": ("Python", _syntax_python, True),
    ".pyw": ("Python", _syntax_python, True),
    ".json": ("JSON", _syntax_json, True),
    ".toml": ("TOML", _syntax_toml, True),
    ".yaml": ("YAML", _syntax_yaml, False),
    ".yml": ("YAML", _syntax_yaml, False),
    ".js": ("JavaScript", _syntax_javascript, False),
    ".mjs": ("JavaScript", _syntax_javascript, False),
    ".cjs": ("JavaScript", _syntax_javascript, False),
}


def _text_before_write(path: str) -> str | None:
    """The text of *path* before a write, or None when it does not exist or cannot be read."""
    if not os.path.isfile(path):
        return None
    try:
        return _read_text_file(path)
    except OSError:
        return None


def _syntax_note(path: str, before: str | None) -> str:
    """A warning when the write left *path* unparseable and *before* parsed.

    *before* is the file's text before the write, or None for a new file.
    Returns "" when there is nothing to report or nothing could be checked.
    """
    suffix = Path(path).suffix.lower()
    entry = _SYNTAX_CHECKERS.get(suffix)
    if entry is None:
        return ""
    language, check, without_baseline = entry
    if before is None and not without_baseline:
        return ""
    try:
        after_problem = check(_read_text_file(path), suffix)
        if not after_problem:
            return ""
        if before is not None and check(before.replace("\r\n", "\n"), suffix):
            return ""
    except Exception:
        return ""
    name = os.path.basename(path)
    if before is None:
        return f"\nSyntax check: {name} does not parse as {language} — {after_problem}. The file was written; correct it."
    return (
        f"\nSyntax check: {name} parsed as {language} before this edit and no longer does — "
        f"{after_problem}. The change was written; correct it."
    )

# ─── Anchored edit ────────────────────────────────────────────────────────────
# Line-number edits go wrong in a way the model cannot see: after one edit every
# later line moves, and a range taken from an earlier read lands on the wrong
# text. `edit_file` locates the passage by its own text, so the anchor is also
# the check: if the file changed under the model, the passage either is still
# there (and the edit is still right) or it is not (and the edit is refused).
# The contract is the mobile app's `edit_file` (docs/specs/mobile_app.md).

# Encodings tried in this order when decoding a file to edit, as the read tools
# do, so the model matches against the text it was shown; the file is written
# back in the encoding it was found in.
_EDIT_ENCODINGS = ("utf-8", "cp1252", "latin-1")
_EDIT_BOMS = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_EDIT_SNIPPET_CONTEXT = 3
_EDIT_SNIPPET_MAX_LINES = 40


def _decode_for_edit(data: bytes) -> tuple[str, str, bytes]:
    """Return (text, encoding, bom) of a file's bytes, the way the read tools decode them."""
    for bom, encoding in _EDIT_BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(encoding), encoding, bom
    # latin-1 decodes any bytes at all, so a binary file has to be told apart
    # before the fallbacks are tried.
    if b"\x00" in data:
        raise ValueError("it is a binary file")
    for encoding in _EDIT_ENCODINGS:
        try:
            return data.decode(encoding), encoding, b""
        except UnicodeDecodeError:
            continue
    raise ValueError("the file is not text in a supported encoding")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _numbered_lines(text: str, first: int, last: int) -> str:
    lines = text.split("\n")
    first = max(1, first)
    last = min(len(lines), last)
    width = len(str(last))
    return "\n".join(f"{n:>{width}}\t{lines[n - 1].rstrip(chr(13))}" for n in range(first, last + 1))


def _edit_snippet(text: str, start: int, length: int) -> str:
    """The edited passage with a few lines around it, numbered as read_content_pos numbers them."""
    first = _line_of(text, start)
    last = _line_of(text, start + max(0, length - 1)) if length else first
    lo, hi = first - _EDIT_SNIPPET_CONTEXT, last + _EDIT_SNIPPET_CONTEXT
    if hi - lo + 1 <= _EDIT_SNIPPET_MAX_LINES:
        return _numbered_lines(text, lo, hi)
    half = _EDIT_SNIPPET_MAX_LINES // 2
    return (
        _numbered_lines(text, lo, lo + half - 1)
        + f"\n… ({hi - lo + 1 - 2 * half} lines not shown) …\n"
        + _numbered_lines(text, hi - half + 1, hi)
    )


def _whitespace_insensitive_lines(text: str, old: str) -> list[int]:
    """Lines where *old* starts if indentation and runs of spaces are ignored.

    Used only to say where the near-miss is when an exact match failed; the
    edit itself never matches loosely.
    """
    wanted = [" ".join(line.split()) for line in old.split("\n")]
    while wanted and not wanted[0]:
        wanted.pop(0)
    while wanted and not wanted[-1]:
        wanted.pop()
    if not wanted:
        return []
    lines = [" ".join(line.split()) for line in text.split("\n")]
    hits = []
    for i in range(len(lines) - len(wanted) + 1):
        if lines[i:i + len(wanted)] == wanted:
            hits.append(i + 1)
            if len(hits) == 3:
                break
    return hits


@opalatex_tool(
    name="edit_file",
    is_safe=False,
    description=(
        "Replace an exact passage of an existing text file. old_string must match the file's "
        "text exactly, including indentation and whitespace, and occur exactly once; include "
        "enough surrounding lines to make it unique, or set replace_all to true to change every "
        "occurrence. No line numbers are involved, so earlier edits cannot shift where this one "
        "lands. Pass an empty new_string to delete the passage. Returns the edited region with "
        "line numbers. This tool does not create files (write_file does) and cannot edit "
        "packaged .jpt files."
    )
)
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")
    _refuse_helper_project_write(resolved)

    AGENT_PROGRESS.update("edit_file", f"path={_preview(resolved)}")

    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory. Cannot edit a directory.")
        if not os.path.exists(resolved):
            candidates = _same_name_candidates(resolved)
            where = (
                " A file with this name exists at: " + ", ".join(candidates) + "."
                if candidates else ""
            )
            raise ValueError(
                f"Error: file not found: {_preview(resolved)}.{where} edit_file only edits a "
                "file that already exists. To create a new file, call write_file with the full "
                "content instead."
            )
    except OSError as e:
        raise ValueError(f"Error: invalid path argument ({e.strerror}).")

    if Path(resolved).suffix.lower() == ".jpt":
        raise ValueError(
            "Packaged .jpt files cannot be edited as text. Use edit_presentation, or "
            "read_file followed by write_file for a full validated deck rewrite."
        )
    if not old_string:
        raise ValueError(
            "Error: old_string is empty. Pass the exact text to replace; to add text, include "
            "a neighbouring passage in old_string and repeat it in new_string."
        )
    if old_string == new_string:
        raise ValueError("Error: old_string and new_string are identical, so there is nothing to change.")

    with open(resolved, "rb") as f:
        data = f.read()
    try:
        text, encoding, bom = _decode_for_edit(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"Error: cannot edit {_preview(resolved)}: {e}.")

    # The read tools show CRLF files with LF line breaks, so the passage the
    # model copies has LF. In a CRLF file it is matched, and written, with the
    # file's own line breaks.
    old, new = old_string, new_string
    count = text.count(old)
    if count == 0 and "\r\n" in text and "\n" in old and "\r" not in old:
        old = old.replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old)

    if count == 0:
        near = _whitespace_insensitive_lines(text.replace("\r\n", "\n"), old_string)
        hint = (
            " A passage that differs only in indentation or spacing starts at line "
            + ", ".join(str(n) for n in near)
            + "; read those lines with read_content_pos and copy the text exactly."
            if near else
            " Read the current text with read_content_pos (search_code finds the line) and "
            "copy the passage exactly, including indentation."
        )
        raise ValueError(f"Error: old_string does not occur in {_preview(resolved)}.{hint}")
    if count > 1 and not replace_all:
        starts, at = [], -1
        while len(starts) < 5:
            at = text.find(old, at + 1)
            if at < 0:
                break
            starts.append(str(_line_of(text, at)))
        raise ValueError(
            f"Error: old_string occurs {count} times in {_preview(resolved)} (starting at lines "
            f"{', '.join(starts)}{', …' if count > len(starts) else ''}). Include more surrounding "
            "lines to make it unique, or set replace_all to true to change every occurrence."
        )

    first_at = text.find(old)
    updated = text.replace(old, new) if replace_all else text[:first_at] + new + text[first_at + len(old):]
    try:
        encoded = bom + updated.encode(encoding)
    except UnicodeEncodeError as e:
        raise ValueError(
            f"Error: {_preview(resolved)} is encoded in {encoding}, which cannot hold "
            f"{e.object[e.start:e.end]!r} from new_string. Use characters that encoding supports."
        )

    with open(resolved, "wb") as f:
        f.write(encoded)
    _note_file_seen(resolved)
    try:
        from .code_index import CODE_INDEX
        CODE_INDEX.rebuild_file(resolved)
    except Exception:
        pass

    what = f"Replaced {count} occurrences" if count > 1 else "Edited"
    return (
        f"{what} in {_preview(resolved)}. The edited region now reads:\n"
        + _edit_snippet(updated, first_at, len(new))
        + _syntax_note(resolved, text)
    )


@opalatex_tool(
    name="read_content_pos",
    is_safe=True,
    description=(
        "Page through a file by 1-indexed line numbers (inclusive start_pos/end_pos). "
        "This is the paging tool read_file points to when a whole-file read is too big "
        "for the remaining context window: call search_code first to find a target line, "
        "or start at (1, N) to page sequentially from the top. If the requested range is "
        "larger than what still fits the context budget, the returned text is capped and "
        "a trailing note reports the total line count, how many lines remain, and the "
        "start_pos to pass on the next call — always re-read that note rather than "
        "assuming the full requested range came back."
    )
)
def read_content_pos(path: str, start_pos: int, end_pos: int) -> str:
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")

    AGENT_PROGRESS.update("read_content_pos", f"path={_preview(resolved)} lines={start_pos}-{end_pos}")

    try:
        if os.path.isdir(resolved):
            raise ValueError(f"Error: '{_preview(resolved)}' is a directory, not a file.")
        if not os.path.exists(resolved):
            raise ValueError(f"Error: file not found: {_preview(resolved)}.")
    except OSError as e:
        raise ValueError(f"Error: invalid path argument ({e.strerror}).")

    if start_pos < 1 or end_pos < 1:
        raise ValueError("start_pos and end_pos must be 1-indexed positive integers.")
    if end_pos < start_pos:
        raise ValueError("end_pos must be greater than or equal to start_pos.")

    try:
        lines = _read_logical_text_file(resolved).splitlines(keepends=True)
        _note_file_seen(resolved)
        total_lines = len(lines)

        start_idx = start_pos - 1

        if start_idx >= total_lines:
            return (
                f"No content read: start_pos {start_pos} is beyond the end of the file "
                f"(total lines: {total_lines}). Use start_pos <= {total_lines} or locate "
                "the target section before retrying."
            )

        requested_end_idx = min(total_lines, end_pos)

        # A page must fit the remaining context budget on its own: read_content_pos is
        # the escape hatch read_file's own refusal points models toward, so it cannot
        # reproduce the same unbounded-read overflow just because the caller asked for
        # a wide range. Cap the slice instead of refusing outright -- paging tools are
        # expected to return partial pages -- but always say so explicitly (PROJECT_DESIGN
        # 2.6, no silent substitution): a capped page must never look like the full
        # requested range.
        budget_chars = free_context_chars()
        if budget_chars is not None and budget_chars <= 0:
            window = context_window_tokens()
            used = used_context_tokens()
            raise ValueError(
                f"Error: the context window is exhausted ({used:,} of {window:,} tokens "
                f"already used), so '{_preview(resolved)}' cannot be read further. "
                "Summarize what you already know and answer, or ask the user to start a "
                "new chat or raise num_ctx in the project settings."
            )

        end_idx = start_idx
        consumed = 0
        for idx in range(start_idx, requested_end_idx):
            consumed += len(lines[idx])
            if budget_chars is not None and consumed > budget_chars and end_idx > start_idx:
                break
            end_idx = idx + 1
            if budget_chars is not None and consumed > budget_chars:
                break

        selected_lines = lines[start_idx:end_idx]
        content = "".join(selected_lines)
        line_truncated = False
        if budget_chars is not None and len(content) > budget_chars:
            # A single line alone exceeds the whole page budget (e.g. a minified JSON
            # line); still make forward progress instead of returning nothing.
            content = content[:budget_chars]
            line_truncated = True

        actual_end_pos = end_idx
        remaining_lines = total_lines - end_idx

        notes = []
        if line_truncated:
            notes.append(f"line {actual_end_pos} was itself too long to fit and was cut short")
        elif end_idx < requested_end_idx:
            notes.append(f"capped to fit the remaining context budget (you asked through line {end_pos})")
        if remaining_lines > 0:
            notes.append(f"{remaining_lines:,} more line(s) remain")

        if notes:
            content += (
                f"\n\n[Showing lines {start_pos}-{actual_end_pos} of {total_lines:,} total "
                f"lines in '{_preview(resolved)}': " + "; ".join(notes) + ". "
                f"Call read_content_pos({path!r}, {actual_end_pos + 1}, <end_pos>) to continue.]"
            )

        return content
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Error reading {_preview(resolved)}: {e}")


# Characters of rendered diff per page when the context window leaves room for
# more: large enough for a typical edit review, small enough that a rewritten
# chapter still arrives in pages instead of one block.
_DIFF_PAGE_CHARS = 12000


def _diff_side(label: str, path: str, start: int, end: int) -> tuple[str, list[str], int, int]:
    """Resolve one side of diff_files to (display path, lines, first, last)."""
    try:
        resolved = _resolve_path(path)
    except Exception as e:
        raise ValueError(f"Error resolving {label}: {e}")
    if os.path.isdir(resolved):
        raise ValueError(f"Error: {label} '{_preview(resolved)}' is a directory, not a file.")
    if not os.path.exists(resolved):
        raise ValueError(f"Error: {label} not found: {_preview(resolved)}.")
    if start < 0 or end < 0:
        raise ValueError(f"start and end for {label} must be 0 (whole file) or 1-indexed line numbers.")

    ext = os.path.splitext(resolved)[1].lower()
    if ext in _DOC_EXTS:
        from .attachments import extract_document_text_from_path

        text = extract_document_text_from_path(resolved)
    elif _binary_read_error(resolved):
        raise ValueError(
            f"Error: {label} '{_preview(resolved)}' is a binary file ({ext or 'no extension'}); "
            "diff_files compares text files and PDF/DOCX/PPTX/XLSX documents."
        )
    else:
        text = _read_logical_text_file(resolved)

    lines = text.splitlines()
    total = len(lines)
    first = start or 1
    last = end or total
    if total and first > total:
        raise ValueError(
            f"Error: {label} starts at line {first}, beyond the end of '{_preview(resolved)}' "
            f"({total} lines)."
        )
    last = min(last, total)
    if end and end < first:
        raise ValueError(f"Error: end must be >= start for {label} (got {start}-{end}).")
    root = get_project_path()
    return _rel(resolved, root), lines[first - 1:last], first, last


@opalatex_tool(
    name="diff_files",
    is_safe=True,
    description=(
        "Compare two text files, or two line ranges (of the same file or of different files), "
        "and return the differences with the ORIGINAL 1-indexed line numbers of both sides, "
        "labelled a (path_a) and b (path_b). start/end 0 means the whole file; otherwise they "
        "are inclusive line numbers, as in read_content_pos. The first lines say whether the "
        "two sides are identical and list every changed block, so a long diff can be judged "
        "from its first page. ignore_whitespace compares lines with spacing collapsed; "
        "context_lines sets the unchanged lines shown around each change (0-10). A long "
        "diff is paged: follow the trailing note's offset. PDF/DOCX/PPTX/XLSX are compared "
        "by their extracted text."
    ),
)
def diff_files(
    path_a: str,
    path_b: str,
    start_a: int = 0,
    end_a: int = 0,
    start_b: int = 0,
    end_b: int = 0,
    context_lines: int = 3,
    ignore_whitespace: bool = False,
    offset: int = 0,
) -> str:
    from .text_diff import describe_range, line_diff, summarize_blocks

    AGENT_PROGRESS.update("diff_files", f"a={_preview(path_a)} b={_preview(path_b)}")
    if not 0 <= context_lines <= 10:
        raise ValueError("context_lines must be between 0 and 10.")
    if offset < 0:
        raise ValueError("offset must be non-negative.")

    name_a, lines_a, first_a, last_a = _diff_side("path_a", path_a, start_a, end_a)
    name_b, lines_b, first_b, last_b = _diff_side("path_b", path_b, start_b, end_b)
    result = line_diff(
        lines_a, lines_b, first_a, first_b,
        context=context_lines, ignore_whitespace=ignore_whitespace,
    )

    header = [
        f"DIFF a={name_a} {describe_range('lines', first_a, last_a)} ({len(lines_a)} lines) | "
        f"b={name_b} {describe_range('lines', first_b, last_b)} ({len(lines_b)} lines)"
        + (" | whitespace ignored" if ignore_whitespace else ""),
    ]
    if result.identical:
        header.append("IDENTICAL: yes")
        return "\n".join(header)
    header.append(
        f"IDENTICAL: no | ADDED: {result.added} | REMOVED: {result.removed} | "
        f"BLOCKS: {len(result.blocks)} | HUNKS: {len(result.hunks)}"
    )
    header.append("CHANGED: " + summarize_blocks(result.blocks, limit=20))

    body = [line for hunk in result.hunks for line in hunk]
    if offset >= len(body):
        raise ValueError(f"offset {offset} is past the end of the diff ({len(body)} lines).")

    budget = free_context_chars()
    if budget is not None and budget <= 0:
        raise ValueError("The context window is exhausted. Summarize before reading further.")
    page_chars = _DIFF_PAGE_CHARS if budget is None else min(_DIFF_PAGE_CHARS, budget)
    page_chars = max(0, page_chars - sum(len(line) + 1 for line in header))

    shown: list[str] = []
    used = 0
    index = offset
    while index < len(body):
        cost = len(body[index]) + 1
        if shown and used + cost > page_chars:
            break
        shown.append(body[index])
        used += cost
        index += 1

    text = "\n".join(header + shown)
    if index < len(body):
        args = [repr(path_a), repr(path_b)]
        for key, value, default in (
            ("start_a", start_a, 0), ("end_a", end_a, 0),
            ("start_b", start_b, 0), ("end_b", end_b, 0),
            ("context_lines", context_lines, 3), ("ignore_whitespace", ignore_whitespace, False),
        ):
            if value != default:
                args.append(f"{key}={value!r}")
        args.append(f"offset={index}")
        text += (
            f"\n[Showing diff lines {offset + 1}-{index} of {len(body)}. "
            f"Call diff_files({', '.join(args)}) to continue.]"
        )
    return text


def _rel(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path

@opalatex_tool(name="inspect_project", is_safe=True, description=(
    "Read environment metadata or statically inspect a Python file without importing or executing it. "
    "operation is environment or python; python requires path. Returns JSON."))
def inspect_project(operation: str, path: str = "") -> str:
    from .diagnostics import inspect_environment, inspect_python
    if operation == "environment":
        result = inspect_environment()
    elif operation == "python":
        if not path:
            raise ValueError("path is required for Python inspection.")
        result = inspect_python(_resolve_path(path))
    else:
        raise ValueError("operation must be environment or python.")
    text = json.dumps(result, ensure_ascii=False)
    budget = free_context_chars()
    if budget is not None and len(text) > budget:
        raise ValueError("Inspection output exceeds the remaining context budget. Read targeted source ranges with read_content_pos instead.")
    return text


@opalatex_tool(name="inspect_git", is_safe=True, description=(
    "Read Git status, diff, staged_diff, or log using fixed queries with no shell or project code execution. "
    "Project filters and submodule inspection are disabled. Optional path restricts the query. "
    "Page output by character offset and limit; follow next_offset."))
def inspect_git(operation: str = "status", path: str = "", offset: int = 0, limit: int = 10000) -> str:
    from .diagnostics import inspect_git as inspect
    budget = free_context_chars()
    if budget is not None and budget <= 0:
        raise ValueError("The context window is exhausted. Summarize before reading further.")
    page = limit if budget is None else min(limit, budget)
    return json.dumps(inspect(get_project_path(), operation, path, offset, page))


@opalatex_tool(name="read_document", is_safe=True, description=(
    "Extract and page text from PDF, DOCX, PPTX or XLSX without writing files or executing code. "
    "offset is a zero-based character offset; follow next_offset until null. Formatting is not preserved."))
def read_document(path: str, offset: int = 0, limit: int = 10000) -> str:
    from .attachments import extract_document_text_from_path
    if Path(path).suffix.lower() not in _DOC_EXTS:
        raise ValueError("read_document supports PDF, DOCX, PPTX and XLSX. Use read_content_pos for text files.")
    if offset < 0 or not 1 <= limit <= 50000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 50000.")
    budget = free_context_chars()
    if budget is not None and budget <= 0:
        raise ValueError("The context window is exhausted. Summarize before reading further.")
    content = extract_document_text_from_path(_resolve_path(path))
    if not content.strip():
        raise ValueError("The document contains no extractable text.")
    end = min(len(content), offset + (limit if budget is None else min(limit, budget)))
    return json.dumps({"content": content[offset:end], "total_chars": len(content),
                       "next_offset": end if end < len(content) else None}, ensure_ascii=False)


# ─── Code navigation ──────────────────────────────────────────────────────────
# Backed by opalatex/code_index.py: definitions found by name instead of by
# grep, and an outline of a file or folder with line numbers. The index lives in
# <project>/.opalatex/code_index.sqlite and is brought up to date by mtime before
# every query, so files changed by any means -- the editor, a command, git --
# are re-read; the first query of a project builds it (about 2 s for 450 files).

_OUTLINE_MAX_FILES = 150
_OUTLINE_SYMBOLS_PER_FILE = 12
_INDEXED_LANGUAGES = (
    "Python, JavaScript/TypeScript, Java, C/C++, C#, Go, Rust, Kotlin, Swift, PHP, Ruby "
    "and other C-like languages (definitions by pattern), LaTeX (sections, frames, "
    "labels, macros, environments, \\input files) and BibTeX (citation keys)"
)


def _ready_code_index():
    from .code_index import CODE_INDEX

    CODE_INDEX.set_project(get_project_path())
    CODE_INDEX.build()
    return CODE_INDEX


def _index_rel_path(index, resolved: str) -> str:
    rel = os.path.relpath(os.path.abspath(resolved), index.root)
    if rel == os.curdir:
        return ""
    if rel.startswith(os.pardir):
        raise ValueError(f"Error: {_preview(resolved)} is outside the project, which is all the index covers.")
    return rel


@opalatex_tool(
    name="find_symbol",
    is_safe=True,
    description=(
        "Find where a symbol is defined, by name: a function, class, method or constant "
        "in code; a section, label, macro or environment in LaTeX; a citation key in "
        "BibTeX. Returns each definition's file:line and signature and, for Python, the "
        "functions that call it (matched by name). An exact "
        "name is matched first; otherwise similar names are listed. Faster and more "
        "precise than search_code for definitions; search_code still finds every textual "
        "use, including LaTeX \\ref and \\cite."
    )
)
def find_symbol(name: str, include_callers: bool = True) -> str:
    query = str(name or "").strip()
    if not query:
        raise ValueError("Error: name is empty. Pass the symbol name to look up, e.g. 'load_config' or 'sec:intro'.")
    AGENT_PROGRESS.update("find_symbol", f"name={_preview(query)}")
    index = _ready_code_index()
    # LaTeX macros are stored with their backslash; accept the name without it.
    candidates = index.search(query, limit=30)
    if not candidates and not query.startswith("\\"):
        candidates = index.search("\\" + query, limit=30)
    exact = [s for s in candidates if s.name.lower() in (query.lower(), "\\" + query.lower())]
    if not exact:
        if not candidates:
            return (
                f"No symbol named '{query}' is defined in the indexed files ({_INDEXED_LANGUAGES}). "
                "search_code finds it as text if it exists anywhere else."
            )
        similar = ", ".join(f"{s.name} ({s.kind}, {s.file}:{s.line})" for s in candidates[:10])
        return f"No symbol is named exactly '{query}'. Similar names: {similar}."

    lines = [f"Definitions of '{query}':"]
    for s in exact[:20]:
        lines.append(f"- {s.kind} {s.name} — {s.file}:{s.line}")
        if s.signature:
            lines.append(f"    {s.signature}")
    # The call graph is complete only where it comes from a parser: Python's
    # `ast`. The pattern fallback of other languages sees the definition line
    # alone, so an empty caller list there would be a false "nobody calls it".
    python_defs = [d for d in exact if d.file.endswith((".py", ".pyw"))]
    if include_callers and python_defs:
        callers = [c for c in index.find_callers(python_defs[0].name, limit=40)
                   if c.file.endswith((".py", ".pyw"))]
        if callers:
            lines.append(f"Called from ({len(callers)}{'+' if len(callers) == 40 else ''}):")
            lines.extend(f"- {c.kind} {c.name} — {c.file}:{c.line}" for c in callers)
        else:
            lines.append("No indexed Python function calls it by name (module-level code is not covered).")
    elif include_callers and exact[0].kind in {"function", "method", "class", "const"}:
        lines.append("Callers are tracked for Python only; search_code finds the uses of this name.")
    return _truncate_to_context_budget("\n".join(lines))


@opalatex_tool(
    name="code_outline",
    is_safe=True,
    description=(
        "Outline of a file or folder with line numbers. For a file: every definition in "
        "order (functions, classes and methods in code; sections, frames, labels and "
        "macros in LaTeX; citation keys in BibTeX), so you can jump to one with "
        "read_content_pos instead of reading the whole file. For a folder (default: the "
        "project root): each indexed file with its main definitions -- a map of where "
        "things live."
    )
)
def code_outline(path: str = ".") -> str:
    try:
        resolved = _resolve_path(path or ".")
    except Exception as e:
        raise ValueError(f"Error resolving path: {e}")
    AGENT_PROGRESS.update("code_outline", f"path={_preview(resolved)}")
    if not os.path.exists(resolved):
        raise ValueError(f"Error: path not found: {_preview(resolved)}.")
    index = _ready_code_index()
    rel = _index_rel_path(index, resolved)

    if os.path.isfile(resolved):
        from .code_index import _INDEXED_EXTS

        if Path(resolved).suffix.lower() not in _INDEXED_EXTS:
            return f"No outline for {rel}: its file type is not indexed. Indexed: {_INDEXED_LANGUAGES}."
        symbols = index.symbols_in_file(rel)
        _note_file_seen(resolved)
        if not symbols:
            return f"{rel}: no definitions found."
        width = len(str(symbols[-1].line))
        body = "\n".join(
            f"{s.line:>{width}}  {s.kind:<11} {s.name}"
            + (f"  — {s.signature}" if s.signature and s.kind in {"function", "method", "class"} else "")
            for s in symbols
        )
        return _truncate_to_context_budget(f"{rel} ({len(symbols)} definitions):\n{body}")

    files = index.indexed_files(rel)
    if not files:
        return f"No indexed files under {rel or 'the project root'}. Indexed: {_INDEXED_LANGUAGES}."
    lines = [f"{len(files)} indexed files under {rel or 'the project root'}:"]
    for file_rel in files[:_OUTLINE_MAX_FILES]:
        symbols = [s for s in index.symbols_in_file(file_rel) if s.exported and s.kind not in {"label", "input"}]
        if symbols:
            names = ", ".join(f"{s.name}:{s.line}" for s in symbols[:_OUTLINE_SYMBOLS_PER_FILE])
            more = f" (+{len(symbols) - _OUTLINE_SYMBOLS_PER_FILE} more)" if len(symbols) > _OUTLINE_SYMBOLS_PER_FILE else ""
            lines.append(f"{file_rel}: {names}{more}")
        else:
            lines.append(file_rel)
    if len(files) > _OUTLINE_MAX_FILES:
        lines.append(f"… {len(files) - _OUTLINE_MAX_FILES} more files; outline a subfolder to see them.")
    return _truncate_to_context_budget("\n".join(lines))


def get_code_navigation_tools():
    """Read-only code navigation, for orchestrators under every policy and for workers."""
    return [find_symbol, code_outline]


def get_diagnostic_tools():
    return [inspect_project, inspect_git, read_document, check_presentation]


def get_workspace_action_tools():
    """Tools that act on the workspace: file writes and command execution.

    The main agent holds all of them under every policy (PROJECT_DESIGN 2.23):
    it is the agent that does the task. A helper gets only the part of this list
    its job needs (`get_helper_tools`).

    Every execution tool here is declared `is_safe=False`, so the `opalatex_tool`
    gate still blocks it in plan mode and asks the user in edit mode no matter
    which role calls it.
    """
    return [
        write_file,
        edit_file,
        write_content_pos,
        replace_content_range,
        create_docx_file,
        create_pptx_file,
        create_presentation,
        edit_presentation,
        set_presentation_theme,
        export_tex_to_docx,
        generate_image,
        compile_latex,
        run_command,
        run_python_script,
        run_background_command,
        run_interactive_command,
    ]


#: The write tools a helper keeps: confined to the scratch area by
#: `_refuse_helper_project_write`, they hold its notes and plans.
_HELPER_SCRATCH_WRITE_TOOLS = ("write_file", "edit_file", "write_content_pos", "replace_content_range")
#: The execution a helper keeps: the skills' own processor scripts.
_HELPER_EXECUTION_TOOLS = ("run_command", "run_python_script")


def get_helper_tools(vision: bool = False):
    """Tools handed to a helper agent (`memgpt_runtime.build_run_skill_tool`).

    A helper gathers and returns data -- reads, searches, extracts, condenses,
    researches the web -- so it gets the read tools, `web_search`, scratch-area
    writes and the commands that run a skill's scripts. It does not get the
    tools that produce deliverables (documents, presentations, images, a
    compile) or that drive the user's terminal: those belong to the main agent.

    `analyze_image` is included only when the helper's own model accepts images
    (*vision*), since that is the model the tool asks inside a helper.
    """
    action_by_name = {getattr(tool, "name", ""): tool for tool in get_workspace_action_tools()}
    tools = [
        search_conversation_history,
        update_achievements_memory,
        get_project_overview,
        search_code,
        *get_code_navigation_tools(),
        read_file,
        read_content_pos,
        diff_files,
        ask_question,
        *get_diagnostic_tools(),
        get_editor_state,
        read_terminal,
        web_search,
        *(action_by_name[name] for name in _HELPER_SCRATCH_WRITE_TOOLS),
        *(action_by_name[name] for name in _HELPER_EXECUTION_TOOLS),
    ]
    if vision:
        tools.append(analyze_image)
    return tools


# ─── Editor State ─────────────────────────────────────────────────────────────
# The GUI stages what it has open — tabs, focused file, live buffer, selection —
# into <project>/.opalatex/_editor_state.json at the start of every agent turn
# (agent_stdin.handle_run). Reading it back is a pure lookup of data the
# front-end already sent, so the tool is safe: it answers in plan mode without a
# permission prompt, which is exactly when knowing what the user is looking at
# matters most.

EDITOR_STATE_FILENAME = "_editor_state.json"


def editor_state_path(project_path: str) -> str:
    """Absolute path of the staged editor state file for *project_path*."""
    return os.path.join(project_path, ".opalatex", EDITOR_STATE_FILENAME)


def _load_editor_state(project_path: str) -> dict:
    """Return the staged editor state, or {} when nothing has been staged yet."""
    path = editor_state_path(project_path)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"Error reading the staged editor state: {e}")
    return state if isinstance(state, dict) else {}


@opalatex_tool(
    name="get_editor_state",
    is_safe=True,
    description=(
        "Report what the user currently has open in the IDE editor: the list of open tabs, "
        "which tab is focused, and the text the user has selected. Call this whenever the user "
        "points at the editor instead of naming a path — 'this file', 'the open document', "
        "'the selected text', 'fix this here' — so you act on what they are looking at instead "
        "of guessing. Pass include_content=True to also get the focused tab's live buffer, which "
        "reflects edits the user has not saved to disk yet; when the saved file is what you need, "
        "read it with read_file instead."
    ),
)
def get_editor_state(include_content: bool = False) -> str:
    AGENT_PROGRESS.update("get_editor_state", f"include_content={include_content}")

    project_path = get_project_path()
    state = _load_editor_state(project_path)
    if not state:
        return (
            "No editor state has been staged for this project yet. The IDE writes it at the "
            "start of each agent turn, so this normally means the request did not come from "
            "the IDE editor. Ask the user which file they mean, or locate it with "
            "get_project_overview / search_code."
        )

    current_file = str(state.get("current_file", "") or "").replace("\\", "/")
    open_files = [str(p).replace("\\", "/") for p in state.get("open_files") or [] if str(p).strip()]
    selected_text = str(state.get("selected_text", "") or "")
    editor_content = str(state.get("editor_content", "") or "")
    # The buffer and the selection are captured once, at the start of the turn,
    # for the tab focused then. open_file/close_file keep the tab list current
    # but cannot capture a buffer, so once the agent moves the focus the two
    # belong to a tab that is no longer the focused one. Absent in states staged
    # before the key existed, where they always belonged to `current_file`.
    buffer_file = str(state.get("editor_content_file", current_file) or "").replace("\\", "/")
    focus_moved = buffer_file != current_file

    lines = ["## Editor State"]

    if open_files:
        listed = "\n".join(
            f"  {i}. `{p}`" + ("  <- focused" if p == current_file else "")
            for i, p in enumerate(open_files, 1)
        )
        lines.append(f"- **Open tabs ({len(open_files)})**:\n{listed}")
    else:
        # Absent (not empty) when the turn came from a surface that does not track
        # tabs, e.g. the inline editor. Say so rather than reporting "no tabs open".
        lines.append("- **Open tabs**: not reported by this turn's caller")

    if current_file:
        lines.append(f"- **Focused file**: `{current_file}`")
        if open_files and current_file not in open_files:
            lines.append(
                "  (the focused file is not among the reported tabs — the user may have "
                "changed tabs after this turn started)"
            )
    else:
        lines.append("- **Focused file**: (no file open in the editor)")

    if focus_moved:
        lines.append(
            "- **Focus changed during this turn**: the selection and live buffer below were "
            f"captured at its start for `{buffer_file or '(no file)'}`. Nothing was captured for "
            f"`{current_file or '(no file)'}`; read it from disk with read_file."
        )

    if selected_text:
        where = f" in `{buffer_file}`" if focus_moved else ""
        lines.append(
            f"\n### Selected text{where} ({len(selected_text.splitlines())} lines, "
            f"{len(selected_text):,} chars)\n```\n"
            + _truncate_to_context_budget(selected_text, reserve_pct=25)
            + "\n```"
        )
    else:
        lines.append("- **Selection**: (nothing selected)")

    if include_content:
        if editor_content:
            lines.append(
                f"\n### Live buffer of `{buffer_file or 'the focused tab'}` "
                f"({len(editor_content.splitlines()):,} lines, {len(editor_content):,} chars)\n"
                "This is the editor buffer, which may contain unsaved edits that differ from "
                "the file on disk.\n```\n"
                + _truncate_to_context_budget(editor_content)
                + "\n```"
            )
        else:
            lines.append("\n- **Live buffer**: (empty)")
    elif editor_content:
        lines.append(
            f"- **Live buffer**: {len(editor_content):,} chars available "
            "— call get_editor_state(include_content=True) to read it."
        )

    return "\n".join(lines)


# ─── IDE Control ──────────────────────────────────────────────────────────────
# get_editor_state reports the window; these move it. Each is a round trip to
# the desktop app (opalatex/ide_control.py), which performs the command with the
# handlers a click uses and answers with the resulting tabs. Those are written
# back into the staged editor state, so get_editor_state stays true for the rest
# of the turn. They change what the window shows, never a file, so they are
# safe: showing the user something is as legitimate in plan mode as reading it.

def _ide_project_file(path: str) -> tuple[str, str]:
    """(project dir, project-relative POSIX path) of *path*, which must be inside the project."""
    if not str(path or "").strip():
        raise ValueError("Error: 'path' is required: the project-relative path of a file.")
    project_dir = os.path.abspath(get_project_path())
    resolved = os.path.abspath(_resolve_path(path))
    try:
        common = os.path.commonpath([os.path.normcase(project_dir), os.path.normcase(resolved)])
    except ValueError:  # different drives on Windows
        common = ""
    if common != os.path.normcase(project_dir):
        raise ValueError(
            f"Error: {_preview(path)} is outside the project. The IDE shows only files of the "
            f"open project ({project_dir})."
        )
    return project_dir, os.path.relpath(resolved, project_dir).replace(os.sep, "/")


def _record_editor_tabs(project_dir: str, open_files: list, current_file: str) -> None:
    """Write the tabs the IDE reported after a command into the staged editor state."""
    try:
        state = _load_editor_state(project_dir)
    except ValueError:
        state = {}
    # The buffer and selection still belong to the tab they were captured for.
    state.setdefault("editor_content_file", state.get("current_file", ""))
    state["open_files"] = [str(p) for p in open_files if str(p).strip()]
    state["current_file"] = current_file
    path = editor_state_path(project_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Warning: Failed to record the editor tabs: {e}", file=sys.stderr)


def _ide_tabs_after(project_dir: str, result: dict) -> str:
    """Record and describe the tab state the IDE answered with."""
    open_files = [str(p) for p in result.get("open_files") or [] if str(p).strip()]
    current_file = str(result.get("current_file") or "")
    _record_editor_tabs(project_dir, open_files, current_file)
    if not open_files:
        return "No tabs are open now."
    listed = ", ".join(
        f"`{p}`" + (" (focused)" if p == current_file else "") for p in open_files
    )
    return f"Open tabs now ({len(open_files)}): {listed}."


def _line_count(path: str) -> int:
    with open(path, "rb") as f:
        data = f.read()
    return max(1, data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0))


@opalatex_tool(
    name="open_file",
    is_safe=True,
    description=(
        "Open a project file in the user's IDE editor, to show them what you are talking about or "
        "what you changed. `path` is project-relative. `line` (1-based, optional) scrolls the "
        "editor to that line. With focus=true (the default) the file becomes the visible tab and "
        "the editor is brought on screen; with focus=false it is only added as a background tab "
        "and the user's view is left alone. The user's keyboard focus is never moved. This changes "
        "only what the window shows: it neither reads the file for you (use read_file) nor "
        "modifies it. Files the IDE has no tab for (images, archives, office documents) are refused."
    ),
)
async def open_file(path: str, line: int = 0, focus: bool = True) -> str:
    AGENT_PROGRESS.update("open_file", f"{path}" + (f":{line}" if line else ""))
    from .ide_control import send_ide_command

    project_dir, rel = _ide_project_file(path)
    resolved = os.path.join(project_dir, rel)
    if not os.path.isfile(resolved):
        candidates = _same_name_candidates(resolved)
        where = (
            "A file with this name exists at: " + ", ".join(candidates) + "."
            if candidates
            else "No file with this name exists anywhere in the project; list it with get_project_overview."
        )
        raise ValueError(f"Error: file not found: {_preview(rel)}. {where}")
    if line < 0:
        raise ValueError("Error: 'line' is 1-based; pass 0 (or omit it) to open without scrolling.")
    if line and not focus:
        raise ValueError(
            "Error: 'line' scrolls the visible editor, so it needs focus=true. A background tab "
            "opens at its top."
        )
    if line:
        total = _line_count(resolved)
        if line > total:
            raise ValueError(f"Error: {rel} has {total} lines; line {line} does not exist.")

    result = await send_ide_command(
        "open_file",
        {"path": rel, "line": line or None, "focus": bool(focus)},
        project_path=project_dir,
    )
    how = "as the focused tab" if focus else "as a background tab"
    at = f", scrolled to line {line}" if line else ""
    return f"Opened `{rel}` in the editor {how}{at}.\n{_ide_tabs_after(project_dir, result)}"


@opalatex_tool(
    name="close_file",
    is_safe=True,
    description=(
        "Close a tab in the user's IDE editor. `path` is project-relative, as get_editor_state "
        "lists the open tabs. A tab with unsaved edits is never closed: the call is refused, so "
        "the user's work stays on screen. Close only tabs you opened yourself or that the user "
        "asked you to close."
    ),
)
async def close_file(path: str) -> str:
    AGENT_PROGRESS.update("close_file", str(path))
    from .ide_control import send_ide_command

    project_dir, rel = _ide_project_file(path)
    result = await send_ide_command("close_file", {"path": rel}, project_path=project_dir)
    return f"Closed the tab of `{rel}`.\n{_ide_tabs_after(project_dir, result)}"


@opalatex_tool(
    name="reveal_in_pdf",
    is_safe=True,
    description=(
        "Show the user where a line of LaTeX source lands in the compiled PDF. The IDE focuses the "
        "`.tex` tab at that line, opens the PDF pane beside it and scrolls the PDF to the typeset "
        "text, highlighting it briefly: the editor's own forward search (Ctrl+Click). `path` is a "
        "project-relative .tex file and `line` is 1-based. It reads the SyncTeX data of the last "
        "full compile, so after changing the source the document must be compiled again (a draft "
        "pass does not update it). The answer says which PDF page the line is on."
    ),
)
async def reveal_in_pdf(path: str, line: int) -> str:
    AGENT_PROGRESS.update("reveal_in_pdf", f"{path}:{line}")
    from .ide_control import send_ide_command

    project_dir, rel = _ide_project_file(path)
    resolved = os.path.join(project_dir, rel)
    if not os.path.isfile(resolved):
        raise ValueError(f"Error: file not found: {_preview(rel)}.")
    if not rel.lower().endswith(".tex"):
        raise ValueError(
            f"Error: {rel} is not a .tex file. Only LaTeX source has a place in the PDF; use "
            "open_file to show any other file."
        )
    if line < 1:
        raise ValueError("Error: 'line' is the 1-based source line to find in the PDF.")
    total = _line_count(resolved)
    if line > total:
        raise ValueError(f"Error: {rel} has {total} lines; line {line} does not exist.")

    result = await send_ide_command(
        "reveal_in_pdf", {"path": rel, "line": line}, project_path=project_dir,
    )
    page = result.get("page")
    where = f"on page {page}" if page else "in the PDF"
    return (
        f"Showing line {line} of `{rel}` {where}, highlighted in the PDF pane beside the editor.\n"
        f"{_ide_tabs_after(project_dir, result)}"
    )


#: The window's layouts, as `set_layout` offers them (`layoutModes.js` holds the
#: same names), with what each one puts on screen.
IDE_LAYOUTS = {
    "ide": "the editor with its PDF/preview pane, the file explorer, the chat panel and the "
           "bottom panel (terminal, Problems)",
    "studio": "editor, PDF/preview, chat and terminal on one screen",
    "document": "only the open file and its preview, side by side",
    "chat": "the conversation fills the window; the editor is hidden",
    "chat-bottom": "the conversation above the bottom panel (terminal, Problems); the editor is hidden",
    "review": "the checkpoint history of the agent's changes, where the user reviews or restores them",
}


@opalatex_tool(
    name="set_layout",
    is_safe=True,
    description=(
        "Switch the user's IDE window to another layout, as the layout buttons of the activity bar "
        "do. Layouts: "
        + "; ".join(f"`{name}`: {what}" for name, what in IDE_LAYOUTS.items())
        + ". Change the layout only when the user asks for it or when what you are about to show "
        "needs it (for example `review` after a change the user wants to inspect); the layout is "
        "the user's workspace, not yours."
    ),
)
async def set_layout(layout: str) -> str:
    AGENT_PROGRESS.update("set_layout", str(layout))
    from .ide_control import send_ide_command

    name = str(layout or "").strip()
    if name not in IDE_LAYOUTS:
        raise ValueError(
            f"Error: unknown layout {_preview(layout)}. Layouts: "
            + ", ".join(f"`{n}`" for n in IDE_LAYOUTS) + "."
        )
    project_dir = os.path.abspath(get_project_path())
    await send_ide_command("set_layout", {"layout": name}, project_path=project_dir)
    return f"The IDE window is now in the `{name}` layout: {IDE_LAYOUTS[name]}."


#: Most recent Problems entries `get_problems` reports, and the length kept of
#: a message that is not a compile log.
_PROBLEMS_SHOWN = 30
_PROBLEM_MESSAGE_CHARS = 1500


@opalatex_tool(
    name="get_problems",
    is_safe=True,
    description=(
        "Read the Problems panel of the user's IDE: the failures the window is showing right now, "
        "such as a LaTeX compile the user ran with the Compile button and the errors of tools. "
        "Each entry gives its source, severity, file and message (a compile entry carries the "
        "compiler output). Use it when the user refers to an error they see, before guessing."
    ),
)
async def get_problems() -> str:
    AGENT_PROGRESS.update("get_problems")
    from .ide_control import send_ide_command

    project_dir = os.path.abspath(get_project_path())
    result = await send_ide_command("get_problems", {}, project_path=project_dir)
    problems = [p for p in result.get("problems") or [] if isinstance(p, dict)]
    if not problems:
        return "The Problems panel is empty."
    shown = problems[-_PROBLEMS_SHOWN:]
    lines = [f"Problems panel ({len(problems)} entries, oldest first):"]
    if len(problems) > len(shown):
        lines.append(f"(the {len(problems) - len(shown)} oldest are omitted)")
    for index, problem in enumerate(shown, 1):
        message = str(problem.get("message") or "").strip()
        if problem.get("source") == "latex_compile":
            message = _trim_compile_log(message)
        elif len(message) > _PROBLEM_MESSAGE_CHARS:
            message = message[:_PROBLEM_MESSAGE_CHARS] + " ... [TRUNCATED]"
        head = f"{index}. [{problem.get('severity') or 'error'}] {problem.get('tool') or 'IDE'}"
        if problem.get("file"):
            head += f" — `{problem['file']}`"
        if problem.get("time"):
            head += f" ({problem['time']})"
        lines.append(f"{head}\n{message}")
    return "\n".join(lines)


#: Lines `read_terminal` returns by default and at most.
_TERMINAL_DEFAULT_LINES = 60
_TERMINAL_MAX_LINES = 400


@opalatex_tool(
    name="read_terminal",
    is_safe=True,
    description=(
        "Read the last lines of the user's main IDE terminal, as plain text: the output of a server "
        "or build started there with run_background_command, or of commands the user ran "
        "themselves. `lines` is how many lines from the bottom to return "
        f"(default {_TERMINAL_DEFAULT_LINES}, at most {_TERMINAL_MAX_LINES}). It only reads; it "
        "never types into the terminal or starts one."
    ),
)
async def read_terminal(lines: int = _TERMINAL_DEFAULT_LINES) -> str:
    AGENT_PROGRESS.update("read_terminal", f"lines={lines}")
    import urllib.error
    import urllib.parse
    import urllib.request

    if lines < 1 or lines > _TERMINAL_MAX_LINES:
        raise ValueError(f"Error: 'lines' must be between 1 and {_TERMINAL_MAX_LINES}.")
    project_dir = os.path.abspath(get_project_path())

    def fetch():
        from .local_auth import local_api_connection
        base_url, cookie = local_api_connection()
        url = base_url + "/api/terminal/output?" + urllib.parse.urlencode({"projectPath": project_dir})
        request = urllib.request.Request(url, headers={"Cookie": cookie})
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read().decode("utf-8")).get("error") or ""
            except ValueError:
                reason = ""
            raise ValueError(f"Error: {reason or f'the IDE answered HTTP {e.code}'}") from None

    # The HTTP server shares this event loop (see run_background_command).
    data = await asyncio.to_thread(fetch)
    screen = str(data.get("text") or "").rstrip("\n").split("\n")
    while screen and not screen[0].strip():
        screen.pop(0)
    if not screen:
        return "The main terminal is running and shows nothing yet."
    tail = screen[-lines:]
    earlier = len(screen) - len(tail)
    note = f"last {len(tail)} of {len(screen)} lines" if earlier else f"all {len(tail)} lines"
    if data.get("truncated") and not earlier:
        note += "; older output is no longer kept"
    return f"Main terminal ({note}):\n```text\n" + "\n".join(tail) + "\n```"


def get_ide_control_tools():
    """Tools that drive or read the desktop window. The main agent's: a helper
    runs out of sight of the user, so there is nothing for it to show them.
    `read_terminal` only reads, and a helper asked about what the user's terminal
    printed gets it too (`get_helper_tools`)."""
    return [open_file, close_file, reveal_in_pdf, set_layout, get_problems, read_terminal]


# ─── Achievements Memory ─────────────────────────────────────────────────────
@opalatex_tool(
    name="update_achievements_memory", 
    is_safe=True,
    description=(
        "Update the transient Achievements Memory with a summary of the important milestones and tasks "
        "you have accomplished so far during this turn. Keep it concise (e.g. bullet points). "
        "This memory is passed to worker sub-agents so they know what has already been done. "
        "Use this FREQUENTLY for: discovering an important file, finishing a heartbeat iteration, "
        "successfully reading/writing a file, or finding the root cause of a bug."
    )
)
def update_achievements_memory(summary: str) -> str:
    global TURN_ACHIEVEMENTS
    AGENT_PROGRESS.update("update_achievements_memory", f"summary={summary[:50]}")
    TURN_ACHIEVEMENTS = summary
    try:
        from opalatex.agent_stdin import print_event
        print_event("achievements_update", {"content": summary})
    except Exception as e:
        pass
    return "Achievements Memory updated successfully."

# ─── Long-Term Memory (MemGPT-style) ──────────────────────────────────────────
@opalatex_tool(name="read_core_memory", is_safe=True, description="Read the global 'Core Memory' of the project. Contains rules, persistent context, and architectural decisions you should follow.")
def read_core_memory() -> str:
    AGENT_PROGRESS.update("read_core_memory")
    if not _PROJECT_SESSION:
        return "Core memory not available (no active session)."
    
    if getattr(_PROJECT_SESSION, "use_shared_memory", True):
        return getattr(_PROJECT_SESSION, "core_memory", "") or "(Core memory is empty)"
    else:
        if not _PROJECT_STORE:
            return "(Core memory is empty)"
        mem = _PROJECT_STORE.get_chat_core_memory(_PROJECT_SESSION.name, getattr(_PROJECT_SESSION, "current_chat_id", "main"))
        return mem or "(Core memory is empty)"

@opalatex_tool(name="append_core_memory", is_safe=True, description="Append a new persistent rule, context, or decision to the Core Memory. Do this when you learn something about the user's preferences, demands or the project's state that you want to remember across different executions.")
def append_core_memory(content: str) -> str:
    AGENT_PROGRESS.update("append_core_memory", f"content={_preview(content)}")
    if not _PROJECT_SESSION or not _PROJECT_STORE:
        return "Core memory not available (no active session)."
    
    use_shared = getattr(_PROJECT_SESSION, "use_shared_memory", True)
    
    if use_shared:
        current = getattr(_PROJECT_SESSION, "core_memory", "")
        new_mem = current + ("\n" if current else "") + "- " + content
        _PROJECT_SESSION.core_memory = new_mem
        try:
            _PROJECT_STORE.save(_PROJECT_SESSION)
            return "Successfully appended to global Core Memory."
        except Exception as e:
            raise ValueError(f"Error saving Core Memory: {e}")
    else:
        chat_id = getattr(_PROJECT_SESSION, "current_chat_id", "main")
        current = _PROJECT_STORE.get_chat_core_memory(_PROJECT_SESSION.name, chat_id)
        new_mem = current + ("\n" if current else "") + "- " + content
        try:
            _PROJECT_STORE.update_chat_core_memory(_PROJECT_SESSION.name, chat_id, new_mem)
            return f"Successfully appended to chat '{chat_id}' isolated Core Memory."
        except Exception as e:
            raise ValueError(f"Error saving isolated Core Memory: {e}")

@opalatex_tool(name="search_conversation_history", is_safe=True, description="Search through the past conversations of this project using semantic search (RAG) to remember previous context, decisions, or user instructions. Use this when you need context about past tasks.")
def search_conversation_history(query: str, limit: int = 5) -> str:
    AGENT_PROGRESS.update("search_conversation_history", f"query={_preview(query)}")
    if not _PROJECT_SESSION:
        return "Archival search not available (no active session)."
    
    try:
        from .archival import search_archival
        chat_id = None
        if not getattr(_PROJECT_SESSION, "use_shared_memory", True):
            chat_id = getattr(_PROJECT_SESSION, "current_chat_id", "main")
        results = search_archival(_PROJECT_SESSION.name, query, limit=limit, chat_id=chat_id)
        if not results:
            return f"No results found in archival memory for query: '{query}'"
            
        out = [f"Found {len(results)} results in Archival Memory:"]
        for r in results:
            out.append(f"[{r['timestamp']} | {r['role'].upper()}] {r['content']}")
        return "\n".join(out)
    except Exception as e:
        raise ValueError(f"Error searching Archival Memory: {e}")

def _record_mode_event(content: str) -> None:
    """Record a state-change event as diagnostic activity, not as a chat message.

    ``project_history`` holds only the visible conversation (what the model reads);
    audit entries belong to ``project_activity`` (PROJECT_DESIGN 2.5). The agent
    still learns the outcome within the turn: it is returned in the ``create_plan``
    tool result.

    This is a fire-and-forget helper: it must never raise, because it is called
    inside tool functions and an exception here would surface as a tool error.
    """
    if not _PROJECT_SESSION or not _PROJECT_STORE:
        return
    try:
        _PROJECT_STORE.append_activity(
            _PROJECT_SESSION,
            "plan_decision",
            content=content,
            agent="chat_orchestrator",
        )
    except Exception:
        pass  # never let history recording crash the tool


def _persist_approved_plan(plan_text: str) -> None:
    """Keep an approved plan readable after the approval dialog closes.

    The plan is presented through a transient ``input_request``: the user reads
    it, answers, and it is gone. That made the deliverable live in a channel that
    does not survive the turn -- observed with `openrouter/openai/gpt-5.6-luna`,
    which handed the user a 5 817-character plan in the dialog and persisted an
    857-character summary of it, so reopening the chat left a summary of a plan
    nobody could read any more. It is the pointer-shaped answer the single text
    channel exists to prevent, one layer up.

    Unlike the approval *decision*, which is an audit entry and belongs in
    ``project_activity`` (PROJECT_DESIGN 2.5), the plan is model-authored text
    addressed to the user: it is conversation, so it goes to ``project_history``
    and the model reads its own plan back like any other answer it gave. Only an
    approved plan is persisted -- replaying a rejected one would teach the model
    that the answer the user turned down was acceptable.

    Fire-and-forget: it must never raise, because a failure here would surface as
    a tool error on a plan the user already approved.
    """
    text = str(plan_text or "").strip()
    if not text or not _PROJECT_SESSION or not _PROJECT_STORE:
        return
    try:
        _PROJECT_STORE.append_message(_PROJECT_SESSION, "assistant", text)
    except Exception as exc:
        # Never raised: the user already approved this plan and a store failure
        # must not surface as a tool error on it. But it is never silent either --
        # swallowing it puts the plan back in a channel that does not survive the
        # turn, which is the whole defect, with nothing to show it happened.
        try:
            from opalatex.agent_stdin import print_event

            print_event("error", {
                "message": f"The approved plan could not be saved to the chat history: {exc}",
                "trace": "",
            })
        except Exception:
            pass


@opalatex_tool(
    name="create_plan",
    is_safe=True,
    description=(
        "Presents a proposed implementation plan to the user for approval, and is "
        "available only in 'plan' mode. Use it when you have gathered enough context "
        "to propose work you are about to carry out. It halts execution and waits for "
        "the user to approve or reject the plan. It is not how you show a plan the "
        "user asked to see: a plan that is itself the answer is written as normal text."
    )
)
async def create_plan(plan_content: str) -> str:
    AGENT_PROGRESS.update("create_plan")
    if not _PROJECT_SESSION:
        return "Failed: No active session."
    from .agent_stdin import unattended_refusal, unattended_turn
    if unattended_turn() is not None:
        return unattended_refusal("the plan was not submitted for approval; write it in your reply instead")

    # The tool list is composed once per turn, so withholding this tool in
    # non-plan modes cannot cover the case where the mode changes *during* a
    # turn: approving a plan flips the session to auto for the execution phase,
    # and the already-built list still carries the tool. Without this check the
    # model can open a second approval dialog in the middle of executing the
    # plan the user just approved.
    _mode = str(getattr(_PROJECT_SESSION, "mode", "") or "").strip().lower()
    if _mode != "plan":
        return (
            f"Failed: create_plan is only available in 'plan' mode (current mode: '{_mode}'). "
            "If the plan is the answer the user asked for, write it as normal text. "
            "If you are executing an approved plan, carry on with the work."
        )
    
    T.info("Presenting Proposed Plan to user for approval...")
    
    # The plan travels as `markdown_content` so a front-end can render it and
    # let the user edit it before approving; the answer comes back through the
    # one input channel every front-end implements.
    import asyncio
    from opalatex.agent_stdin import request_user_input

    try:
        raw = await request_user_input(
            "Review the proposed plan below:",
            options=["yes", "no"],
            default="no",
            input_type="confirm",
            markdown_content=plan_content,
        )

        # A front-end that allows editing answers with JSON carrying the edited plan.
        import json
        try:
            data = json.loads(raw)
            approved_str = data.get("response", "no")
            edited_plan = data.get("editedContent", plan_content)
        except Exception:
            approved_str = str(raw).strip()
            edited_plan = plan_content

        approved = approved_str.lower() in ("yes", "y", "s", "sim", "true", "1")
    except asyncio.TimeoutError:
        _record_mode_event("[PLAN EXPIRED] No approval was received. Execution remains blocked.")
        raise ValueError("Plan approval expired without a response. The plan was NOT approved; do not execute it.")
    
    if approved:
        prev_mode = getattr(_PROJECT_SESSION, "_initial_mode", "plan")
        _PROJECT_SESSION.mode = "auto"
        if edited_plan != plan_content:
            _PROJECT_SESSION.plan_text = edited_plan
        # Record approval immediately in chat history. Do not save the whole
        # project here, because the mode is only temporarily auto for this turn.
        _record_mode_event(
            f"[PLAN APPROVED] The user approved the proposed plan. "
            f"Temporary mode for this turn: '{prev_mode}' -> 'auto'. "
            "The project mode must be restored at the end of the turn."
        )
        _persist_approved_plan(edited_plan)
        return f"The user APPROVED the plan. The system is temporarily in 'auto' mode for this turn only. Proceed to execute the plan.\n\nPlan Content:\n{edited_plan}"
    else:
        # Record rejection immediately in chat history
        _record_mode_event(
            "[PLAN REJECTED] The user rejected the proposed plan. "
            "The agent must NOT proceed with execution. "
            "Wait for user feedback before proposing a new plan."
        )
        # Throw an error to halt the agent immediately and prevent it from continuing
        raise ValueError("The user REJECTED the plan. Wait for the user to provide feedback in the chat.")


def context_window_tokens() -> int | None:
    """Return the active context window, which is what num_ctx actually caps.

    Resolved via config.resolve_effective_num_ctx: an explicit project
    override wins, otherwise the model's catalog entry, otherwise "Auto" (the
    provider-reported window). None means the window is unknown.
    """
    from .config import resolve_effective_num_ctx
    return resolve_effective_num_ctx("memgpt")


def used_context_tokens() -> int:
    """Return how much of the window is already occupied.

    Prefers the provider-reported `prompt_tokens` of the last call (see
    opalatex.token_usage), which is the only figure that counts the system
    prompt, the tool schemas and previous tool results. The char/4 estimate over
    project_history is the fallback before the first measured call, and it
    understates the real occupancy badly.
    """
    from opalatex.token_usage import get_context_prompt_tokens

    measured = get_context_prompt_tokens()
    if measured is not None:
        return measured
    history = getattr(_PROJECT_SESSION, "history", None) or []
    return len(json.dumps(history)) // 4


def free_context_chars(reserve_pct: int = 50) -> int | None:
    """Return how many characters a tool result may still add.

    Only *reserve_pct* of the remaining window is granted to any single tool
    result, leaving room for the rest of the turn and the model's own reply.
    None when the window is unknown: then no tool result is refused or cut on
    OpalaTex's side, and the provider alone decides whether the request fits.
    """
    window = context_window_tokens()
    if not window:
        return None
    free_tokens = max(0, window - used_context_tokens())
    return int(free_tokens * 4 * reserve_pct / 100)


def _truncate_to_context_budget(text: str, reserve_pct: int = 50) -> str:
    """Truncate *text* so it fits within the active model's free context budget."""
    allowed_chars = free_context_chars(reserve_pct)
    if allowed_chars is None or allowed_chars <= 0 or len(text) <= allowed_chars:
        return text
    return (
        text[:allowed_chars]
        + f"\n\n[Result truncated: {len(text):,} chars total, "
        f"{allowed_chars:,} shown ({reserve_pct}% of free context budget)]"
    )


@opalatex_tool(
    name="web_search",
    is_safe=True,
    description=(
        "Search the web for current information, documentation, news, or any topic. "
        "Use this when you need information that may be recent, external, or not in your training data, "
        "including any term, concept or acronym you do not confidently recognize or that may be misspelled. "
        "Returns a list of results with titles, URLs and snippets. "
        "Requires web search to be enabled in OpalaTex settings."
    ),
)
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo (default) or the configured MCP server."""
    AGENT_PROGRESS.update("web_search", f"query={_preview(query)}")
    try:
        from .web_search_config import is_enabled, do_search
    except ImportError as exc:
        return f"[web_search] Configuration module unavailable: {exc}"

    if not is_enabled():
        return (
            "[web_search] Web search is currently disabled. "
            "Enable it via the Web Search toggle in the OpalaTex chat panel."
        )

    # Bridge the async do_search into the synchronous tool interface.
    # Always use a fresh ThreadPoolExecutor so that asyncio.run() creates its own
    # event loop in a clean thread — this is safe regardless of whether the caller
    # is the main thread, an async worker thread (agenticblocks), or any other
    # context.  Using asyncio.get_event_loop() in Python 3.10+ raises RuntimeError
    # when called from a thread that never had an event loop (e.g. 'asyncio_0').
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(__import__("asyncio").run, do_search(query, max_results))
            result = future.result(timeout=30)
    except Exception as exc:
        return f"[web_search] Search failed: {exc}"

    return _truncate_to_context_budget(result)
