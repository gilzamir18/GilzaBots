"""The monitor contract: validation, condition evaluation and message templates.

Everything here is deterministic and free of I/O, so the scheduler can decide
whether a monitor fires without calling a model (the "level 1" evaluation of
the design). Only an ``llm`` check asks a model, and it is validated here like
any other.

A specification that does not make sense is refused with a diagnostic naming
the field and what would be accepted. Nothing is guessed or substituted: a
monitor that silently watched something other than what was asked for would be
worse than one that was never registered.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


CHECK_TYPES = ("timer", "http", "command", "file", "llm")
CONDITION_TYPES = (
    "always", "changed", "equals", "not_equals", "contains", "regex",
    "gt", "ge", "lt", "le", "delta", "exit_code", "exists", "missing", "llm",
)
ACTION_TYPES = ("notify", "resume_chat", "notify_and_resume")
RESUMING_ACTIONS = ("resume_chat", "notify_and_resume")
#: Which chat a wake-up runs in: the one the monitor was registered from, with
#: its conversation and the orchestrator's saved state, or a chat created for
#: each wake-up, which leaves that conversation untouched.
WAKE_CHAT_ORIGIN = "origin"
WAKE_CHAT_NEW = "new"
WAKE_CHATS = (WAKE_CHAT_ORIGIN, WAKE_CHAT_NEW)
NOTIFYING_ACTIONS = ("notify", "notify_and_resume")

#: Which conditions mean something for each check. A timer has no value, a
#: file has no number, and an ``llm`` check answers its own question.
_CONDITIONS_FOR_CHECK = {
    "timer": ("always",),
    "http": ("always", "changed", "equals", "not_equals", "contains", "regex",
             "gt", "ge", "lt", "le", "delta"),
    "command": ("always", "changed", "equals", "not_equals", "contains", "regex",
                "gt", "ge", "lt", "le", "delta", "exit_code"),
    "file": ("always", "changed", "exists", "missing"),
    "llm": ("llm",),
}

MIN_INTERVAL_SECONDS = 10
#: A model call per check costs tokens on every tick; a watcher that asks a
#: model every ten seconds would spend a user's budget unseen.
MIN_LLM_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 30 * 24 * 3600
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30
MAX_NAME_CHARS = 120
MAX_TEXT_CHARS = 4000
FILE_WATCH_MODES = ("mtime", "content")
HTTP_METHODS = ("GET", "HEAD")


class MonitorSpecError(ValueError):
    """A monitor specification that cannot be registered as given."""


def _text(raw: dict, key: str, *, required: bool = False, limit: int = MAX_TEXT_CHARS) -> str:
    value = raw.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MonitorSpecError(f"'{key}' must be text, got {type(value).__name__}.")
    value = value.strip()
    if required and not value:
        raise MonitorSpecError(f"'{key}' is required.")
    if len(value) > limit:
        raise MonitorSpecError(f"'{key}' is too long ({len(value)} characters, limit {limit}).")
    return value


def _optional_int(raw: dict, key: str, *, minimum: int, maximum: int | None = None) -> int | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise MonitorSpecError(f"'{key}' must be a whole number, got a boolean.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise MonitorSpecError(f"'{key}' must be a whole number, got {value!r}.") from None
    if isinstance(value, float) and value != number:
        raise MonitorSpecError(f"'{key}' must be a whole number, got {value!r}.")
    if number < minimum:
        raise MonitorSpecError(f"'{key}' must be at least {minimum}, got {number}.")
    if maximum is not None and number > maximum:
        raise MonitorSpecError(f"'{key}' must be at most {maximum}, got {number}.")
    return number


def _bool(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise MonitorSpecError(f"'{key}' must be true or false, got {value!r}.")


def _mapping(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MonitorSpecError(f"'{key}' must be an object, got {type(value).__name__}.")
    return value


def normalize_check(raw: dict) -> dict:
    kind = _text(raw, "type", required=True)
    if kind not in CHECK_TYPES:
        raise MonitorSpecError(
            f"Unknown check type '{kind}'. Use one of: {', '.join(CHECK_TYPES)}."
        )
    check: dict[str, Any] = {"type": kind}
    if kind == "http":
        url = _text(raw, "url", required=True, limit=2048)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise MonitorSpecError(f"'url' must start with http:// or https://, got '{url}'.")
        check["url"] = url
        method = (_text(raw, "method") or "GET").upper()
        if method not in HTTP_METHODS:
            raise MonitorSpecError(f"'method' must be one of {', '.join(HTTP_METHODS)}, got '{method}'.")
        check["method"] = method
        json_path = _text(raw, "json_path", limit=500)
        regex = _text(raw, "regex", limit=500)
        if json_path and regex:
            raise MonitorSpecError("Give either 'json_path' or 'regex' to extract the value, not both.")
        if json_path:
            parse_json_path(json_path)
            check["json_path"] = json_path
        if regex:
            _compile(regex, "regex")
            check["regex"] = regex
        headers = _mapping(raw, "headers")
        clean_headers = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise MonitorSpecError("'headers' must map header names to text values.")
            clean_headers[name] = value
        if clean_headers:
            check["headers"] = clean_headers
    elif kind == "command":
        check["command"] = _text(raw, "command", required=True)
        cwd = _text(raw, "cwd", limit=1024)
        if cwd:
            check["cwd"] = cwd
    elif kind == "file":
        check["path"] = _text(raw, "path", required=True, limit=1024)
        watch = _text(raw, "watch") or "mtime"
        if watch not in FILE_WATCH_MODES:
            raise MonitorSpecError(
                f"'watch' must be one of {', '.join(FILE_WATCH_MODES)}, got '{watch}'."
            )
        check["watch"] = watch
    elif kind == "llm":
        check["question"] = _text(raw, "question", required=True)
        check["use_web"] = _bool(raw, "use_web", True)
    if kind in ("http", "command", "llm"):
        timeout = _optional_int(raw, "timeout_seconds", minimum=1, maximum=MAX_TIMEOUT_SECONDS)
        check["timeout_seconds"] = timeout or (120 if kind == "llm" else DEFAULT_TIMEOUT_SECONDS)
    return check


def normalize_condition(raw: dict, check_type: str) -> dict:
    kind = _text(raw, "type") or ("always" if check_type == "timer" else "llm" if check_type == "llm" else "")
    if not kind:
        raise MonitorSpecError(
            "'condition.type' is required for a "
            f"'{check_type}' check. Use one of: {', '.join(_CONDITIONS_FOR_CHECK[check_type])}."
        )
    if kind not in CONDITION_TYPES:
        raise MonitorSpecError(
            f"Unknown condition type '{kind}'. Use one of: {', '.join(CONDITION_TYPES)}."
        )
    allowed = _CONDITIONS_FOR_CHECK[check_type]
    if kind not in allowed:
        raise MonitorSpecError(
            f"Condition '{kind}' does not apply to a '{check_type}' check. "
            f"Use one of: {', '.join(allowed)}."
        )
    condition: dict[str, Any] = {"type": kind}
    if kind in ("equals", "not_equals", "contains"):
        value = raw.get("value")
        if value is None or (isinstance(value, str) and not value):
            raise MonitorSpecError(f"Condition '{kind}' requires 'value'.")
        if isinstance(value, (dict, list)):
            raise MonitorSpecError(f"Condition '{kind}' compares with a single value, not {type(value).__name__}.")
        condition["value"] = value
    elif kind == "regex":
        pattern = _text(raw, "pattern", required=True, limit=500)
        _compile(pattern, "pattern")
        condition["pattern"] = pattern
    elif kind in ("gt", "ge", "lt", "le"):
        condition["value"] = _number_field(raw, "value")
    elif kind == "delta":
        threshold = _number_field(raw, "threshold")
        if threshold <= 0:
            raise MonitorSpecError(f"'threshold' must be greater than zero, got {threshold}.")
        condition["threshold"] = threshold
        direction = _text(raw, "direction") or "any"
        if direction not in ("up", "down", "any"):
            raise MonitorSpecError(f"'direction' must be up, down or any, got '{direction}'.")
        condition["direction"] = direction
        if raw.get("reference") is not None:
            condition["reference"] = _number_field(raw, "reference")
    elif kind == "exit_code":
        condition["value"] = _optional_int(raw, "value", minimum=0, maximum=255) or 0
    return condition


def normalize_action(raw: dict) -> dict:
    kind = _text(raw, "type", required=True)
    if kind not in ACTION_TYPES:
        raise MonitorSpecError(f"Unknown action '{kind}'. Use one of: {', '.join(ACTION_TYPES)}.")
    action: dict[str, Any] = {"type": kind}
    if kind in NOTIFYING_ACTIONS:
        action["message"] = _text(raw, "message")
        action["title"] = _text(raw, "title", limit=200)
    if kind in RESUMING_ACTIONS:
        action["prompt"] = _text(raw, "prompt", required=True)
        chat = _text(raw, "chat") or WAKE_CHAT_ORIGIN
        if chat not in WAKE_CHATS:
            raise MonitorSpecError(f"'chat' must be one of {', '.join(WAKE_CHATS)}, got '{chat}'.")
        action["chat"] = chat
    return action


def normalize_spec(raw: dict) -> dict:
    """Validate a monitor specification and return its normalized form.

    Raises :class:`MonitorSpecError` with a field-level diagnostic.
    """
    if not isinstance(raw, dict):
        raise MonitorSpecError("A monitor specification must be an object.")
    name = _text(raw, "name", required=True, limit=MAX_NAME_CHARS)
    check = normalize_check(_mapping(raw, "check"))
    condition = normalize_condition(_mapping(raw, "condition"), check["type"])
    action = normalize_action(_mapping(raw, "action"))

    minimum = MIN_LLM_INTERVAL_SECONDS if check["type"] == "llm" else MIN_INTERVAL_SECONDS
    interval = _optional_int(raw, "interval_seconds", minimum=minimum, maximum=MAX_INTERVAL_SECONDS)
    if interval is None:
        raise MonitorSpecError(
            f"'interval_seconds' is required (at least {minimum} for a '{check['type']}' check)."
        )
    first = _optional_int(raw, "first_check_seconds", minimum=0, maximum=MAX_INTERVAL_SECONDS)
    max_checks = _optional_int(raw, "max_checks", minimum=1, maximum=100000)
    ttl = _optional_int(raw, "ttl_seconds", minimum=MIN_INTERVAL_SECONDS, maximum=365 * 24 * 3600)
    resumes = action["type"] in RESUMING_ACTIONS
    # A wake-up is done once it has woken the chat; a watchdog keeps watching.
    repeat = _bool(raw, "repeat", not resumes)

    project = _text(raw, "project", limit=500)
    chat_id = _text(raw, "chat_id", limit=200)
    if resumes and not project:
        raise MonitorSpecError(
            f"Action '{action['type']}' wakes a chat, so the monitor needs the project it belongs to."
        )
    if resumes and action["chat"] == WAKE_CHAT_ORIGIN and not chat_id:
        raise MonitorSpecError(
            f"Action '{action['type']}' wakes the chat the monitor was registered from, and no chat "
            f"was given. Open a chat, or wake a new chat each time (chat '{WAKE_CHAT_NEW}')."
        )
    if check["type"] in ("command", "file") and not _text(raw, "project_path", limit=4096):
        if not (check["type"] == "file" and _is_absolute(check["path"])):
            raise MonitorSpecError(
                f"A '{check['type']}' check runs relative to a project folder; none was given."
            )
    return {
        "name": name,
        "description": _text(raw, "description"),
        "owner": _text(raw, "owner") or ("agent" if resumes else "user"),
        "project": project,
        "project_path": _text(raw, "project_path", limit=4096),
        "chat_id": chat_id,
        "sessions_db": _text(raw, "sessions_db", limit=4096),
        "check": check,
        "condition": condition,
        "action": action,
        "interval_seconds": interval,
        "first_check_seconds": first,
        "max_checks": max_checks,
        "ttl_seconds": ttl,
        "repeat": repeat,
    }


def _is_absolute(path: str) -> bool:
    import os

    return os.path.isabs(os.path.expanduser(path))


def _compile(pattern: str, field: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise MonitorSpecError(f"'{field}' is not a valid regular expression: {exc}.") from None


def _number_field(raw: dict, key: str) -> float:
    value = raw.get(key)
    number = to_number(value)
    if number is None:
        raise MonitorSpecError(f"'{key}' must be a number, got {value!r}.")
    return number


# ── Values ──────────────────────────────────────────────────────────────────

_DECIMAL_COMMA = re.compile(r"^[+-]?\d+,\d+$")


def to_number(value: Any) -> float | None:
    """A float for a numeric value, or None when it is not one.

    A decimal comma ("5,42") is read as a decimal point, since Brazilian and
    European sources write prices that way; a thousands separator is not
    guessed at, so "1.234,56" is not a number here.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if _DECIMAL_COMMA.match(text):
            text = text.replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def parse_json_path(path: str) -> list[str | int]:
    """Split ``a.b[0].c`` into keys and indexes. Raises MonitorSpecError."""
    parts: list[str | int] = []
    position = 0
    text = path.strip()
    if text.startswith("$"):
        text = text[1:].lstrip(".")
    while position < len(text):
        if text[position] == ".":
            position += 1
            continue
        match = _PATH_TOKEN.match(text, position)
        if not match:
            raise MonitorSpecError(f"'json_path' could not be read at '{text[position:]}'.")
        parts.append(int(match.group(2)) if match.group(2) is not None else match.group(1))
        position = match.end()
    if not parts:
        raise MonitorSpecError("'json_path' is empty.")
    return parts


