"""The command-line interface: startup, the turn it drives, and how it renders.

The REPL used to be a second implementation of an agent turn. It now drives
``agent_stdin.handle_run`` -- the same engine the desktop app runs -- and renders
the events that turn publishes. These tests cover the seams that makes: what
starts a turn, what stops it, what the terminal shows, and how a question the
agent asks reaches a human when there is no browser to ask in.
"""

import asyncio
import os
import subprocess
import sys
import types

import pytest

import gilzabots.agent_stdin as agent_stdin
import gilzabots.cli as cli
import gilzabots.cli_render as cli_render
from gilzabots.cli_commands import REPLState, _registry
from gilzabots.project import ProjectStore


# ── Packaging promise ────────────────────────────────────────────────────────


def test_the_cli_never_imports_the_desktop_stack():
    """`pip install gilzabots` must not need Qt, so the CLI must not load it.

    Every Qt/pywebview import sits inside a function of `gilzabots/ide_server.py`;
    this is what keeps a module-level one from creeping back and breaking a
    command-line-only install at import time.
    """
    probe = (
        "import gilzabots.cli, gilzabots.cli_render, gilzabots.agent_stdin, "
        "gilzabots.memgpt_runtime, sys;"
        "loaded = {m.split('.')[0] for m in sys.modules};"
        "print(sorted(loaded & {'PyQt6', 'qtpy', 'webview', 'faster_whisper'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout


def test_importing_agent_stdin_leaves_stdout_alone():
    """Only the stdin protocol owns stdout; importing the module must not take it.

    The redirection used to run at import time, and the REPL imports this module
    to reuse the turn engine -- so every Rich frame the CLI drew went to stderr
    and `gilzabots --cli > log.txt` captured nothing.
    """
    # The second report is written to the protocol channel on purpose: once the
    # protocol has claimed stdout, `print` is exactly what must no longer land there.
    probe = (
        "import sys; import gilzabots.agent_stdin as a;"
        "before = sys.stdout is sys.stderr;"
        "a.claim_stdout_for_protocol();"
        "after = sys.stdout is sys.stderr;"
        "print('this must not reach stdout');"
        "a._real_stdout.write(f'{before} {after}')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False True"
    assert "this must not reach stdout" in result.stderr


# ── Startup ──────────────────────────────────────────────────────────────────


def _args(**overrides):
    defaults = dict(
        mode=None, model="ollama/test", db="", lang="en", project=None,
        here=False, prompt=None, cli=True, debug=False,
    )
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _project(tmp_path, with_chat=True, **kwargs):
    """A registered project, opened on a chat named "First" unless *with_chat* is false."""
    db = os.path.join(str(tmp_path), "s.db")
    store = ProjectStore(db_path=db)
    name = kwargs.pop("name", "p")
    project = store.create(
        name=name,
        mode=kwargs.pop("mode", "auto"),
        model=kwargs.pop("model", "ollama/test"),
        project_name="P",
        project_path=str(tmp_path),
        **kwargs,
    )
    if with_chat:
        store.create_chat(name, f"first-{name}", "First")
        project = store.load(name)
    return store, project


def test_the_working_directory_selects_its_project(tmp_path, monkeypatch):
    store, project = _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    resolved = asyncio.run(cli.resolve_project(store, _args(db=store.db_path)))

    assert resolved.name == project.name


def test_the_stored_mode_survives_a_run_that_did_not_ask_to_change_it(tmp_path, monkeypatch):
    """`--mode` has a value even when nobody passed it; writing it back reset the project.

    A project left in 'auto' in the desktop app came back 'plan' after opening
    the CLI once, because the flag's default was saved over the stored setting.
    """
    store, _ = _project(tmp_path, mode="auto")
    monkeypatch.chdir(tmp_path)

    resolved = asyncio.run(cli.resolve_project(store, _args(db=store.db_path)))
    assert resolved.mode == "auto"
    assert store.load("p").mode == "auto"

    resolved = asyncio.run(cli.resolve_project(store, _args(db=store.db_path, mode="edit")))
    assert resolved.mode == "edit"
    assert store.load("p").mode == "edit"


def test_an_unknown_project_name_stops_the_run(tmp_path):
    store, _ = _project(tmp_path)
    with pytest.raises(cli.T.AppExit):
        asyncio.run(cli.resolve_project(store, _args(project="nope", db=store.db_path)))


# ── Creating a project ───────────────────────────────────────────────────────


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """An isolated model store, so a test never touches the real catalog."""
    from gilzabots import models_store
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models.json")
    models_store._invalidate_models_cache()
    yield models_store
    models_store._invalidate_models_cache()


def _register(catalog, *names):
    catalog.add_or_update_connection({"id": "local", "label": "Local", "provider": "ollama"})
    for name in names:
        catalog.add_or_update_model({"id": f"ollama/{name}", "name": name, "connection_id": "local"})


def _create(tmp_path, monkeypatch, answers, **arg_overrides):
    """Run the interactive creation, feeding `answers` to the prompts in order."""
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))
    monkeypatch.chdir(tmp_path)
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    args = _args(db=store.db_path, **{"model": None, **arg_overrides})
    return asyncio.run(cli._create_project(store, args))


