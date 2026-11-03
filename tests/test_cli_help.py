"""`/help` has to be enough to use the commands it lists.

The listing printed one line per command through Rich's markup parser, so every
usage string written with brackets -- `[n]`, `[auto|plan|edit]`,
`[list | add key=value...]` -- was read as markup and dropped. Exactly the
commands with an argument shape worth looking up showed none of it, and there
was no way to ask for more.
"""

import asyncio
import re

import pytest
from rich.console import Console

import gilzabots.terminal as T
from gilzabots.cli_commands import COMMAND_GROUPS, REPLState, _registry
from gilzabots.project import ProjectStore


@pytest.fixture
def state(tmp_path):
    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(
        name="p", mode="auto", model="", project_name="P", project_path=str(tmp_path)
    )
    return REPLState(project, store, renderer=object())


def capture(state, line, width=200):
    class _Buffer:
        def __init__(self):
            self.parts = []

        def write(self, text):
            self.parts.append(text)

        def flush(self):
            pass

        def isatty(self):
            return False

    buffer = _Buffer()
    original, T.console = T.console, Console(
        file=buffer, width=width, no_color=True, highlight=False
    )
    try:
        cmd, *rest = line.split(maxsplit=1)
        asyncio.run(_registry.dispatch(state, cmd, rest))
    finally:
        T.console = original
    return "".join(buffer.parts)


# ── The defect ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fragment", [
    "/history [n]",
    "/mode [auto | plan | edit]",
    "/thoughts [on|off]",
    "/chat [list | new <name> | switch <name>]",
    "/providers [list | add key=value... | set <id> key=value... | remove <id>]",
])
def test_the_listing_shows_the_argument_shape(state, fragment):
    assert fragment in capture(state, "/help")


def test_the_listing_shows_the_catalog_subcommands(state):
    out = capture(state, "/help")
    assert "add key=value..." in out
    assert "set <id> key=value..." in out


def test_every_registered_command_appears(state):
    out = capture(state, "/help")
    for name in _registry.names():
        if name in ("/h",) or _registry.aliases(name)[0] != name:
            continue
        assert re.search(rf"{re.escape(name)}(\s|$)", out), f"{name} missing from /help"


def test_every_command_is_placed_in_a_section():
    """A command left out of the map would be filed under 'Other' and lost."""
    grouped = {name for names in COMMAND_GROUPS.values() for name in names}
    primary = {e["name"] for e in _registry.entries()}
    assert primary - grouped == set(), f"ungrouped commands: {sorted(primary - grouped)}"


def test_an_alias_is_named_rather_than_listed_twice(state):
    out = capture(state, "/help")
    assert "(also /context)" in out
    assert out.count("Show how much of the context window") == 1


# ── Per-command help ─────────────────────────────────────────────────────────


def test_help_for_one_command_shows_worked_examples(state):
    out = capture(state, "/help /models")
    assert "/models add name=" in out
    assert "connection=" in out
    assert "extra.<name>=<value>" in out


def test_help_for_providers_explains_both_halves(state):
    out = capture(state, "/help /providers")
    assert "/providers add label=" in out
    assert "api_base" in out and "api_key" in out


def test_help_for_chat_shows_how_to_create_and_switch(state):
    out = capture(state, "/help /chat")
    assert "/chat new" in out
    assert "/chat switch" in out


def test_the_leading_slash_is_optional(state):
    assert capture(state, "/help chat") == capture(state, "/help /chat")


def test_help_for_an_unknown_command_says_so(state):
    assert "nonsense" in capture(state, "/help /nonsense")


def test_the_listing_points_at_the_per_command_help(state):
    assert "/help <command>" in capture(state, "/help")


# ── Reaching the desktop chat ────────────────────────────────────────────────


def test_the_chat_receives_the_help_as_preformatted_text():
    """Markdown would collapse the alignment that makes a usage block readable.

    The desktop front-end's converter also re-reads console output as command
    listings, which turned worked examples into bullets and dropped the shapes
    they exist to show.
    """
    from gilzabots.agent_stdin import handle_slash_command

    result = asyncio.run(handle_slash_command({"prompt": "/help /models"}))
    text = result["messages"][0]
    assert text.count("```") == 2
    assert "/models add name=gemma4:26b connection=ollama-gil" in text
    assert "🔹" not in text


def test_a_terminal_only_command_is_named_as_such_not_as_unknown():
    from gilzabots.agent_stdin import handle_slash_command

    result = asyncio.run(handle_slash_command({"prompt": "/help /chat"}))
    assert "command-line interface" in result["messages"][0]


def test_the_chat_listing_hides_terminal_only_commands():
    from gilzabots.agent_stdin import handle_slash_command

    text = asyncio.run(handle_slash_command({"prompt": "/help"}))["messages"][0]
    assert "/thoughts" not in text
    assert "/models" in text