def extract_json_path(document: Any, path: str) -> Any:
    """The value at *path* in *document*. Raises LookupError naming the step."""
    current = document
    walked = ""
    for part in parse_json_path(path):
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                raise LookupError(f"'{walked or '$'}' has no index [{part}].")
            current = current[part]
            walked += f"[{part}]"
        else:
            if not isinstance(current, dict) or part not in current:
                keys = ", ".join(list(current)[:10]) if isinstance(current, dict) else type(current).__name__
                raise LookupError(f"'{walked or '$'}' has no key '{part}' (found: {keys}).")
            current = current[part]
            walked += ("." if walked else "") + part
    return current


# ── Conditions ──────────────────────────────────────────────────────────────

@dataclass
class Evaluation:
    """The outcome of applying a condition to one observation."""

    met: bool
    #: Fire now. A level condition (``gt``, ``exists``...) fires on the check
    #: where it becomes true, not on every check while it stays true.
    fire: bool
    #: The value to compare the next observation with (``delta``'s reference).
    reference: Any = None
    details: dict | None = None


class ConditionError(ValueError):
    """An observation the condition cannot be applied to (e.g. not a number)."""


def subject_of(check: dict, condition: dict, value: Any) -> Any:
    """The part of an observation a condition is about."""
    kind = check.get("type")
    if kind == "command":
        if condition.get("type") == "exit_code":
            return (value or {}).get("exit_code")
        return (value or {}).get("output", "")
    if kind == "file":
        observed = value or {}
        if condition.get("type") in ("exists", "missing"):
            return bool(observed.get("exists"))
        return observed.get("sha256") if check.get("watch") == "content" else observed.get("mtime")
    if kind == "llm":
        return value or {}
    return value