def test_a_new_project_does_not_start_on_a_model_nobody_registered(tmp_path, monkeypatch, catalog, capsys):
    """The built-in default was stored on every CLI project and failed to connect."""
    project = _create(tmp_path, monkeypatch, ["T", "", "desc"])

    assert project.model == ""
    assert "catalog is empty" in " ".join(capsys.readouterr().out.split())


def test_the_only_registered_model_becomes_the_main_model(tmp_path, monkeypatch, catalog):
    _register(catalog, "gemma4:31b-cloud")

    project = _create(tmp_path, monkeypatch, ["T", "", "desc"])

    assert project.model == "ollama/gemma4:31b-cloud"


def test_with_several_registered_models_the_user_picks_one(tmp_path, monkeypatch, catalog):
    _register(catalog, "a", "b")

    project = _create(tmp_path, monkeypatch, ["T", "", "desc", "2"])

    assert project.model == "ollama/b"


def test_an_explicit_model_flag_still_wins(tmp_path, monkeypatch, catalog):
    _register(catalog, "a")

    project = _create(tmp_path, monkeypatch, ["T", "", "desc"], model="ollama/flag")

    assert project.model == "ollama/flag"


def test_the_model_flag_has_no_built_in_default():
    assert cli.build_parser().parse_args([]).model is None


def test_a_folder_that_cannot_hold_project_state_is_asked_for_again(tmp_path, monkeypatch, catalog, capsys):
    """Under snap, `~` itself accepts files but not `~/.gilzabots`.

    Stands in for that with a `.gilzabots` that is a file: the folder is writable,
    its state directory is not. Creating the project there anyway printed
    "Failed to save editor state: Permission denied" on every message.
    """
    blocked = tmp_path / "home"
    blocked.mkdir()
    (blocked / ".gilzabots").write_text("")

    project = _create(tmp_path, monkeypatch, ["T", str(blocked), "", "desc"])

    assert "as a project folder" in " ".join(capsys.readouterr().out.split())
    assert project.project_path == str(blocked / "T")
    assert (blocked / "T" / ".gilzabots").is_dir()


def test_project_new_creates_a_project_and_switches_to_it(tmp_path, monkeypatch, catalog):
    _register(catalog, "a")
    store, first = _project(tmp_path)
    state = REPLState(first, store, renderer=object())
    replies = iter(["Second", str(tmp_path / "second"), "desc"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))

    asyncio.run(_registry.dispatch(state, "/project", ["new"]))

    assert state.project.project_name == "Second"
    assert state.project.project_path == str(tmp_path / "second")
    assert state.project.model == "ollama/a"
    assert store.find_by_path(str(tmp_path / "second")) == state.project.name


def test_project_new_can_be_cancelled(tmp_path, monkeypatch, catalog):
    store, first = _project(tmp_path)
    state = REPLState(first, store, renderer=object())
    monkeypatch.setattr("builtins.input", lambda *_a: "cancel")

    assert asyncio.run(_registry.dispatch(state, "/project", ["new"])) == "continue"
    assert state.project.name == first.name


def test_opening_a_project_whose_folder_cannot_hold_its_state_says_so(tmp_path, capsys):
    """A project registered at `~` under snap before the folder was checked."""
    (tmp_path / ".gilzabots").write_text("")
    store, project = _project(tmp_path)
    capsys.readouterr()

    from gilzabots.cli_commands import warn_if_project_dir_unusable
    assert warn_if_project_dir_unusable(project) is True
    out = " ".join(capsys.readouterr().out.split())
    assert "as a project folder" in out
    assert "/project new" in out


def test_a_usable_project_folder_prints_nothing(tmp_path, capsys):
    store, project = _project(tmp_path)
    capsys.readouterr()

    from gilzabots.cli_commands import warn_if_project_dir_unusable
    assert warn_if_project_dir_unusable(project) is False
    assert capsys.readouterr().out == ""


def _start(tmp_path, monkeypatch, cwd, answers, store=None, **arg_overrides):
    """Resolve the startup project from `cwd`, feeding `answers` to the prompts."""
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))
    monkeypatch.chdir(cwd)
    store = store or ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    args = _args(db=store.db_path, **{"model": None, **arg_overrides})
    return store, asyncio.run(cli.resolve_project(store, args))


