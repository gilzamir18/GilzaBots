"""Read-only diagnostic operations. No project code is imported or executed."""
from __future__ import annotations

import ast
import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tokenize


def inspect_python(path: str) -> dict:
    """Parse Python source as data, including files that would fail on import."""
    if os.path.getsize(path) > 2 * 1024 * 1024:
        raise ValueError("Python source exceeds the 2 MiB analysis limit. Read selected ranges instead.")
    with tokenize.open(path) as handle:
        source = handle.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return {"valid_syntax": False, "line": error.lineno, "column": error.offset, "error": error.msg}
    symbols, imports = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append({"name": node.name, "kind": type(node).__name__, "line": node.lineno,
                            "end_line": node.end_lineno})
        elif isinstance(node, ast.Import):
            imports.extend({"module": item.name, "line": node.lineno} for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append({"module": "." * node.level + (node.module or ""), "line": node.lineno})
    return {"valid_syntax": True, "symbols": sorted(symbols, key=lambda item: item["line"]), "imports": imports}


def inspect_environment() -> dict:
    """Read distribution metadata and executable locations without launching them."""
    packages = {}
    for name in ("gilzabots", "pydantic", "litellm", "pytest", "pymupdf", "python-docx", "openpyxl"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"os": platform.system(), "python": platform.python_version(), "packages": packages,
            "executables": {name: shutil.which(name) for name in ("git", "tectonic", "node", "npm")}}


def inspect_git(root: str, operation: str, path: str = "", offset: int = 0, limit: int = 10000) -> dict:
    """Fixed Git queries with every project-configured executable disabled."""
    if operation not in {"status", "diff", "staged_diff", "log"}:
        raise ValueError("operation must be status, diff, staged_diff, or log.")
    if offset < 0 or not 1 <= limit <= 50000:
        raise ValueError("offset must be non-negative and limit must be between 1 and 50000.")
    command = ["git", "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "color.ui=false", "-C", root]
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1")
    # Even status/diff can run a clean or long-running process filter while
    # comparing working-tree bytes. Read their names as configuration data and
    # override each driver before touching the index or working tree.
    config = subprocess.run(
        [*command, "config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        env=env, stdin=subprocess.DEVNULL,
    )
    if config.returncode not in (0, 1):
        raise ValueError(config.stderr.strip()[:2000] or "Could not inspect Git filter configuration.")
    drivers = {entry.split("\n", 1)[0][7:].rsplit(".", 1)[0]
               for entry in config.stdout.split("\0") if entry}
    for driver in sorted(drivers):
        for option in ("clean", "smudge", "process"):
            command += ["-c", f"filter.{driver}.{option}="]
        command += ["-c", f"filter.{driver}.required=false"]
    if operation == "status":
        command += ["status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"]
    elif operation in {"diff", "staged_diff"}:
        command += ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all"]
        if operation == "staged_diff":
            command += ["--cached"]
    else:
        command += ["log", "-50", "--format=%h %ad %s", "--date=iso-strict"]
    command += ["--"]
    if path:
        target = Path(root, path).resolve()
        try:
            relative = target.relative_to(Path(root).resolve())
        except ValueError:
            raise ValueError("Git inspection path must be inside the project.") from None
        command.append(str(relative))
    # Ignore environment-based Git command/config injection and repository
    # redirection. No caller-controlled argv options are accepted.
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=15, env=env, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError(result.stderr.strip()[:2000] or "Git inspection failed.")
    end = min(len(result.stdout), offset + limit)
    return {"content": result.stdout[offset:end], "total_chars": len(result.stdout),
            "next_offset": end if end < len(result.stdout) else None,
            "filters_disabled": True, "submodules_inspected": False}
