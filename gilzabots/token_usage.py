"""Provider-reported token usage for the chat context indicator.

The chat context indicator and the attachment budget must describe what the
provider actually charged for a request, not a ``len(text) / 4`` guess over the
visible chat bubbles. Those bubbles exclude the system prompt, the tool JSON
schemas, every tool call and every tool result -- exactly the parts that
dominate a real agent context -- so an estimate built from them reports a nearly
empty window while the model is already close to eviction.

AgenticBlocks measures this for us: both ``LLMAgentBlock`` and
``MemGPTAgentBlock`` emit a ``TokenUsage`` record through their
``on_token_usage`` callback after every LLM call. ``attach_usage_tracking``
subscribes to it, so this module never touches the request or response path.
Streaming is covered too: AgenticBlocks sends
``stream_options={"include_usage": True}`` and rebuilds a full response through
``stream_chunk_builder``, so the counters are populated in both streaming and
non-streaming mode.

This module only observes. It never alters a request, a response, or the agent
history.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from agenticblocks.runtime.state import TokenUsage

_log = logging.getLogger(__name__)

# Agents whose usage describes "the chat context window". Sub-agents (worker,
# inline_editor, skill_*) run their own independent histories, so their usage
# must never move the indicator shown next to the conversation.
CONTEXT_AGENTS = frozenset({"chat_orchestrator", "orchestrator"})

UsageListener = Callable[[str, dict], None]

_lock = threading.RLock()
_last_usage: dict[str, dict[str, int]] = {}
_context_usage: Optional[dict[str, int]] = None
_context_window: int = 0
_context_scope: str = ""
_listener: Optional[UsageListener] = None


def context_scope_key(project_path: str, chat_id: str) -> str:
    """Build the scope key shared by the run handler and the history endpoint."""
    return f"{project_path or ''}::{chat_id or ''}"


def set_usage_listener(listener: Optional[UsageListener]) -> None:
    """Install (or clear, with ``None``) the callback fired per recorded call."""
    global _listener
    with _lock:
        _listener = listener


def set_context_scope(scope: str) -> None:
    """Bind the stored context usage to one project/chat pair.

    Switching project or chat replaces the agent history entirely, so a previous
    measurement no longer describes the window. Discard it instead of reporting
    it for the wrong conversation.
    """
    global _context_scope, _context_usage, _context_window
    with _lock:
        if scope != _context_scope:
            _context_scope = scope
            _context_usage = None
            _context_window = 0


def set_context_window(window: int) -> None:
    """Record the window the current scope's usage is measured against."""
    global _context_window
    with _lock:
        _context_window = max(0, int(window or 0))


def reset_context_usage(scope: Optional[str] = None) -> None:
    """Forget the measured occupancy, for when a chat's context is really gone.

    Clearing a chat wipes both its history and the orchestrator's saved working
    state, so the next request starts from the system prompt again. Keeping the
    previous measurement would report the erased conversation's occupancy. Pass
    ``scope`` to act only when that project/chat is the one being measured.
    """
    global _context_usage
    with _lock:
        if scope is not None and scope != _context_scope:
            return
        _context_usage = None


def extract_usage(usage: Any) -> Optional[dict[str, int]]:
    """Return the counters of a ``TokenUsage`` record, or ``None``."""
    if usage is None:
        return None
    prompt = _as_int(getattr(usage, "prompt_tokens", None))
    completion = _as_int(getattr(usage, "completion_tokens", None))
    total = _as_int(getattr(usage, "total_tokens", None))
    # Providers that report no usage still produce an all-zero record. Storing
    # it would tell the indicator the window is empty in the middle of a turn,
    # so the previous (real) measurement is kept instead.
    if prompt <= 0 and completion <= 0 and total <= 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
    }


def attach_usage_tracking(agent: Any) -> Any:
    """Subscribe to an agent's ``on_token_usage`` callback.

    This is the framework's own hook, so nothing about how the agent builds,
    sends, or parses a request changes.
    """
    if getattr(agent, "on_token_usage", None) is record_token_usage:
        return agent
    try:
        agent.on_token_usage = record_token_usage
    except (AttributeError, TypeError, ValueError) as exc:
        _log.warning(
            "Agent %r does not accept on_token_usage: %s",
            getattr(agent, "name", agent), exc,
        )
    return agent


def record_token_usage(usage: TokenUsage) -> Optional[dict[str, int]]:
    """Store one ``TokenUsage`` record and notify the listener.

    ``prompt_tokens`` is the size of the request that was just sent, so the last
    call of a turn carries the peak occupancy of the window. After a MemGPT
    eviction the next call reports a smaller prompt, and the indicator recovers
    on its own.
    """
    global _context_usage
    try:
        record = extract_usage(usage)
    except Exception as exc:  # never let observation break a turn
        _log.debug("Could not read provider token usage: %s", exc)
        return None
    if record is None:
        return None

    name = str(getattr(usage, "block_name", "") or "")
    with _lock:
        _last_usage[name] = record
        if name in CONTEXT_AGENTS:
            _context_usage = record
        listener = _listener

    if listener:
        try:
            listener(name, dict(record))
        except Exception as exc:
            _log.warning("Token usage listener failed: %s", exc)
    return record


def record_context_tokens(prompt_tokens: int) -> None:
    """Store a locally counted occupancy for the chat context window.

    Used between provider responses: once a tool result has been appended, the
    request is already that much bigger, but no ``usage`` comes back until the
    next call completes. Counting the assembled request with the model's own
    tokenizer keeps both the indicator and the tool budget honest in the gap.
    """
    global _context_usage
    tokens = _as_int(prompt_tokens)
    if tokens <= 0:
        return
    with _lock:
        _context_usage = {
            "prompt_tokens": tokens,
            "completion_tokens": 0,
            "total_tokens": tokens,
        }


def get_last_usage(agent_name: str) -> Optional[dict[str, int]]:
    """Return the last recorded usage for one agent, or ``None``."""
    with _lock:
        record = _last_usage.get(str(agent_name or ""))
        return dict(record) if record else None


def get_context_usage(scope: Optional[str] = None) -> Optional[dict[str, int]]:
    """Return the last provider usage measured for the chat context window.

    Pass ``scope`` to read it only when it belongs to that project/chat pair;
    the value is process state, so a caller serving an arbitrary conversation
    (the history endpoint) must not receive another chat's measurement.
    """
    with _lock:
        if scope is not None and scope != _context_scope:
            return None
        if not _context_usage:
            return None
        record = dict(_context_usage)
        if _context_window > 0:
            record["context_window"] = _context_window
        return record


def get_context_prompt_tokens() -> Optional[int]:
    """Return the measured tokens occupied by the chat context, or ``None``."""
    record = get_context_usage()
    if not record:
        return None
    prompt = record.get("prompt_tokens", 0)
    return prompt if prompt > 0 else None


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