def test_a_directory_with_an_unregistered_project_is_opened(tmp_path, monkeypatch, catalog):
    """A project copied or cloned from elsewhere: `.gilzabots/` present, no registration."""
    folder = tmp_path / "thesis"
    (folder / ".gilzabots").mkdir(parents=True)

    store, project = _start(tmp_path, monkeypatch, folder, [])

    assert project.project_path == str(folder)
    assert store.find_by_path(str(folder)) == project.name


def test_a_directory_that_is_not_a_project_offers_to_leave(tmp_path, monkeypatch, catalog):
    folder = tmp_path / "plain"
    folder.mkdir()

    with pytest.raises(cli.T.AppExit):
        _start(tmp_path, monkeypatch, folder, ["3"])
    assert not (folder / ".gilzabots").exists()


def test_a_project_can_be_created_in_the_current_directory(tmp_path, monkeypatch, catalog):
    folder = tmp_path / "plain"
    folder.mkdir()

    _store, project = _start(tmp_path, monkeypatch, folder, ["1", "", "desc"])

    assert project.project_path == str(folder)
    assert project.project_name == "plain"


def test_a_project_can_be_created_in_another_directory(tmp_path, monkeypatch, catalog):
    folder = tmp_path / "plain"
    folder.mkdir()
    other = tmp_path / "elsewhere"

    _store, project = _start(tmp_path, monkeypatch, folder, ["2", "Other", str(other), "desc"])

    assert project.project_path == str(other)
    assert not (folder / ".gilzabots").exists()


def test_a_directory_that_cannot_take_a_project_returns_to_the_question(tmp_path, monkeypatch, catalog, capsys):
    """Under snap, `~` itself: creating there fails, and the run must not just end."""
    folder = tmp_path / "home"
    folder.mkdir()
    (folder / ".gilzabots").write_text("")  # stands in for the AppArmor denial

    with pytest.raises(cli.T.AppExit):
        _start(tmp_path, monkeypatch, folder, ["1", "3"])
    out = " ".join(capsys.readouterr().out.split())
    assert "as a project folder" in out
    assert out.count("not an GilzaBots project") == 2


def test_a_registration_without_its_state_directory_is_explained(tmp_path, monkeypatch, catalog, capsys):
    store, project = _project(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / ".gilzabots")

    with pytest.raises(cli.T.AppExit):
        _start(tmp_path, monkeypatch, tmp_path, ["1", "3"], store=store)
    out = " ".join(capsys.readouterr().out.split())
    assert f"gilzabots --delete {project.name}" in out
    assert "already being used by the project" in out


def test_a_repeated_project_name_gets_the_next_free_key(tmp_path, monkeypatch, catalog):
    """Only `<name>_1` was tried; a third project of the same name crashed on the UNIQUE key."""
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    keys = []
    for i in range(3):
        folder = tmp_path / f"d{i}"
        folder.mkdir()
        _store, project = _start(tmp_path, monkeypatch, folder, ["diag", ""], store=store, here=True)
        keys.append(project.name)
    assert keys == ["diag", "diag_1", "diag_2"]


def test_here_still_creates_without_asking(tmp_path, monkeypatch, catalog):
    folder = tmp_path / "plain"
    folder.mkdir()

    _store, project = _start(tmp_path, monkeypatch, folder, ["", "desc"], here=True)

    assert project.project_path == str(folder)


def test_startup_offers_to_continue_an_unfinished_turn(tmp_path, monkeypatch):
    """The old code read a checkpoint constant that exists nowhere: it raised NameError.

    An unfinished turn is recorded in the chat itself, with the markers the
    desktop app's continue button matches.
    """
    store, project = _project(tmp_path)
    store.append_message(project, "user", "do the thing")
    store.append_message(
        project, "assistant",
        "half an answer\n\n" + agent_stdin.INTERRUPTED_AGENT_HISTORY_MARKER,
    )
    project = store.load("p")
    state = REPLState(project, store, renderer=object())

    resumed = {}

    async def _fake_turn(_state, prompt, *, db_path, resume_interrupted=False):
        resumed["prompt"] = prompt
        resumed["resume"] = resume_interrupted
        return True

    monkeypatch.setattr(cli, "run_turn", _fake_turn)
    monkeypatch.setattr(cli.T, "aconfirm", lambda *a, **k: _true())

    asyncio.run(cli._offer_resume_at_startup(state, _args(db=store.db_path)))

    assert resumed == {"prompt": "", "resume": True}


