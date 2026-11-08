"""MemGPTAgentBlock compaction: one structured summary replaces the whole context.

Compaction is the Claude Code-style counterpart to FIFO eviction: the model is
asked for a sectioned summary (analysis scratchpad discarded), and the context
restarts from a single user message that says the session continues from a
previous conversation. It runs on request (``compact()``) or automatically when
the window crosses ``auto_compact_threshold``.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput
from agenticblocks.blocks.llm.compaction import (
    CONTINUATION_PREAMBLE,
    CONTINUATION_RESUME_INSTRUCTION,
    CompactionError,
    build_compaction_request,
    extract_summary,
    format_transcript,
)
from agenticblocks.blocks.llm.memgpt_agent import DEFAULT_RECURSIVE_SUMMARY, MemGPTAgentBlock


def _answer(content, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


SUMMARY_REPLY = (
    "<analysis>scratch work the user must never see</analysis>\n"
    "<summary>1. Primary Request and Intent: write the report.</summary>"
)


def _is_summary_request(messages):
    return any("<conversation>" in str(m.get("content", "")) for m in messages)


def _history(turns=6, words=10):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "word " * words}
        for i in range(turns)
    ]


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_extract_summary_drops_analysis_and_keeps_summary_block():
    assert extract_summary(SUMMARY_REPLY) == "1. Primary Request and Intent: write the report."


def test_extract_summary_without_tags_uses_text_minus_analysis():
    assert extract_summary("<analysis>x</analysis>\nPlain summary.") == "Plain summary."
    assert extract_summary("Just text.") == "Just text."


def test_extract_summary_unclosed_analysis_yields_nothing():
    assert extract_summary("<analysis>never closed, the summary was cut off") == ""


def test_transcript_renders_tool_calls_results_and_attachments():
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "print('hi')"},
        {"role": "assistant", "content": "It prints hi."},
    ]
    text = format_transcript(history, previous_summary="older facts")
    assert "older facts" in text
    assert "[image attachment]" in text
    assert '[assistant tool call] read_file({"path": "a.py"})' in text
    assert "[tool result: read_file]\nprint('hi')" in text
    assert "It prints hi." in text


def test_request_carries_custom_instructions_and_no_tool_messages():
    request = build_compaction_request(_history(2), custom_instructions="focus on the API")
    assert [m["role"] for m in request] == ["system", "user"]
    assert request[1]["content"].rstrip().endswith("focus on the API")


# ── manual compaction ────────────────────────────────────────────────────────

def test_compact_replaces_history_with_continuation_message():
    agent = MemGPTAgentBlock(name="o", system_prompt="s", max_context_tokens=100_000)
    agent.internal_history = _history()
    agent.recursive_summary = "evicted earlier"
    sent = {}

    async def fake(messages, **kwargs):
        sent["messages"] = messages
        sent["kwargs"] = kwargs
        return _answer(SUMMARY_REPLY)

    agent._acompletion = fake
    agent.model_kargs = {"stream": True, "temperature": 0.1}
    events = []
    agent.on_compaction = events.append

    result = asyncio.run(agent.compact("keep the numbers"))

    assert [m["role"] for m in agent.internal_history] == ["user"]
    content = agent.internal_history[0]["content"]
    assert content.startswith(CONTINUATION_PREAMBLE)
    assert "write the report" in content
    assert "scratch work" not in content
    # A manual compaction is followed by the user's own next message.
    assert CONTINUATION_RESUME_INSTRUCTION not in content
    assert agent.recursive_summary == DEFAULT_RECURSIVE_SUMMARY
    # The earlier rolling summary is folded into the new one.
    assert "evicted earlier" in sent["messages"][1]["content"]
    assert "keep the numbers" in sent["messages"][1]["content"]
    # Never streamed into the answer channel, never able to call tools.
    assert "stream" not in sent["kwargs"] and "tools" not in sent["kwargs"]
    assert sent["kwargs"]["temperature"] == 0.1
    assert [e.phase for e in events] == ["started", "completed"]
    assert result.trigger == "manual"
    assert result.messages_compacted == 6
    assert result.post_tokens < result.pre_tokens


def _failing_agent(reply=None, exc=None, **kwargs):
    agent = MemGPTAgentBlock(name="o", system_prompt="s", **kwargs)

    async def fake(_messages, **_kw):
        if exc:
            raise exc
        return _answer(reply)

    agent._acompletion = fake
    return agent


@pytest.mark.parametrize("reply,exc", [("", None), ("<analysis>only</analysis>", None), (None, RuntimeError("boom"))])
def test_failed_compaction_leaves_context_untouched(reply, exc):
    agent = _failing_agent(reply=reply, exc=exc, max_context_tokens=100_000)
    agent.internal_history = _history()
    before = [dict(m) for m in agent.internal_history]
    events = []
    agent.on_compaction = events.append

    with pytest.raises(CompactionError):
        asyncio.run(agent.compact())

    assert agent.internal_history == before
    assert events[-1].phase == "failed" and events[-1].error


def test_nothing_to_compact_is_an_error():
    agent = _failing_agent(reply=SUMMARY_REPLY)
    with pytest.raises(CompactionError, match="no conversation"):
        asyncio.run(agent.compact())


def test_unanswered_tool_call_cannot_be_compacted():
    agent = _failing_agent(reply=SUMMARY_REPLY)
    agent.internal_history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}},
        ]},
    ]
    with pytest.raises(CompactionError, match="tool call"):
        asyncio.run(agent.compact())


def test_request_larger_than_window_fails_instead_of_cutting():
    agent = _failing_agent(reply=SUMMARY_REPLY, max_context_tokens=500)
    agent.internal_history = _history(turns=10, words=200)
    with pytest.raises(CompactionError, match="too long to compact"):
        asyncio.run(agent.compact())
    assert len(agent.internal_history) == 10


# ── automatic compaction ─────────────────────────────────────────────────────

def _auto_agent(threshold, window=100_000, summary_reply=SUMMARY_REPLY):
    agent = MemGPTAgentBlock(
        name="o", system_prompt="s", max_context_tokens=window,
        eviction_threshold=0.95, memory_pressure_threshold=0.99,
        auto_compact_threshold=threshold,
    )
    calls = {"summary": 0, "turn": []}

    async def fake(messages, **_kw):
        if _is_summary_request(messages):
            calls["summary"] += 1
            if isinstance(summary_reply, Exception):
                raise summary_reply
            return _answer(summary_reply)
        calls["turn"].append(messages)
        return _answer("Done.")

    async def no_eviction(_messages):
        raise AssertionError("compaction must act before eviction")

    agent._acompletion = fake
    agent._summarize = no_eviction
    return agent, calls


def test_auto_compaction_fires_past_threshold_and_resumes_the_task():
    agent, calls = _auto_agent(0.5)
    agent.internal_history = _history(turns=40, words=1500)  # ~60K+ tokens of 100K
    events = []
    agent.on_compaction = events.append

    asyncio.run(agent.run(input=AgentInput(prompt="next step")))

    assert calls["summary"] == 1
    first_turn_request = calls["turn"][0]
    continuation = [m for m in first_turn_request if m.get("role") == "user"]
    # The current request is folded into the summary like Claude Code does, and
    # the model is told to carry on instead of asking what to do.
    assert len(continuation) == 1
    assert CONTINUATION_RESUME_INSTRUCTION in continuation[0]["content"]
    assert [e.phase for e in events] == ["started", "completed"]
    assert events[-1].trigger == "auto"


def test_auto_compaction_below_threshold_does_nothing():
    agent, calls = _auto_agent(0.9)
    agent.internal_history = _history()
    asyncio.run(agent.run(input=AgentInput(prompt="hi")))
    assert calls["summary"] == 0


def test_auto_compaction_disabled_by_default_and_by_one():
    for threshold in (None, 1.0):
        agent, calls = _auto_agent(threshold)
        agent._summarize = lambda _m: asyncio.sleep(0, result="s")
        agent.internal_history = _history(turns=40, words=1500)
        asyncio.run(agent.run(input=AgentInput(prompt="hi")))
        assert calls["summary"] == 0


def test_auto_compaction_disabled_when_window_unknown():
    agent, calls = _auto_agent(0.1)
    agent.max_context_tokens = None
    agent.internal_history = _history(turns=40, words=1500)
    asyncio.run(agent.run(input=AgentInput(prompt="hi")))
    assert calls["summary"] == 0


def test_failed_auto_compaction_is_not_retried_every_heartbeat():
    agent, calls = _auto_agent(0.5, summary_reply=RuntimeError("provider down"))
    agent.eviction_threshold = 1.0
    agent.internal_history = _history(turns=40, words=1500)
    tool_call = SimpleNamespace(
        id="c1", function=SimpleNamespace(name="new_heartbeat", arguments="{}"),
    )
    replies = iter([_answer("", [tool_call]), _answer("Done.")])

    async def fake(messages, **_kw):
        if _is_summary_request(messages):
            calls["summary"] += 1
            raise RuntimeError("provider down")
        return next(replies)

    agent._acompletion = fake
    out = asyncio.run(agent.run(input=AgentInput(prompt="hi")))

    assert out.response == "Done."
    assert calls["summary"] == 1
    # The original history is still there: the failure changed nothing.
    assert len(agent.internal_history) > 40


# ── host extension points ────────────────────────────────────────────────────

def _summary_agent(window=100_000):
    agent = MemGPTAgentBlock(name="o", system_prompt="s", max_context_tokens=window)
    agent.internal_history = _history()
    sent = []

    async def fake(messages, **_kw):
        sent.append(messages)
        return _answer(SUMMARY_REPLY)

    agent._acompletion = fake
    return agent, sent


def test_request_hook_adds_instructions_and_metadata_reaches_the_event():
    agent, sent = _summary_agent()
    seen = {}

    def before(request):
        seen["history"] = len(request.history)
        request.custom_instructions += "\nkeep the numbers"
        request.metadata["hooks"] = ["ran"]

    agent.on_compaction_request = before
    event = asyncio.run(agent.compact("focus"))

    assert seen["history"] == 6
    assert "focus\nkeep the numbers" in sent[0][1]["content"]
    assert event.custom_instructions == "focus\nkeep the numbers"
    assert event.extra == {"hooks": ["ran"]}


def test_a_blocked_request_fails_and_reports_its_metadata():
    agent, sent = _summary_agent()
    events = []
    agent.on_compaction = events.append

    async def before(request):
        request.blocked_reason = "backup failed"
        request.metadata["hooks"] = ["blocked"]

    agent.on_compaction_request = before
    with pytest.raises(CompactionError, match="blocked: backup failed"):
        asyncio.run(agent.compact())
    assert sent == []
    assert len(agent.internal_history) == 6
    assert events[-1].phase == "failed" and events[-1].extra == {"hooks": ["blocked"]}


def test_context_hook_adds_text_after_the_summary_and_before_the_resume_line():
    agent, _sent = _summary_agent()

    def after(context):
        assert context.summary.startswith("1. Primary Request")
        assert len(context.history) == 6
        context.extra_context.append("FILE CONTENTS")
        context.metadata["restored_files"] = ["a.py"]

    agent.on_compacted_context = after
    event = asyncio.run(agent.compact(trigger="auto"))

    content = agent.internal_history[0]["content"]
    assert content.index("write the report") < content.index("FILE CONTENTS")
    assert content.index("FILE CONTENTS") < content.index(CONTINUATION_RESUME_INSTRUCTION)
    assert event.extra == {"restored_files": ["a.py"]}


def test_extra_context_that_does_not_fit_fails_and_restores_the_history():
    agent, _sent = _summary_agent(window=3_000)
    agent.internal_history = _history(turns=4, words=50)
    before = [dict(m) for m in agent.internal_history]
    agent.on_compacted_context = lambda context: context.extra_context.append("word " * 5000)

    with pytest.raises(CompactionError, match="would not fit"):
        asyncio.run(agent.compact())
    assert agent.internal_history == before


def test_an_exception_in_a_host_step_fails_the_compaction():
    agent, _sent = _summary_agent()

    def broken(_request):
        raise ValueError("hooks.json is invalid")

    agent.on_compaction_request = broken
    with pytest.raises(CompactionError, match="hooks.json is invalid"):
        asyncio.run(agent.compact())
    assert len(agent.internal_history) == 6
