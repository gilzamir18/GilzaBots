"""An assistant turn with no visible text must never become user-facing text.

Observed with a local Qwen3-class model: it wrote a whole report into the
reasoning channel and left the visible channel empty. The loop replaced that turn
with the marker ``(removed: empty response)``, the model read the marker back as
its own last utterance on the next call, echoed it, and the runtime published the
marker as the answer -- non-empty text, so every downstream correction was
skipped.

These tests drive the real MemGPT loop with a stubbed completion.
"""
import asyncio
from types import SimpleNamespace

from agenticblocks.blocks.llm.agent import AgentInput
from agenticblocks.blocks.llm.memgpt_agent import (
    EMPTY_RESPONSE_PLACEHOLDER,
    MemGPTAgentBlock,
)
from gilzabots.memgpt_runtime import seed_chat_orchestrator_history


def _response(content="", reasoning=None):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _agent(scripted, **kwargs):
    """Return an agent whose calls replay *scripted* (last entry repeats)."""
    kwargs.setdefault("max_heartbeats", 3)
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model", **kwargs)
    sent = []

    async def fake_acompletion(messages, **_kw):
        sent.append([dict(m) for m in messages])
        index = min(len(sent) - 1, len(scripted) - 1)
        return scripted[index]

    agent._acompletion = fake_acompletion
    return agent, sent


def _placeholders(messages):
    return [m for m in messages if str(m.get("content", "")) == EMPTY_RESPONSE_PLACEHOLDER]


# ---------------------------------------------------------------------------
# 1. The empty turn is dropped, not described in the assistant's own voice
# ---------------------------------------------------------------------------

def test_empty_turn_never_enters_the_history_as_assistant_text():
    agent, sent = _agent([_response(content=""), _response(content="the real answer")])

    out = asyncio.run(agent.run(AgentInput(prompt="check the article")))

    assert out.response == "the real answer"
    assert _placeholders(agent.internal_history) == []
    # What the model reads on the retry is the alert, never a stand-in answer.
    assert _placeholders(sent[1]) == []
    assert sent[1][-1]["role"] == "system"
    assert "no text and no native tool call" in sent[1][-1]["content"]


def test_empty_turn_leaves_no_empty_assistant_message_behind():
    """Dropping means dropping: an empty assistant turn is not kept blank either."""
    agent, _sent = _agent([_response(content=""), _response(content="answer")])

    asyncio.run(agent.run(AgentInput(prompt="go")))

    assistant_turns = [m for m in agent.internal_history if m.get("role") == "assistant"]
    assert [m["content"] for m in assistant_turns] == ["answer"]


def test_exhausted_heartbeats_report_an_empty_response():
    """With nothing visible ever produced, the run ends empty so the caller retries."""
    agent, _sent = _agent([_response(content="")])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == ""
    assert _placeholders(agent.internal_history) == []


# ---------------------------------------------------------------------------
# 2. The marker is never accepted back as a final answer
# ---------------------------------------------------------------------------

def test_echoed_marker_is_treated_as_an_empty_response():
    """A model that read the marker from a legacy state must not publish it."""
    agent, _sent = _agent([
        _response(content=EMPTY_RESPONSE_PLACEHOLDER),
        _response(content="the real answer"),
    ])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "the real answer"
    assert _placeholders(agent.internal_history) == []


def test_marker_is_matched_regardless_of_case_and_padding():
    agent, _sent = _agent([_response(content="  (Removed: Empty Response)  \n")])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == ""


def test_a_reply_merely_mentioning_the_marker_is_still_a_real_answer():
    """The guard recognises the marker alone, not any text that quotes it."""
    text = f"I previously answered {EMPTY_RESPONSE_PLACEHOLDER}, which was a bug."
    agent, _sent = _agent([_response(content=text)])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == text


def test_load_state_drops_legacy_marker_turns():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")

    agent.load_state({
        "internal_history": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": EMPTY_RESPONSE_PLACEHOLDER},
            {"role": "system", "content": "SYSTEM ALERT: You returned no text."},
            {"role": "assistant", "content": "answer"},
        ],
        "recursive_summary": "summary",
    })

    assert agent.internal_history == [
        {"role": "user", "content": "go"},
        {"role": "system", "content": "SYSTEM ALERT: You returned no text."},
        {"role": "assistant", "content": "answer"},
    ]
    assert agent.recursive_summary == "summary"


def test_seeding_skips_a_marker_persisted_as_a_chat_message():
    agent = MemGPTAgentBlock(name="chat_orchestrator", model="fake/model")
    project = SimpleNamespace(history=[
        {"role": "user", "content": "check the article"},
        {"role": "assistant", "content": EMPTY_RESPONSE_PLACEHOLDER},
        {"role": "user", "content": "and now?"},
    ])

    seed_chat_orchestrator_history(agent, project)

    assert agent.internal_history == [
        {"role": "user", "content": "check the article"},
        {"role": "user", "content": "and now?"},
    ]


# ---------------------------------------------------------------------------
# 3. Opt-in: publish the reasoning when the visible channel came back empty
# ---------------------------------------------------------------------------

def test_reasoning_is_not_published_by_default():
    agent, sent = _agent([
        _response(content="", reasoning="## Report\nthe whole answer"),
        _response(content="the visible answer"),
    ])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "the visible answer"
    assert len(sent) == 2


def test_reasoning_fallback_publishes_the_reasoning_and_stops():
    agent, sent = _agent(
        [_response(content="", reasoning="## Report\nthe whole answer")],
        empty_response_reasoning_fallback=True,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "## Report\nthe whole answer"
    assert len(sent) == 1, "the answer was already written; no heartbeat should be spent"
    assert agent.internal_history[-1] == {
        "role": "assistant",
        "content": "## Report\nthe whole answer",
    }


def test_reasoning_fallback_also_covers_inline_think_tags():
    """Providers that inline reasoning in content reach the same state."""
    agent, _sent = _agent(
        [_response(content="<think>the whole answer</think>")],
        empty_response_reasoning_fallback=True,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "the whole answer"


def test_reasoning_fallback_still_needs_reasoning_to_exist():
    """Nothing was written anywhere: the run must not invent a response."""
    agent, _sent = _agent(
        [_response(content="", reasoning="   ")],
        empty_response_reasoning_fallback=True,
        max_heartbeats=1,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == ""


def test_reasoning_fallback_applies_with_no_heartbeats_left():
    """The last call is exactly when re-asking is impossible."""
    agent, sent = _agent(
        [_response(content="", reasoning="the whole answer")],
        empty_response_reasoning_fallback=True,
        max_heartbeats=0,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "the whole answer"
    assert len(sent) == 1


def test_reasoning_fallback_does_not_republish_the_marker():
    """An echoed marker is not an answer, whichever channel carried it."""
    agent, _sent = _agent(
        [_response(content=EMPTY_RESPONSE_PLACEHOLDER, reasoning=EMPTY_RESPONSE_PLACEHOLDER)],
        empty_response_reasoning_fallback=True,
        max_heartbeats=1,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == ""
