"""Chat compaction: the GilzaBots side of ``MemGPTAgentBlock.compact``.

The framework replaces the orchestrator's working context with a structured
summary (``agenticblocks/blocks/llm/compaction.py``). This module does what the
application has to do around it, identically for the ``/compact`` command and
for an automatic compaction during a turn:

- the chat's visible history is never touched -- like Claude Code's transcript,
  every message stays readable; only what the model *receives* is compacted;
- a ``compaction`` activity row marks where it happened and keeps the summary,
  so the chat shows a divider there after a reload, like the error bubbles that
  are also stored as activity (PROJECT_DESIGN 2.6);
- the context indicator is moved to the compacted size, both in process and in
  the stored measurement that survives a restart.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from agenticblocks.blocks.llm.compaction import (
    COMPACTION_TRIGGER_MANUAL,
    CompactionError,
    CompactionEvent,
)

#: Fraction of the window past which a turn compacts the conversation. Below the
#: eviction threshold (0.85), so compaction is the first response to a filling
#: window and FIFO eviction only handles what a compaction could not shrink.
DEFAULT_AUTO_COMPACT_THRESHOLD = 0.8

#: ``project_activity.event`` of the row that marks a compaction.
COMPACTION_ACTIVITY_EVENT = "compaction"

#: Event name on the run stream (desktop and terminal front-ends).
COMPACTION_STREAM_EVENT = "context_compaction"


class ChatBusyError(RuntimeError):
    """Another process is running a turn in this chat."""


def compaction_payload(event: CompactionEvent, timestamp: str = "") -> dict:
    """The record shown to the front-ends and stored with the activity row."""
    payload = {
        "phase": event.phase,
        "trigger": event.trigger,
        "pre_tokens": int(event.pre_tokens or 0),
        "post_tokens": int(event.post_tokens or 0),
        "context_window": int(event.context_window or 0),
        "messages_compacted": int(event.messages_compacted or 0),
        "custom_instructions": event.custom_instructions or "",
    }
    if event.phase == "completed":
        payload["summary"] = event.summary
    if event.error:
        payload["error"] = event.error
    # What the host steps below did (restored files, hook runs); paths and
    # outcomes only -- the restored contents live in the agent state, not here.
    extra = event.extra or {}
    for key in ("restored_files", "skipped_files", "hooks"):
        if extra.get(key):
            payload[key] = list(extra[key])
    if timestamp:
        payload["timestamp"] = timestamp
    return payload


# ── Restoring the files the conversation was working on ──────────────────────
#
# Claude Code re-attaches the files it read most recently after compacting, so
# the agent continues with their exact text instead of whatever excerpts the
# summary kept. The same is done here from the compacted history's own tool
# calls: the files are re-read from disk, so the agent sees them as they are
# now, including its own edits.

#: Tools whose ``path`` argument names a project text file the agent worked on.
#: ``read_document`` is absent on purpose: it extracts PDFs and office files,
#: whose raw bytes are not what the agent read.
RESTORED_FILE_TOOLS = frozenset({
    "read_file", "read_content_pos", "write_file", "edit_file",
    "write_content_pos", "replace_content_range",
})
MAX_RESTORED_FILES = 5
MAX_TOKENS_PER_RESTORED_FILE = 5_000
MAX_RESTORED_TOKENS = 50_000
#: Restored files never take more than this fraction of a known window, so the
#: compacted context stays well under the auto-compact threshold.
RESTORED_WINDOW_FRACTION = 0.2

RESTORED_FILES_HEADER = (
    "Current contents of the files this conversation was working on, read again "
    "from disk after the summary was written:"
)
SKIPPED_FILES_HEADER = "Files from this conversation that were not restored (read them again if you need them):"


def recent_file_paths(history: list[dict]) -> list[str]:
    """Paths the history's file tool calls named, most recent first, without repeats."""
    import json as _json

    seen: set[str] = set()
    paths: list[str] = []
    for message in reversed(history or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in reversed(message.get("tool_calls") or []):
            fn = (call or {}).get("function") or {}
            if fn.get("name") not in RESTORED_FILE_TOOLS:
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except ValueError:
                    continue
            path = (args or {}).get("path") if isinstance(args, dict) else None
            if not isinstance(path, str) or not path.strip():
                continue
            key = os.path.normcase(os.path.normpath(path.strip()))
            if key in seen:
                continue
            seen.add(key)
            paths.append(path.strip())
    return paths


def _inside(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(root), os.path.normcase(path)]) == os.path.normcase(root)
    except ValueError:  # different drives on Windows
        return False


