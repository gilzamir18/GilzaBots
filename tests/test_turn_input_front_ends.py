"""The questions a turn asks reach whichever front-end is running it.

`ask_question`, confirmations and `run_interactive_command` go through the
turn's input hooks. Those hooks used to register their future by hand and only
publish the event, so the desktop app (which answers over HTTP) worked and the
terminal, which answers through ``set_input_transport``, never received the
question: the turn waited out the 24h timeout.

The GUI half of these tests pins the event the desktop app receives, because it
works and must keep working: the same payload per request type, answered by
resolving ``_gui_input_pending`` exactly as /api/gilzabots/input_response does.
"""

import asyncio
import json

import pytest

import gilzabots.agent_stdin as agent_stdin
import gilzabots.cli_render as cli_render


@pytest.fixture
def gui(monkeypatch):
    """A front-end that sees events and answers over HTTP: no transport installed."""
    published = []

    def hook(payload):
        published.append(payload)
        if payload.get("event") == "input_request" and answers:
            fut = agent_stdin._gui_input_pending[payload["id"]]
            asyncio.get_event_loop().call_soon(fut.set_result, answers.pop(0))

    answers: list[str] = []
    monkeypatch.setattr(agent_stdin, "event_hook", hook)
    monkeypatch.setattr(agent_stdin, "_input_transport", None)
    return published, answers


def _requests(published):
    return [
        {k: v for k, v in p.items() if k not in ("event", "id")}
        for p in published if p.get("event") == "input_request"
    ]


def test_the_gui_receives_a_free_text_question_without_yes_no_choices(gui):
    published, answers = gui
    answers.append("LaTeX")

    answer = asyncio.run(agent_stdin._turn_ask_hook("Which format?"))

    assert answer == "LaTeX"
    assert _requests(published) == [{"prompt": "Which format?", "type": "ask"}]
    assert any(p.get("event") == "input_request_closed" for p in published)


def test_the_gui_receives_choices_and_the_multi_select_flag(gui):
    published, answers = gui
    answers.append('["a", "c"]')

    answer = asyncio.run(agent_stdin._turn_ask_hook("Columns?", ["a", "b", "c"], True))

    assert answer == '["a", "c"]'
    assert _requests(published) == [{
        "prompt": "Columns?", "type": "ask", "options": ["a", "b", "c"], "is_multi_select": True,
    }]


def test_the_gui_receives_the_interactive_terminal_request_unchanged(gui):
    published, answers = gui
    answers.append("yes")

    answer = asyncio.run(agent_stdin._turn_interactive_terminal_hook("npm init", "temp_1"))

    assert answer == "yes"
    assert _requests(published) == [{
        "prompt": "Interactive terminal spawned", "type": "interactive_terminal",
        "command": "npm init", "term_id": "temp_1",
    }]


def test_the_gui_receives_a_confirmation_with_yes_no(gui):
    published, answers = gui
    answers.append("no")

    assert asyncio.run(agent_stdin._turn_confirm_hook("Proceed?", default=True)) is False
    assert _requests(published) == [{
        "prompt": "Proceed?", "type": "confirm", "options": ["yes", "no"], "default": "yes",
    }]


# ── The terminal ─────────────────────────────────────────────────────────────


@pytest.fixture
def terminal(monkeypatch):
    """The CLI's transport, with the user's typing supplied by the test."""
    typed: list[str] = []
    monkeypatch.setattr(agent_stdin, "event_hook", lambda _payload: None)
    monkeypatch.setattr(agent_stdin, "_input_transport", cli_render.terminal_input_transport)
    monkeypatch.setattr("builtins.input", lambda *_a: typed.pop(0))
    return typed


def _ask(*args):
    return asyncio.run(asyncio.wait_for(agent_stdin._turn_ask_hook(*args), timeout=5))


def test_ask_question_is_answered_in_the_terminal(terminal):
    """It used to hang: nothing in the CLI could resolve the question."""
    terminal.append("the introduction")
    assert _ask("Which section?") == "the introduction"


def test_a_choice_is_answered_by_number_or_in_words(terminal):
    terminal.extend(["2"])
    assert _ask("Format?", ["PDF", "DOCX"]) == "DOCX"
    terminal.extend(["HTML"])
    assert _ask("Format?", ["PDF", "DOCX"]) == "HTML"