def test_startup_stays_quiet_when_the_last_turn_finished(tmp_path, monkeypatch):
    store, project = _project(tmp_path)
    store.append_message(project, "user", "hi")
    store.append_message(project, "assistant", "a complete answer")
    state = REPLState(store.load("p"), store, renderer=object())

    called = []
    monkeypatch.setattr(
        cli, "run_turn",
        lambda *a, **k: called.append(True) or _true(),
    )
    asyncio.run(cli._offer_resume_at_startup(state, _args(db=store.db_path)))
    assert called == []


async def _true():
    return True


# ── Driving a turn ───────────────────────────────────────────────────────────


def test_a_turn_goes_through_the_shared_engine(tmp_path, monkeypatch):
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())
    seen = {}

    async def _fake_run(data):
        seen.update(data)

    monkeypatch.setattr(agent_stdin, "handle_run", _fake_run)
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_memgpt", None)

    ok = asyncio.run(cli.run_turn(state, "write chapter 2", db_path=store.db_path))

    assert ok is True
    assert seen["agent"] == "chat_orchestrator"
    assert seen["prompt"] == "write chapter 2"
    assert seen["chat_id"] == project.current_chat_id
    assert "resume_interrupted" not in seen


def test_a_first_message_starts_a_chat_when_the_project_has_none(tmp_path, monkeypatch):
    """A project may have no chat; sending a message is what creates one."""
    store, project = _project(tmp_path, with_chat=False)
    assert project.current_chat_id == ""
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())
    seen = {}

    async def _fake_run(data):
        seen.update(data)

    monkeypatch.setattr(agent_stdin, "handle_run", _fake_run)
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_memgpt", None)

    assert asyncio.run(cli.run_turn(state, "hello", db_path=store.db_path)) is True

    chats = store.load("p").chats
    assert len(chats) == 1
    assert seen["chat_id"] == chats[0]["id"]
    assert chats[0]["name"] == cli._("default_chat_name")


def test_a_resumed_turn_does_not_start_a_chat(tmp_path, monkeypatch):
    store, project = _project(tmp_path, with_chat=False)
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())

    async def _fake_run(_data):
        pass

    monkeypatch.setattr(agent_stdin, "handle_run", _fake_run)
    monkeypatch.setattr(agent_stdin, "current_project", project)
    monkeypatch.setattr(agent_stdin, "current_store", store)
    monkeypatch.setattr(agent_stdin, "current_memgpt", None)

    asyncio.run(cli.run_turn(state, "", db_path=store.db_path, resume_interrupted=True))

    assert store.load("p").chats == []


def test_a_resumed_turn_asks_the_engine_to_rebuild_the_prompt(tmp_path, monkeypatch):
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())
    seen = {}

    async def _fake_run(data):
        seen.update(data)

    monkeypatch.setattr(agent_stdin, "handle_run", _fake_run)
    asyncio.run(cli.run_turn(state, "", db_path=store.db_path, resume_interrupted=True))

    assert seen["resume_interrupted"] is True


def test_an_interrupted_turn_returns_to_the_prompt_instead_of_quitting(tmp_path, monkeypatch):
    """Ctrl+C used to break the REPL loop, so interrupting a turn quit the app."""
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())

    async def _slow_run(_data):
        await asyncio.sleep(30)

    monkeypatch.setattr(agent_stdin, "handle_run", _slow_run)

    installed = {}

    def _fake_install(handler):
        installed["handler"] = handler
        return lambda: installed.setdefault("restored", True)

    monkeypatch.setattr(cli, "_install_sigint", _fake_install)

    async def _drive():
        task = asyncio.ensure_future(
            cli.run_turn(state, "long job", db_path=store.db_path)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        installed["handler"]()
        return await task

    completed = asyncio.run(_drive())

    assert completed is False
    assert installed.get("restored") is True


def test_the_repl_adopts_the_state_the_turn_owns(tmp_path, monkeypatch):
    """`handle_run` keeps project/store/orchestrator in module globals for a turn.

    The REPL's own references are stale the moment the turn writes history, so
    the next prompt would run against a project missing the turn that just ran.
    """
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=cli_render.TerminalEventRenderer())

    turn_project = store.load("p")
    turn_project.mode = "edit"
    sentinel = object()

    async def _fake_run(_data):
        agent_stdin.current_project = turn_project
        agent_stdin.current_store = store
        agent_stdin.current_memgpt = sentinel

    monkeypatch.setattr(agent_stdin, "handle_run", _fake_run)
    monkeypatch.setattr(agent_stdin, "current_project", None, raising=False)
    monkeypatch.setattr(agent_stdin, "current_memgpt", None, raising=False)

    asyncio.run(cli.run_turn(state, "go", db_path=store.db_path))

    assert state.project is turn_project
    assert state.memgpt is sentinel


