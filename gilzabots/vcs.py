"""Version Control System (VCS) strategies for GilzaBots."""

import asyncio
import os
import shlex
import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from typing import List, Callable, NamedTuple

from .subprocess_utils import utf8_text_kwargs


def _get_project_path() -> str:
    try:
        from .tools import get_project_path
        return get_project_path()
    except Exception:
        return os.getcwd()


def _preview(value: object, max_len: int = 60) -> str:
    text = str(value).replace("\n", " ")
    return text[:max_len] + "..." if len(text) > max_len else text


def _update_agent_progress(tool_name: str, args_preview: str = "") -> None:
    try:
        from .tools import AGENT_PROGRESS
        AGENT_PROGRESS.update(tool_name, args_preview)
    except Exception:
        pass

SHADOW_GITIGNORE_PATTERNS = [
    ".env",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    ".gilzabots/.shadowgit/",
    ".gilzabots/_editor_state.json",
    "gilzabots_partial_*",
    "*.aux",
    "*.bbl",
    "*.blg",
    "*.fdb_latexmk",
    "*.fls",
    "*.log",
    "*.out",
    "*.synctex.gz",
    "*.toc",
]

_SHADOW_GIT_LOCK = threading.RLock()

# ─── Base Strategy ────────────────────────────────────────────────────────────

class VersionControlStrategy(ABC):
    def __init__(self, project_path: str):
        self.project_path = os.path.abspath(project_path)

    @abstractmethod
    def setup(self):
        """Initialize VCS environment (e.g. create shadow git, gitignore)."""
        pass

    @abstractmethod
    def pre_run(self, context_msg: str):
        """Called before the agent starts executing its plan."""
        pass

    @abstractmethod
    def post_run(self, success: bool, msg: str = ""):
        """Called after the agent finishes its execution."""
        pass

    @abstractmethod
    def get_tools(self) -> List[Callable]:
        """Return a list of git tools available to the agent."""
        pass

    @abstractmethod
    def manual_commit(self, message: str) -> tuple[bool, str]:
        """Manually commit changes to the VCS."""
        pass

    @abstractmethod
    def undo_last(self) -> tuple[bool, str]:
        """Undo the last change in the VCS."""
        pass

    @abstractmethod
    def list_checkpoints(self) -> tuple[bool, str]:
        """List all checkpoints."""
        pass

    @abstractmethod
    def restore_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        """Restore the project to a specific checkpoint."""
        pass

    @abstractmethod
    def remove_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        """Remove a specific checkpoint."""
        pass


# ─── Shadow Git Helper ────────────────────────────────────────────────────────

def _run_shadow_git(command: str | list[str], project_path: str | None = None) -> subprocess.CompletedProcess:
    """Run a Git command using the internal shadow git directory."""
    if project_path is None:
        project_path = _get_project_path()
    shadow_dir = os.path.join(project_path, ".gilzabots", ".shadowgit")
    command_args = command if isinstance(command, list) else shlex.split(command)
    full_cmd = ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", *command_args]
    return subprocess.run(
        full_cmd,
        capture_output=True,
        **utf8_text_kwargs(),
        cwd=project_path
    )


