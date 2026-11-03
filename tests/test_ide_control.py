"""The agent's control of the IDE window: its tools and their channel.

The tools act on the window, not the disk, so every one of them is a round trip
(gilzabots/ide_control.py): an `ide_command` event goes out on the turn's stream,
the desktop app carries it out and posts its answer with the resulting tabs.
These tests play the window's part by answering from the event hook, the way
the desktop app answers through /api/gilzabots/ide_command_result.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gilzabots import agent_stdin, ide_control
from gilzabots.ide_control import IdeCommandError, resolve_ide_command, send_ide_command
from gilzabots.prompt_surface import GUI_ONLY_TOOLS
from gilzabots.tools import (
    close_file,
    editor_state_path,
    get_editor_state,
    get_problems,
    open_file,
    read_terminal,
    reveal_in_pdf,
    set_layout,
    set_project_context,
)


def _call(tool, **kwargs):
    raw = getattr(tool, "_func", None) or tool
    return asyncio.run(raw(**kwargs))


class FakeWindow:
    """Answers `ide_command` events as the desktop app would."""

    def __init__(self, answer=None):
        self.commands = []
        self.answer = answer or (lambda command: {"ok": True, "open_files": [], "current_file": ""})

    def __call__(self, payload):
        if payload.get("event") != "ide_command":
            return
        self.commands.append(payload)
        resolve_ide_command(payload["id"], self.answer(payload))


@pytest.fixture
def window(monkeypatch):
    fake = FakeWindow()
    monkeypatch.setattr(agent_stdin, "event_hook", fake)
    return fake


@pytest.fixture
def project(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "main.tex").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (tmp_path / "chapters" / "intro.tex").write_text("intro\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan"))
    return tmp_path


def _stage(project, **state):
    path = editor_state_path(str(project))
    (project / ".gilzabots").mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _staged(project):
    with open(editor_state_path(str(project)), encoding="utf-8") as f:
        return json.load(f)


# ── The channel ──────────────────────────────────────────────────────────────


def test_the_windows_answer_is_returned(window):
    window.answer = lambda c: {"ok": True, "open_files": ["a.tex"], "current_file": "a.tex"}
    result = asyncio.run(send_ide_command("open_file", {"path": "a.tex"}, project_path="/p"))
    assert result["current_file"] == "a.tex"
    sent = window.commands[0]
    assert sent["action"] == "open_file" and sent["args"] == {"path": "a.tex"}
    assert sent["project_path"] == "/p"
    assert ide_control._pending == {}


def test_a_refusal_carries_the_windows_reason(window):
    window.answer = lambda c: {"ok": False, "error": "x.tex has unsaved edits"}
    with pytest.raises(IdeCommandError, match="x.tex has unsaved edits"):
        asyncio.run(send_ide_command("close_file", {"path": "x.tex"}, project_path="/p"))


def test_no_window_fails_fast_instead_of_claiming_success(monkeypatch):
    monkeypatch.setattr(agent_stdin, "event_hook", lambda payload: None)
    with pytest.raises(IdeCommandError, match="did not answer"):
        asyncio.run(send_ide_command("open_file", {"path": "a"}, project_path="/p", timeout=0.05))
    assert ide_control._pending == {}


def test_an_answer_for_no_pending_command_is_rejected():
    assert resolve_ide_command("nope", {"ok": True}) is False


# ── open_file ────────────────────────────────────────────────────────────────


def test_open_file_sends_the_project_relative_path(project, window):
    window.answer = lambda c: {"ok": True, "open_files": ["chapters/intro.tex"], "current_file": "chapters/intro.tex"}
    out = _call(open_file, path=str(project / "chapters" / "intro.tex"))

    args = window.commands[0]["args"]
    assert args == {"path": "chapters/intro.tex", "line": None, "focus": True}
    assert "Opened `chapters/intro.tex`" in out and "focused tab" in out
    assert "`chapters/intro.tex` (focused)" in out


def test_open_file_scrolls_to_a_line(project, window):
    window.answer = lambda c: {"ok": True, "open_files": ["main.tex"], "current_file": "main.tex"}
    out = _call(open_file, path="main.tex", line=2)
    assert window.commands[0]["args"]["line"] == 2
    assert "scrolled to line 2" in out


def test_a_background_tab_keeps_the_users_focus(project, window):
    window.answer = lambda c: {"ok": True, "open_files": ["main.tex", "chapters/intro.tex"], "current_file": "main.tex"}
    out = _call(open_file, path="chapters/intro.tex", focus=False)
    assert window.commands[0]["args"]["focus"] is False
    assert "background tab" in out
    assert "`main.tex` (focused)" in out


@pytest.mark.parametrize("kwargs, message", [
    ({"path": "intro.tex"}, "chapters/intro.tex"),            # names where it does exist
    ({"path": "../elsewhere.tex"}, "outside the project"),
    ({"path": "main.tex", "line": 4}, "has 3 lines"),
    ({"path": "main.tex", "line": 2, "focus": False}, "needs focus=true"),
    ({"path": "main.tex", "line": -1}, "1-based"),
    ({"path": ""}, "'path' is required"),
])
def test_bad_requests_never_reach_the_window(project, window, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _call(open_file, **kwargs)
    assert window.commands == []


def test_open_file_is_safe_in_plan_mode(project, window):
    # The project fixture is in plan mode: showing a file changes no file.
    out = _call(open_file, path="main.tex")
    assert "Execution blocked" not in out
    assert window.commands


# ── close_file ───────────────────────────────────────────────────────────────


def test_close_file_reports_the_tabs_left(project, window):
    window.answer = lambda c: {"ok": True, "open_files": ["main.tex"], "current_file": "main.tex"}
    out = _call(close_file, path="chapters/intro.tex")
    assert window.commands[0]["action"] == "close_file"
    assert window.commands[0]["args"] == {"path": "chapters/intro.tex"}
    assert "Closed the tab of `chapters/intro.tex`" in out


def test_a_file_deleted_from_disk_can_still_have_its_tab_closed(project, window):
    _call(close_file, path="gone.tex")
    assert window.commands[0]["args"] == {"path": "gone.tex"}


def test_an_unsaved_tab_is_refused_with_the_windows_reason(project, window):
    window.answer = lambda c: {"ok": False, "error": "main.tex has unsaved edits in the editor"}
    with pytest.raises(ValueError, match="unsaved edits"):
        _call(close_file, path="main.tex")


# ── The staged editor state follows the window ───────────────────────────────


def test_the_answer_is_written_back_into_the_editor_state(project, window):
    _stage(project, current_file="main.tex", open_files=["main.tex"],
           selected_text="two", editor_content="one\ntwo\n", editor_content_file="main.tex")
    window.answer = lambda c: {"ok": True, "open_files": ["main.tex", "chapters/intro.tex"],
                               "current_file": "chapters/intro.tex"}
    _call(open_file, path="chapters/intro.tex")

    state = _staged(project)
    assert state["open_files"] == ["main.tex", "chapters/intro.tex"]
    assert state["current_file"] == "chapters/intro.tex"
    # The buffer was captured for main.tex and still says so.
    assert state["editor_content_file"] == "main.tex"


def test_editor_state_says_the_buffer_belongs_to_the_old_tab(project, window):
    _stage(project, current_file="main.tex", open_files=["main.tex"],
           selected_text="two", editor_content="UNSAVED", editor_content_file="main.tex")
    window.answer = lambda c: {"ok": True, "open_files": ["main.tex", "chapters/intro.tex"],
                               "current_file": "chapters/intro.tex"}
    _call(open_file, path="chapters/intro.tex")

    out = _call(get_editor_state, include_content=True)
    assert "Focus changed during this turn" in out
    assert "Selected text in `main.tex`" in out
    assert "Live buffer of `main.tex`" in out
    assert "Focused file**: `chapters/intro.tex`" in out


def test_a_state_staged_before_the_key_existed_is_read_as_before(project):
    _stage(project, current_file="main.tex", open_files=["main.tex"], editor_content="X")
    out = _call(get_editor_state, include_content=True)
    assert "Focus changed" not in out
    assert "Live buffer of `main.tex`" in out


def test_staging_records_which_tab_the_buffer_belongs_to(tmp_path):
    staged = agent_stdin.stage_editor_state(str(tmp_path), {
        "current_file": "a.tex", "open_files": ["a.tex"], "editor_content": "x",
    })
    assert staged["editor_content_file"] == "a.tex"


# ── Who is given the tools ───────────────────────────────────────────────────


def _orchestrator_tool_names(tmp_path, model="ollama/test"):
    from gilzabots.memgpt_runtime import build_chat_orchestrator
    from gilzabots.project import ProjectStore

    store = ProjectStore(db_path=str(tmp_path / "s.db"))
    project = store.create(name="p", mode="auto", model=model,
                           project_name="P", project_path=str(tmp_path))
    agent = build_chat_orchestrator(project, store)
    return {str(getattr(t, "name", "")) for t in agent.tools}, agent


def test_the_desktop_orchestrator_holds_the_ide_tools(tmp_path):
    names, agent = _orchestrator_tool_names(tmp_path)
    assert GUI_ONLY_TOOLS <= names
    assert "## The IDE you work in" in agent.system_prompt


def test_the_terminal_orchestrator_does_not(tmp_path):
    agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_TERMINAL)
    try:
        names, agent = _orchestrator_tool_names(tmp_path)
    finally:
        agent_stdin.set_reply_surface(None)
    assert not GUI_ONLY_TOOLS & names
    assert "The IDE you work in" not in agent.system_prompt
    for name in GUI_ONLY_TOOLS:
        assert name not in agent.system_prompt, name


def test_helpers_are_not_given_the_window_controls():
    from gilzabots.tools import get_helper_tools

    names = {str(getattr(t, "name", "")) for t in get_helper_tools()}
    assert not {"open_file", "close_file", "reveal_in_pdf", "set_layout", "get_problems"} & names


def test_helpers_read_the_terminal_on_the_desktop_only_and_never_drive_it():
    from gilzabots.prompt_surface import tools_for_surface
    from gilzabots.tools import get_helper_tools

    names = {str(getattr(t, "name", "")) for t in tools_for_surface(get_helper_tools())}
    assert "read_terminal" in names
    # Starting processes in the user's terminal is the main agent's.
    assert not {"run_background_command", "run_interactive_command"} & names

    agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_TERMINAL)
    try:
        names = {str(getattr(t, "name", "")) for t in tools_for_surface(get_helper_tools())}
    finally:
        agent_stdin.set_reply_surface(None)
    assert "read_terminal" not in names


# ── The map of the window ────────────────────────────────────────────────────


def test_the_map_names_every_part_the_main_agent_reaches():
    """Under both policies the main agent holds every tool the map names."""
    from gilzabots.memgpt_runtime import desktop_workspace_block

    block = desktop_workspace_block()
    assert "`compile_latex`" in block and "`run_background_command`" in block
    assert "`open_file`" in block and "`get_editor_state`" in block
    for name in ("reveal_in_pdf", "get_problems", "read_terminal", "set_layout"):
        assert f"`{name}`" in block


# ── reveal_in_pdf ────────────────────────────────────────────────────────────


def test_reveal_in_pdf_reports_the_page_and_the_focused_source(project, window):
    window.answer = lambda c: {"ok": True, "page": 7, "open_files": ["main.tex"], "current_file": "main.tex"}
    out = _call(reveal_in_pdf, path="main.tex", line=2)

    assert window.commands[0]["action"] == "reveal_in_pdf"
    assert window.commands[0]["args"] == {"path": "main.tex", "line": 2}
    assert "line 2 of `main.tex` on page 7" in out
    assert "`main.tex` (focused)" in out
    assert _staged(project)["current_file"] == "main.tex"


@pytest.mark.parametrize("kwargs, message", [
    ({"path": "refs.bib", "line": 1}, "not a .tex file"),
    ({"path": "main.tex", "line": 0}, "1-based"),
    ({"path": "main.tex", "line": 9}, "has 3 lines"),
    ({"path": "missing.tex", "line": 1}, "file not found"),
    ({"path": "../elsewhere.tex", "line": 1}, "outside the project"),
])
def test_reveal_in_pdf_bad_requests_never_reach_the_window(project, window, kwargs, message):
    (project / "refs.bib").write_text("@book{x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _call(reveal_in_pdf, **kwargs)
    assert window.commands == []


def test_reveal_in_pdf_passes_on_why_the_pane_could_not_show_it(project, window):
    window.answer = lambda c: {"ok": False, "error": "The PDF of main.tex has no SyncTeX data"}
    with pytest.raises(ValueError, match="no SyncTeX data"):
        _call(reveal_in_pdf, path="main.tex", line=1)


# ── set_layout ───────────────────────────────────────────────────────────────


def test_set_layout_switches_the_window(project, window):
    window.answer = lambda c: {"ok": True, "layout": "document"}
    out = _call(set_layout, layout="document")
    assert window.commands[0]["args"] == {"layout": "document"}
    assert "`document` layout" in out and "preview" in out


def test_an_unknown_layout_lists_the_real_ones_without_asking_the_window(project, window):
    with pytest.raises(ValueError, match="`ide`, `studio`, `document`, `chat`, `chat-bottom`, `review`"):
        _call(set_layout, layout="zen")
    assert window.commands == []


def test_the_layouts_offered_are_the_windows_own():
    import re
    from pathlib import Path

    from gilzabots.tools import IDE_LAYOUTS

    source = (Path(__file__).parent.parent / "gui_src/src/utils/layoutModes.js").read_text(encoding="utf-8")
    declared = re.search(r"export const LAYOUT_MODES = \[([^\]]*)\]", source).group(1)
    assert set(re.findall(r"'([\w-]+)'", declared)) == set(IDE_LAYOUTS)


# ── get_problems ─────────────────────────────────────────────────────────────


def test_an_empty_problems_panel_says_so(project, window):
    window.answer = lambda c: {"ok": True, "problems": []}
    assert _call(get_problems) == "The Problems panel is empty."
    assert window.commands[0]["action"] == "get_problems"


def test_problems_are_listed_with_a_compile_log_trimmed(project, window):
    log = "error: main.tex:3: Undefined control sequence\n" + "x" * 20000
    window.answer = lambda c: {"ok": True, "problems": [
        {"tool": "LaTeX compiler", "severity": "error", "source": "latex_compile",
         "file": "main.tex", "time": "10:02:03", "message": log},
        {"tool": "run_command", "severity": "warning", "source": "", "file": "", "time": "",
         "message": "exit code 2"},
    ]}
    out = _call(get_problems)
    assert "Problems panel (2 entries, oldest first)" in out
    assert "1. [error] LaTeX compiler — `main.tex` (10:02:03)" in out
    assert "Undefined control sequence" in out and "[TRUNCATED]" in out
    assert len(out) < 10000
    assert "2. [warning] run_command\nexit code 2" in out


def test_only_the_most_recent_problems_are_shown(project, window):
    window.answer = lambda c: {"ok": True, "problems": [
        {"tool": "t", "message": f"problem {i}"} for i in range(45)
    ]}
    out = _call(get_problems)
    assert "the 15 oldest are omitted" in out
    assert "problem 44" in out and "problem 14\n" not in out


# ── read_terminal ────────────────────────────────────────────────────────────


def test_screen_text_reads_like_the_terminal():
    from gilzabots.terminal_manager import screen_text

    raw = (
        b"\x1b]0;user@host: ~\x07\x1b[01;32muser@host\x1b[00m:~$ npm run dev\r\n"
        b"\x1b[?2004l building  10%\r building  60%\r building 100%\r\n"
        b"Local:   \x1b[36mhttp://localhost:5173/\x1b[39m\r\n"
        b"typo\x08\x08po\n"
    )
    assert screen_text(raw).split("\n") == [
        "user@host:~$ npm run dev",
        " building 100%",
        "Local:   http://localhost:5173/",
        "typo",
        "",
    ]


class _Session:
    def __init__(self, project_path, output, running=True, dropped=0):
        self.project_path = project_path
        self.is_running = running
        self.buffer = bytearray(output)
        self.total_bytes = len(output) + dropped

    def backlog_since(self, last_offset=None):
        return bytes(self.buffer), self.total_bytes


def _terminal_output(session, project_path):
    from unittest.mock import AsyncMock

    from gilzabots.ide_server import AsyncHTTPServer

    server = AsyncHTTPServer()
    server.active_terminal = session
    responses = []
    server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )
    asyncio.run(server.route_api(
        "GET", "/api/terminal/output", {"projectPath": [project_path]}, {}, b"", AsyncMock(),
    ))
    return responses[0]


def test_the_terminal_endpoint_returns_the_screen_as_text(tmp_path):
    status, body = _terminal_output(_Session(str(tmp_path), b"\x1b[1m$ ls\x1b[0m\r\na.tex\r\n"), str(tmp_path))
    assert status == 200
    assert body == {"text": "$ ls\na.tex\n", "truncated": False}


def test_the_terminal_endpoint_never_starts_or_crosses_into_another_terminal(tmp_path):
    assert _terminal_output(None, str(tmp_path))[0] == 404
    assert _terminal_output(_Session(str(tmp_path), b"", running=False), str(tmp_path))[0] == 404
    status, body = _terminal_output(_Session("/elsewhere", b"secret"), str(tmp_path))
    assert status == 409 and "another project" in body["error"]


@pytest.fixture
def terminal_api(monkeypatch):
    """Answers read_terminal's request as the endpoint would."""
    import io
    import urllib.error
    import urllib.request

    from gilzabots import local_auth

    reply = {"status": 200, "body": {"text": "", "truncated": False}}
    monkeypatch.setattr(local_auth, "local_api_connection", lambda: ("http://127.0.0.1:1", "c=1"))

    def urlopen(request, timeout=None):
        reply["url"] = request.full_url
        payload = json.dumps(reply["body"]).encode("utf-8")
        if reply["status"] != 200:
            raise urllib.error.HTTPError(request.full_url, reply["status"], "", {}, io.BytesIO(payload))
        response = io.BytesIO(payload)
        response.__enter__ = lambda *a: response
        response.__exit__ = lambda *a: None
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return reply


