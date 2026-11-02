"""A sync pass must not freeze the app, and a turn's reasoning must not grow
the history by one row per token.

Measured on a real project: 950k activity rows (872k of them `thought` rows
averaging three characters), a 309 MB cloud export rebuilt on every sync pass,
and a single `json.dumps` that held the interpreter for about four seconds —
the server, the terminal and the window froze together. These tests cover the
three changes that answer it: the export is skipped when the history did not
move, it is serialized in pieces, and reasoning is stored in segments.
"""

import json
import os
import sqlite3
import threading
import time

import pytest

from opalatex import agent_stdin
from opalatex.cloud import chats
from opalatex.project import ProjectStore


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "sessions.db")


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return str(path)


def _project(db_path, name, project_path):
    store = ProjectStore(db_path)
    store.create(name, "plan", "ollama/test", project_name=name, project_path=project_path)
    store.create_chat(name, f"chat-{name}", "Chat")
    return store, store.load(name)


# ─── History revision ─────────────────────────────────────────────────────────

def test_every_kind_of_write_moves_the_projects_revision(db, project_dir, tmp_path):
    store, project = _project(db, "alpha", project_dir)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    _project(db, "beta", str(other_dir))

    def revision():
        return chats.history_revision("alpha", db)

    seen = [revision()]

    store.append_message(project, "user", "hello")
    seen.append(revision())
    store.append_activity(project, "thought", "reasoning", "chat_orchestrator")
    store.close_activity_connection()
    seen.append(revision())
    store.save_chat_agent_state("alpha", project.current_chat_id, {"internal_history": [1]})
    seen.append(revision())
    store.update_chat_core_memory("alpha", project.current_chat_id, "memory")
    seen.append(revision())
    store.clear_project_history("alpha", project.current_chat_id)
    seen.append(revision())

    assert seen == sorted(seen) and len(set(seen)) == len(seen)

    # Another project's writes leave this one alone.
    before = revision()
    beta = store.load("beta")
    store.append_message(beta, "user", "unrelated")
    assert revision() == before


def test_a_write_through_another_connection_moves_it_too(db, project_dir):
    """The cloud merge writes through its own connection, not the store."""
    _project(db, "alpha", project_dir)
    before = chats.history_revision("alpha", db)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO project_history (project, chat_id, timestamp, role, content) "
            "VALUES ('alpha', 'chat-alpha', '2026-01-01T00:00:00+00:00', 'user', 'merged')"
        )

    assert chats.history_revision("alpha", db) > before


def test_renaming_a_project_moves_both_names(db, project_dir):
    store, project = _project(db, "alpha", project_dir)
    store.append_message(project, "user", "hello")

    assert store.rename("alpha", "gamma")

    assert chats.history_revision("gamma", db) > 0


# ─── Skipping an unchanged export ─────────────────────────────────────────────

def _counting_export(monkeypatch):
    calls = []
    original = chats.export_project

    def counted(name, db_path=None):
        calls.append(name)
        return original(name, db_path)

    monkeypatch.setattr(chats, "export_project", counted)
    return calls


def test_an_unchanged_history_is_not_exported_again(db, project_dir, monkeypatch):
    store, project = _project(db, "alpha", project_dir)
    store.append_message(project, "user", "hello")
    calls = _counting_export(monkeypatch)

    assert chats.refresh_export("alpha", project_dir, db) is True
    mtime = os.stat(chats.export_path(project_dir)).st_mtime_ns
    assert chats.refresh_export("alpha", project_dir, db) is False

    assert calls == ["alpha"]
    assert os.stat(chats.export_path(project_dir)).st_mtime_ns == mtime


def test_a_new_message_is_exported(db, project_dir, monkeypatch):
    store, project = _project(db, "alpha", project_dir)
    chats.refresh_export("alpha", project_dir, db)
    calls = _counting_export(monkeypatch)

    store.append_message(project, "user", "a new message")

    assert chats.refresh_export("alpha", project_dir, db) is True
    assert calls == ["alpha"]
    assert "a new message" in [m["content"] for m in chats.read_export(project_dir)["messages"]]