def _ensure_shadow_gitignore(gitignore_path: str):
    existing = set()
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
            existing = {line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")}
    missing = [pattern for pattern in SHADOW_GITIGNORE_PATTERNS if pattern not in existing]
    if not missing:
        return

    os.makedirs(os.path.dirname(gitignore_path), exist_ok=True)
    has_content = os.path.exists(gitignore_path) and os.path.getsize(gitignore_path) > 0
    with open(gitignore_path, "a", encoding="utf-8") as f:
        if has_content:
            f.write("\n")
        for pattern in missing:
            f.write(pattern + "\n")


def _untrack_shadow_internal_files(project_path: str):
    _run_shadow_git(
        ["rm", "--cached", "-r", "--ignore-unmatch", ".gilzabots/.shadowgit", ".gilzabots/_editor_state.json"],
        project_path,
    )


def _init_shadow_git(project_path: str):
    """Initialize the shadow git repository if it doesn't exist."""
    shadow_base = os.path.join(project_path, ".gilzabots")
    shadow_dir = os.path.join(shadow_base, ".shadowgit")
    old_shadow_dir = os.path.join(shadow_base, ".git")
    gitignore_path = os.path.join(shadow_base, ".gitignore")
    
    os.makedirs(shadow_base, exist_ok=True)

    # Migrate existing projects from .git to .shadowgit
    if os.path.exists(old_shadow_dir) and not os.path.exists(shadow_dir):
        shutil.move(old_shadow_dir, shadow_dir)

    _ensure_shadow_gitignore(gitignore_path)

    if not os.path.exists(shadow_dir):
        # Init repo
        subprocess.run(
            ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "init"],
            capture_output=True,
            cwd=project_path,
        )

        subprocess.run(
            ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "core.excludesFile", gitignore_path],
            capture_output=True,
            cwd=project_path,
        )
        subprocess.run(
            ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "user.email", "gilzabots-agent@local"],
            capture_output=True,
            cwd=project_path,
        )
        subprocess.run(
            ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "user.name", "GilzaBots Agent"],
            capture_output=True,
            cwd=project_path,
        )
        
        # Initial commit
        _run_shadow_git("add .", project_path)
        _run_shadow_git(["commit", "-m", "Initial checkpoint (Auto)"], project_path)

    subprocess.run(
        ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "core.excludesFile", gitignore_path],
        capture_output=True,
        cwd=project_path,
    )
    subprocess.run(
        ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "user.email", "gilzabots-agent@local"],
        capture_output=True,
        cwd=project_path,
    )
    subprocess.run(
        ["git", f"--git-dir={shadow_dir}", f"--work-tree={project_path}", "config", "user.name", "GilzaBots Agent"],
        capture_output=True,
        cwd=project_path,
    )
    _untrack_shadow_internal_files(project_path)

def _auto_checkpoint(message: str, project_path: str | None = None):
    """Automatically create a checkpoint in the shadow git."""
    _run_shadow_git("add .", project_path)
    res = _run_shadow_git(["commit", "-m", message], project_path)
    return res.returncode == 0


def auto_checkpoint_if_changed(message: str, project_path: str | None = None) -> bool:
    """Create a shadow-git checkpoint only when the work tree has changes."""
    if project_path is None:
        project_path = _get_project_path()
    project_path = os.path.abspath(project_path)
    if not project_path or not os.path.isdir(project_path):
        return False

    with _SHADOW_GIT_LOCK:
        try:
            _init_shadow_git(project_path)
            status = _run_shadow_git("status --porcelain", project_path)
            if status.returncode != 0 or not status.stdout.strip():
                return False
            _run_shadow_git("add .", project_path)
            staged_status = _run_shadow_git("status --porcelain", project_path)
            if staged_status.returncode != 0 or not staged_status.stdout.strip():
                return False
            res = _run_shadow_git(["commit", "-m", message], project_path)
            return res.returncode == 0
        except Exception:
            return False


# ─── Nested Repository Detection ──────────────────────────────────────────────

# Directories that never hold reviewable project content; skipping them keeps the
# scan cheap on trees that carry a virtualenv or a node_modules folder.
NESTED_REPO_SCAN_SKIP_DIRS = {
    ".git",
    ".gilzabots",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
}

NESTED_REPO_SCAN_MAX_DEPTH = 4
NESTED_REPO_SCAN_LIMIT = 50


def find_nested_git_repos(
    project_path: str | None = None,
    max_depth: int = NESTED_REPO_SCAN_MAX_DEPTH,
    limit: int = NESTED_REPO_SCAN_LIMIT,
) -> list[str]:
    """List sub-directories of the project that carry their own Git repository.

    Git records such a directory in the shadow repository as a gitlink (mode
    160000), never as a tree of files: `git add .` stages a new commit id only
    when the nested repository's HEAD moves, and edits to the files inside it
    stage nothing at all. An agent turn that only touches those files therefore
    produces no net diff between its start and end checkpoints, both checkpoints
    are discarded by `finalize_agent_turn_checkpoint`, and the turn leaves no
    entry in the Review UI. Callers report these paths so that blind spot is
    stated instead of looking like an agent that changed nothing.

    Returns project-relative POSIX paths, sorted. Directories that are
    themselves nested repositories are not descended into: everything below them
    is already invisible for the same reason, and listing the outermost one is
    what the user has to act on.
    """
    if project_path is None:
        project_path = _get_project_path()
    project_path = os.path.abspath(project_path)
    if not project_path or not os.path.isdir(project_path):
        return []

    found: list[str] = []
    # Breadth-first so that, when the limit truncates the scan, what survives is
    # the shallow directories the user recognizes rather than a deep tail.
    queue: list[tuple[str, int]] = [(project_path, 0)]
    while queue and len(found) < limit:
        current, depth = queue.pop(0)
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if len(found) >= limit:
                break
            if entry.name in NESTED_REPO_SCAN_SKIP_DIRS:
                continue
            # Symlinks are not followed: they can point outside the project or
            # back into it, and neither is worth a cycle guard here.
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            # A `.git` entry may be a directory (a normal clone) or a file (a
            # worktree or a submodule checkout); both make the directory a
            # gitlink for the shadow repository.
            if os.path.exists(os.path.join(entry.path, ".git")):
                found.append(os.path.relpath(entry.path, project_path).replace(os.sep, "/"))
                continue
            if depth + 1 < max_depth:
                queue.append((entry.path, depth + 1))

    return sorted(found)


