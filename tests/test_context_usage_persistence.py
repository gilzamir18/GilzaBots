"""The measured context occupancy must survive leaving and reopening a chat.

The in-process measurement is a single slot bound to one project/chat scope, so
running any other conversation rebinds it and a server restart drops it
entirely. Reopening the chat then answered `context_usage: null` and the panel
fell back to the character estimate over the visible bubbles — reporting an
almost empty window for a conversation the orchestrator restores in full from
its saved `agent_state`.

These tests pin the durable copy stored next to that state.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gilzabots import token_usage
from gilzabots.project import ProjectStore


def _store_with_project(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(
        name="proj",
        mode="auto",
        model="fake/model",
        project_name="Proj",
        project_path=str(tmp_path),
    )
    store.create_chat("proj", "chat-proj", "Chat")
    return store, store.load("proj")


@pytest.fixture(autouse=True)
def clean_usage_state():
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    yield
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------

def test_measured_occupancy_survives_reopening_the_chat(tmp_path):
    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id

    store.save_chat_context_usage("proj", chat_id, {
        "prompt_tokens": 34_500,
        "completion_tokens": 800,
        "total_tokens": 35_300,
        "context_window": 1_000_000,
    })

    # A fresh store stands in for the next server process / another chat having
    # taken over the in-process slot.
    reopened = ProjectStore(db_path=str(tmp_path / "projects.db"))
    assert reopened.get_chat_context_usage("proj", chat_id) == {
        "prompt_tokens": 34_500,
        "completion_tokens": 800,
        "total_tokens": 35_300,
        "context_window": 1_000_000,
    }


def test_a_chat_never_reads_another_chats_measurement(tmp_path):
    store, project = _store_with_project(tmp_path)
    store.create_chat("proj", "chat-2", "Second")
    store.save_chat_context_usage("proj", project.current_chat_id, {"prompt_tokens": 90_000})

    assert store.get_chat_context_usage("proj", "chat-2") == {}


def test_an_unmeasured_chat_reports_nothing_rather_than_an_empty_window(tmp_path):
    store, project = _store_with_project(tmp_path)

    # No measurement at all, and a zeroed record are the same state: the caller
    # must keep estimating instead of drawing a full battery.
    assert store.get_chat_context_usage("proj", project.current_chat_id) == {}
    store.save_chat_context_usage("proj", project.current_chat_id, {"prompt_tokens": 0})
    assert store.get_chat_context_usage("proj", project.current_chat_id) == {}
    store.save_chat_context_usage("proj", project.current_chat_id, None)
    assert store.get_chat_context_usage("proj", project.current_chat_id) == {}


def test_an_unknown_chat_reports_nothing(tmp_path):
    store, _ = _store_with_project(tmp_path)

    assert store.get_chat_context_usage("proj", "no-such-chat") == {}


# ---------------------------------------------------------------------------
# The stored copy describes the saved working state, so it dies with it
# ---------------------------------------------------------------------------

def test_clearing_the_chat_drops_the_stored_occupancy(tmp_path):
    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id
    store.save_chat_context_usage("proj", chat_id, {"prompt_tokens": 42_000})

    store.clear_project_history("proj", chat_id)

    assert store.get_chat_context_usage("proj", chat_id) == {}


def test_superseding_a_message_drops_the_stored_occupancy(tmp_path):
    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id
    anchor_id = store.append_message(project, "user", "original question")
    store.append_message(project, "assistant", "original answer")
    store.save_chat_context_usage("proj", chat_id, {"prompt_tokens": 42_000})

    store.supersede_chat_history_from_message("proj", chat_id, message_id=anchor_id)

    # The working state was dropped, so the next turn rebuilds a smaller window;
    # keeping the old number would over-report it.
    assert store.get_chat_context_usage("proj", chat_id) == {}


def test_a_branched_chat_starts_unmeasured(tmp_path):
    store, project = _store_with_project(tmp_path)
    anchor_id = store.append_message(project, "user", "original question")
    store.append_message(project, "assistant", "original answer")
    store.save_chat_context_usage("proj", project.current_chat_id, {"prompt_tokens": 42_000})

    store.branch_chat_before_message(
        "proj", project.current_chat_id, "chat-branch", "Branch", message_id=anchor_id,
    )

    assert store.get_chat_context_usage("proj", "chat-branch") == {}


# ---------------------------------------------------------------------------
# End of turn writes the measurement through
# ---------------------------------------------------------------------------

def test_a_turn_persists_its_measurement_for_the_chat_it_ran_in(tmp_path, monkeypatch):
    from gilzabots import agent_stdin

    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_project", project)

    token_usage.set_context_scope(
        token_usage.context_scope_key(project.project_path, chat_id)
    )
    token_usage.set_context_window(128_000)
    token_usage.record_context_tokens(51_200)

    agent_stdin._persist_context_usage(chat_id)

    stored = store.get_chat_context_usage("proj", chat_id)
    assert stored["prompt_tokens"] == 51_200
    assert stored["context_window"] == 128_000


@pytest.mark.asyncio
async def test_a_real_turn_writes_the_measurement_to_the_database(tmp_path, monkeypatch):
    """End to end through ``handle_run``, against a real store.

    The unit test above calls the persistence helper directly, which cannot show
    that the turn's ``finally`` block reaches it with the scope still bound to
    the chat that ran.
    """
    import gilzabots.agent_stdin as stdin_mod
    from agenticblocks.runtime.state import TokenUsage

    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            # What AgenticBlocks emits after an LLM call.
            token_usage.record_token_usage(TokenUsage(
                block_name="chat_orchestrator",
                step=1,
                prompt_tokens=48_000,
                completion_tokens=600,
                total_tokens=48_600,
            ))
            return SimpleNamespace(response="done")

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "hello",
        "chat_id": chat_id,
    })

    stored = store.get_chat_context_usage("proj", chat_id)
    assert stored["prompt_tokens"] == 48_000


# ---------------------------------------------------------------------------
# Conversations that ran before the measurement was ever persisted
# ---------------------------------------------------------------------------

def test_occupancy_is_derived_from_the_saved_working_state(tmp_path):
    """A chat with history but no stored measurement still reports a real number.

    This is the pre-existing-conversation case: the working state the
    orchestrator will restore is what fills the window, so counting it beats
    reporting nothing and letting the panel estimate over the visible bubbles.
    """
    from gilzabots.memgpt_runtime import derive_context_usage_from_state

    store, project = _store_with_project(tmp_path)
    store.save_chat_agent_state("proj", project.current_chat_id, {
        "internal_history": [
            {"role": "user", "content": "analyse this table " + ("x " * 4000)},
            {"role": "assistant", "content": "here is the analysis " + ("y " * 4000)},
        ],
        "recursive_summary": "The user asked about the results table.",
    })

    record = derive_context_usage_from_state(project, store)

    assert record["source"] == "state"
    # The two messages alone are thousands of tokens; a character estimate over
    # empty visible bubbles would have reported nothing.
    assert record["prompt_tokens"] > 4_000
    assert record["total_tokens"] == record["prompt_tokens"]


def test_a_chat_that_never_ran_derives_nothing(tmp_path):
    from gilzabots.memgpt_runtime import derive_context_usage_from_state

    store, project = _store_with_project(tmp_path)

    assert derive_context_usage_from_state(project, store) is None


def test_the_derived_count_includes_the_system_prompt(tmp_path):
    """The prompt is most of a short chat's window, and all of an empty one's."""
    from gilzabots.memgpt_runtime import (
        chat_orchestrator_system_prompt, derive_context_usage_from_state,
    )

    store, project = _store_with_project(tmp_path)
    store.save_chat_agent_state("proj", project.current_chat_id, {
        "internal_history": [{"role": "user", "content": "hi"}],
        "recursive_summary": "No history has been evicted yet.",
    })

    record = derive_context_usage_from_state(project, store)
    prompt_chars = len(chat_orchestrator_system_prompt(project, store))

    assert prompt_chars > 0
    # Counting only the visible message would report a handful of tokens.
    assert record["prompt_tokens"] > 100


def test_rendering_the_system_prompt_does_not_rescope_the_global_tools(tmp_path):
    """A read-only request must not repoint the file tools at another project.

    ``build_chat_orchestrator`` calls ``set_project_context``; the extracted
    prompt renderer must not, or opening a chat while another project's turn is
    running would send that turn's writes to the wrong directory.
    """
    from gilzabots import tools
    from gilzabots.memgpt_runtime import chat_orchestrator_system_prompt

    store, project = _store_with_project(tmp_path)
    before = tools.get_project_path()

    chat_orchestrator_system_prompt(project, store)

    assert tools.get_project_path() == before


@pytest.mark.asyncio
async def test_a_real_measurement_wins_over_the_derived_count(tmp_path, monkeypatch):
    """Ranking: provider measurement first, derived count only as the last resort."""
    from gilzabots.ide_server import _chat_context_usage

    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id
    store.save_chat_agent_state("proj", chat_id, {
        "internal_history": [{"role": "user", "content": "hello " + ("z " * 2000)}],
        "recursive_summary": "",
    })
    store.save_chat_context_usage("proj", chat_id, {"prompt_tokens": 12_345})

    record = await _chat_context_usage(store, project, chat_id)

    assert record["prompt_tokens"] == 12_345
    assert "source" not in record


def test_a_turn_without_a_measurement_keeps_the_previous_one(tmp_path, monkeypatch):
    from gilzabots import agent_stdin

    store, project = _store_with_project(tmp_path)
    chat_id = project.current_chat_id
    store.save_chat_context_usage("proj", chat_id, {"prompt_tokens": 7_000})
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_project", project)

    # A turn that failed before any LLM call says nothing about the window.
    token_usage.set_context_scope(
        token_usage.context_scope_key(project.project_path, chat_id)
    )

    agent_stdin._persist_context_usage(chat_id)

    assert store.get_chat_context_usage("proj", chat_id) == {"prompt_tokens": 7_000}