# ── Rendering ────────────────────────────────────────────────────────────────


def _render(events, **kwargs):
    from rich.console import Console

    import gilzabots.terminal as T

    renderer = cli_render.TerminalEventRenderer(**kwargs)
    console = Console(file=_Buffer(), width=200, no_color=True, highlight=False)
    original, T.console = T.console, console
    try:
        renderer.begin_turn()
        for event in events:
            renderer(event)
    finally:
        T.console = original
    return console.file.getvalue(), renderer


class _Buffer:
    def __init__(self):
        self._parts = []

    def write(self, text):
        self._parts.append(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(self._parts)


def test_the_streamed_answer_is_not_printed_twice():
    """One user-facing text channel: the stream and the final response are one answer."""
    out, _ = _render([
        {"event": "stream_chunk", "content": "The result "},
        {"event": "stream_chunk", "content": "is 42."},
        {"event": "agent_response", "response": "The result is 42."},
    ])
    assert out.count("The result is 42.") == 1


def test_a_final_response_the_stream_never_showed_is_printed():
    out, _ = _render([{"event": "agent_response", "response": "Done."}])
    assert "Done." in out


def test_only_the_part_the_stream_missed_is_added():
    out, _ = _render([
        {"event": "stream_chunk", "content": "Half"},
        {"event": "agent_response", "response": "Half and the rest"},
    ])
    assert out.count("Half") == 1
    assert "and the rest" in out


def test_tool_calls_and_results_are_shown():
    out, _ = _render([
        {"event": "tool_call", "tool": "read_file", "arguments": {"path": "main.tex"}},
        {"event": "tool_result", "tool": "read_file", "result": "\\documentclass"},
    ])
    assert "read_file" in out
    assert "main.tex" in out
    assert "documentclass" in out


def test_tool_output_can_be_hidden():
    out, _ = _render(
        [{"event": "tool_call", "tool": "read_file", "arguments": {"path": "main.tex"}}],
        show_tools=False,
    )
    assert "read_file" not in out


def test_reasoning_can_be_hidden():
    out, _ = _render(
        [{"event": "thought", "content": "let me think"}], show_thoughts=False
    )
    assert "let me think" not in out


def test_streamed_reasoning_reads_as_running_text():
    """Reasoning arrives token by token; each delta used to get a line of its own."""
    deltas = ["The", " user", " just", " said", " hello", "."]
    out, _ = _render(
        [{"event": "thought", "content": d} for d in deltas]
        + [{"event": "stream_chunk", "content": "Olá!"}]
    )
    assert "The user just said hello." in out
    assert "Olá!" in out.split("hello.", 1)[1]


def test_think_tags_never_reach_the_visible_answer():
    """The desktop app drops them from the live response; the terminal does too."""
    out, _ = _render([
        {"event": "stream_chunk", "content": "<think>"},
        {"event": "stream_chunk", "content": "Olá!</think>"},
    ])
    assert "<think>" not in out and "</think>" not in out
    assert "Olá!" in out


def test_retracted_reasoning_is_not_left_looking_like_the_answer():
    """An orphan `</think>` proves already-streamed text was reasoning.

    A pipe cannot unprint it, so it must at least be labelled -- it arrives again
    as a thought event, so nothing is lost either way.
    """
    out, renderer = _render([
        {"event": "stream_chunk", "content": "maybe the user means X"},
        {"event": "stream_retract", "content": "maybe the user means X"},
        {"event": "agent_response", "response": "X it is."},
    ])
    assert renderer.streamed_text == ""
    assert "reasoning" in out or "raciocínio" in out
    assert "X it is." in out


def test_a_broken_event_never_takes_the_turn_down():
    renderer = cli_render.TerminalEventRenderer()
    renderer.begin_turn()
    renderer({"event": "tool_result", "result": object()})  # not a string
    renderer({"event": "totally_unknown", "whatever": 1})


# ── Asking a human ───────────────────────────────────────────────────────────


def test_the_terminal_answers_an_input_request(monkeypatch):
    """Tool permission and plan approval used to be answerable only over HTTP.

    Both waited on a future that only the GUI endpoint resolved, so `--cli` and
    `--stdin` sat on the 24h timeout.
    """
    asked = {}

    async def _transport(request):
        asked.update(request)
        return "always"

    agent_stdin.set_input_transport(_transport)
    try:
        answer = asyncio.run(
            agent_stdin.request_user_input(
                "Run rm -rf?", options=["yes", "no", "always"], default="no"
            )
        )
    finally:
        agent_stdin.set_input_transport(None)

    assert answer == "always"
    assert asked["prompt"] == "Run rm -rf?"
    assert asked["options"] == ["yes", "no", "always"]


def test_a_front_end_on_the_event_stream_can_still_answer(monkeypatch):
    """The GUI resolves the pending future instead of installing a transport."""
    events = []
    monkeypatch.setattr(agent_stdin, "event_hook", lambda payload: events.append(payload))

    async def _drive():
        task = asyncio.ensure_future(
            agent_stdin.request_user_input("Proceed?", options=["yes", "no"])
        )
        await asyncio.sleep(0)
        request = next(e for e in events if e["event"] == "input_request")
        agent_stdin.handle_input_response({"id": request["id"], "response": "yes"})
        return await task

    assert asyncio.run(_drive()) == "yes"
    assert any(e["event"] == "input_request_closed" for e in events)


def test_an_unanswered_request_times_out_rather_than_hanging():
    async def _drive():
        with pytest.raises(asyncio.TimeoutError):
            await agent_stdin.request_user_input("Proceed?", timeout=0.01)

    asyncio.run(_drive())


def test_an_unsafe_tool_is_refused_when_the_terminal_says_no(tmp_path):
    """Edit mode asks before each change, and 'no' has to mean no everywhere."""
    import gilzabots.tools as tools

    store, project = _project(tmp_path, mode="edit")
    tools.set_project_context(project, store)
    tools._DENIED_TOOLS.clear()

    async def _transport(_request):
        return "no"

    agent_stdin.set_input_transport(_transport)
    try:
        tool = tools.write_file
        result = asyncio.run(
            tool.run(tool.input_schema()(path=str(tmp_path / "x.txt"), content="hello"))
        )
    finally:
        agent_stdin.set_input_transport(None)
        tools._DENIED_TOOLS.clear()
        tools.set_project_context(None, None)

    assert "Execution blocked" in str(result.result)
    assert not (tmp_path / "x.txt").exists()


# ── Commands ─────────────────────────────────────────────────────────────────


def test_the_orchestrator_is_built_only_when_a_command_needs_it(tmp_path, monkeypatch):
    """The GUI builds a REPLState per slash command; `/help` must not build an agent."""
    store, project = _project(tmp_path)
    built = []
    import gilzabots.memgpt_runtime as runtime

    monkeypatch.setattr(
        runtime, "build_chat_orchestrator",
        lambda *a, **k: built.append(True) or object(),
    )

    state = REPLState(project, store)
    asyncio.run(_registry.dispatch(state, "/help", []))
    assert built == []

    assert state.memgpt is not None
    assert built == [True]


def test_a_terminal_only_command_is_refused_by_the_gui(tmp_path):
    """It would run against a state with no renderer and report success anyway."""
    assert _registry.is_cli_only("/thoughts")
    assert not _registry.is_cli_only("/mode")

    result = asyncio.run(agent_stdin.handle_slash_command({"prompt": "/thoughts on"}))
    assert result["status"] == "done"
    assert "/thoughts" in result["messages"][0]


def test_help_hides_terminal_only_commands_outside_the_terminal(tmp_path):
    names = [line for line, _desc in _registry.help_lines(include_cli_only=False)]
    assert not any(line.startswith("/thoughts") for line in names)
    names = [line for line, _desc in _registry.help_lines(include_cli_only=True)]
    assert any(line.startswith("/thoughts") for line in names)


def test_mode_is_stored_and_reaches_the_permission_gate(tmp_path):
    import gilzabots.tools as tools

    store, project = _project(tmp_path, mode="auto")
    state = REPLState(project, store, renderer=object())
    try:
        asyncio.run(_registry.dispatch(state, "/mode", ["edit"]))
        assert store.load("p").mode == "edit"
        assert tools._PROJECT_SESSION.mode == "edit"
    finally:
        tools.set_project_context(None, None)


def test_chat_switching_reloads_the_project_on_that_chat(tmp_path):
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=object())
    first_chat = project.current_chat_id

    asyncio.run(_registry.dispatch(state, "/chat", ["new draft"]))
    assert state.project.current_chat_id != first_chat
    new_chat = state.project.current_chat_id

    asyncio.run(_registry.dispatch(state, "/chat", ["switch First"]))
    assert state.project.current_chat_id == first_chat

    asyncio.run(_registry.dispatch(state, "/chat", ["switch draft"]))
    assert state.project.current_chat_id == new_chat


