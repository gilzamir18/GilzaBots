"""The desktop chat's `/` commands: the completion catalog and the result shape.

The chat composer opens a completion menu on `/` and shows a command's output in
a window of its own instead of writing it into the conversation. Both rely on
the backend: the menu lists what `list_slash_commands` returns, and the window
tells success from failure by `status`, including for a command that paused for
a confirmation and finished through `handle_slash_command_continue`.
"""

import asyncio

import pytest

import gilzabots.terminal as T
from gilzabots import agent_stdin
from gilzabots.agent_stdin import (
    handle_slash_command,
    handle_slash_command_continue,
    list_slash_commands,
)
from gilzabots.cli_commands import _registry


@pytest.fixture
def temp_command(monkeypatch):
    """Register a throwaway command under `name` for one test."""

    def register(name, handler, usage=""):
        monkeypatch.setitem(_registry._cmds, name, (handler, usage, "test command", False, ""))

    monkeypatch.setattr(agent_stdin, "current_project", None, raising=False)
    monkeypatch.setattr(agent_stdin, "current_store", None, raising=False)
    return register


def test_the_catalog_lists_what_the_gui_door_accepts():
    commands = list_slash_commands()
    names = [command["name"] for command in commands]

    assert names[0] == "/help"
    assert "/compact" in names and "/clear_chat" in names
    # Terminal-only commands are refused by the GUI door, so the menu must not
    # offer them.
    for cli_only in ("/chat", "/thoughts", "/tools", "/compile", "/resume"):
        assert cli_only not in names
    for command in commands:
        assert set(command) >= {"name", "aliases", "usage", "description", "details", "group"}
        assert not _registry.is_cli_only(command["name"])


def test_aliases_are_folded_into_their_command():
    by_name = {command["name"]: command for command in list_slash_commands()}
    assert "/context" in by_name["/cost"]["aliases"]
    assert "/context" not in by_name


def test_an_unknown_command_is_an_error_in_english():
    result = asyncio.run(handle_slash_command({"prompt": "/no-such-command"}))
    assert result["status"] == "error"
    assert "Unknown command: /no-such-command" in result["messages"][0]


def test_a_terminal_only_command_is_an_error():
    result = asyncio.run(handle_slash_command({"prompt": "/thoughts on"}))
    assert result["status"] == "error"
    assert "command-line interface" in result["messages"][0]


def test_a_command_that_raises_reports_failure(temp_command):
    async def boom(_state, _args):
        T.info("about to fail")
        raise RuntimeError("disk on fire")

    temp_command("/boom", boom)
    result = asyncio.run(handle_slash_command({"prompt": "/boom"}))

    assert result["status"] == "error"
    assert result["error"] == "disk on fire"
    text = result["messages"][0]
    assert "about to fail" in text and "disk on fire" in text


def test_a_command_with_no_output_returns_empty_text(temp_command):
    async def quiet(_state, _args):
        return None

    temp_command("/quiet", quiet)
    result = asyncio.run(handle_slash_command({"prompt": "/quiet"}))

    assert result["status"] == "done"
    # The front-end says "no output" in the user's language.
    assert result["messages"] == [""]


def test_a_confirmed_command_finishes_with_the_same_result_shape(temp_command):
    async def guarded(_state, _args):
        if await T.aconfirm("Really?"):
            T.success("did it")
        else:
            T.warning("cancelled")

    temp_command("/guarded", guarded)

    async def scenario():
        first = await handle_slash_command({"prompt": "/guarded"})
        assert first["status"] == "confirm"
        assert first["prompt"] == "Really?"
        return await handle_slash_command_continue({"id": first["id"], "value": "yes"})

    result = asyncio.run(scenario())
    assert result["status"] == "done"
    assert "did it" in result["messages"][0]
    # /clear goes through here, and the panel needs the occupancy it reset.
    assert "context_usage" in result
