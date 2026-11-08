"""Chat compaction in GilzaBots: `/compact` and automatic compaction during a turn.

The framework replaces the orchestrator's context with a summary
(`tests/test_memgpt_compaction.py`). These tests pin what the application adds:
the compacted state is what the next turn restores, the visible history is never
touched, a `compaction` activity row marks the spot (it is what the chat panel
draws its divider from after a reload), and the context indicator moves to the
compacted size.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenticblocks.blocks.llm.compaction import CONTINUATION_PREAMBLE, CompactionEvent
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from gilzabots import token_usage
from gilzabots.compaction import (
    COMPACTION_ACTIVITY_EVENT,
    COMPACTION_STREAM_EVENT,
    DEFAULT_AUTO_COMPACT_THRESHOLD,
    ChatBusyError,
    compact_chat,
)
from gilzabots.project import ProjectStore


SUMMARY_REPLY = "<analysis>notes</analysis><summary>1. Primary Request and Intent: ship it.</summary>"


def _answer(content):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _store_with_chat(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="proj", mode="auto", model="fake/model",
        project_name="Proj", project_path=str(tmp_path),
    )
    store.create_chat("proj", "chat-1", "Chat")
    project = store.load("proj", chat_id="chat-1")
    for i in range(4):
        store.append_message(project, "user" if i % 2 == 0 else "assistant", f"visible {i}")
    return store, store.load("proj", chat_id="chat-1")


def _agent(reply=SUMMARY_REPLY):
    agent = MemGPTAgentBlock(name="chat_orchestrator", system_prompt="s", max_context_tokens=50_000)
    agent.internal_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "word " * 300}
        for i in range(8)
    ]

    async def fake(_messages, **_kw):
        return _answer(reply)

    agent._acompletion = fake
    return agent


@pytest.fixture(autouse=True)
def clean_usage_state():
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    yield
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")


def test_compact_chat_saves_the_compacted_state_and_keeps_the_visible_history(tmp_path):
    store, project = _store_with_chat(tmp_path)
    visible_before = [m["content"] for m in project.history]
    agent = _agent()

    event, timestamp = asyncio.run(compact_chat(project, store, agent, "focus on X"))

    state = store.get_chat_agent_state("proj", "chat-1")
    assert len(state["internal_history"]) == 1
    assert state["internal_history"][0]["content"].startswith(CONTINUATION_PREAMBLE)
    assert "ship it" in state["internal_history"][0]["content"]

    reloaded = store.load("proj", chat_id="chat-1")
    assert [m["content"] for m in reloaded.history] == visible_before

    rows = [a for a in store.list_activity("proj", "chat-1") if a["event"] == COMPACTION_ACTIVITY_EVENT]
    assert len(rows) == 1
    assert rows[0]["content"] == "1. Primary Request and Intent: ship it."
    assert rows[0]["timestamp"] == timestamp
    assert rows[0]["payload"]["trigger"] == "manual"
    assert rows[0]["payload"]["custom_instructions"] == "focus on X"
    assert rows[0]["payload"]["pre_tokens"] == event.pre_tokens

    stored = store.get_chat_context_usage("proj", "chat-1")
    assert stored["prompt_tokens"] == event.post_tokens
    assert stored["context_window"] == 50_000
    live = token_usage.get_context_usage(token_usage.context_scope_key(str(tmp_path), "chat-1"))
    assert live["prompt_tokens"] == event.post_tokens


def test_failed_compaction_changes_nothing(tmp_path):
    from gilzabots.compaction import CompactionError

    store, project = _store_with_chat(tmp_path)
    agent = _agent(reply="")
    store.save_chat_agent_state("proj", "chat-1", agent.dump_state())

    with pytest.raises(CompactionError):
        asyncio.run(compact_chat(project, store, agent))

    assert len(store.get_chat_agent_state("proj", "chat-1")["internal_history"]) == 8
    assert not [a for a in store.list_activity("proj", "chat-1") if a["event"] == COMPACTION_ACTIVITY_EVENT]


def test_compaction_refuses_a_chat_another_process_is_running(tmp_path):
    from gilzabots.monitors.store import MonitorStore

    store, project = _store_with_chat(tmp_path)
    agent = _agent()
    other_pid = os.getppid()  # alive, and not this process
    MonitorStore().acquire_lease("proj", "chat-1", other_pid, "turn")

    with pytest.raises(ChatBusyError):
        asyncio.run(compact_chat(project, store, agent))
    assert len(agent.internal_history) == 8


def test_the_compact_command_reports_the_result_for_the_gui(tmp_path):
    from gilzabots.cli_commands import REPLState, _registry

    store, project = _store_with_chat(tmp_path)
    state = REPLState(project, store)
    state.memgpt = _agent()

    asyncio.run(_registry.dispatch(state, "/compact", ["keep", "the", "API"]))

    assert state.last_compaction["phase"] == "completed"
    assert state.last_compaction["custom_instructions"] == "keep the API"
    assert state.last_compaction["summary"] == "1. Primary Request and Intent: ship it."
    assert state.last_compaction["timestamp"]
    assert "/compact" in _registry and not _registry.is_cli_only("/compact")


def test_the_orchestrator_auto_compacts_by_default_and_honours_the_project(tmp_path, monkeypatch):
    from gilzabots import memgpt_runtime

    store, project = _store_with_chat(tmp_path)
    monkeypatch.setattr(memgpt_runtime, "get_project_agent_params", lambda *_a, **_k: {})
    agent = memgpt_runtime.build_chat_orchestrator(project, store)
    assert agent.auto_compact_threshold == DEFAULT_AUTO_COMPACT_THRESHOLD
    assert DEFAULT_AUTO_COMPACT_THRESHOLD < agent.eviction_threshold

    monkeypatch.setattr(
        memgpt_runtime, "get_project_agent_params",
        lambda *_a, **_k: {"auto_compact_threshold": 1.0},
    )
    agent = memgpt_runtime.build_chat_orchestrator(project, store)
    assert agent.auto_compact_threshold == 1.0
    assert not agent._auto_compact_enabled()


def test_auto_compact_threshold_is_an_accepted_project_parameter():
    from gilzabots.config import sanitize_model_params

    assert sanitize_model_params({"auto_compact_threshold": 0.6}) == {"auto_compact_threshold": 0.6}


@pytest.mark.asyncio
async def test_a_turn_reports_and_records_an_automatic_compaction(tmp_path, monkeypatch):
    import gilzabots.agent_stdin as stdin_mod

    store, project = _store_with_chat(tmp_path)
    events = []

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        on_compaction = None
        on_compaction_request = None
        on_compacted_context = None

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            self.on_compaction(CompactionEvent(phase="started", trigger="auto", pre_tokens=41_000))
            self.on_compaction(CompactionEvent(
                phase="completed", trigger="auto", pre_tokens=41_000, post_tokens=1_200,
                context_window=50_000, messages_compacted=30, summary="the summary",
            ))
            return SimpleNamespace(response="done")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data=None, **_k: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)

    agent = FakeMemGPT()
    monkeypatch.setattr(stdin_mod, "current_memgpt", agent)
    await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "hello", "chat_id": "chat-1"})

    # The same PreCompact/PostCompact hooks and file restoration as /compact.
    assert agent.on_compaction_request is not None and agent.on_compacted_context is not None

    compaction_events = [data for name, data in events if name == COMPACTION_STREAM_EVENT]
    assert [e["phase"] for e in compaction_events] == ["started", "completed"]
    assert compaction_events[1]["summary"] == "the summary"
    assert compaction_events[1]["timestamp"]

    rows = [a for a in store.list_activity("proj", "chat-1") if a["event"] == COMPACTION_ACTIVITY_EVENT]
    assert len(rows) == 1 and rows[0]["payload"]["trigger"] == "auto"