def test_resume_only_offers_itself_when_there_is_something_to_continue(tmp_path):
    store, project = _project(tmp_path)
    state = REPLState(project, store, renderer=object())
    assert asyncio.run(_registry.dispatch(state, "/resume", [])) == "continue"

    store.append_message(project, "user", "go")
    store.append_message(
        project, "assistant", "part\n\n" + agent_stdin.INTERRUPTED_AGENT_HISTORY_MARKER
    )
    state.project = store.load("p")
    assert asyncio.run(_registry.dispatch(state, "/resume", [])) == "resume"


def test_cost_reports_the_measurement_that_was_persisted(tmp_path):
    store, project = _project(tmp_path)
    store.save_chat_context_usage(
        "p", project.current_chat_id, {"prompt_tokens": 1200, "context_window": 8192}
    )
    state = REPLState(store.load("p"), store, renderer=object())

    messages = []
    import gilzabots.terminal as T

    original = T.console.print
    T.console.print = lambda *a, **k: messages.append(" ".join(str(x) for x in a))
    try:
        asyncio.run(_registry.dispatch(state, "/cost", []))
    finally:
        T.console.print = original

    text = "\n".join(messages)
    assert "1200" in text and "8192" in text


def test_the_narration_of_a_tool_call_is_not_printed_beside_the_tool_call():
    """`print_event` re-announces tool events as thoughts for panel-based UIs.

    The terminal shows the tool call itself, so the narration would print each
    one twice.
    """
    out, _ = _render([
        {"event": "tool_call", "tool": "write_file", "arguments": {"path": "a.tex"}},
        {"event": "thought", "content": "Decided to execute tool 'write_file'.", "auxiliary": True},
    ])
    assert "Decided to execute" not in out
    assert "write_file" in out


