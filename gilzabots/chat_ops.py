"""Chat lifecycle operations shared by the CLI commands and the IDE server.

Clearing a chat is not a UI gesture: it erases the stored conversation, the
orchestrator's saved working memory, the chat's archival entries, and the
measured context occupancy. The `/clear_chat` command and the panel's "Clear
chat" action must produce exactly the same state, so the effects live here once
instead of being reimplemented per entry point.
"""
from __future__ import annotations

from typing import Any

from .archival import clear_archival_chat
from .project import ChatNotFoundError
from .token_usage import context_scope_key, reset_context_usage


def clear_chat(project: Any, store: Any, chat_id: str) -> Any:
    """Erase one chat and everything derived from it. Returns the reloaded project.

    ``clear_project_history`` also resets the chat's ``agent_state`` and its
    persisted ``context_usage``, so the next turn rebuilds the orchestrator with
    an empty ``internal_history`` rather than restoring the conversation the user
    just deleted, and reopening the chat cannot rehydrate the occupancy of a
    window that no longer exists.
    """
    # An empty id would make `clear_project_history` erase every chat.
    if not chat_id:
        raise ChatNotFoundError(f"Project '{project.name}' has no open chat to clear")
    store.clear_project_history(project.name, chat_id)

    # An isolated chat keeps its own core memory; a shared one must not be
    # wiped here, because the other chats still rely on it.
    if not getattr(project, "use_shared_memory", True):
        store.update_chat_core_memory(project.name, chat_id, "")

    clear_archival_chat(project.name, chat_id)

    # The indicator was describing the conversation that no longer exists.
    reset_context_usage(context_scope_key(
        getattr(project, "project_path", "") or "", chat_id,
    ))

    return store.load(project.name, chat_id=chat_id)