def test_a_soft_deleted_message_is_exported(db, project_dir):
    """Editing a message marks the tail in place; no row is added."""
    store, project = _project(db, "alpha", project_dir)
    first_id = store.append_message(project, "user", "first")
    store.append_message(project, "assistant", "answer")
    chats.refresh_export("alpha", project_dir, db)

    store.supersede_chat_history_from_message(
        "alpha", project.current_chat_id, message_id=first_id, superseded_by="edit"
    )

    assert chats.refresh_export("alpha", project_dir, db) is True
    marked = [m for m in chats.read_export(project_dir)["messages"] if m["superseded_by"]]
    assert marked


def test_a_replaced_or_missing_file_is_rebuilt_even_when_the_history_did_not_move(db, project_dir, monkeypatch):
    store, project = _project(db, "alpha", project_dir)
    store.append_message(project, "user", "hello")
    chats.refresh_export("alpha", project_dir, db)
    target = chats.export_path(project_dir)
    expected = open(target, encoding="utf-8").read()
    calls = _counting_export(monkeypatch)

    # A download put the other machine's document in its place.
    with open(target, "w", encoding="utf-8") as handle:
        handle.write('{"version": 1, "messages": []}')
    assert chats.refresh_export("alpha", project_dir, db) is True
    assert open(target, encoding="utf-8").read() == expected

    os.remove(target)
    assert chats.refresh_export("alpha", project_dir, db) is True
    assert os.path.isfile(target)
    assert calls == ["alpha", "alpha"]


def test_a_write_during_the_export_is_picked_up_by_the_next_pass(db, project_dir, monkeypatch):
    """The counter is read before the rows: a late write leaves the mark behind."""
    store, project = _project(db, "alpha", project_dir)
    original = chats.export_project
    landed = []

    def export_with_a_concurrent_write(name, db_path=None):
        if not landed:
            landed.append(store.append_message(project, "user", "arrived mid-export"))
        return original(name, db_path)

    monkeypatch.setattr(chats, "export_project", export_with_a_concurrent_write)
    chats.refresh_export("alpha", project_dir, db)
    monkeypatch.setattr(chats, "export_project", original)

    # The first pass may or may not have seen the row; the mark must not claim it.
    chats.refresh_export("alpha", project_dir, db)
    contents = [m["content"] for m in chats.read_export(project_dir)["messages"]]
    assert "arrived mid-export" in contents


# ─── Serializing in pieces ────────────────────────────────────────────────────

def _payload():
    return {
        "version": 1,
        "project": "alpha",
        "settings": {"model": "ollama/test", "compile_on_save_full": 0, "ratio": 0.1},
        "messages": [
            {"role": "user", "content": f"mensagem {i} — ação, \"aspas\"\n\tlinha", "deleted_at": ""}
            for i in range(300)
        ],
        "activity": [
            {"event": "thought", "content": "ünïcödé 🙂 " * 3, "payload": '{"agent": "x"}', "n": None}
            for _ in range(200)
        ],
        "chats": [],
    }


def test_pieces_reproduce_json_dumps_exactly(monkeypatch):
    monkeypatch.setattr(chats, "_EXPORT_WRITE_CHARS", 256)
    payload = _payload()

    pieces = list(chats._serialized_chunks(payload))

    assert len(pieces) > 10
    assert "".join(pieces) == json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def test_an_identical_document_leaves_the_file_and_no_temp_behind(project_dir, monkeypatch):
    monkeypatch.setattr(chats, "_EXPORT_WRITE_CHARS", 256)
    payload = _payload()

    assert chats.write_export(project_dir, payload) is True
    target = chats.export_path(project_dir)
    mtime = os.stat(target).st_mtime_ns

    assert chats.write_export(project_dir, payload) is False
    assert os.stat(target).st_mtime_ns == mtime
    assert [name for name in os.listdir(os.path.dirname(target)) if name.endswith(".tmp")] == []


@pytest.mark.parametrize("change", ["tail", "shorter", "longer"])
def test_a_difference_anywhere_is_written(project_dir, monkeypatch, change):
    monkeypatch.setattr(chats, "_EXPORT_WRITE_CHARS", 256)
    payload = _payload()
    chats.write_export(project_dir, payload)

    changed = _payload()
    if change == "tail":
        changed["settings"]["ratio"] = 0.2  # sorted last: only the end differs
    elif change == "shorter":
        changed["messages"] = changed["messages"][:-1]
    else:
        changed["messages"].append({"role": "user", "content": "extra", "deleted_at": ""})

    assert chats.write_export(project_dir, changed) is True
    with open(chats.export_path(project_dir), encoding="utf-8") as handle:
        assert json.load(handle) == changed


