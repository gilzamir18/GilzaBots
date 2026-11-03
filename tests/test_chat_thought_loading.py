"""Stored reasoning is read when it is opened, not when the chat is.

Reasoning is persisted once per streamed token, so a long chat holds hundreds
of thousands of rows: one measured chat carried 115,605 of them, and the
transcript endpoint shipped all of it — 24 MB — on every open, for text that
starts collapsed behind "AI Thoughts". The transcript now carries a count and
an id range per assistant message, and the panel fetches one window when the
user expands it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gilzabots.project import ProjectStore


@pytest.fixture
def chat(tmp_path):
    """A project whose chat holds two answered turns, each with its reasoning."""
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="p", mode="auto", model="m",
                           project_name="P", project_path=str(tmp_path))
    store.create_chat("p", "chat-p", "Chat")
    project = store.load("p")
    chat_id = project.current_chat_id

    def thought(text):
        store.append_activity(project, "thought", text, "chat_orchestrator")

    store.append_message(project, "user", "Primeira pergunta")
    thought("Thinking ")
    thought("about the first one.")
    store.append_message(project, "assistant", "Primeira resposta")
    store.append_message(project, "user", "Segunda pergunta")
    thought("Now ")
    thought("the second one.")
    store.append_message(project, "assistant", "Segunda resposta")
    store.save(project)
    return store, store.load(project.name, chat_id=chat_id), chat_id


def test_each_answer_reports_the_reasoning_it_owns(chat):
    store, project, chat_id = chat

    windows = store.thought_windows(project.name, chat_id, project.history)

    assert [w["count"] for w in windows] == [2, 2]
    # Anchored to the assistant messages, which is where the panel shows them.
    assert [project.history[w["index"]]["role"] for w in windows] == ["assistant", "assistant"]


def test_expanding_one_answer_reads_only_its_own_reasoning(chat):
    store, project, chat_id = chat

    windows = store.thought_windows(project.name, chat_id, project.history)
    first = store.thought_tail(project.name, chat_id, windows[0]["from_id"], windows[0]["to_id"])
    second = store.thought_tail(project.name, chat_id, windows[1]["from_id"], windows[1]["to_id"])

    assert first["content"] == "Thinking about the first one."
    assert second["content"] == "Now the second one."


def test_the_transcript_no_longer_carries_the_reasoning(chat):
    store, project, chat_id = chat

    activity = store.list_activity(project.name, chat_id, limit=1000,
                                   truncate_events=("stream_chunk",),
                                   exclude_events=("thought",))

    assert not [item for item in activity if item["event"] == "thought"]


def test_other_events_are_untouched_by_the_exclusion(chat):
    """Errors reach the transcript, and streamed text keeps its own tail limit."""
    store, project, chat_id = chat
    store.append_activity(project, "error", "boom", "chat_orchestrator")
    store.append_activity(project, "stream_chunk", "texto", "chat_orchestrator")

    activity = store.list_activity(project.name, chat_id, limit=1000,
                                   truncate_events=("stream_chunk",),
                                   exclude_events=("thought",))

    events = [item["event"] for item in activity]
    assert "error" in events and "stream_chunk" in events


def test_a_chat_with_no_reasoning_reports_none(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="q", mode="auto", model="m",
                           project_name="Q", project_path=str(tmp_path))
    store.create_chat("q", "chat-q", "Chat")
    project = store.load("q")
    store.append_message(project, "user", "oi")
    store.append_message(project, "assistant", "olá")
    store.save(project)

    assert store.thought_windows(project.name, project.current_chat_id, project.history) == []


def test_reasoning_produced_after_the_last_answer_is_not_lost(chat):
    """An interrupted turn reasons without answering; that still belongs to the chat."""
    store, project, chat_id = chat
    store.append_activity(project, "thought", " And more.", "chat_orchestrator")

    windows = store.thought_windows(project.name, chat_id, project.history)

    assert windows[-1]["count"] == 3
    tail = store.thought_tail(project.name, chat_id, windows[-1]["from_id"], windows[-1]["to_id"])
    assert tail["content"].endswith(" And more.")


def test_a_runaway_turn_costs_no_more_to_open_than_any_other(tmp_path):
    """The window is read backwards and stopped at the budget."""
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="r", mode="auto", model="m",
                           project_name="R", project_path=str(tmp_path))
    store.create_chat("r", "chat-r", "Chat")
    project = store.load("r")
    chat_id = project.current_chat_id
    store.append_message(project, "user", "faça algo")
    for index in range(5000):
        store.append_activity(project, "thought", f"chunk{index} ", "chat_orchestrator")
    store.append_message(project, "assistant", "pronto")
    store.save(project)
    project = store.load(project.name, chat_id=chat_id)

    window = store.thought_windows(project.name, chat_id, project.history)[0]
    assert window["count"] == 5000

    tail = store.thought_tail(project.name, chat_id, window["from_id"], window["to_id"],
                              max_tokens=500)

    assert tail["tokens"] <= 500
    assert tail["omitted_tokens"] > 0
    # It is the *recent* reasoning that is kept, and it reads as running text.
    assert tail["content"].endswith("chunk4999 ")
    assert "chunk0 " not in tail["content"]