def test_the_narration_returns_when_tool_output_is_hidden():
    out, _ = _render(
        [{"event": "thought", "content": "Decided to execute tool 'write_file'.", "auxiliary": True}],
        show_tools=False,
    )
    assert "Decided to execute" in out


def test_the_last_reflection_is_absorbed_by_the_final_response():
    """The reflection of the closing iteration *is* the answer.

    With streaming off there is no streamed text to compare against, so a
    reflection printed as it arrives showed the answer once as working notes and
    again as the response.
    """
    out, _ = _render([
        {"event": "reflection", "content": "Done: notes.txt written."},
        {"event": "agent_step", "step": 1},
        {"event": "agent_response", "response": "Done: notes.txt written."},
    ])
    assert out.count("Done: notes.txt written.") == 1


def test_a_reflection_the_turn_moved_past_is_still_shown():
    out, _ = _render([
        {"event": "reflection", "content": "First I will read the chapter."},
        {"event": "tool_call", "tool": "read_file", "arguments": {"path": "ch.tex"}},
        {"event": "agent_response", "response": "The chapter is fine."},
    ])
    assert "First I will read the chapter." in out
    assert "The chapter is fine." in out


def test_bookkeeping_events_print_nothing():
    out, _ = _render([
        {"event": "token_usage", "prompt_tokens": 10},
        {"event": "agent_step", "step": 0, "max_steps": 50},
        {"event": "project_loaded", "project_name": "P"},
    ])
    assert out.strip() == ""


def test_the_permission_question_is_asked_in_the_active_language():
    """It was hardcoded Portuguese, so an English session was asked in Portuguese."""
    from gilzabots.i18n import _ as translate, set_lang

    set_lang("en")
    try:
        assert translate("tool_permission_question", tool="write_file") == (
            "The agent wants to use the tool 'write_file'. Allow it?"
        )
    finally:
        set_lang("en")


def test_asking_for_the_window_without_the_bundle_says_so(monkeypatch, tmp_path, capsys):
    """A command-line install has no front-end bundle and no Qt.

    Starting the server anyway serves an API with no pages and opens a browser on
    a blank one; quietly starting the REPL instead would hand back a different
    product than the one that was asked for.
    """
    monkeypatch.setattr(cli, "gui_bundle_path", lambda: str(tmp_path / "absent" / "index.html"))
    started = []
    monkeypatch.setattr(
        "gilzabots.ide_server.start_gui_server",
        lambda **kw: started.append(kw),
        raising=False,
    )

    with pytest.raises(SystemExit) as exit_info:
        cli._start_gui_or_explain()

    assert exit_info.value.code == 2
    assert started == []


# ── Project list at startup (like the desktop window) ────────────────────────


def _pick(tmp_path, monkeypatch, cwd, answers, store, **arg_overrides):
    """Open the REPL's project list from `cwd`, feeding `answers` to the prompts."""
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(replies))
    monkeypatch.chdir(cwd)
    args = _args(db=store.db_path, **{"model": None, **arg_overrides})
    return asyncio.run(cli.resolve_project(store, args, pick=True))


def _registered(store, tmp_path, key, *, with_state=True):
    folder = tmp_path / key
    folder.mkdir()
    if with_state:
        (folder / ".gilzabots").mkdir()
    return store.create(name=key, mode="auto", model="", project_name=key.title(),
                        project_path=str(folder))