def test_the_write_in_choice_asks_for_the_text(terminal):
    terminal.extend(["3", "Markdown"])
    assert _ask("Format?", ["PDF", "DOCX", "Other (please specify)"]) == "Markdown"


def test_a_multi_select_answer_has_the_desktop_wire_format(terminal):
    """A JSON array of the chosen labels plus any write-in (formatAskResponse)."""
    terminal.extend(["3, 1, 4", "notes"])
    answer = _ask("Columns?", ["name", "age", "city"], True)
    assert json.loads(answer) == ["name", "city", "notes"]


def test_the_turn_hooks_install_the_shared_path(monkeypatch):
    """`ask_question` reaches the hook through gilzabots.terminal.aask."""
    import gilzabots.terminal as T

    restore = agent_stdin._install_turn_input_hooks(
        agent_stdin._turn_confirm_hook,
        agent_stdin._turn_ask_hook,
        agent_stdin._turn_interactive_terminal_hook,
    )
    try:
        assert T._async_ask_hook is agent_stdin._turn_ask_hook
    finally:
        restore()


def test_an_interactive_command_runs_in_the_terminal_and_the_user_reports(terminal, monkeypatch, tmp_path):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    monkeypatch.setattr("gilzabots.tools.get_project_path", lambda: str(tmp_path))
    terminal.append("")  # accept the default, which follows the exit code

    answer = asyncio.run(asyncio.wait_for(
        agent_stdin._turn_interactive_terminal_hook("npm init", "temp_1"), timeout=5))

    assert answer == "yes"
    assert calls == [("npm init", {"shell": True, "cwd": str(tmp_path)})]


def test_an_interactive_command_the_user_calls_failed_is_reported_as_cancelled(terminal, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: 1)
    terminal.append("")  # exit code 1 makes "no" the default

    answer = asyncio.run(asyncio.wait_for(
        agent_stdin._turn_interactive_terminal_hook("make", "temp_2"), timeout=5))

    assert answer == "cancel"


# ── run_background_command ───────────────────────────────────────────────────


def test_a_background_command_runs_as_a_process_of_the_terminal_session(tmp_path, monkeypatch):
    """The desktop app sends it to the IDE's main terminal over HTTP; the CLI has neither."""
    import time

    from gilzabots import tools

    monkeypatch.setattr(tools, "get_project_path", lambda: str(tmp_path))
    monkeypatch.setattr(agent_stdin, "_background_command_runner", cli_render.terminal_background_command)
    try:
        fn = getattr(tools.run_background_command, "_func", tools.run_background_command)
        result = asyncio.run(fn("echo started"))
        assert result.startswith("SUCCESS")
        log = next((tmp_path / ".gilzabots" / "background").glob("command-*.log"))
        for _ in range(50):
            if log.read_text().strip():
                break
            time.sleep(0.05)
        assert log.read_text().strip() == "started"
        assert str(log) in result
    finally:
        cli_render.stop_background_commands()


def test_the_gui_path_is_used_when_no_runner_is_installed(tmp_path, monkeypatch):
    from gilzabots import tools

    monkeypatch.setattr(agent_stdin, "_background_command_runner", None)
    monkeypatch.setattr(tools, "get_project_path", lambda: str(tmp_path))
    sent = []

    def fake_connection():
        sent.append("http")
        raise ConnectionRefusedError("no server")

    monkeypatch.setattr("gilzabots.local_auth.local_api_connection", fake_connection)
    fn = getattr(tools.run_background_command, "_func", tools.run_background_command)
    result = asyncio.run(fn("npm run dev"))

    assert sent == ["http"]
    assert result.startswith("FAILED to send command to background terminal")
    assert not (tmp_path / ".gilzabots" / "background").exists()


def test_installing_the_terminal_sets_and_clears_both_front_end_hooks():
    cli_render.install()
    try:
        assert agent_stdin._input_transport is cli_render.terminal_input_transport
        assert agent_stdin.background_command_runner() is cli_render.terminal_background_command
    finally:
        cli_render.uninstall()
    assert agent_stdin._input_transport is None
    assert agent_stdin.background_command_runner() is None
