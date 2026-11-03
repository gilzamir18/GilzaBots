"""Nothing the agent produced is lost, whatever ends the turn.

Regression for a chat that came back holding only the user's question. A system
standby killed the connection to the provider mid-answer (`Streaming error:
Connection lost`, then `Could not connect to ollama/...`); the error handler
stored nothing, so the streamed answer survived only as `project_activity`
fragments and "Continue" had no assistant turn to resume from. The interrupted
path had the same defect in a smaller shape: it stored its marker *alone* and
dropped the partial answer that was on screen.
"""

import pytest

import gilzabots.agent_stdin as stdin_mod


class FakeStore:
    def __init__(self):
        self.messages = []
        self.saves = 0

    def append_message(self, _project, role, content, **_kwargs):
        self.messages.append((role, content))
        return len(self.messages)

    def save(self, _project):
        self.saves += 1


@pytest.fixture
def turn(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(stdin_mod, "current_store", store)
    monkeypatch.setattr(stdin_mod, "current_project", object())
    monkeypatch.setattr(stdin_mod, "_ACTIVE_VISIBLE_CHUNKS", [])
    return store


def test_a_failed_turn_keeps_the_text_it_had_already_streamed(turn):
    stdin_mod._ACTIVE_VISIBLE_CHUNKS.extend(["Vou começar ", "pela seção 2."])

    stdin_mod._persist_unfinished_turn("chat_orchestrator", stdin_mod.TURN_FAILED_MARKER)

    assert len(turn.messages) == 1
    role, content = turn.messages[0]
    assert role == "assistant"
    assert content.startswith("Vou começar pela seção 2.")
    assert content.endswith(stdin_mod.TURN_FAILED_MARKER)
    assert turn.saves == 1, "the row has to survive a restart, so it is committed"


def test_an_interrupted_turn_keeps_its_partial_answer_too(turn):
    """It used to store the marker alone, losing the work that was on screen."""
    stdin_mod._ACTIVE_VISIBLE_CHUNKS.append("Parcial.")

    stdin_mod._record_interrupted_agent_turn("chat_orchestrator")

    _role, content = turn.messages[0]
    assert content.startswith("Parcial.")
    assert content.endswith(stdin_mod.INTERRUPTED_AGENT_HISTORY_MARKER)


def test_a_failure_with_nothing_to_save_writes_no_row(turn):
    """Nothing was produced, so nothing was lost.

    A bare marker would put a failure row into the conversation the model reads
    back, which is why errors are panel activity instead (PROJECT_DESIGN 2.5),
    and it would amount to inventing a reply for a run that produced none.
    """
    stdin_mod._persist_unfinished_turn(
        "chat_orchestrator", stdin_mod.TURN_FAILED_MARKER, only_with_work=True
    )

    assert turn.messages == []


def test_an_interruption_records_itself_even_with_nothing_to_save(turn):
    """The user stopping the agent is context the model needs, not noise."""
    stdin_mod._record_interrupted_agent_turn("chat_orchestrator")

    assert turn.messages == [
        ("assistant", stdin_mod.INTERRUPTED_AGENT_HISTORY_MARKER)
    ]


def test_only_the_conversation_roles_write_to_the_chat(turn):
    """A worker or inline-editor run is not the conversation and must not appear in it."""
    stdin_mod._ACTIVE_VISIBLE_CHUNKS.append("worker output")

    stdin_mod._persist_unfinished_turn("inline_editor", stdin_mod.TURN_FAILED_MARKER)
    stdin_mod._persist_unfinished_turn("worker", stdin_mod.TURN_FAILED_MARKER)

    assert turn.messages == []


def test_a_store_that_refuses_the_write_never_breaks_the_turn(monkeypatch):
    class BrokenStore(FakeStore):
        def append_message(self, *_a, **_k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr(stdin_mod, "current_store", BrokenStore())
    monkeypatch.setattr(stdin_mod, "current_project", object())
    monkeypatch.setattr(stdin_mod, "_ACTIVE_VISIBLE_CHUNKS", ["text"])

    stdin_mod._persist_unfinished_turn("chat_orchestrator", stdin_mod.TURN_FAILED_MARKER)


def test_every_streamed_chunk_reaches_the_buffer(monkeypatch):
    """The accumulator is fed at the one place every visible chunk passes."""
    monkeypatch.setattr(stdin_mod, "_ACTIVE_VISIBLE_CHUNKS", [])
    monkeypatch.setattr(stdin_mod, "event_hook", lambda _payload: None)
    monkeypatch.setattr(stdin_mod, "_persist_activity_event", lambda *_a, **_k: None)

    stdin_mod.print_event("stream_chunk", {"content": "uma "})
    stdin_mod.print_event("stream_chunk", {"content": "resposta"})
    stdin_mod.print_event("tool_call", {"tool": "read_file", "arguments": {}})

    assert stdin_mod._turn_visible_text() == "uma resposta"


def test_the_marker_is_the_one_the_front_end_matches():
    """utils/turnMarkers.js carries this string byte-for-byte; drift hides the notice."""
    import re

    assert stdin_mod.TURN_FAILED_MARKER == (
        "[TURN-FAILED] This turn stopped on an error before the model gave a "
        "final answer. The text above is work in progress, not a reply."
    )

    module = open("gui_src/src/utils/turnMarkers.js", encoding="utf-8").read()
    declared = re.search(r"export const TURN_FAILED_MARKER =\n?(.*?);", module, re.S)
    assert declared, "the front-end must declare the marker it matches on"
    assert "".join(re.findall(r"'([^']*)'", declared.group(1))) == stdin_mod.TURN_FAILED_MARKER