def test_serializing_a_large_export_lets_other_threads_run():
    """The whole point of the pieces: no single call holds the interpreter."""
    payload = {
        "activity": [
            {"chat_id": "c", "timestamp": f"2026-11-02T12:00:00.{i:06d}+00:00", "event": "thought",
             "content": " token", "payload": '{"agent": "chat_orchestrator"}'}
            for i in range(150_000)
        ]
    }
    stop = threading.Event()
    worst = [0.0]

    def probe():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            worst[0] = max(worst[0], now - last)
            last = now

    thread = threading.Thread(target=probe)
    thread.start()
    try:
        for _ in chats._serialized_chunks(payload):
            pass
    finally:
        stop.set()
        thread.join()

    # json.dumps holds it for the whole document on Python 3.14 (~0.5 s here).
    assert worst[0] < 0.2


# ─── Reasoning segments ───────────────────────────────────────────────────────

@pytest.fixture
def turn(tmp_path, monkeypatch):
    """A real store wired as the active turn, with the event hook silenced."""
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    store.create(name="p", mode="auto", model="m", project_name="P", project_path=str(tmp_path))
    store.create_chat("p", "chat-p", "Chat")
    project = store.load("p")
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "event_hook", lambda _payload: None)
    agent_stdin.flush_thought_segments()
    yield store, project
    agent_stdin.flush_thought_segments()
    store.close_activity_connection()


def _thoughts(store, project):
    return [
        row for row in store.list_activity(project.name, project.current_chat_id, limit=None)
        if row["event"] == "thought"
    ]


def test_a_turns_reasoning_is_stored_as_one_segment_with_its_line_breaks(turn):
    store, project = turn
    tokens = ["The", " user", " asks", ".", "\n\n", "First", " step", "."]
    for token in tokens:
        agent_stdin.print_event("thought", {"content": token, "agent": "chat_orchestrator"})

    assert _thoughts(store, project) == []  # still a segment in memory
    agent_stdin.flush_thought_segments()

    rows = _thoughts(store, project)
    assert len(rows) == 1
    # The "\n\n" token used to be dropped as blank, gluing paragraphs together.
    assert rows[0]["content"] == "".join(tokens)


def test_a_segment_is_stored_once_it_reaches_its_size(turn, monkeypatch):
    store, project = turn
    monkeypatch.setattr(agent_stdin, "THOUGHT_SEGMENT_MAX_CHARS", 10)
    for _ in range(25):
        agent_stdin.print_event("thought", {"content": "abcd", "agent": "chat_orchestrator"})

    stored = _thoughts(store, project)
    assert [len(row["content"]) for row in stored] == [12] * 8
    agent_stdin.flush_thought_segments()
    assert "".join(row["content"] for row in _thoughts(store, project)) == "abcd" * 25


def test_other_activity_lands_after_the_reasoning_before_it(turn):
    store, project = turn
    agent_stdin.print_event("thought", {"content": "Plan it.", "agent": "chat_orchestrator"})
    agent_stdin.print_event("stream_chunk", {"content": "Answer", "agent": "chat_orchestrator"})
    agent_stdin.print_event("thought", {"content": "Check it.", "agent": "chat_orchestrator"})
    agent_stdin.flush_thought_segments()

    rows = store.list_activity(project.name, project.current_chat_id, limit=None)
    assert [(row["event"], row["content"]) for row in rows] == [
        ("thought", "Plan it."),
        ("stream_chunk", "Answer"),
        ("thought", "Check it."),
    ]


def test_a_late_segment_still_belongs_to_the_answer_it_preceded(turn):
    """Pairing is by timestamp: the segment keeps the time of its last token."""
    store, project = turn
    store.append_message(project, "user", "question")
    agent_stdin.print_event("thought", {"content": "Reasoning.", "agent": "chat_orchestrator"})
    time.sleep(0.01)
    # An answer saved by a path that did not flush first (an interrupted turn's
    # marker, for instance), and only then the turn's final flush.
    store.append_message(project, "assistant", "answer")
    agent_stdin.flush_thought_segments()
    store.append_message(project, "user", "next question")
    store.append_message(project, "assistant", "next answer")

    history = store.load(project.name, chat_id=project.current_chat_id).history
    windows = store.thought_windows(project.name, project.current_chat_id, history)

    assert [history[w["index"]]["content"] for w in windows] == ["answer"]