def test_the_repl_opens_on_the_registered_projects(tmp_path, monkeypatch, catalog, capsys):
    """The window lists every project in the global store; the REPL did not."""
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    _registered(store, tmp_path, "thesis")
    _registered(store, tmp_path, "paper")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    project = _pick(tmp_path, monkeypatch, elsewhere, ["2"], store)

    out = capsys.readouterr().out
    assert "Thesis" in out and "Paper" in out
    assert project.name in ("thesis", "paper")


def test_enter_opens_the_working_directory_project(tmp_path, monkeypatch, catalog):
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    _registered(store, tmp_path, "thesis")
    paper = _registered(store, tmp_path, "paper")

    project = _pick(tmp_path, monkeypatch, paper.project_path, [""], store)

    assert project.name == "paper"


def test_enter_elsewhere_opens_the_first_project_whose_folder_exists(tmp_path, monkeypatch, catalog):
    """The window's own fallback when it remembers no project."""
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    _registered(store, tmp_path, "thesis")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    project = _pick(tmp_path, monkeypatch, elsewhere, [""], store)

    assert project.name == "thesis"


def test_a_project_whose_folder_is_gone_is_listed_but_not_opened(tmp_path, monkeypatch, catalog, capsys):
    import shutil
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    gone = _registered(store, tmp_path, "gone")
    shutil.rmtree(gone.project_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(cli.T.AppExit):
        # 1 = the missing project, then Exit (last of: gone, create here, new, import, exit).
        _pick(tmp_path, monkeypatch, elsewhere, ["1", "5"], store)
    out = " ".join(capsys.readouterr().out.split())
    assert "(folder not found)" in out
    assert "does not exist on disk" in out
    assert "gilzabots --delete gone" in out


def test_an_existing_project_folder_can_be_imported_from_the_list(tmp_path, monkeypatch, catalog):
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    cloned = tmp_path / "cloned"
    (cloned / ".gilzabots").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # Empty store: create here, new, import, exit.
    project = _pick(tmp_path, monkeypatch, elsewhere, ["3", str(cloned)], store)

    assert project.project_path == str(cloned)
    assert store.find_by_path(str(cloned)) == project.name


def test_an_unregistered_project_in_the_working_directory_is_the_default(tmp_path, monkeypatch, catalog):
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    _registered(store, tmp_path, "thesis")
    cloned = tmp_path / "cloned"
    (cloned / ".gilzabots").mkdir(parents=True)

    project = _pick(tmp_path, monkeypatch, cloned, [""], store)

    assert project.project_path == str(cloned)


def test_a_failed_import_returns_to_the_list(tmp_path, monkeypatch, catalog, capsys):
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(cli.T.AppExit):
        _pick(tmp_path, monkeypatch, plain, ["3", str(plain), "4"], store)
    assert "not a valid GilzaBots project" in " ".join(capsys.readouterr().out.split())


def test_here_skips_the_list(tmp_path, monkeypatch, catalog):
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    _registered(store, tmp_path, "thesis")
    paper = _registered(store, tmp_path, "paper")

    project = _pick(tmp_path, monkeypatch, paper.project_path, [], store, here=True)

    assert project.name == "paper"


# ── Replies written for a terminal ───────────────────────────────────────────


def test_the_orchestrator_is_told_its_replies_are_read_in_a_terminal(tmp_path):
    """The desktop chat renders Markdown and math; a terminal prints them literally."""
    from gilzabots import agent_stdin, cli_render
    from gilzabots.memgpt_runtime import chat_orchestrator_system_prompt

    _store, project = _project(tmp_path)

    gui_prompt = chat_orchestrator_system_prompt(project)
    assert "plain-text terminal" not in gui_prompt

    cli_render.install()
    try:
        assert agent_stdin.reply_surface() == agent_stdin.REPLY_SURFACE_TERMINAL
        cli_prompt = chat_orchestrator_system_prompt(project)
    finally:
        cli_render.uninstall()

    assert "plain-text terminal" in cli_prompt
    assert "Do not typeset mathematics" in cli_prompt
    assert agent_stdin.reply_surface() is None


def test_bare_load_in_the_terminal_offers_the_project_list(tmp_path, monkeypatch, catalog):
    from gilzabots.cli_commands import REPLState, _registry
    store = ProjectStore(db_path=os.path.join(str(tmp_path), "s.db"))
    thesis = _registered(store, tmp_path, "thesis")
    _registered(store, tmp_path, "paper")
    state = REPLState(thesis, store, renderer=object())
    monkeypatch.setattr("builtins.input", lambda *_a: "1")

    asyncio.run(_registry.dispatch(state, "/load", []))

    assert state.project.name == "paper"
