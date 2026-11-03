"""Clearing a chat must erase agent state, not just the visible bubbles.

The panel's "Clear chat" action and the /clear_chat command both go through
`chat_ops.clear_chat`, so a chat can never end up visually empty while the
orchestrator still replays its history.
"""
import pytest

from gilzabots import chat_ops, token_usage


class _FakeStore:
    def __init__(self):
        self.cleared = []
        self.core_memory_cleared = []
        self.loaded = []

    def clear_project_history(self, name, chat_id=None):
        self.cleared.append((name, chat_id))

    def update_chat_core_memory(self, name, chat_id, value):
        self.core_memory_cleared.append((name, chat_id, value))

    def load(self, name, chat_id=None):
        self.loaded.append((name, chat_id))
        return _FakeProject(name, shared_memory=True)


class _FakeProject:
    def __init__(self, name="proj", shared_memory=True, path="/tmp/proj"):
        self.name = name
        self.project_path = path
        self.use_shared_memory = shared_memory


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(chat_ops, "clear_archival_chat", lambda *a, **k: None)
    token_usage.set_context_scope("reset")
    yield
    token_usage.set_context_scope("reset")


def test_clear_chat_erases_history_and_returns_the_reloaded_project():
    store = _FakeStore()
    project = _FakeProject()

    reloaded = store and chat_ops.clear_chat(project, store, "chat-1")

    assert store.cleared == [("proj", "chat-1")]
    assert store.loaded == [("proj", "chat-1")]
    assert reloaded is not project


def test_clear_chat_drops_the_measured_context_occupancy():
    # The indicator kept reporting the erased conversation's tokens.
    store = _FakeStore()
    project = _FakeProject()
    token_usage.set_context_scope(token_usage.context_scope_key("/tmp/proj", "chat-1"))
    token_usage.record_context_tokens(7800)

    chat_ops.clear_chat(project, store, "chat-1")

    assert token_usage.get_context_prompt_tokens() is None


def test_clear_chat_leaves_another_chats_measurement_alone():
    store = _FakeStore()
    project = _FakeProject()
    token_usage.set_context_scope(token_usage.context_scope_key("/tmp/proj", "chat-1"))
    token_usage.record_context_tokens(7800)

    chat_ops.clear_chat(project, store, "chat-2")

    assert token_usage.get_context_prompt_tokens() == 7800


def test_isolated_chat_memory_is_wiped_but_shared_memory_is_kept():
    store = _FakeStore()
    chat_ops.clear_chat(_FakeProject(shared_memory=True), store, "chat-1")
    assert store.core_memory_cleared == []

    store = _FakeStore()
    chat_ops.clear_chat(_FakeProject(shared_memory=False), store, "chat-1")
    assert store.core_memory_cleared == [("proj", "chat-1", "")]
