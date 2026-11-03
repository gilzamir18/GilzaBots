"""`create_plan` belongs to plan mode, and an approved plan outlives its dialog.

Two defects observed in one turn (`openrouter/openai/gpt-5.6-luna`, project in
`auto` mode, asked only to *show* a correction plan):

  * The model opened an approval dialog for a plan nobody was going to execute.
    `create_plan` halts the turn on a confirmation, which contradicts `auto`'s own
    contract -- action is pre-authorized there precisely so no dialog appears --
    and its description had always said "in plan mode". Saying so was not enough.
  * The plan itself, 5 817 characters, was presented through a transient
    `input_request` and never persisted. What survived the turn was the model's
    857-character summary of it, so reopening the chat left a summary of a plan
    that could no longer be read: the pointer-shaped answer again, one layer above
    the turn protocol.
"""
import sys
from types import SimpleNamespace

import pytest

from gilzabots.memgpt_runtime import build_chat_orchestrator
from gilzabots.project import ProjectData


def _project(tmp_path, mode):
    return ProjectData(
        name="t", project_name="t", project_path=str(tmp_path),
        model="ollama/gemma4:12b", mode=mode,
    )


def _tool_names(agent):
    return {getattr(t, "name", "") for t in agent.tools}


# ── The tool is scoped to the mode it was written for ────────────────────────

def test_plan_mode_gets_create_plan(tmp_path):
    assert "create_plan" in _tool_names(build_chat_orchestrator(_project(tmp_path, "plan"), None))


@pytest.mark.parametrize("mode", ["auto", "edit"])
def test_no_other_mode_gets_create_plan(tmp_path, mode):
    """An approval dialog in a mode whose contract is 'no confirmation dialogs'."""
    assert "create_plan" not in _tool_names(build_chat_orchestrator(_project(tmp_path, mode), None))


def test_withholding_it_leaves_the_rest_of_the_toolset_alone(tmp_path):
    plan = _tool_names(build_chat_orchestrator(_project(tmp_path, "plan"), None))
    auto = _tool_names(build_chat_orchestrator(_project(tmp_path, "auto"), None))

    assert plan - auto == {"create_plan"}
    assert "ask_question" in auto and "read_file" in auto


# ── An approved plan is conversation, not a dialog that closed ───────────────

def test_an_approved_plan_is_persisted_as_conversation(monkeypatch):
    import gilzabots.tools as tools_mod

    saved = []

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved.append((role, content))

    monkeypatch.setattr(tools_mod, "_PROJECT_SESSION", SimpleNamespace(name="p"))
    monkeypatch.setattr(tools_mod, "_PROJECT_STORE", FakeStore())

    tools_mod._persist_approved_plan("## Plan\n- step one\n- step two")

    assert saved == [("assistant", "## Plan\n- step one\n- step two")]


def test_an_empty_plan_is_not_persisted(monkeypatch):
    import gilzabots.tools as tools_mod

    saved = []

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved.append((role, content))

    monkeypatch.setattr(tools_mod, "_PROJECT_SESSION", SimpleNamespace(name="p"))
    monkeypatch.setattr(tools_mod, "_PROJECT_STORE", FakeStore())

    tools_mod._persist_approved_plan("   ")

    assert saved == []


def test_persisting_never_raises_on_a_plan_the_user_already_approved(monkeypatch):
    """A store failure here would surface as a tool error on an approved plan."""
    import gilzabots.tools as tools_mod

    class ExplodingStore:
        def append_message(self, *_a, **_kw):
            raise RuntimeError("db is gone")

    monkeypatch.setattr(tools_mod, "_PROJECT_SESSION", SimpleNamespace(name="p"))
    monkeypatch.setattr(tools_mod, "_PROJECT_STORE", ExplodingStore())

    tools_mod._persist_approved_plan("## Plan")  # must not raise


def test_the_agent_already_gets_the_plan_back_in_the_tool_result():
    """The gap was never the agent's copy: it is handed the whole plan verbatim."""
    import inspect

    source = inspect.getsource(sys.modules["gilzabots.tools"])
    assert 'Proceed to execute the plan.\\n\\nPlan Content:\\n{edited_plan}' in source


# ── The static tool list cannot cover a mid-turn mode change ─────────────────

def test_create_plan_refuses_once_the_turn_has_moved_to_execution(monkeypatch):
    """Approving a plan flips the session to auto; the built list still has the tool."""
    import asyncio

    import gilzabots.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_PROJECT_SESSION", SimpleNamespace(mode="auto"))

    raw = getattr(tools_mod.create_plan, "_func", None) or tools_mod.create_plan
    result = asyncio.run(raw("## A second plan, mid-execution"))

    assert result.startswith("Failed:")
    assert "plan" in result and "normal text" in result


def test_a_failed_plan_save_is_reported_not_swallowed(monkeypatch):
    """Silence here puts the plan back in a channel that does not survive."""
    import gilzabots.agent_stdin as stdin_mod
    import gilzabots.tools as tools_mod

    events = []

    class ExplodingStore:
        def append_message(self, *_a, **_kw):
            raise RuntimeError("db is gone")

    monkeypatch.setattr(tools_mod, "_PROJECT_SESSION", SimpleNamespace(name="p"))
    monkeypatch.setattr(tools_mod, "_PROJECT_STORE", ExplodingStore())
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))

    tools_mod._persist_approved_plan("## Plan")  # still must not raise

    assert any(e == "error" and "could not be saved" in d.get("message", "") for e, d in events)
