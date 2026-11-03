"""Tests for chat turn structure.

Covers the invariants a chat turn must satisfy end to end:
1. Corrective system alerts never split an assistant tool_calls message from its
   tool results (OpenAI adjacency requirement).
2. Context eviction never drops the user turn currently being answered.
3. Runtime corrections enter the history as system feedback, not as the user.
4. Persisted history seeds only the visible conversation, verbatim.
5. The orchestrator's working memory survives the per-turn rebuild.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from gilzabots.memgpt_runtime import (
    restore_chat_orchestrator_state,
    save_chat_orchestrator_state,
    seed_chat_orchestrator_history,
)
from gilzabots.project import ProjectData, ProjectStore


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


# ---------------------------------------------------------------------------
# 1. Tool-block adjacency
# ---------------------------------------------------------------------------

def test_in_batch_corrections_never_split_the_tool_block():
    """A correction raised while processing one call is delivered as a tool result.

    OpenAI-compatible endpoints require every "tool" result to follow its assistant
    tool_calls message with nothing in between, so a correction that entered the
    history as a system message would break the very request it is fixing. Every
    in-batch correction the loop can raise -- an exceeded tool_call_limit, a blocked
    repeat, an unknown tool -- is therefore answered in the tool channel.
    """
    from agenticblocks.core.function_block import as_tool

    @as_tool(name="noop", description="does nothing")
    def noop() -> str:
        return "ok"

    agent = MemGPTAgentBlock(
        name="chat_orchestrator",
        model="fake/model",
        max_heartbeats=2,
        tools=[noop],
        tool_call_limits={"noop": 1},
    )

    responses = [
        _response(tool_calls=[
            _tool_call("c1", "noop"),
            # Over the limit: corrected while the batch is still being processed.
            _tool_call("c2", "noop"),
        ]),
        _response(content="final answer"),
    ]

    async def fake_acompletion(_messages, **_kwargs):
        return responses.pop(0)

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="do it")))

    roles = [m["role"] for m in agent.internal_history]
    assistant_index = roles.index("assistant")
    # Both results follow the assistant message directly, correction included.
    assert roles[assistant_index + 1:assistant_index + 3] == ["tool", "tool"]
    corrections = [
        m["content"] for m in agent.internal_history
        if m["role"] == "tool" and "maximum limit" in str(m.get("content", ""))
    ]
    assert corrections, "the exceeded limit must still be reported to the model"


# ---------------------------------------------------------------------------
# 2. Eviction
# ---------------------------------------------------------------------------

def test_eviction_never_drops_the_current_user_turn():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "the question being answered right now"},
    ]

    # Even when asked to evict everything, the current turn survives.
    index = agent._get_safe_eviction_index(history, len(history))
    assert index <= 2
    assert history[index:][-1]["content"] == "the question being answered right now"


def test_eviction_index_is_zero_when_only_the_current_turn_exists():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    history = [{"role": "user", "content": "only turn"}]

    assert agent._get_safe_eviction_index(history, 1) == 0


def test_eviction_keeps_user_turn_after_an_orphan_tool_call():
    """An interrupted run can leave an assistant tool call right before the turn."""
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "user", "content": "current turn"},
    ]

    index = agent._get_safe_eviction_index(history, 1)

    assert history[index:] == [{"role": "user", "content": "current turn"}]


# ---------------------------------------------------------------------------
# 3. Runtime correction role
# ---------------------------------------------------------------------------

def test_memgpt_accepts_a_system_role_prompt_for_runtime_corrections():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model", max_heartbeats=1)

    async def fake_acompletion(_messages, **_kwargs):
        return _response(content="recovered answer")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="SYSTEM ALERT: say something", role="system")))

    assert agent.internal_history[0] == {
        "role": "system",
        "content": "SYSTEM ALERT: say something",
    }


def test_memgpt_rejects_an_unknown_input_role():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")

    with pytest.raises(ValueError):
        asyncio.run(agent.run(AgentInput(prompt="hi", role="assistant")))


def test_worker_agent_rejects_a_non_user_role():
    """A block without a conversation history has nowhere to put system feedback."""
    agent = LLMAgentBlock(name="worker", model="fake/model")

    with pytest.raises(ValueError):
        asyncio.run(agent.run(AgentInput(prompt="hi", role="system")))


# ---------------------------------------------------------------------------
# 4. Seeding from persisted history
# ---------------------------------------------------------------------------

def test_seeding_replays_only_the_visible_conversation_verbatim():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    project = SimpleNamespace(history=[
        {"role": "system", "content": "[MODE] Agent turn started.", "timestamp": "2026-08-15T10:00:00"},
        {"role": "user", "content": "hello", "timestamp": "2026-08-15T10:00:01"},
        {"role": "tool", "content": "tool output", "timestamp": "2026-08-15T10:00:02"},
        {"role": "assistant", "content": "hi there", "timestamp": "2026-08-15T10:00:03"},
        {"role": "assistant", "content": "Agent Error: boom", "timestamp": "2026-08-15T10:00:04"},
    ])

    seed_chat_orchestrator_history(agent, project)

    assert agent.internal_history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


# ---------------------------------------------------------------------------
# 5. Working-memory persistence across the per-turn rebuild
# ---------------------------------------------------------------------------

def test_orchestrator_state_survives_a_rebuild(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="proj",
        mode="auto",
        model="fake/model",
        project_name="Proj",
        project_path=str(tmp_path),
    )
    store.create_chat("proj", "chat-proj", "Chat")
    project = store.load("proj")

    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    agent.internal_history = [
        {"role": "user", "content": "remember the number 7"},
        {"role": "assistant", "content": "noted"},
    ]
    agent.recursive_summary = "The user shared a number."
    save_chat_orchestrator_state(agent, project, store)

    rebuilt = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    assert restore_chat_orchestrator_state(rebuilt, project, store) is True
    assert rebuilt.internal_history == agent.internal_history
    assert rebuilt.recursive_summary == "The user shared a number."


def test_orchestrator_state_is_dropped_when_the_chat_is_cleared(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="proj",
        mode="auto",
        model="fake/model",
        project_name="Proj",
        project_path=str(tmp_path),
    )
    store.create_chat("proj", "chat-proj", "Chat")
    project = store.load("proj")

    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    agent.internal_history = [{"role": "user", "content": "forget me"}]
    save_chat_orchestrator_state(agent, project, store)

    store.clear_project_history("proj", project.current_chat_id)

    rebuilt = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    assert restore_chat_orchestrator_state(rebuilt, project, store) is False
    assert rebuilt.internal_history == []


def test_orchestrator_state_is_dropped_when_a_message_is_superseded(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="proj",
        mode="auto",
        model="fake/model",
        project_name="Proj",
        project_path=str(tmp_path),
    )
    store.create_chat("proj", "chat-proj", "Chat")
    project = store.load("proj")
    anchor_id = store.append_message(project, "user", "original question")
    store.append_message(project, "assistant", "original answer")

    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    agent.internal_history = [
        {"role": "user", "content": "original question"},
        {"role": "assistant", "content": "original answer"},
    ]
    save_chat_orchestrator_state(agent, project, store)

    store.supersede_chat_history_from_message("proj", project.current_chat_id, message_id=anchor_id)

    rebuilt = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    assert restore_chat_orchestrator_state(rebuilt, project, store) is False


def test_dynamic_tools_change_between_requests_not_within_a_response():
    """Approval cannot authorize a write the model bundled before it knew its tools."""
    from agenticblocks.core.function_block import as_tool
    enabled, written, advertised = [], [], []

    @as_tool
    def approve() -> str:
        enabled.append(True)
        return "approved"

    @as_tool
    def write() -> str:
        written.append(True)
        return "written"

    agent = MemGPTAgentBlock(name="dynamic", model="fake/model", max_heartbeats=4,
                            tools_provider=lambda: [write] if enabled else [approve])
    responses = [
        _response(tool_calls=[_tool_call("a", "approve"), _tool_call("b", "write")]),
        _response(tool_calls=[_tool_call("c", "write")]),
        _response(content="done"),
    ]
    async def completion(messages, **kwargs):
        advertised.append({item["function"]["name"] for item in kwargs["tools"]})
        return responses.pop(0)
    agent._acompletion = completion
    asyncio.run(agent.run(AgentInput(prompt="work")))
    assert "approve" in advertised[0] and "write" not in advertised[0]
    assert "write" in advertised[1] and "approve" not in advertised[1]
    assert written == [True]