def restore_recent_files(
    history: list[dict],
    project_path: str,
    model: str,
    context_window: int | None,
) -> tuple[str, list[str], list[dict]]:
    """Build the restored-files block; return it with what was restored and skipped.

    A file is skipped -- and named, with the reason, so the agent knows to read
    it again -- when it is outside the project, gone, not text, larger than
    ``MAX_TOKENS_PER_RESTORED_FILE``, or past the total budget. Nothing is cut.
    """
    from agenticblocks.blocks.llm.tokens import count_text_tokens

    from .tools import _read_text_file

    root = os.path.abspath(project_path or "")
    budget = MAX_RESTORED_TOKENS
    if context_window:
        budget = min(budget, int(context_window * RESTORED_WINDOW_FRACTION))

    restored: list[str] = []
    skipped: list[dict] = []
    blocks: list[str] = []
    spent = 0
    for path in recent_file_paths(history):
        if len(restored) >= MAX_RESTORED_FILES:
            break
        absolute = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
        shown = os.path.relpath(absolute, root).replace(os.sep, "/") if root and _inside(root, absolute) else path
        if not root or not _inside(root, absolute):
            skipped.append({"path": shown, "reason": "outside the project folder"})
            continue
        if not os.path.isfile(absolute):
            skipped.append({"path": shown, "reason": "no longer exists"})
            continue
        try:
            with open(absolute, "rb") as handle:
                if b"\x00" in handle.read(8192):
                    skipped.append({"path": shown, "reason": "not a text file"})
                    continue
            content = _read_text_file(absolute)
        except OSError as exc:
            skipped.append({"path": shown, "reason": f"cannot be read: {exc}"})
            continue
        tokens = count_text_tokens(model, content)
        if tokens > MAX_TOKENS_PER_RESTORED_FILE:
            skipped.append({"path": shown, "reason": f"too large to restore ({tokens} tokens)"})
            continue
        if spent + tokens > budget:
            skipped.append({"path": shown, "reason": "the restoration budget was already spent"})
            continue
        spent += tokens
        restored.append(shown)
        blocks.append(f'<file path="{shown}">\n{content}\n</file>')

    parts: list[str] = []
    if blocks:
        parts.append(RESTORED_FILES_HEADER + "\n\n" + "\n\n".join(blocks))
    if skipped:
        parts.append(SKIPPED_FILES_HEADER + "\n" + "\n".join(
            f"- {item['path']}: {item['reason']}" for item in skipped
        ))
    return "\n\n".join(parts), restored, skipped


def print_compaction_details(payload: dict) -> None:
    """Report restored files and hook runs of a compaction through ``terminal``.

    Shared by ``/compact`` (whose output the GUI also captures) and the terminal
    renderer of an automatic compaction.
    """
    from . import terminal as T
    from .i18n import _

    restored = payload.get("restored_files") or []
    if restored:
        T.info(_("cli_compact_restored", files=", ".join(restored)))
    for item in payload.get("skipped_files") or []:
        T.info(_("cli_compact_skipped", path=item.get("path", ""), reason=item.get("reason", "")))
    for hook in payload.get("hooks") or []:
        if hook.get("outcome") in ("error", "timeout"):
            T.warning(_(
                "cli_hook_failed", event=hook.get("event", ""),
                command=hook.get("command", ""), message=hook.get("message", ""),
            ))


# ── Host steps: PreCompact / PostCompact hooks and file restoration ──────────

def _hook_payload(project: Any, trigger: str, instructions: str) -> dict:
    return {
        "project": getattr(project, "name", "") or "",
        "project_path": getattr(project, "project_path", "") or "",
        "chat_id": getattr(project, "current_chat_id", "") or "",
        "trigger": trigger,
        "custom_instructions": instructions,
    }


def attach_compaction_extensions(agent: Any, project: Any) -> Any:
    """Give an orchestrator the GilzaBots steps around every compaction.

    Before the summary (``on_compaction_request``): the ``PreCompact`` hooks
    run; what they print is added to the summary's instructions, and an exit
    code of 2 blocks the compaction. After it (``on_compacted_context``): the
    files the conversation was working on are restored, then the
    ``PostCompact`` hooks run with the summary and what they print is added to
    the new context. Every hook run is reported in the compaction's event.
    """
    from agenticblocks.blocks.llm.compaction import CompactionContext, CompactionRequest, format_transcript

    from .hooks import run_hooks

    cwd = getattr(project, "project_path", "") or ""

    async def before(request: CompactionRequest) -> None:
        payload = _hook_payload(project, request.trigger, request.custom_instructions)
        payload["message_count"] = len(request.history)
        payload["transcript"] = format_transcript(request.history)
        results = await run_hooks("PreCompact", request.trigger, payload, cwd=cwd)
        if results:
            request.metadata.setdefault("hooks", []).extend(r.to_dict() for r in results)
        additions = [r.output for r in results if r.outcome == "ok" and r.output]
        if additions:
            request.custom_instructions = "\n\n".join(
                [request.custom_instructions, *additions]
            ).strip()
        blocked = next((r for r in results if r.outcome == "blocked"), None)
        if blocked is not None:
            request.blocked_reason = f"{blocked.message} (PreCompact hook: {blocked.command})"

    async def after(context: CompactionContext) -> None:
        block, restored, skipped = restore_recent_files(
            context.history, cwd, getattr(agent, "model", "") or "",
            getattr(agent, "max_context_tokens", None),
        )
        if block:
            context.extra_context.append(block)
        if restored:
            context.metadata["restored_files"] = restored
        if skipped:
            context.metadata["skipped_files"] = skipped

        payload = _hook_payload(project, context.trigger, context.custom_instructions)
        payload["summary"] = context.summary
        results = await run_hooks("PostCompact", context.trigger, payload, cwd=cwd)
        if results:
            context.metadata.setdefault("hooks", []).extend(r.to_dict() for r in results)
        for result in results:
            if result.outcome == "ok" and result.output:
                context.extra_context.append(
                    f"Additional context from a PostCompact hook ({result.command}):\n{result.output}"
                )

    agent.on_compaction_request = before
    agent.on_compacted_context = after
    return agent


