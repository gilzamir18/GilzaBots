"""User hooks: commands GilzaBots runs at fixed moments, configured in ``hooks.json``.

Modelled on Claude Code's hooks. The configuration lives in
``<gilzabots home>/hooks.json`` and only there: a hook is a command run on this
machine, so it is the user's own setting, never something a project folder (or a
project mirrored from another device) can bring along.

    {
      "hooks": {
        "PreCompact": [
          {"matcher": "auto|manual",
           "hooks": [{"type": "command", "command": "python backup.py", "timeout": 60}]}
        ],
        "PostCompact": [ ... ]
      }
    }

``matcher`` is a regular expression matched in full against the event's subject
(for the compaction events, the trigger: ``manual`` or ``auto``); empty or ``*``
matches everything. Each command runs through the system shell, in the project
folder, with the event as a JSON object on its standard input and
``GILZABOTS_HOOK_EVENT`` / ``GILZABOTS_PROJECT_DIR`` in its environment.

Exit codes, as in Claude Code:

- ``0``: success; what the command printed is used by the event (see each event).
- ``2``: a blocking error; for an event that can be blocked, it is stopped and
  the command's standard error is the reason. Elsewhere it is reported like any
  other failure.
- anything else, a timeout, or a command that cannot start: a non-blocking
  error. It is reported to the user and the event goes on.

Output longer than ``MAX_HOOK_OUTPUT_CHARS`` is not used and is reported, rather
than cut to fit.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .subprocess_utils import utf8_text_kwargs

HOOKS_FILE_NAME = "hooks.json"

HOOK_EVENTS = {
    # Before a compaction's summary is requested. Output: extra instructions for
    # the summary. Exit code 2 blocks the compaction.
    "PreCompact": {"blockable": True},
    # After the summary exists, before it replaces the context. Output: extra
    # context placed after the summary in what the agent continues from.
    "PostCompact": {"blockable": False},
}

DEFAULT_HOOK_TIMEOUT = 60
MAX_HOOK_TIMEOUT = 600
MAX_HOOK_OUTPUT_CHARS = 20_000
BLOCKING_EXIT_CODE = 2


class HookConfigError(ValueError):
    """``hooks.json`` cannot be used as written."""


@dataclass
class HookCommand:
    command: str
    timeout: int = DEFAULT_HOOK_TIMEOUT


@dataclass
class HookMatcher:
    matcher: str
    hooks: list[HookCommand] = field(default_factory=list)

    def matches(self, subject: str) -> bool:
        pattern = (self.matcher or "").strip()
        if pattern in ("", "*"):
            return True
        return re.fullmatch(pattern, subject or "") is not None


@dataclass
class HookResult:
    """What one command did, as reported to the user."""

    event: str
    command: str
    outcome: str  # "ok", "blocked", "error", "timeout"
    exit_code: int | None = None
    output: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "command": self.command,
            "outcome": self.outcome,
            "exit_code": self.exit_code,
            "message": self.message,
        }


def hooks_file_path() -> str:
    from .config import get_gilzabots_home

    return os.path.join(get_gilzabots_home(), HOOKS_FILE_NAME)


def _parse_command(event: str, index: int, raw: Any) -> HookCommand:
    where = f"hooks.{event}[{index}]"
    if not isinstance(raw, dict):
        raise HookConfigError(f"{where}: each hook must be an object")
    kind = raw.get("type", "command")
    if kind != "command":
        raise HookConfigError(f"{where}: unsupported hook type {kind!r}; only 'command' is supported")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HookConfigError(f"{where}: 'command' must be a non-empty string")
    timeout = raw.get("timeout", DEFAULT_HOOK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (0 < timeout <= MAX_HOOK_TIMEOUT):
        raise HookConfigError(f"{where}: 'timeout' must be a number of seconds between 1 and {MAX_HOOK_TIMEOUT}")
    return HookCommand(command=command, timeout=int(timeout))


def parse_hooks_config(data: Any) -> dict[str, list[HookMatcher]]:
    """Validate a decoded ``hooks.json``; raise :class:`HookConfigError` with the place."""
    if not isinstance(data, dict):
        raise HookConfigError("the file must hold a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigError("'hooks' must be an object keyed by event name")
    parsed: dict[str, list[HookMatcher]] = {}
    for event, matchers in hooks.items():
        if event not in HOOK_EVENTS:
            known = ", ".join(sorted(HOOK_EVENTS))
            raise HookConfigError(f"unknown hook event {event!r}; known events: {known}")
        if not isinstance(matchers, list):
            raise HookConfigError(f"hooks.{event} must be a list")
        entries: list[HookMatcher] = []
        for index, entry in enumerate(matchers):
            where = f"hooks.{event}[{index}]"
            if not isinstance(entry, dict):
                raise HookConfigError(f"{where} must be an object")
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                raise HookConfigError(f"{where}.matcher must be a string")
            if matcher.strip() not in ("", "*"):
                try:
                    re.compile(matcher)
                except re.error as exc:
                    raise HookConfigError(f"{where}.matcher is not a valid regular expression: {exc}") from exc
            commands = entry.get("hooks")
            if not isinstance(commands, list) or not commands:
                raise HookConfigError(f"{where}.hooks must be a non-empty list")
            entries.append(HookMatcher(
                matcher=matcher,
                hooks=[_parse_command(event, i, c) for i, c in enumerate(commands)],
            ))
        parsed[event] = entries
    return parsed


def load_hooks(path: str | None = None) -> dict[str, list[HookMatcher]]:
    """Read the configured hooks. No file means no hooks; a broken file is an error."""
    path = path or hooks_file_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise HookConfigError(f"{path} cannot be read: {exc}") from exc
    try:
        return parse_hooks_config(data)
    except HookConfigError as exc:
        raise HookConfigError(f"{path} is invalid: {exc}") from exc


def matching_commands(config: dict[str, list[HookMatcher]], event: str, subject: str) -> list[HookCommand]:
    commands: list[HookCommand] = []
    for entry in config.get(event, []):
        if entry.matches(subject):
            commands.extend(entry.hooks)
    return commands


def _run_command_blocking(command: HookCommand, stdin_text: str, cwd: str, env: dict) -> tuple[int | None, str, str, bool]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # No console window flashes up from the desktop app.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            command.command,
            shell=True,
            input=stdin_text,
            capture_output=True,
            timeout=command.timeout,
            cwd=cwd,
            env=env,
            **utf8_text_kwargs(),
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return None, out, err, True
    return done.returncode, done.stdout or "", done.stderr or "", False


async def run_hooks(
    event: str,
    subject: str,
    payload: dict,
    *,
    cwd: str = "",
    config: dict[str, list[HookMatcher]] | None = None,
) -> list[HookResult]:
    """Run every command configured for *event* whose matcher accepts *subject*.

    Commands run one after the other, in configuration order, off the event
    loop. Raises :class:`HookConfigError` when ``hooks.json`` is broken: running
    some of the user's hooks and silently skipping the rest would be worse.
    """
    if event not in HOOK_EVENTS:
        raise ValueError(f"Unknown hook event: {event}")
    config = load_hooks() if config is None else config
    commands = matching_commands(config, event, subject)
    if not commands:
        return []

    workdir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    stdin_text = json.dumps({"hook_event_name": event, **payload}, ensure_ascii=False, default=str)
    env = dict(os.environ)
    env["GILZABOTS_HOOK_EVENT"] = event
    env["GILZABOTS_PROJECT_DIR"] = workdir
    blockable = HOOK_EVENTS[event]["blockable"]

    results: list[HookResult] = []
    for command in commands:
        try:
            code, out, err, timed_out = await asyncio.to_thread(
                _run_command_blocking, command, stdin_text, workdir, env,
            )
        except OSError as exc:
            results.append(HookResult(event, command.command, "error", None, "", f"could not start: {exc}"))
            continue
        err = err.strip()
        if timed_out:
            results.append(HookResult(
                event, command.command, "timeout", None, "",
                f"did not finish within {command.timeout} s and was stopped",
            ))
            continue
        if code == 0:
            output = out.strip()
            if len(output) > MAX_HOOK_OUTPUT_CHARS:
                results.append(HookResult(
                    event, command.command, "error", code, "",
                    f"printed {len(output)} characters; output over {MAX_HOOK_OUTPUT_CHARS} is not used",
                ))
                continue
            results.append(HookResult(event, command.command, "ok", code, output, ""))
            continue
        if code == BLOCKING_EXIT_CODE and blockable:
            results.append(HookResult(
                event, command.command, "blocked", code, "",
                err or "the hook exited with code 2 without giving a reason",
            ))
            # Nothing after a block runs: the event is not going to happen.
            break
        results.append(HookResult(
            event, command.command, "error", code, "",
            err or f"exited with code {code}",
        ))
    return results


def describe_hooks(config: dict[str, list[HookMatcher]]) -> list[str]:
    """One line per configured command, for ``/hooks``."""
    lines: list[str] = []
    for event in sorted(config):
        for entry in config[event]:
            matcher = entry.matcher.strip() or "*"
            for command in entry.hooks:
                lines.append(f"{event} [{matcher}] {command.command} (timeout {command.timeout} s)")
    return lines