def _numeric(value: Any) -> float:
    number = to_number(value)
    if number is None:
        raise ConditionError(f"The observed value is not a number: {str(value)[:120]!r}.")
    return number


def evaluate(
    check: dict,
    condition: dict,
    value: Any,
    *,
    previous: Any = None,
    has_previous: bool = False,
    previous_met: bool | None = None,
    reference: Any = None,
) -> Evaluation:
    """Apply *condition* to an observation. Raises ConditionError."""
    kind = condition["type"]
    subject = subject_of(check, condition, value)
    prev_subject = subject_of(check, condition, previous) if has_previous else None

    if kind == "always":
        return Evaluation(met=True, fire=True)
    if kind == "changed":
        if not has_previous:
            return Evaluation(met=False, fire=False, details={"baseline": True})
        changed = subject != prev_subject
        return Evaluation(met=changed, fire=changed)
    if kind == "delta":
        current = _numeric(subject)
        ref = to_number(reference)
        if ref is None:
            ref = to_number(condition.get("reference"))
        if ref is None:
            # The first observation is the reference the movement is measured from.
            return Evaluation(met=False, fire=False, reference=current, details={"baseline": True})
        delta = current - ref
        direction = condition.get("direction", "any")
        moved = (
            abs(delta) >= condition["threshold"] if direction == "any"
            else delta >= condition["threshold"] if direction == "up"
            else -delta >= condition["threshold"]
        )
        # Measured from the last firing, so each further move of the threshold
        # fires once instead of every check after the first.
        return Evaluation(
            met=moved,
            fire=moved,
            reference=current if moved else ref,
            details={"delta": delta, "reference": ref},
        )

    if kind == "llm":
        met = bool((subject or {}).get("met"))
    elif kind == "equals":
        met = _loosely_equal(subject, condition["value"])
    elif kind == "not_equals":
        met = not _loosely_equal(subject, condition["value"])
    elif kind == "contains":
        met = str(condition["value"]) in ("" if subject is None else str(subject))
    elif kind == "regex":
        met = re.search(condition["pattern"], "" if subject is None else str(subject)) is not None
    elif kind in ("gt", "ge", "lt", "le"):
        current = _numeric(subject)
        bound = condition["value"]
        met = {
            "gt": current > bound, "ge": current >= bound,
            "lt": current < bound, "le": current <= bound,
        }[kind]
    elif kind == "exit_code":
        if subject is None:
            raise ConditionError("The command produced no exit code.")
        met = int(subject) == int(condition["value"])
    elif kind == "exists":
        met = bool(subject)
    elif kind == "missing":
        met = not bool(subject)
    else:  # pragma: no cover - normalize_condition refuses anything else
        raise ConditionError(f"Unknown condition '{kind}'.")
    # Fires on the rising edge: the first check where it holds.
    return Evaluation(met=met, fire=met and not bool(previous_met))


def _loosely_equal(observed: Any, expected: Any) -> bool:
    left, right = to_number(observed), to_number(expected)
    if left is not None and right is not None:
        return left == right
    return str(observed).strip() == str(expected).strip()


# ── Messages ────────────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def display_value(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, dict):
        if "output" in value or "exit_code" in value:
            text = str(value.get("output", "")).strip() or f"exit code {value.get('exit_code')}"
        elif "summary" in value or "value" in value:
            text = str(value.get("summary") or value.get("value") or "")
        else:
            import json

            text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_template(template: str, variables: dict) -> str:
    """Replace ``{name}`` placeholders; unknown names are left as written.

    Only bare word placeholders are recognised, so a template cannot reach
    attributes or indexes the way ``str.format`` would let it.
    """
    def _replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        value = variables[key]
        if isinstance(value, float):
            return f"{value:+.6g}" if key == "delta" else f"{value:.6g}"
        return display_value(value)

    return _PLACEHOLDER.sub(_replace, template)
