"""A message sent while the agent is working must reach the turn in flight.

The composer used to be dead for the whole turn, so the only way to add anything
was to interrupt the agent and start over. These tests drive the real loops with
a stubbed completion and cover the contract that replaced that: the message is
delivered at a boundary where the history is well formed, it is stored in the
order the model actually read it, and whatever the turn never delivered is
handed back instead of dropped.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.blocks.llm.inbox import (
    InboxClosedError,
    InboxFullError,
    MessageInbox,
)
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from agenticblocks.core.function_block import as_tool
from agenticblocks.utils.messages import build_user_content, history_accepts_user_message


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(tool_calls=None, content=""):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


# ── MessageInbox ────────────────────────────────────────────────────────────


def test_submit_then_drain_preserves_order():
    inbox = MessageInbox()
    inbox.submit("first")
    inbox.submit("second")

    assert len(inbox) == 2
    assert [item.content for item in inbox.drain()] == ["first", "second"]
    assert len(inbox) == 0


def test_submit_is_refused_after_close():
    inbox = MessageInbox()
    inbox.close()

    # A queued message that will never be delivered is worse than a refused one.
    with pytest.raises(InboxClosedError):
        inbox.submit("too late")


def test_submit_is_refused_when_full():
    inbox = MessageInbox(max_pending=2)
    inbox.submit("a")
    inbox.submit("b")

    with pytest.raises(InboxFullError):
        inbox.submit("c")


def test_submit_rejects_empty_content_and_misplaced_attachments():
    inbox = MessageInbox()

    with pytest.raises(ValueError):
        inbox.submit("   ")
    with pytest.raises(ValueError):
        inbox.submit("hi", role="system", attachments=[{"type": "image"}])
    with pytest.raises(ValueError):
        inbox.submit("hi", role="assistant")


def test_cancel_removes_only_a_pending_item():
    inbox = MessageInbox()
    item = inbox.submit("drop me")

    assert inbox.cancel(item.item_id) is True
    assert inbox.cancel(item.item_id) is False

    delivered = inbox.submit("keep me")
    inbox.drain()
    # Already handed to the agent: cancelling must not claim to take it back.
    assert inbox.cancel(delivered.item_id) is False


def test_close_returns_what_was_never_delivered():
    inbox = MessageInbox()
    inbox.submit("late one")

    leftovers = inbox.close()

    assert [item.content for item in leftovers] == ["late one"]
    assert inbox.closed is True
    assert len(inbox) == 0


# ── history_accepts_user_message ────────────────────────────────────────────


def test_history_with_unanswered_tool_call_rejects_a_message():
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}], "content": ""},
    ]
    assert history_accepts_user_message(history) is False


def test_history_accepts_once_every_tool_call_is_answered():
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}, {"id": "c2"}], "content": ""},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert history_accepts_user_message(history) is False

    history.append({"role": "tool", "tool_call_id": "c2", "content": "ok"})
    assert history_accepts_user_message(history) is True


def test_empty_and_plain_histories_accept():
    assert history_accepts_user_message([]) is True
    assert history_accepts_user_message([{"role": "assistant", "content": "hi"}]) is True


def test_build_user_content_matches_the_multimodal_shape():
    assert build_user_content("plain", []) == "plain"
    parts = build_user_content("look", [{"type": "image", "mime": "image/png", "data": "AAA"}])
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAA"


# ── MemGPTAgentBlock ────────────────────────────────────────────────────────


def _memgpt_agent(inbox, **kwargs):
    @as_tool(name="probe", description="Probe something.")
    def probe(target: str) -> str:
        return f"probed {target}"

    kwargs.setdefault("max_heartbeats", 4)
    agent = MemGPTAgentBlock(
        name="chat_orchestrator",
        model="fake/model",
        tools=[probe],
        inbox=inbox,
        **kwargs,
    )
    return agent


def test_message_submitted_mid_turn_is_delivered_at_the_next_heartbeat():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)
    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # The user types while the first tool call is in flight.
            inbox.submit("also check the bibliography")
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        return _response(content="done")

    agent._acompletion = fake_acompletion
    out = asyncio.run(agent.run(AgentInput(prompt="check the preamble")))

    assert out.response == "done"
    roles = [(m["role"], m.get("content")) for m in agent.internal_history]
    assert roles[0] == ("user", "check the preamble")
    assert roles[1][0] == "assistant"
    assert roles[2][0] == "tool"
    # Delivered after the tool result, before the answer: the model read it.
    assert roles[3] == ("user", "also check the bibliography")
    assert roles[4] == ("assistant", "done")
    assert len(inbox) == 0


def test_delivered_message_reaches_the_provider_request():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)
    sent = []
    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        sent.append([dict(m) for m in messages])
        calls["n"] += 1
        if calls["n"] == 1:
            inbox.submit("and keep it short")
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        return _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="write it")))

    assert not any(m.get("content") == "and keep it short" for m in sent[0])
    assert any(m.get("content") == "and keep it short" for m in sent[1])


def test_delivery_callback_can_finalize_the_message_before_it_is_read():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)
    seen = []

    def on_delivery(item):
        seen.append(item.metadata.get("client_message_id"))
        # The host budgets/annotates here; whatever the item carries when this
        # returns is what the model receives.
        item.content = f"[annotated] {item.content}"

    agent.on_message_delivery = on_delivery
    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            inbox.submit("more", metadata={"client_message_id": "abc"})
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        return _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="go")))

    assert seen == ["abc"]
    assert any(m.get("content") == "[annotated] more" for m in agent.internal_history)


def test_delivery_never_splits_a_tool_call_from_its_results():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)
    agent.internal_history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}], "content": ""},
    ]
    inbox.submit("meanwhile")

    delivered = asyncio.run(agent._drain_inbox())

    assert delivered == []
    assert len(inbox) == 1, "the message stays queued for the next safe boundary"

    agent.internal_history.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    delivered = asyncio.run(agent._drain_inbox())

    assert [item.content for item in delivered] == ["meanwhile"]
    assert agent.internal_history[-1] == {"role": "user", "content": "meanwhile"}


def test_message_sent_during_the_final_response_stays_undelivered():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)

    async def fake_acompletion(messages, **_kw):
        # The loop breaks on this response, so nothing drains the inbox again.
        inbox.submit("one more thing")
        return _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="go")))

    assert [item.content for item in inbox.close()] == ["one more thing"]
    assert not any(m.get("content") == "one more thing" for m in agent.internal_history)


def test_delivered_message_becomes_the_turn_eviction_will_not_cross():
    agent = _memgpt_agent(MessageInbox())
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "injected"},
    ]
    assert agent._get_safe_eviction_index(history, target_count=3) == 2


def test_delivered_message_is_saved_with_the_agent_state():
    inbox = MessageInbox()
    agent = _memgpt_agent(inbox)
    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            inbox.submit("persisted too")
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        return _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="go")))

    state = agent.dump_state()
    assert any(m.get("content") == "persisted too" for m in state["internal_history"])


# ── LLMAgentBlock ───────────────────────────────────────────────────────────


def test_llm_agent_block_also_delivers_at_the_iteration_boundary():
    inbox = MessageInbox()

    @as_tool(name="probe", description="Probe something.")
    def probe(target: str) -> str:
        return f"probed {target}"

    agent = LLMAgentBlock(
        name="worker",
        system_prompt="s",
        tools=[probe],
        max_iterations=5,
        inbox=inbox,
    )
    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            inbox.submit("adjust the scope")
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        return _response(content="done")

    agent._acompletion = fake_acompletion
    captured = []

    async def capture(_iteration, messages):
        captured.append([dict(m) for m in messages])

    agent.on_iteration = capture
    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "done"
    assert any(m.get("content") == "adjust the scope" for m in captured[-1])


def test_a_block_without_an_inbox_is_unchanged():
    @as_tool(name="probe", description="Probe something.")
    def probe(target: str) -> str:
        return "ok"

    agent = LLMAgentBlock(name="worker", system_prompt="s", tools=[probe], max_iterations=3)

    async def fake_acompletion(messages, **_kw):
        return _response(content="done")

    agent._acompletion = fake_acompletion
    assert asyncio.run(agent.run(AgentInput(prompt="go"))).response == "done"


# ── GilzaBots wiring ─────────────────────────────────────────────────────────


@pytest.fixture
def gilzabots_turn(tmp_path, monkeypatch):
    """A running orchestrator turn with an inbox open, plus the events it emits."""
    from gilzabots import agent_stdin
    from gilzabots.project import ProjectStore

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

    events = []
    monkeypatch.setattr(
        agent_stdin, "print_event",
        lambda event, data: events.append((event, data)),
    )
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "_active_inbox", None)
    monkeypatch.setattr(agent_stdin, "_active_inbox_scope", ("", ""))

    agent = SimpleNamespace(inbox=None, on_message_delivery=None)
    inbox = agent_stdin._open_turn_inbox(
        agent,
        "chat_orchestrator",
        project_path=str(tmp_path),
        chat_id=project.current_chat_id,
    )
    yield SimpleNamespace(
        module=agent_stdin, store=store, project=project,
        agent=agent, inbox=inbox, events=events,
    )
    agent_stdin._close_turn_inbox(agent, inbox, "chat_orchestrator")


def test_submitting_without_a_running_turn_is_refused(monkeypatch):
    from gilzabots import agent_stdin

    monkeypatch.setattr(agent_stdin, "_active_inbox", None)
    with pytest.raises(InboxClosedError):
        agent_stdin.submit_chat_message("hello")


def test_submitting_to_another_chat_is_refused(gilzabots_turn):
    module = gilzabots_turn.module

    with pytest.raises(module.InboxScopeError):
        module.submit_chat_message(
            "hello",
            project_path=gilzabots_turn.project.project_path,
            chat_id="some-other-chat",
        )
    # The right chat still goes through.
    result = module.submit_chat_message(
        "hello",
        project_path=gilzabots_turn.project.project_path,
        chat_id=gilzabots_turn.project.current_chat_id,
    )
    assert result["pending"] == 1


def test_the_active_scope_is_published_while_a_turn_runs(gilzabots_turn):
    scope = gilzabots_turn.module.active_inbox_scope()

    assert scope["chat_id"] == gilzabots_turn.project.current_chat_id
    assert scope["pending"] == 0


def test_delivery_stores_the_message_and_reports_its_identity(gilzabots_turn):
    module = gilzabots_turn.module
    module.submit_chat_message(
        "check the bibliography too",
        client_message_id="client-1",
        chat_id=gilzabots_turn.project.current_chat_id,
    )

    item = gilzabots_turn.inbox.drain()[0]
    gilzabots_turn.agent.on_message_delivery(item)

    history = gilzabots_turn.store.load("proj").history
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "check the bibliography too"),
    ]

    delivered = [d for e, d in gilzabots_turn.events if e == "user_message_delivered"]
    saved = [d for e, d in gilzabots_turn.events if e == "user_message_saved"]
    assert delivered[0]["client_message_id"] == "client-1"
    # Same stable identity every other message operation anchors on.
    assert saved[0]["client_message_id"] == "client-1"
    assert saved[0]["message_id"] == delivered[0]["message_id"] is not None


def test_the_turn_ending_hands_back_what_it_never_delivered(gilzabots_turn):
    module = gilzabots_turn.module
    module.submit_chat_message(
        "too late",
        client_message_id="client-2",
        chat_id=gilzabots_turn.project.current_chat_id,
    )

    module._close_turn_inbox(gilzabots_turn.agent, gilzabots_turn.inbox, "chat_orchestrator")

    backlog = [d for e, d in gilzabots_turn.events if e == "user_message_backlog"]
    assert backlog[0]["items"] == [{
        "item_id": backlog[0]["items"][0]["item_id"],
        "client_message_id": "client-2",
    }]
    # An undelivered message is never written to the conversation: it was not read.
    assert gilzabots_turn.store.load("proj").history == []


def test_closing_the_turn_detaches_the_inbox_from_a_reused_agent(gilzabots_turn):
    module = gilzabots_turn.module

    module._close_turn_inbox(gilzabots_turn.agent, gilzabots_turn.inbox, "chat_orchestrator")

    # The chat orchestrator block outlives the turn: leaving a closed inbox
    # attached would make the next turn reject every message.
    assert gilzabots_turn.agent.inbox is None
    assert gilzabots_turn.agent.on_message_delivery is None
    assert module.active_inbox_scope() is None


def test_cancelling_a_queued_message_keeps_it_out_of_the_conversation(gilzabots_turn):
    module = gilzabots_turn.module
    result = module.submit_chat_message(
        "never mind",
        chat_id=gilzabots_turn.project.current_chat_id,
    )

    assert module.cancel_chat_message(result["item_id"]) is True
    assert module.cancel_chat_message(result["item_id"]) is False
    assert len(gilzabots_turn.inbox) == 0


def test_delivery_is_reported_during_the_turn_not_after_it(gilzabots_turn, monkeypatch):
    """The badge that says "read by the agent" depends on this event ordering.

    The front-end has no other way to know a queued message stopped waiting, so
    `user_message_delivered` has to leave the backend while the turn is still
    running -- before the answer that took it into account, not after the stream
    has already closed.
    """
    module = gilzabots_turn.module
    agent = _memgpt_agent(gilzabots_turn.inbox)
    agent.on_message_delivery = gilzabots_turn.agent.on_message_delivery
    monkeypatch.setattr(module, "_active_inbox", gilzabots_turn.inbox)

    calls = {"n": 0}

    async def fake_acompletion(messages, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            module.submit_chat_message(
                "and cite the source",
                client_message_id="client-3",
                chat_id=gilzabots_turn.project.current_chat_id,
            )
            return _response(tool_calls=[_tool_call("c1", "probe", '{"target": "x"}')])
        module.print_event("agent_response", {"response": "done"})
        return _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="write it")))

    names = [event for event, _data in gilzabots_turn.events]
    assert "user_message_delivered" in names
    assert names.index("user_message_delivered") < names.index("agent_response")