# ─── Agent Git Tools ──────────────────────────────────────────────────────────

def _remove_agent_turn_checkpoints(project_path: str, start_checkpoint: str) -> bool:
    parent = _run_shadow_git(["rev-parse", f"{start_checkpoint}^"], project_path)
    if parent.returncode == 0:
        reset_target = parent.stdout.strip()
        res = _run_shadow_git(["reset", "--mixed", reset_target], project_path)
        return res.returncode == 0

    head = _run_shadow_git("rev-parse HEAD", project_path)
    if head.returncode != 0:
        return False
    _run_shadow_git(["update-ref", "-d", "HEAD"], project_path)
    _run_shadow_git("reset", project_path)
    return True


def _agent_checkpoint_message(phase: str, agent_label: str | None = None) -> str:
    base = f"Agent turn {phase} checkpoint"
    if agent_label:
        return f"{base}: {agent_label}"
    return base


def begin_agent_turn_checkpoint(project_path: str | None = None, agent_label: str | None = None) -> str | None:
    """Create a deterministic start checkpoint for an agent turn."""
    if project_path is None:
        project_path = _get_project_path()
    project_path = os.path.abspath(project_path)
    if not project_path or not os.path.isdir(project_path):
        return None

    with _SHADOW_GIT_LOCK:
        try:
            _init_shadow_git(project_path)
            _run_shadow_git("add .", project_path)
            res = _run_shadow_git(
                ["commit", "--allow-empty", "-m", _agent_checkpoint_message("start", agent_label)],
                project_path,
            )
            if res.returncode != 0:
                return None
            head = _run_shadow_git("rev-parse HEAD", project_path)
            if head.returncode != 0:
                return None
            return head.stdout.strip()
        except Exception:
            return None


class AgentTurnFinalization(NamedTuple):
    """Outcome of closing an agent turn's checkpoints.

    ``changed_files`` lists the project-relative POSIX paths that exist at the
    end of the turn and differ from its start (deletions are left out: there is
    nothing left to point at). It is empty when the turn had no net diff or the
    checkpoint could not be saved.
    """

    saved: bool
    changed_files: tuple[str, ...] = ()


def _agent_turn_changed_files(project_path: str, start_checkpoint: str) -> tuple[str, ...]:
    diff = _run_shadow_git(
        ["diff", "--name-only", "-z", "--no-renames", "--diff-filter=d", start_checkpoint, "HEAD", "--"],
        project_path,
    )
    if diff.returncode != 0:
        return ()
    return tuple(sorted(name for name in diff.stdout.split("\0") if name))


def finalize_agent_turn_checkpoint_details(
    project_path: str | None = None,
    start_checkpoint: str | None = None,
    agent_label: str | None = None,
) -> AgentTurnFinalization:
    """Create an end checkpoint, discard the turn when it has no net diff, and
    report which files the turn changed."""
    if project_path is None:
        project_path = _get_project_path()
    project_path = os.path.abspath(project_path)
    if not project_path or not os.path.isdir(project_path):
        return AgentTurnFinalization(False)

    with _SHADOW_GIT_LOCK:
        try:
            _init_shadow_git(project_path)
            if not start_checkpoint:
                return AgentTurnFinalization(False)
            verify_start = _run_shadow_git(
                ["rev-parse", "--verify", f"{start_checkpoint}^{{commit}}"],
                project_path,
            )
            if verify_start.returncode != 0:
                return AgentTurnFinalization(False)
            _run_shadow_git("add .", project_path)
            res = _run_shadow_git(
                ["commit", "--allow-empty", "-m", _agent_checkpoint_message("end", agent_label)],
                project_path,
            )
            if res.returncode != 0:
                return AgentTurnFinalization(False)

            diff = _run_shadow_git(["diff", "--quiet", start_checkpoint, "HEAD", "--"], project_path)
            if diff.returncode == 0:
                return AgentTurnFinalization(_remove_agent_turn_checkpoints(project_path, start_checkpoint))
            if diff.returncode != 1:
                return AgentTurnFinalization(False)
            # Read under the same lock as the end commit, so a concurrent
            # checkpoint cannot move HEAD between the two.
            return AgentTurnFinalization(True, _agent_turn_changed_files(project_path, start_checkpoint))
        except Exception:
            return AgentTurnFinalization(False)


