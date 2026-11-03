"""Parse and apply per-turn meta-parameters embedded in chat messages.

Syntax: @key=value@ anywhere in the message text.
  - String values may be quoted: @system_prompt="be ironic"@
  - Numeric values are auto-cast: @max_tokens=512@ @temperature=0.9@
  - Multiple params per message are supported.
  - Unknown keys are silently ignored.

Allowed keys: max_tokens, system_prompt, temperature, top_k, top_p, min_p.

Numeric params below their safe minimum are clamped (see _MIN_VALUES).
max_tokens minimum is 256 — lower values break MemGPT heartbeat tool calls.

Usage:
    clean_text, overrides = parse_meta_params(raw_message)
    with apply_meta_params(agent, overrides):
        result = await agent.run(...)

See docs/specs/10-chat-meta-params.md for full documentation.
"""

import re
from contextlib import contextmanager
from typing import Any

# Keys that go into agent.model_kargs (litellm kwargs).
# `think` is deliberately absent: it is resolved from the selected model's
# catalog capability alone (config.resolve_think_request), and a per-message
# override would silently change the wire format for that turn.
_LITELLM_KEYS = {"max_tokens", "temperature", "top_k", "top_p", "min_p"}
# Keys that are direct attributes on LLMAgentBlock
_AGENT_ATTR_KEYS = {"system_prompt"}
_ALLOWED_KEYS = _LITELLM_KEYS | _AGENT_ATTR_KEYS

_PATTERN = re.compile(
    r'@(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>"[^"]*"|\'[^\']*\'|[^\s@]+)@'
)

# Minimum safe values for numeric params to prevent broken tool-call responses.
_MIN_VALUES: dict[str, int | float] = {
    "max_tokens": 256,
    "temperature": 0.0,
    "top_k": 1,
    "top_p": 0.0,
    "min_p": 0.0,
}


def _cast(value: str) -> Any:
    """Strip optional quotes and cast to int/float/str as appropriate."""
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_meta_params(text: str) -> tuple[str, dict[str, Any]]:
    """Return (clean_text, overrides).

    clean_text has all @key=value@ tokens removed.
    Numeric values below their safe minimum are clamped with a warning.
    Unknown keys are silently ignored.
    """
    overrides: dict[str, Any] = {}
    for match in _PATTERN.finditer(text):
        key = match.group("key")
        if key not in _ALLOWED_KEYS:
            continue
        value = _cast(match.group("value"))
        min_val = _MIN_VALUES.get(key)
        if min_val is not None and isinstance(value, (int, float)) and value < min_val:
            import sys
            print(
                f"[meta-params] {key}={value} is below the safe minimum ({min_val}); "
                f"clamped to {min_val}.",
                file=sys.stderr,
            )
            value = min_val
        overrides[key] = value
    clean = _PATTERN.sub("", text).strip()
    return clean, overrides


@contextmanager
def apply_meta_params(agent: Any, overrides: dict[str, Any]):
    """Context manager: temporarily apply overrides to agent, restore on exit."""
    if not overrides:
        yield
        return

    saved_attrs: dict[str, Any] = {}
    saved_kargs: dict[str, Any] = {}

    # Snapshot and apply
    for key, value in overrides.items():
        if key in _AGENT_ATTR_KEYS:
            saved_attrs[key] = getattr(agent, key, None)
            setattr(agent, key, value)
        elif key in _LITELLM_KEYS:
            kargs = getattr(agent, "model_kargs", None)
            if kargs is None:
                kargs = getattr(agent, "model_kwargs", None)
            if isinstance(kargs, dict):
                saved_kargs[key] = kargs.get(key)
                kargs[key] = value

    try:
        yield
    finally:
        # Restore agent attributes
        for key, old_value in saved_attrs.items():
            if old_value is None:
                try:
                    delattr(agent, key)
                except AttributeError:
                    setattr(agent, key, old_value)
            else:
                setattr(agent, key, old_value)
        # Restore litellm kargs
        kargs = getattr(agent, "model_kargs", None)
        if kargs is None:
            kargs = getattr(agent, "model_kwargs", None)
        if isinstance(kargs, dict):
            for key, old_value in saved_kargs.items():
                if old_value is None:
                    kargs.pop(key, None)
                else:
                    kargs[key] = old_value
