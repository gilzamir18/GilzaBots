"""Running a monitor's check: one observation of the thing it watches.

A check either returns a value or raises :class:`CheckError` with a diagnostic
that is shown to the user and counted toward the monitor's error budget. A
check never decides whether its monitor fires; that is ``spec.evaluate``.

Command checks follow the project's Docker sandbox setting exactly as the
agent's ``run_command`` does: when the sandbox is enabled the command runs in
the project's container, and a container that cannot start fails the check --
it never falls back to the host.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Awaitable, Callable

from .spec import extract_json_path

MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 4000
MAX_HASHED_FILE_BYTES = 64 * 1024 * 1024


class CheckError(RuntimeError):
    """A check that could not observe anything."""


def _user_agent() -> str:
    try:
        from gilzabots import __version__
    except Exception:  # pragma: no cover
        __version__ = "0"
    return f"GilzaBots-Monitor/{__version__}"


def _tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


# ── HTTP ────────────────────────────────────────────────────────────────────

def _http_fetch(check: dict) -> tuple[int, str, str]:
    request = urllib.request.Request(check["url"], method=check.get("method", "GET"))
    request.add_header("User-Agent", _user_agent())
    request.add_header("Accept", "application/json, text/plain;q=0.9, */*;q=0.5")
    for name, value in (check.get("headers") or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=check.get("timeout_seconds", 30)) as response:
            status = int(getattr(response, "status", 200))
            content_type = response.headers.get("Content-Type", "")
            body = response.read(MAX_HTTP_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise CheckError(f"{check['url']} answered HTTP {exc.code} {exc.reason}.") from None
    except urllib.error.URLError as exc:
        raise CheckError(f"Could not reach {check['url']}: {exc.reason}.") from None
    except TimeoutError:
        raise CheckError(f"{check['url']} did not answer within {check.get('timeout_seconds', 30)} s.") from None
    except OSError as exc:
        raise CheckError(f"Could not reach {check['url']}: {exc}.") from None
    if len(body) > MAX_HTTP_BYTES:
        raise CheckError(f"{check['url']} returned more than {MAX_HTTP_BYTES // 1024} KB; watch a smaller resource.")
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type)
    if match:
        charset = match.group(1)
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return status, content_type, text


def http_value(check: dict, status: int, text: str) -> Any:
    """Extract the observed value from a response body."""
    if check.get("method") == "HEAD":
        return status
    if check.get("json_path"):
        try:
            document = json.loads(text)
        except ValueError:
            raise CheckError(
                f"{check['url']} did not return JSON, so 'json_path' cannot be applied "
                f"(body starts with {text[:80]!r})."
            ) from None
        try:
            return extract_json_path(document, check["json_path"])
        except LookupError as exc:
            raise CheckError(f"'json_path' {check['json_path']!r} did not match: {exc}") from None
    if check.get("regex"):
        match = re.search(check["regex"], text)
        if not match:
            raise CheckError(f"'regex' {check['regex']!r} matched nothing in the response.")
        return match.group(1) if match.groups() else match.group(0)
    return _tail(text)


async def run_http(check: dict) -> Any:
    status, _content_type, text = await asyncio.to_thread(_http_fetch, check)
    return http_value(check, status, text)


# ── Command ─────────────────────────────────────────────────────────────────

def _command_cwd(check: dict, project_path: str) -> str:
    cwd = check.get("cwd") or ""
    if cwd:
        cwd = os.path.expanduser(cwd)
        if not os.path.isabs(cwd):
            cwd = os.path.join(project_path, cwd)
    else:
        cwd = project_path
    if not cwd or not os.path.isdir(cwd):
        raise CheckError(f"The command's folder does not exist: {cwd or '(none)'}.")
    return cwd


def _run_command(check: dict, project_path: str) -> dict:
    timeout = check.get("timeout_seconds", 30)
    from gilzabots import sandbox_config

    if sandbox_config.is_enabled():
        from gilzabots.docker_sandbox import run_in_sandbox

        if check.get("cwd"):
            raise CheckError(
                "The Docker sandbox is enabled, where a command always runs in the project "
                "folder (/workspace); remove 'cwd' or use 'cd <dir> && ...'."
            )
        try:
            result = run_in_sandbox(check["command"], project_path, timeout)
        except ValueError as exc:
            raise CheckError(f"The sandbox container could not start: {exc}") from None
        if result.get("error") == "TimeoutExpired":
            raise CheckError(f"The command did not finish within {timeout} s.")
        output = (result.get("stdout") or "") + (result.get("stderr") or "")
        return {"exit_code": int(result.get("exit_code", -1)), "output": _tail(output)}

    cwd = _command_cwd(check, project_path)
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            check["command"], shell=True, cwd=cwd, timeout=timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise CheckError(f"The command did not finish within {timeout} s.") from None
    except OSError as exc:
        raise CheckError(f"The command could not start: {exc}.") from None
    output = completed.stdout.decode("utf-8", errors="replace")
    return {"exit_code": int(completed.returncode), "output": _tail(output)}


async def run_command(check: dict, project_path: str) -> dict:
    return await asyncio.to_thread(_run_command, check, project_path)


# ── File ────────────────────────────────────────────────────────────────────

def resolve_file(check: dict, project_path: str) -> str:
    path = os.path.expanduser(check["path"])
    if not os.path.isabs(path):
        if not project_path:
            raise CheckError(f"'{check['path']}' is relative and the monitor has no project folder.")
        path = os.path.join(project_path, path)
    return os.path.abspath(path)


def _observe_file(check: dict, project_path: str) -> dict:
    path = resolve_file(check, project_path)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return {"exists": False, "mtime": None, "sha256": None, "path": path}
    except OSError as exc:
        raise CheckError(f"Could not read {path}: {exc}.") from None
    observed = {"exists": True, "mtime": stat.st_mtime, "sha256": None, "path": path}
    if check.get("watch") == "content" and os.path.isfile(path):
        if stat.st_size > MAX_HASHED_FILE_BYTES:
            raise CheckError(
                f"{path} is larger than {MAX_HASHED_FILE_BYTES // (1024 * 1024)} MB; watch its modification time instead."
            )
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise CheckError(f"Could not read {path}: {exc}.") from None
        observed["sha256"] = digest.hexdigest()
    return observed


async def run_file(check: dict, project_path: str) -> dict:
    return await asyncio.to_thread(_observe_file, check, project_path)


# ── Dispatch ────────────────────────────────────────────────────────────────

LlmRunner = Callable[[dict], Awaitable[dict]]


async def run_check(monitor: dict, *, llm_runner: LlmRunner | None = None) -> Any:
    """Observe *monitor*'s subject once. Raises CheckError."""
    check = monitor["check"]
    kind = check["type"]
    project_path = monitor.get("project_path") or ""
    if kind == "timer":
        return None
    if kind == "http":
        return await run_http(check)
    if kind == "command":
        return await run_command(check, project_path)
    if kind == "file":
        return await run_file(check, project_path)
    if kind == "llm":
        if llm_runner is None:
            raise CheckError("No model runner is available for an 'llm' check in this process.")
        return await llm_runner(monitor)
    raise CheckError(f"Unknown check type '{kind}'.")