def finalize_agent_turn_checkpoint(
    project_path: str | None = None,
    start_checkpoint: str | None = None,
    agent_label: str | None = None,
) -> bool:
    """Create an end checkpoint and discard the turn checkpoints when no net diff exists."""
    return finalize_agent_turn_checkpoint_details(project_path, start_checkpoint, agent_label).saved


async def _await_checkpoint_operation(operation, *args):
    """Finish a shadow-Git operation even if the caller is cancelled.

    ``asyncio.to_thread`` does not stop its worker when the awaiting task is
    cancelled.  Losing the awaiter is particularly dangerous for a checkpoint:
    the Git operation can create a commit after the caller has already forgotten
    its id.  Keep the worker in a shielded task and absorb repeated cancellation
    requests until its result is known.  The caller re-propagates cancellation
    only after the checkpoint lifecycle is safe.
    """
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    was_cancelled = False
    while True:
        try:
            return await asyncio.shield(task), was_cancelled
        except asyncio.CancelledError:
            was_cancelled = True
            if task.done():
                return task.result(), was_cancelled


class AsyncAgentTurnCheckpoint:
    """Cancellation-safe owner of one agent turn's start/end checkpoints."""

    def __init__(self, project_path: str, agent_label: str | None = None):
        self.project_path = project_path
        self.agent_label = agent_label
        self.start_checkpoint: str | None = None
        self.finalize_result: bool | None = None
        # Files the turn changed, known once finalize() has run (see
        # AgentTurnFinalization). Empty for a turn with no net diff.
        self.changed_files: tuple[str, ...] = ()
        self._begin_finished = False
        self._finalize_finished = False

    async def begin(self) -> str | None:
        if self._begin_finished:
            return self.start_checkpoint

        result, was_cancelled = await _await_checkpoint_operation(
            begin_agent_turn_checkpoint,
            self.project_path,
            self.agent_label,
        )
        self.start_checkpoint = result
        self._begin_finished = True

        if was_cancelled:
            # ``begin`` may already have committed in its worker thread.  Close or
            # remove that checkpoint before allowing cancellation to escape.
            await self.finalize()
            raise asyncio.CancelledError

        return self.start_checkpoint

    async def finalize(self) -> bool:
        if self._finalize_finished:
            return bool(self.finalize_result)
        if not self._begin_finished or not self.start_checkpoint:
            self.finalize_result = False
            self._finalize_finished = True
            return False

        result, was_cancelled = await _await_checkpoint_operation(
            finalize_agent_turn_checkpoint_details,
            self.project_path,
            self.start_checkpoint,
            self.agent_label,
        )
        self.finalize_result = bool(result.saved)
        self.changed_files = tuple(result.changed_files)
        self._finalize_finished = True

        if was_cancelled:
            raise asyncio.CancelledError

        return bool(self.finalize_result)


def git_status() -> str:
    _update_agent_progress("git_status")
    res = _run_shadow_git("status -s")
    return res.stdout if res.stdout.strip() else "Working tree clean."

def git_diff() -> str:
    _update_agent_progress("git_diff")
    res = _run_shadow_git("diff")
    return res.stdout if res.stdout.strip() else "No changes."

def git_commit(message: str) -> str:
    _update_agent_progress("git_commit", _preview(message))
    _run_shadow_git("add .")
    res = _run_shadow_git(["commit", "-m", message])
    if res.returncode == 0:
        return f"Successfully committed: {message}"
    return f"Failed to commit or nothing to commit. Output: {res.stderr or res.stdout}"