def test_status_lines_and_each_agents_reasoning_stay_apart(turn):
    store, project = turn
    agent_stdin.print_event("thought", {"content": "orchestrator ", "agent": "chat_orchestrator"})
    agent_stdin.print_event("thought", {"content": "worker ", "agent": "worker:latex"})
    agent_stdin.print_event("thought", {"content": "thinks", "agent": "chat_orchestrator"})
    agent_stdin._persist_activity_event(
        "thought", {"content": "Decided to execute tool 'x'.", "agent": "chat_orchestrator", "auxiliary": True}
    )
    agent_stdin.flush_thought_segments()

    rows = _thoughts(store, project)
    by_content = {row["content"]: row for row in rows}
    assert set(by_content) == {"orchestrator thinks", "worker ", "Decided to execute tool 'x'."}
    assert by_content["Decided to execute tool 'x'."]["payload"].get("auxiliary") is True


def test_the_segment_keeps_the_turns_running_token_count(turn, monkeypatch):
    store, project = turn
    from opalatex.thought_context import ThoughtTokenMeter

    monkeypatch.setattr(agent_stdin, "_ACTIVE_THOUGHT_METER", ThoughtTokenMeter(""))
    for token in ["one", " two", " three"]:
        agent_stdin.print_event("thought", {"content": token, "agent": "chat_orchestrator"})
    agent_stdin.flush_thought_segments()

    (row,) = _thoughts(store, project)
    assert row["payload"]["thought_tokens"] == agent_stdin._ACTIVE_THOUGHT_METER.total


def test_a_preview_cuts_inside_a_segment_and_keeps_its_end(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    store.create(name="p", mode="auto", model="m", project_name="P", project_path=str(tmp_path))
    store.create_chat("p", "chat-p", "Chat")
    project = store.load("p")
    store.append_message(project, "user", "question")
    store.append_activity(project, "thought", "old " * 500, "chat_orchestrator")
    store.append_activity(project, "thought", "a" * 3000 + "THE END", "chat_orchestrator")
    store.append_message(project, "assistant", "answer")
    store.close_activity_connection()
    project = store.load("p")
    window = store.thought_windows("p", project.current_chat_id, project.history)[0]

    tail = store.thought_tail("p", project.current_chat_id, window["from_id"], window["to_id"], max_tokens=100)

    assert tail["content"].endswith("THE END")
    assert tail["tokens"] <= 100
    assert len(tail["content"]) == 400
    assert "old" not in tail["content"]
    assert tail["omitted_tokens"] > 0


@pytest.mark.asyncio
async def test_a_real_turn_stores_its_reasoning_as_a_segment_owned_by_its_answer(tmp_path, monkeypatch):
    """End to end through `handle_run`: tokens published live, one row stored."""
    from types import SimpleNamespace

    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    store.create(name="p", mode="auto", model="fake/model", project_name="P", project_path=str(tmp_path))
    store.create_chat("p", "chat-p", "Chat")
    project = store.load("p")
    published = []
    tokens = ["Read", " the", " file", ".", "\n", "Then", " answer", "."]

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            for token in tokens:
                agent_stdin.publish_thought(token, "chat_orchestrator")
            return SimpleNamespace(response="Done.")

    monkeypatch.setattr(agent_stdin, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "event_hook", published.append)

    await agent_stdin.handle_run({"agent": "chat_orchestrator", "prompt": "do it"})

    live = [e["content"] for e in published if e.get("event") == "thought" and not e.get("auxiliary")]
    assert live == tokens  # the live stream still gets every token

    history = store.load("p", chat_id=project.current_chat_id).history
    rows = [r for r in store.list_activity("p", project.current_chat_id, limit=None)
            if r["event"] == "thought" and not r["payload"].get("auxiliary")]
    assert [r["content"] for r in rows] == ["".join(tokens)]
    windows = store.thought_windows("p", project.current_chat_id, history)
    assert [history[w["index"]]["content"] for w in windows] == ["Done."]
