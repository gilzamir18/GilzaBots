"""Each front-end is told about its own affordances, and only its own.

The orchestrator's SKILL bodies and tool list are shared by the desktop window
and the command-line interface, so whatever is added for one of them lands in
the other's prompt. Terminal commands (`/add-provider`, `/chat`, `/compile`, …)
are refused outright by the desktop chat, and `get_editor_state` reports what
the user has open in the IDE, which a terminal session does not have — there it
can only answer "nothing staged" or hand back a snapshot a desktop session left
on disk earlier.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opalatex import agent_stdin
from opalatex.cli_commands import _registry
from opalatex.prompt_surface import (
    GUI_ONLY_TOOLS,
    body_for_surface,
    cli_only_command_names,
    strip_items,
    tools_for_surface,
)

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "skills", "chat-orchestrator")
VARIANTS = ["SKILL.md", "SKILL.light.md"]

#: The first name inside backticks: `/project [new]`, `get_editor_state`.
NAMES = re.compile(r"`([A-Za-z_/][\w./-]*)[^`]*`")


@pytest.fixture
def terminal():
    """Run the body as the command-line interface would (cli_render.install)."""
    agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_TERMINAL)
    yield
    agent_stdin.set_reply_surface(None)


@pytest.fixture(autouse=True)
def desktop_by_default():
    """The desktop app leaves the surface unset; no test may leak one."""
    agent_stdin.set_reply_surface(None)
    yield
    agent_stdin.set_reply_surface(None)


def _body(variant):
    with open(os.path.join(SKILL_DIR, variant), encoding="utf-8") as handle:
        return handle.read()


# ── Commands ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_desktop_prompt_never_offers_a_terminal_only_command(variant):
    named = set(NAMES.findall(body_for_surface(_body(variant))))
    assert not (named & cli_only_command_names())


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_terminal_prompt_keeps_every_command(variant, terminal):
    source = set(NAMES.findall(_body(variant)))
    named = set(NAMES.findall(body_for_surface(_body(variant))))
    assert (source & cli_only_command_names()) <= named


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_shared_command_survives_on_both_surfaces(variant, terminal):
    """The filter removes items; it must never take a neighbour with it."""
    source = {n for n in NAMES.findall(_body(variant)) if n.startswith("/")}
    shared = source - cli_only_command_names()
    on_terminal = set(NAMES.findall(body_for_surface(_body(variant))))
    agent_stdin.set_reply_surface(None)
    on_desktop = set(NAMES.findall(body_for_surface(_body(variant))))
    assert shared <= on_desktop
    assert shared <= on_terminal


def test_the_registry_is_what_decides(monkeypatch):
    """A command marked cli_only later is dropped without editing the prompt."""
    body = "* `/mode [auto|plan|edit]`: show or set the mode\n* `/cost`: context usage\n"
    assert "/cost" in body_for_surface(body)
    monkeypatch.setattr(_registry, "is_cli_only", lambda name: name == "/cost")
    filtered = body_for_surface(body)
    assert "/cost" not in filtered
    assert "/mode" in filtered


# ── Tools ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_editor_tool_is_absent_from_the_terminal_prompt(variant, terminal):
    assert "get_editor_state" not in body_for_surface(_body(variant))


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_desktop_prompt_keeps_the_editor_tool(variant):
    assert "get_editor_state" in body_for_surface(_body(variant))


def test_the_terminal_agent_is_not_given_the_editor_tool(terminal):
    class _Tool:
        def __init__(self, name):
            self.name = name

    tools = [_Tool("read_file"), _Tool("get_editor_state"), _Tool("search_code")]
    kept = [t.name for t in tools_for_surface(tools)]
    assert kept == ["read_file", "search_code"]

    agent_stdin.set_reply_surface(None)
    assert len(tools_for_surface(tools)) == 3


def test_the_orchestrator_built_for_a_terminal_has_no_editor_tool(tmp_path, terminal):
    from opalatex.memgpt_runtime import build_chat_orchestrator
    from opalatex.project import ProjectStore

    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="p", mode="auto", model="ollama/test",
                           project_name="P", project_path=str(tmp_path))

    agent = build_chat_orchestrator(project, store)
    assert "get_editor_state" not in {str(getattr(t, "name", "")) for t in agent.tools}

    agent_stdin.set_reply_surface(None)
    desktop = build_chat_orchestrator(project, store)
    assert "get_editor_state" in {str(getattr(t, "name", "")) for t in desktop.tools}


# ── The filter itself ────────────────────────────────────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_no_editor_tool_reaches_the_terminal_prompt(variant, terminal):
    body = body_for_surface(_body(variant))
    for name in GUI_ONLY_TOOLS:
        assert name not in body, name


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_desktop_prompt_offers_the_ide_control_tools(variant):
    body = body_for_surface(_body(variant))
    assert "`open_file`" in body and "`close_file`" in body


def test_a_routing_row_whose_route_is_removed_goes():
    table = (
        '| "What file is open?" | your own `get_editor_state` | `command-line` |\n'
        '| "Change line 42 of `main.tex`" | your own `replace_content_range` | `command-line` |\n'
    )
    filtered = strip_items(table, {"get_editor_state"})
    assert "get_editor_state" not in filtered
    assert "replace_content_range" in filtered


def test_a_fenced_example_is_left_alone():
    body = "* `/chat`: switch chat\n```\n/chat new draft\n```\n"
    filtered = strip_items(body, {"/chat"})
    assert "/chat new draft" in filtered
    assert "switch chat" not in filtered


def test_an_entry_inside_a_surviving_bullet_is_still_removed():
    body = "* Your tools: `read_file`, `get_editor_state`, `search_code`.\n"
    filtered = strip_items(body, GUI_ONLY_TOOLS)
    assert filtered.strip() == "* Your tools: `read_file`, `search_code`."


def test_a_comma_inside_brackets_does_not_split_an_entry():
    body = "`/models` (list, show, add), `/chat` (list, new), `/mode`\n"
    assert strip_items(body, {"/chat"}).strip() == "`/models` (list, show, add), `/mode`"


# ── A monitor's background turn (PROJECT_DESIGN 2.24) ───────────────────────


@pytest.fixture
def background():
    """Run as a monitor's wake-up does without a window (monitors/wake_runner.py)."""
    agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_BACKGROUND)
    yield
    agent_stdin.set_reply_surface(None)


def test_the_orchestrator_built_for_a_background_turn_has_no_window(tmp_path, background):
    from opalatex.memgpt_runtime import build_chat_orchestrator
    from opalatex.project import ProjectStore

    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="p", mode="auto", model="ollama/test",
                           project_name="P", project_path=str(tmp_path))

    agent = build_chat_orchestrator(project, store)
    names = {str(getattr(t, "name", "")) for t in agent.tools}
    assert not (names & GUI_ONLY_TOOLS)
    assert "register_monitor" in names
    assert "Where this turn runs: in the background" in agent.system_prompt
    assert "The IDE you work in" not in agent.system_prompt
    assert "plain-text terminal" not in agent.system_prompt


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_background_prompt_offers_neither_window_tools_nor_terminal_commands(variant, background):
    body = body_for_surface(_body(variant))
    named = set(NAMES.findall(body))
    assert not (named & GUI_ONLY_TOOLS)
    assert not (named & cli_only_command_names())