def _agent_git_tools() -> List[Callable]:
    from agenticblocks.core.function_block import as_tool
    return [
        as_tool(
            name="git_status",
            description="Get the status of the internal version control. Shows modified/added files.",
        )(git_status),
        as_tool(
            name="git_diff",
            description="Get the diff of the internal version control to see exact code changes.",
        )(git_diff),
        as_tool(
            name="git_commit",
            description="Commit all current changes to the internal version control. Use this to save milestones.",
        )(git_commit),
    ]


# ─── Concrete Strategies ──────────────────────────────────────────────────────

class AutoGitStrategy(VersionControlStrategy):
    """Deterministic mode: Shadow Git initialized, pre/post checkpoints enforced, NO tools given to agent."""

    def setup(self):
        _init_shadow_git(self.project_path)

    def pre_run(self, context_msg: str):
        _auto_checkpoint("Pre-run checkpoint: Before executing plan", self.project_path)

    def post_run(self, success: bool, msg: str = ""):
        status = "Success" if success else "Failed"
        _auto_checkpoint(f"Post-run checkpoint: {status}. {msg}", self.project_path)

    def get_tools(self) -> List[Callable]:
        return []

    def manual_commit(self, message: str) -> tuple[bool, str]:
        _run_shadow_git("add .", self.project_path)
        res = _run_shadow_git(["commit", "-m", message], self.project_path)
        if res.returncode == 0:
            return True, "Committed."
        err_out = res.stderr or res.stdout
        if "nothing to commit" in err_out.lower():
            from .i18n import _
            return False, _("commit_nothing")
        return False, err_out

    def undo_last(self) -> tuple[bool, str]:
        res = _run_shadow_git("rev-parse HEAD~1", self.project_path)
        if res.returncode != 0:
            return False, "Cannot undo. No previous checkpoints."
        _run_shadow_git("reset --hard HEAD~1", self.project_path)
        _run_shadow_git("clean -fd", self.project_path)
        return True, "Last change undone."

    def list_checkpoints(self) -> tuple[bool, str]:
        res = _run_shadow_git("log --oneline", self.project_path)
        if res.returncode == 0:
            return True, res.stdout
        return False, res.stderr or res.stdout

    def restore_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"reset --hard {checkpoint_id}", self.project_path)
        if res.returncode == 0:
            _run_shadow_git("clean -fd", self.project_path)
            return True, f"Restored to checkpoint {checkpoint_id}."
        return False, f"Failed to restore checkpoint {checkpoint_id}: {res.stderr}"

    def remove_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"rebase --onto {checkpoint_id}^ {checkpoint_id} HEAD", self.project_path)
        if res.returncode == 0:
            return True, f"Checkpoint {checkpoint_id} removed successfully."
        _run_shadow_git("rebase --abort", self.project_path)
        return False, f"Failed to remove checkpoint {checkpoint_id} (likely conflicts). Stderr: {res.stderr}"


class HybridGitStrategy(VersionControlStrategy):
    """Hybrid mode: Shadow Git initialized, agent has tools, orchestrator ensures safety checkpoints."""

    def setup(self):
        _init_shadow_git(self.project_path)

    def pre_run(self, context_msg: str):
        _auto_checkpoint("Pre-run checkpoint: Before executing plan", self.project_path)

    def post_run(self, success: bool, msg: str = ""):
        status = "Success" if success else "Failed"
        _auto_checkpoint(f"Post-run checkpoint: {status}. {msg}", self.project_path)

    def get_tools(self) -> List[Callable]:
        return _agent_git_tools()

    def manual_commit(self, message: str) -> tuple[bool, str]:
        _run_shadow_git("add .", self.project_path)
        res = _run_shadow_git(["commit", "-m", message], self.project_path)
        if res.returncode == 0:
            return True, "Committed."
        err_out = res.stderr or res.stdout
        if "nothing to commit" in err_out.lower():
            from .i18n import _
            return False, _("commit_nothing")
        return False, err_out

    def undo_last(self) -> tuple[bool, str]:
        res = _run_shadow_git("rev-parse HEAD~1", self.project_path)
        if res.returncode != 0:
            return False, "Cannot undo. No previous checkpoints."
        _run_shadow_git("reset --hard HEAD~1", self.project_path)
        _run_shadow_git("clean -fd", self.project_path)
        return True, "Last change undone."

    def list_checkpoints(self) -> tuple[bool, str]:
        res = _run_shadow_git("log --oneline", self.project_path)
        if res.returncode == 0:
            return True, res.stdout
        return False, res.stderr or res.stdout

    def restore_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"reset --hard {checkpoint_id}", self.project_path)
        if res.returncode == 0:
            _run_shadow_git("clean -fd", self.project_path)
            return True, f"Restored to checkpoint {checkpoint_id}."
        return False, f"Failed to restore checkpoint {checkpoint_id}: {res.stderr}"

    def remove_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"rebase --onto {checkpoint_id}^ {checkpoint_id} HEAD", self.project_path)
        if res.returncode == 0:
            return True, f"Checkpoint {checkpoint_id} removed successfully."
        _run_shadow_git("rebase --abort", self.project_path)
        return False, f"Failed to remove checkpoint {checkpoint_id} (likely conflicts). Stderr: {res.stderr}"