def test_read_terminal_returns_the_last_lines(project, terminal_api):
    terminal_api["body"] = {"text": "\n\n" + "\n".join(f"line {i}" for i in range(1, 101)) + "\n", "truncated": False}
    out = _call(read_terminal, lines=3)
    assert "last 3 of 100 lines" in out
    assert out.endswith("```text\nline 98\nline 99\nline 100\n```")
    assert "projectPath=" in terminal_api["url"]


def test_read_terminal_says_when_older_output_was_dropped(project, terminal_api):
    terminal_api["body"] = {"text": "a\nb\n", "truncated": True}
    assert "older output is no longer kept" in _call(read_terminal)


def test_read_terminal_reports_the_ides_reason(project, terminal_api):
    terminal_api["status"] = 404
    terminal_api["body"] = {"error": "The main terminal is not running."}
    with pytest.raises(ValueError, match="not running"):
        _call(read_terminal)


def test_read_terminal_bounds_its_size(project, terminal_api):
    with pytest.raises(ValueError, match="between 1 and 400"):
        _call(read_terminal, lines=0)


def test_the_window_tools_read_and_show_in_plan_mode(project, window, terminal_api):
    window.answer = lambda c: {"ok": True, "problems": [], "layout": "ide"}
    for tool, kwargs in ((get_problems, {}), (set_layout, {"layout": "ide"}), (read_terminal, {})):
        assert "Execution blocked" not in _call(tool, **kwargs)
