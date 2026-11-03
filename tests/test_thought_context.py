"""Agent reasoning is stored whole, counted in tokens, and summarized to resume."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agenticblocks.blocks.llm.tokens import count_text_tokens
from gilzabots import thought_context as tc
from gilzabots.project import ProjectStore
from gilzabots.ui_settings import clamp_thought_context_tokens


def test_text_tokens_are_counted_and_empty_text_is_zero():
    assert count_text_tokens("gpt-4o", "") == 0
    assert count_text_tokens("gpt-4o", "hello world") > 0
    # A model with no registered tokenizer still gets a usable number.
    assert count_text_tokens("nonexistent/model", "x" * 400) > 0


def test_the_meter_counts_in_batches_and_estimates_the_uncounted_tail(monkeypatch):
    calls = []
    monkeypatch.setattr(tc, "count_text_tokens", lambda model, text: calls.append(text) or 7)
    meter = tc.ThoughtTokenMeter("m")
    assert meter.add("abcd") == 1          # estimate only, no tokenizer call
    assert calls == []
    meter.add("x" * tc._COUNT_BATCH_CHARS)
    assert len(calls) == 1                  # one exact count for the batch
    assert meter.total == 7


def test_the_configured_size_is_clamped():
    assert clamp_thought_context_tokens(None) == 32000
    assert clamp_thought_context_tokens(5) == 1000
    assert clamp_thought_context_tokens(10**9) == 1_000_000


def _store_with_turn(tmp_path):
    store = ProjectStore(str(tmp_path / "sessions.db"))
    project = store.create("p", "auto", "fake/model", project_name="p", project_path=str(tmp_path))
    store.create_chat("p", "chat-p", "Chat")
    project = store.load("p")
    store.append_message(project, "user", "old request")
    store.append_activity(project, "thought", content="stale reasoning", agent="chat_orchestrator")
    store.append_message(project, "assistant", "old answer")
    import time
    time.sleep(0.01)
    store.append_message(project, "user", "new request")
    time.sleep(0.01)
    store.append_activity(project, "thought", content="step one. ", agent="chat_orchestrator")
    store.append_activity(project, "stream_chunk", content="Working on it", agent="chat_orchestrator")
    store.append_activity(project, "thought", content="step two.", agent="chat_orchestrator")
    store.append_message(project, "assistant", "[INTERRUPTED] The user interrupted the agent execution.")
    store.close_activity_connection()
    return store, store.load("p", chat_id=project.current_chat_id)


def test_only_the_interrupted_turn_is_collected(tmp_path):
    store, project = _store_with_turn(tmp_path)
    reasoning, visible = tc.collect_interrupted_turn(store, project.name, project.current_chat_id, project.history)
    assert reasoning == "step one. step two."
    assert visible == "Working on it"


@pytest.mark.asyncio
async def test_reasoning_that_fits_is_replayed_whole(tmp_path):
    store, project = _store_with_turn(tmp_path)
    summarize = AsyncMock(side_effect=AssertionError("must not summarize"))
    prompt = await tc.resume_prompt_for_chat(
        store, project, model="gpt-4o", limit_tokens=1000, summarize_piece=summarize, piece_tokens=1000,
    )
    assert "step one. step two." in prompt
    assert "Working on it" in prompt
    assert "Summary of" not in prompt


@pytest.mark.asyncio
async def test_reasoning_beyond_the_size_is_replaced_by_a_summary(tmp_path, monkeypatch):
    store, project = _store_with_turn(tmp_path)
    monkeypatch.setattr(tc, "count_text_tokens", lambda model, text: len(text))
    announced = []
    prompt = await tc.resume_prompt_for_chat(
        store, project, model="m", limit_tokens=10,
        summarize_piece=AsyncMock(return_value="short"), piece_tokens=1000,
        on_summarizing=announced.append,
    )
    assert "step one" not in prompt
    assert "short" in prompt
    assert "Summary of 19 tokens" in prompt
    assert announced == [19]


@pytest.mark.asyncio
async def test_long_reasoning_is_summarized_in_pieces_until_it_fits(monkeypatch):
    monkeypatch.setattr(tc, "count_text_tokens", lambda model, text: len(text))
    pieces = []

    async def summarize(piece):
        pieces.append(piece)
        return piece[: len(piece) // 4]

    text = "\n\n".join(["p" * 100] * 8)
    result = await tc.summarize_reasoning(text, model="m", limit_tokens=150, piece_tokens=200, summarize_piece=summarize)
    assert len(result) <= 150
    assert len(pieces) > 1
    assert all(len(p) <= 260 for p in pieces)


@pytest.mark.asyncio
async def test_a_summary_that_never_fits_fails_instead_of_being_cut(monkeypatch):
    monkeypatch.setattr(tc, "count_text_tokens", lambda model, text: len(text))
    with pytest.raises(tc.ThoughtSummaryError):
        await tc.summarize_reasoning(
            "x" * 500, model="m", limit_tokens=10, piece_tokens=1000,
            summarize_piece=AsyncMock(return_value="y" * 400),
        )


@pytest.mark.asyncio
async def test_thought_settings_endpoint_round_trips(monkeypatch, tmp_path):
    import gilzabots.ui_settings as ui
    from gilzabots.ide_server import AsyncHTTPServer

    monkeypatch.setattr(ui, "_SETTINGS_PATH", tmp_path / "ui_settings.json")
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )
    await server.route_api("POST", "/api/settings/thoughts", {}, {}, json.dumps({"thought_context_tokens": 64000}).encode(), AsyncMock())
    await server.route_api("GET", "/api/settings/thoughts", {}, {}, b"", AsyncMock())
    assert responses[-1] == (200, {
        "thought_context_tokens": 64000,
        "chat_thought_preview_tokens": 1000,
    })

    # The chat preview is a second, independent size: the chat shows the last
    # lines of the running reasoning and the Agent Thinking panel holds all of
    # it. Sending one must not reset the other.
    await server.route_api("POST", "/api/settings/thoughts", {}, {}, json.dumps({"chat_thought_preview_tokens": 2500}).encode(), AsyncMock())
    await server.route_api("GET", "/api/settings/thoughts", {}, {}, b"", AsyncMock())
    assert responses[-1] == (200, {
        "thought_context_tokens": 64000,
        "chat_thought_preview_tokens": 2500,
    })


def test_recorded_reasoning_is_never_discarded_and_carries_its_token_count(monkeypatch):
    import gilzabots.agent_stdin as stdin_mod

    chunks = []
    events = []
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_CHUNKS", chunks)
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_METER", tc.ThoughtTokenMeter("m"))
    monkeypatch.setattr(stdin_mod, "event_hook", events.append)
    monkeypatch.setattr(stdin_mod, "_persist_activity_event", lambda *_a, **_k: None)

    repeated = r"–" * 80          # the kind of stream the old guard dropped
    long_chunk = "z" * 10_000
    stdin_mod.print_event("thought", {"content": repeated, "agent": "chat_orchestrator"})
    stdin_mod.print_event("thought", {"content": long_chunk, "agent": "chat_orchestrator"})

    assert chunks == [repeated, long_chunk]
    thought_events = [e for e in events if e["event"] == "thought" and e["content"] in (repeated, long_chunk)]
    assert len(thought_events) == 2
    assert thought_events[1]["thought_tokens"] >= thought_events[0]["thought_tokens"] > 0