class AgentDrivenGitStrategy(VersionControlStrategy):
    """Agent-driven mode: Orchestrator does nothing automatically. Agent gets tools and decides when to use them."""

    def setup(self):
        _init_shadow_git(self.project_path)

    def pre_run(self, context_msg: str):
        pass

    def post_run(self, success: bool, msg: str = ""):
        pass

    def get_tools(self) -> List[Callable]:
        return _agent_git_tools()

    def manual_commit(self, message: str) -> tuple[bool, str]:
        _run_shadow_git("add .", self.project_path)
        res = _run_shadow_git(["commit", "-m", message], self.project_path)
        if res.returncode == 0:
            return True, "Committed."
        err_out = res.stderr or res.stdout
        if "nothing to commit" in err_out.lower():
            from .i18n import _
            return False, _("commit_nothing")
        return False, err_out

    def undo_last(self) -> tuple[bool, str]:
        res = _run_shadow_git("rev-parse HEAD~1", self.project_path)
        if res.returncode != 0:
            return False, "Cannot undo. No previous checkpoints."
        _run_shadow_git("reset --hard HEAD~1", self.project_path)
        _run_shadow_git("clean -fd", self.project_path)
        return True, "Last change undone."

    def list_checkpoints(self) -> tuple[bool, str]:
        res = _run_shadow_git("log --oneline", self.project_path)
        if res.returncode == 0:
            return True, res.stdout
        return False, res.stderr or res.stdout

    def restore_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"reset --hard {checkpoint_id}", self.project_path)
        if res.returncode == 0:
            _run_shadow_git("clean -fd", self.project_path)
            return True, f"Restored to checkpoint {checkpoint_id}."
        return False, f"Failed to restore checkpoint {checkpoint_id}: {res.stderr}"

    def remove_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        res = _run_shadow_git(f"rebase --onto {checkpoint_id}^ {checkpoint_id} HEAD", self.project_path)
        if res.returncode == 0:
            return True, f"Checkpoint {checkpoint_id} removed successfully."
        _run_shadow_git("rebase --abort", self.project_path)
        return False, f"Failed to remove checkpoint {checkpoint_id} (likely conflicts). Stderr: {res.stderr}"


class NoGitStrategy(VersionControlStrategy):
    """Null strategy: No version control at all."""
    
    def setup(self):
        pass

    def pre_run(self, context_msg: str):
        pass
        
    def post_run(self, success: bool, msg: str = ""):
        pass

    def get_tools(self) -> List[Callable]:
        return []

    def manual_commit(self, message: str) -> tuple[bool, str]:
        return False, "VCS is disabled."

    def undo_last(self) -> tuple[bool, str]:
        return False, "VCS is disabled."

    def list_checkpoints(self) -> tuple[bool, str]:
        return False, "VCS is disabled."

    def restore_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        return False, "VCS is disabled."

    def remove_checkpoint(self, checkpoint_id: str) -> tuple[bool, str]:
        return False, "VCS is disabled."


def get_vcs_strategy(strategy_name: str, project_path: str) -> VersionControlStrategy:
    """Factory method to get the selected strategy."""
    strategies = {
        "auto": AutoGitStrategy,
        "hybrid": HybridGitStrategy,
        "agent_driven": AgentDrivenGitStrategy,
        "none": NoGitStrategy
    }
    klass = strategies.get(strategy_name.lower(), HybridGitStrategy)
    return klass(project_path)