def record_compaction(store: Any, project: Any, event: CompactionEvent, agent: str = "") -> str:
    """Store the activity row that marks a completed compaction; return its time."""
    timestamp = datetime.now(timezone.utc).isoformat()
    payload = compaction_payload(event)
    payload.pop("summary", None)  # carried once, as the row's content
    store.append_activity(
        project,
        COMPACTION_ACTIVITY_EVENT,
        content=event.summary,
        agent=agent,
        payload=payload,
        timestamp=timestamp,
    )
    return timestamp


def report_compacted_context(project: Any, store: Any, event: CompactionEvent) -> dict | None:
    """Point the context indicator at the compacted size and persist it.

    The summary request is an auxiliary call and reports no provider usage
    (like MemGPT's eviction summary, PROJECT_DESIGN 2.6), so the new occupancy
    is the local count the framework took of the compacted context. Returns the
    usage record, or ``None`` when nothing was measured.
    """
    from .token_usage import (
        context_scope_key,
        get_context_usage,
        record_context_tokens,
        set_context_scope,
        set_context_window,
    )

    tokens = int(event.post_tokens or 0)
    if tokens <= 0:
        return None
    chat_id = getattr(project, "current_chat_id", "") or ""
    scope = context_scope_key(getattr(project, "project_path", "") or "", chat_id)
    set_context_scope(scope)
    if event.context_window:
        set_context_window(int(event.context_window))
    record_context_tokens(tokens)
    usage = get_context_usage(scope)
    if usage is not None:
        usage["source"] = "local"
        if store is not None and chat_id:
            try:
                store.save_chat_context_usage(project.name, chat_id, usage)
            except Exception:
                pass
    return usage


def _take_chat_lease(project: Any) -> tuple[str, str] | None:
    """Hold the chat against a turn in another process (the CLI, the monitor daemon).

    A turn saves the orchestrator's state when it ends, so a compaction saved
    while it runs would be overwritten by the context it replaced.
    """
    chat_id = getattr(project, "current_chat_id", "") or ""
    if not chat_id:
        return None
    try:
        from .monitors.store import MonitorStore

        holder = MonitorStore().acquire_lease(project.name, chat_id, os.getpid(), "compact")
    except Exception:
        return None
    if holder is not None:
        from .i18n import _

        raise ChatBusyError(_("chat_busy_in_background", holder=holder.get("holder", "")))
    return (project.name, chat_id)


def _release_chat_lease(lease: tuple[str, str] | None) -> None:
    if not lease:
        return
    try:
        from .monitors.store import MonitorStore

        MonitorStore().release_lease(lease[0], lease[1], os.getpid())
    except Exception:
        pass


async def compact_chat(
    project: Any,
    store: Any,
    memgpt: Any,
    custom_instructions: str = "",
) -> tuple[CompactionEvent, str]:
    """Compact the open chat's working context on request (``/compact``).

    Returns the completed event and the activity row's timestamp. Raises
    :class:`CompactionError` (context unchanged) or :class:`ChatBusyError`.
    """
    from .litellm_compat import sanitize_agent_state, wrap_agent_litellm_compat
    from .memgpt_runtime import save_chat_orchestrator_state

    lease = _take_chat_lease(project)
    try:
        # The same request path a turn uses (handle_run): provider kwargs are
        # sanitized, the project's prompt prefix is applied, and a tool call an
        # interrupted turn left unanswered is repaired before it is summarized.
        memgpt.user_prompt_prefix = getattr(project, "user_prompt_prefix", "") or ""
        wrap_agent_litellm_compat(memgpt)
        sanitize_agent_state(memgpt)
        attach_compaction_extensions(memgpt, project)
        event = await memgpt.compact(custom_instructions, trigger=COMPACTION_TRIGGER_MANUAL)
        save_chat_orchestrator_state(memgpt, project, store)
        timestamp = record_compaction(store, project, event, agent="chat_orchestrator")
        report_compacted_context(project, store, event)
        return event, timestamp
    finally:
        _release_chat_lease(lease)


__all__ = [
    "attach_compaction_extensions",
    "restore_recent_files",
    "COMPACTION_ACTIVITY_EVENT",
    "COMPACTION_STREAM_EVENT",
    "ChatBusyError",
    "CompactionError",
    "DEFAULT_AUTO_COMPACT_THRESHOLD",
    "compact_chat",
    "compaction_payload",
    "record_compaction",
    "report_compacted_context",
]
