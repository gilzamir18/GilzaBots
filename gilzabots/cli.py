"""GilzaBots CLI – entry point.

The REPL is a terminal front-end over the same turn engine the desktop app runs:
every message goes through ``agent_stdin.handle_run``, and the structured events
that turn publishes are rendered by ``gilzabots/cli_render.py``. It used to call
``memgpt.run()`` directly, which was a second, thinner implementation of a turn --
no streaming, no tool trace, no empty-response or serialized-tool-call recovery,
no interrupted-turn persistence, no context measurement. Those all live in
``handle_run``; routing through it is what keeps the two front-ends from drifting.

The agent workflow itself is untouched: the fixed MemGPT chat-orchestrator
converses and delegates to skills through ``run_skill`` exactly as before.
"""

import asyncio
import argparse
import os
import signal
import sys

from . import __version__
from .config import DEFAULT_MAX_RETRIES, DEFAULT_MODE, DEFAULT_DB_PATH, DEFAULT_LANG
from .project import ProjectStore, ProjectData
from . import terminal as T
from .i18n import _, set_lang
from rich.markup import escape as _escape
from .cli_commands import REPLState, _registry

#: Where the REPL keeps its input history between sessions.
HISTORY_FILENAME = "cli_history"


# ─── Project startup ──────────────────────────────────────────────────────────

class ProjectDirRejected(Exception):
    """A directory the user named cannot hold a new project; already reported."""


def _is_project_dir(path: str) -> bool:
    """Whether `path` already holds an GilzaBots project: it has its `.gilzabots/`."""
    return os.path.isdir(os.path.join(path, ".gilzabots"))


async def startup_menu(store: ProjectStore, args, cwd: str) -> ProjectData:
    """Offer what to do in a directory that is not a project.

    Creating a project there, creating one somewhere else, or leaving. A choice
    that fails (a folder the project could not be kept in, one already
    registered) comes back to this question instead of ending the run.
    """
    create_here = _("cli_start_create_here")
    create_elsewhere = _("cli_start_create_elsewhere")
    leave = _("cli_start_exit")
    while True:
        choice = T.choose(_("cli_start_not_a_project", path=cwd), [create_here, create_elsewhere, leave])
        if choice == leave:
            raise T.AppExit()
        try:
            if choice == create_here:
                return await _create_project(store, args, project_path=cwd)
            return await _create_project(store, args)
        except ProjectDirRejected:
            continue


def _registered_path(entry: dict) -> str:
    raw = entry.get("project_path") or ""
    return os.path.abspath(os.path.expanduser(raw)) if raw else ""


def _import_folder(store: ProjectStore, folder: str) -> ProjectData:
    """Register a folder holding a project, as the desktop Import Project does."""
    from .project import import_project

    project = import_project(store, folder)
    T.success(_("cli_project_imported", name=project.project_name or project.name, path=project.project_path))
    return project


async def project_picker(store: ProjectStore, args, cwd: str) -> ProjectData:
    """The desktop app's project list, as a terminal menu.

    The window opens on the projects registered in the global store
    (``~/.gilzabots/sessions.db``) -- the same list, in the same order (most
    recently updated first) -- and offers New and Import beside them. The REPL
    used to skip all of that and decide from the working directory alone, so it
    read as a different tool that knew nothing of the user's projects.

    The working directory still matters, as the preselected answer: the project
    it holds when there is one, otherwise the first project whose folder exists,
    which is what the window selects when it has no remembered project. A
    project whose folder is gone is listed but cannot be opened, as in the
    window. Any choice that fails comes back to this menu.
    """
    from .project import ProjectImportError

    while True:
        projects = store.list_projects()
        cwd_key = store.find_by_path(cwd)
        cwd_holds_project = _is_project_dir(cwd)

        choices: dict[str, tuple[str, object]] = {}
        default = None
        for entry in projects:
            path = _registered_path(entry)
            exists = bool(path) and os.path.isdir(path)
            label = f"{entry.get('project_name') or entry['name']}  —  {path}"
            if not exists:
                label += f"  {_('cli_picker_folder_missing')}"
            choices[label] = ("open", entry)
            if entry["name"] == cwd_key and exists:
                default = label
            elif default is None and exists and not cwd_key:
                default = label

        if cwd_holds_project and not cwd_key:
            label = _("cli_picker_open_cwd", path=cwd)
            choices[label] = ("import_cwd", None)
            default = label
        if not cwd_holds_project and not cwd_key:
            choices[_("cli_start_create_here")] = ("create_here", None)
        choices[_("cli_picker_new")] = ("create", None)
        choices[_("cli_picker_import")] = ("import", None)
        choices[_("cli_start_exit")] = ("exit", None)

        prompt = _("cli_picker_title") if projects else _("cli_picker_title_empty")
        picked = T.choose(prompt, list(choices), default=default)
        action, entry = choices[picked]

        if action == "exit":
            raise T.AppExit()
        try:
            if action == "open":
                path = _registered_path(entry)
                if not path or not os.path.isdir(path):
                    T.error(_("cli_project_dir_missing", path=path or "—"))
                    T.console.print(f"[dim]{_escape(_('cli_project_dir_missing_hint', project_key=entry['name']))}[/dim]")
                    continue
                project = store.load(entry["name"])
                T.success(_("cli_project_opened", name=project.project_name or project.name))
            elif action == "import_cwd":
                project = _import_folder(store, cwd)
            elif action == "import":
                answer = T.ask(_("cli_picker_import_path", path=cwd)).strip()
                project = _import_folder(store, os.path.expanduser(answer) if answer else cwd)
            elif action == "create_here":
                project = await _create_project(store, args, project_path=cwd)
            else:
                project = await _create_project(store, args)
        except (ProjectDirRejected, T.UserCancelled):
            continue
        except ProjectImportError as exc:
            T.error(str(exc))
            continue
        _apply_mode_override(project, store, args)
        return project


def _project_dir_problem(store: ProjectStore, path: str) -> str:
    """Why `path` cannot take a new project, or ""."""
    from .config import project_dir_error
    registered = store.find_by_path(path)
    if registered:
        existing = store.load(registered)
        return _("project_exists_in_folder",
                 name=(existing.project_name or existing.name) if existing else registered)
    return project_dir_error(path)


async def _create_project(store: ProjectStore, args, project_path: str = "") -> ProjectData:
    """Interactively create a new project.

    A new project starts minimal: only the mandatory chat-orchestrator skill is
    active. Other skills are opt-in — the user adds them with /addskill (which
    writes <project>/skills.yaml). No development skill is auto-loaded.

    ``project_path``, when given, is used without asking: it is the directory the
    user already chose by running the CLI there. When it cannot take a project
    the problem is printed and ``ProjectDirRejected`` raised. Without it the
    folder is asked for, and asked again until one can.
    """
    from .skills import discover_skills, MANDATORY_SKILLS

    if project_path:
        project_path = os.path.abspath(project_path)
        problem = _project_dir_problem(store, project_path)
        if problem:
            T.error(problem)
            raise ProjectDirRejected()

    default_name = os.path.basename(project_path) if project_path else ""
    prompt = f"Project name [{default_name}]" if default_name else "Project name"
    project_name = T.ask(prompt).strip() or default_name or "default"

    if not project_path:
        default_path = os.path.join(os.getcwd(), project_name)
        while True:
            entered_path = T.ask(f"Project path [{default_path}]").strip()
            project_path = os.path.abspath(os.path.expanduser(entered_path) if entered_path else default_path)
            existed = os.path.isdir(project_path)
            problem = _project_dir_problem(store, project_path)
            if not problem:
                break
            # Re-ask instead of creating a project whose state cannot be saved;
            # a subfolder named after the project is the usual answer.
            T.error(problem)
            if existed:
                default_path = os.path.join(project_path, project_name)
        if not existed:
            T.success(f"Directory created: {project_path}")

    description = T.ask("Brief project description").strip()

    optional = [s["name"] for s in discover_skills(project_path)
                if s["name"] not in MANDATORY_SKILLS]
    if optional:
        T.info(f"Available skills (add with /addskill): {', '.join(optional)}")

    base_key = db_key = project_name.replace(" ", "_").lower()
    counter = 1
    while store.exists(db_key):
        db_key = f"{base_key}_{counter}"
        counter += 1

    model = _choose_project_model(args)

    project = store.create(
        name=db_key,
        mode=args.mode or DEFAULT_MODE,
        model=model,
        project_name=project_name,
        project_path=project_path,
        skills=list(MANDATORY_SKILLS),
        description=description,
    )
    T.success(f"Project '{project_name}' created.")
    return project


def _choose_project_model(args) -> str:
    """The main model for a project created in this run.

    ``--model`` wins when it was passed. Otherwise the choice comes from the
    model catalog, the same list the desktop app offers. With nothing registered
    the project starts without a model: the REPL says so and the first turn
    refuses to run, rather than a built-in default the user never configured
    failing to connect.
    """
    if getattr(args, "model", None):
        return args.model
    from .models_store import load_models

    ids = [m["id"] for m in load_models() if m.get("id")]
    if not ids:
        T.warning(_("cli_no_catalog_models"))
        return ""
    if len(ids) == 1:
        T.info(_("cli_project_model_selected", id=ids[0]))
        return ids[0]
    return T.choose(_("cli_choose_project_model"), ids)


def _apply_mode_override(project: ProjectData, store: ProjectStore, args) -> None:
    """Apply ``--mode`` only when it was actually passed.

    The mode is a stored, per-project setting that the desktop app also reads and
    writes. Writing the flag's default over it on every start meant opening the
    CLI silently reset a project the user had left in 'auto' back to 'plan', for
    the GUI too.
    """
    if getattr(args, "mode", None):
        project.mode = args.mode
        store.save(project)


async def resolve_project(store: ProjectStore, args, *, pick: bool = False) -> ProjectData:
    """Pick the project this run works on.

    An explicit ``--project`` wins. The interactive REPL (``pick``) then opens
    on the project list, like the desktop window (``project_picker``), unless
    ``--here`` asks for the working directory's project directly.

    Otherwise -- a one-shot ``-p`` run, or ``--here`` -- the working directory
    decides: a directory holding a project (its ``.gilzabots/``) is opened --
    registered for it already, or imported the way the desktop app's Import
    Project does -- so ``cd thesis && gilzabots -p ...`` behaves like a shell
    tool. Any other directory gets the question of what to do there
    (``startup_menu``), or, with ``--here``, a project created in it without
    asking.
    """
    if args.project:
        if not store.exists(args.project):
            T.error(f"Project '{args.project}' not found.")
            raise T.AppExit()
        project = store.load(args.project)
        _apply_mode_override(project, store, args)
        return project

    cwd = os.getcwd()
    if pick and not getattr(args, "here", False):
        return await project_picker(store, args, cwd)

    registered = store.find_by_path(cwd)
    if _is_project_dir(cwd):
        if registered:
            project = store.load(registered)
            T.success(f"Project '{project.project_name or project.name}' loaded from {cwd}.")
        else:
            from .project import import_project
            project = import_project(store, cwd)
            T.success(_("cli_project_imported", name=project.project_name or project.name, path=cwd))
        _apply_mode_override(project, store, args)
        return project

    if registered:
        # Registered, but its state directory is gone or could never be
        # created.
        existing = store.load(registered)
        T.warning(_("cli_registered_without_state",
                    name=(existing.project_name or existing.name) if existing else registered,
                    project_key=registered, path=cwd))

    if args.here:
        try:
            return await _create_project(store, args, project_path=cwd)
        except ProjectDirRejected:
            raise T.AppExit()

    return await startup_menu(store, args, cwd)


# ─── Agent turns ──────────────────────────────────────────────────────────────

def _install_sigint(handler) -> callable:
    """Route SIGINT to *handler* for the duration of a turn; return the undo.

    Ctrl+C used to break out of the REPL's ``while`` loop, so interrupting a
    long turn quit the application. It now cancels the turn's task, which is the
    same thing /api/gilzabots/interrupt does in the desktop app: ``handle_run``
    catches the cancellation, records the interruption, and keeps whatever the
    agent had already written.
    """
    loop = asyncio.get_event_loop()
    previous = signal.getsignal(signal.SIGINT)

    try:
        loop.add_signal_handler(signal.SIGINT, handler)

        def restore() -> None:
            try:
                loop.remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError):
                pass
            try:
                signal.signal(signal.SIGINT, previous)
            except (TypeError, ValueError, OSError):
                pass

        return restore
    except (NotImplementedError, RuntimeError):
        # Windows has no add_signal_handler. The C-level handler runs in the
        # main thread, so it can only hand the work back to the loop.
        def _forward(_signum, _frame) -> None:
            loop.call_soon_threadsafe(handler)

        try:
            signal.signal(signal.SIGINT, _forward)
        except (TypeError, ValueError, OSError):
            return lambda: None

        def restore_windows() -> None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (TypeError, ValueError, OSError):
                pass

        return restore_windows


def _adopt_turn_state(state: REPLState) -> None:
    """Take over the project/store/orchestrator the finished turn owns.

    ``handle_run`` keeps them in module globals for the duration of a turn
    (PROJECT_DESIGN 2.6) and writes the turn's history and mode into them, so the
    REPL's own references are stale the moment a turn ends.
    """
    from . import agent_stdin

    if agent_stdin.current_project is not None:
        state.project = agent_stdin.current_project
    if agent_stdin.current_store is not None:
        state.store = agent_stdin.current_store
    if agent_stdin.current_memgpt is not None:
        state.memgpt = agent_stdin.current_memgpt


def _ensure_chat(state: REPLState) -> None:
    """Create and open a chat when the project has none, as the GUI does on send.

    A project may have no chat at all; the first message is what starts one.
    """
    if state.project.current_chat_id:
        return
    import uuid

    from .tools import set_project_context

    chat_id = str(uuid.uuid4())
    name = _("default_chat_name")
    state.store.create_chat(state.project.name, chat_id, name)
    state.project = state.store.load(state.project.name, chat_id=chat_id)
    set_project_context(state.project, state.store)
    state.invalidate_memgpt()
    T.info(_("cli_chat_created", name=name))


async def run_turn(
    state: REPLState,
    prompt: str,
    *,
    db_path: str,
    resume_interrupted: bool = False,
) -> bool:
    """Run one agent turn and render it. Returns False when it was interrupted."""
    from . import agent_stdin

    # Resuming continues an existing chat; only a new message starts one.
    if not resume_interrupted:
        _ensure_chat(state)

    if state.renderer is not None:
        state.renderer.begin_turn()

    data = {
        "agent": "chat_orchestrator",
        "prompt": prompt,
        "project_name": state.project.name,
        "project_path": state.project.project_path,
        "chat_id": state.project.current_chat_id,
        "db": db_path,
    }
    if resume_interrupted:
        data["resume_interrupted"] = True

    task = asyncio.ensure_future(agent_stdin.handle_run(data))

    def _cancel() -> None:
        if not task.done():
            T.warning(_("cli_interrupting"))
            task.cancel()

    restore_sigint = _install_sigint(_cancel)
    try:
        # Waiting on the task rather than awaiting it keeps a cancellation from
        # propagating into this coroutine, which was never cancelled itself.
        await asyncio.wait({task})
    finally:
        restore_sigint()
        _adopt_turn_state(state)

    if task.cancelled():
        T.warning(_("cli_turn_interrupted"))
        return False

    error = task.exception()
    if error is not None:
        # handle_run reports everything it catches through the event stream; a
        # failure reaching here escaped it, so it has not been shown yet.
        T.error(_("unexpected_error", err=error))
        if os.environ.get("GILZABOTS_DEBUG") == "1":
            import traceback

            traceback.print_exception(type(error), error, error.__traceback__)
    return True


def _has_model(project: ProjectData) -> bool:
    return bool(str(getattr(project, "model", "") or "").strip())


# ─── REPL ─────────────────────────────────────────────────────────────────────

def _history_path() -> str:
    from .config import get_gilzabots_home

    return os.path.join(get_gilzabots_home(), HISTORY_FILENAME)


def _setup_readline(extra_names: list[str] | None = None) -> None:
    """Persistent input history and slash-command completion, when available.

    *extra_names* are completed like commands: the skills the user may run in
    this project with `/name` (PROJECT_DESIGN 2.29).
    """
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows without pyreadline
        return

    path = _history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            readline.read_history_file(path)
        readline.set_history_length(2000)
    except OSError:
        pass

    names = sorted(set(_registry.names()) | set(extra_names or []))

    def complete(text: str, index: int):
        if not text.startswith("/"):
            return None
        matches = [name for name in names if name.startswith(text)]
        return matches[index] + " " if index < len(matches) else None

    try:
        readline.set_completer(complete)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")
    except Exception:
        pass


def _save_readline_history() -> None:
    try:
        import readline

        readline.write_history_file(_history_path())
    except Exception:
        pass


def _read_prompt(state: REPLState) -> str:
    """Read one line from the user. Raises EOFError on Ctrl+D."""
    T.console.print(
        f"\n[bold cyan]{_escape(state.display_name)}[/bold cyan] "
        f"[dim]({state.project.mode})[/dim]"
    )
    raw = input("› ").strip()
    T._check_cancel(raw)
    return raw


async def repl_loop(project: ProjectData, store: ProjectStore, args) -> None:
    from .tools import set_project_context
    from .skills import active_skills
    from . import cli_render

    set_project_context(project, store)
    renderer = cli_render.install()
    from .skill_invocation import invocable_skills
    _setup_readline([f"/{skill['name']}" for skill in invocable_skills(project.project_path)])

    state = REPLState(project, store, renderer=renderer)

    T.section(f"Active Project: {_escape(project.project_name or project.name)}")
    T.console.print(f"  [dim]Path:   {_escape(project.project_path)}[/dim]")
    _active = ", ".join(s["name"] for s in active_skills(project.project_path))
    T.console.print(f"  [dim]Skills: {_active}[/dim]")
    T.console.print(f"  [dim]{_('type_help')} {_('cli_interrupt_hint')}[/dim]")

    from .cli_commands import warn_if_project_dir_unusable
    warn_if_project_dir_unusable(state.project)

    if not _has_model(state.project):
        T.warning(_("cli_no_model_configured"))

    await _offer_resume_at_startup(state, args)

    while True:
        try:
            user_input = _read_prompt(state)
            if not user_input:
                continue

            if user_input.startswith("/"):
                cmd, *rest = user_input.split(maxsplit=1)
                if cmd not in _registry:
                    kind = _classify_skill_line(state.project, user_input)
                    if kind == "unknown":
                        T.error(_("unknown_command", cmd=cmd))
                    elif kind == "skill":
                        if not _has_model(state.project):
                            T.error(_("cli_no_model_configured"))
                        else:
                            await run_turn(state, user_input, db_path=args.db)
                    continue
                result = await _registry.dispatch(state, cmd, rest)
                if result == "break":
                    break
                if result == "resume":
                    T.info(_("cli_resume_starting"))
                    await run_turn(state, "", db_path=args.db, resume_interrupted=True)
                continue

            if not _has_model(state.project):
                T.error(_("cli_no_model_configured"))
                continue

            await run_turn(state, user_input, db_path=args.db)

        except KeyboardInterrupt:
            # At the prompt, not during a turn: nothing is running to interrupt.
            T.console.print()
            T.info(_("cli_interrupt_hint"))
        except EOFError:
            T.info(_("exiting"))
            break
        except T.UserCancelled:
            T.info(_("repl_cancelled"))
        except T.AppExit:
            T.info(_("exiting"))
            break
        except Exception as e:
            import traceback

            traceback.print_exc()
            T.error(_("unexpected_error", err=e))

    _save_readline_history()
    cli_render.uninstall()


async def _offer_resume_at_startup(state: REPLState, args) -> None:
    """Offer to continue a turn this chat never finished.

    The previous code looked for a checkpoint *file* whose path constant no
    longer existed anywhere in the project -- the REPL raised ``NameError`` here
    on every start. Checkpoints moved to shadow Git (``gilzabots/vcs.py``) and an
    unfinished turn is now recorded in the chat itself, with the same markers the
    desktop app's "Continue" button matches (``agent_stdin.TURN_MARKERS``).
    """
    from .agent_stdin import unfinished_turn_content

    if not unfinished_turn_content(getattr(state.project, "history", []) or []):
        return
    T.warning(_("cli_unfinished_turn_detected"))
    if not await T.aconfirm(_("resume_or_clear") + f" [{_('resume')}]", default=True):
        return
    T.info(_("cli_resume_starting"))
    await run_turn(state, "", db_path=args.db, resume_interrupted=True)


# ─── One-shot ─────────────────────────────────────────────────────────────────

async def run_once(project: ProjectData, store: ProjectStore, prompt: str, args) -> int:
    """Run a single prompt and exit. The scriptable form of the REPL."""
    from .tools import set_project_context
    from . import cli_render

    set_project_context(project, store)
    if not _has_model(project):
        T.error(_("cli_no_model_configured"))
        return 2

    renderer = cli_render.install()
    state = REPLState(project, store, renderer=renderer)
    try:
        completed = await run_turn(state, prompt, db_path=args.db)
    finally:
        cli_render.uninstall()
    return 0 if completed else 130


def _classify_skill_line(project: ProjectData, line: str) -> str:
    """How a `/word` line that is not a built-in command is handled.

    "skill" when it runs a skill the user may invoke (a turn, PROJECT_DESIGN
    2.29); "error" when it names an installed skill that cannot run now, already
    reported; "unknown" otherwise.
    """
    from .memgpt_runtime import main_agent_prompt_profile
    from .skill_invocation import SkillInvocationError, resolve_skill_invocation

    try:
        invocation = resolve_skill_invocation(
            project.project_path, line, profile=main_agent_prompt_profile(project)
        )
    except SkillInvocationError as exc:
        T.error(str(exc))
        return "error"
    return "skill" if invocation is not None else "unknown"


async def run_command_once(project: ProjectData, store: ProjectStore, line: str, args) -> int:
    """Run a single slash command and exit.

    Registering a model, switching mode or listing checkpoints from a shell
    script is the same need a one-shot prompt serves, and sending `/models add
    ...` to the model as if it were a question would be the wrong action
    entirely.
    """
    from .tools import set_project_context
    from . import cli_render

    set_project_context(project, store)
    cmd, *rest = line.split(maxsplit=1)
    if cmd not in _registry:
        kind = _classify_skill_line(project, line)
        if kind == "skill":
            return await run_once(project, store, line, args)
        if kind == "unknown":
            T.error(_("unknown_command", cmd=cmd))
        return 2

    renderer = cli_render.install()
    state = REPLState(project, store, renderer=renderer)
    try:
        result = await _registry.dispatch(state, cmd, rest)
        if result == "resume":
            T.info(_("cli_resume_starting"))
            completed = await run_turn(state, "", db_path=args.db, resume_interrupted=True)
            return 0 if completed else 130
    finally:
        cli_render.uninstall()
    return 0


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gilzabots",
        description="GilzaBots – project-centric coding agent",
    )
    parser.add_argument("--version", action="version", version=f"GilzaBots {__version__}")
    parser.add_argument(
        "--mode", choices=["auto", "plan", "edit"], default=None,
        help="Override the project's stored mode for this run onwards (default: keep it)",
    )
    parser.add_argument("--model", default=None, help="LLM model for a project created in this run (default: choose from the model catalog)")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--lang", choices=["en", "pt"], default=DEFAULT_LANG)
    parser.add_argument("--delete", metavar="PROJECT_NAME", help="Delete a project and exit")
    parser.add_argument("--list-projects", action="store_true", help="List all projects and exit")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--stdin", action="store_true", help="Start agent server in stdin/stdout mode")
    parser.add_argument("--gui", action="store_true", help="Start agent server with React Web GUI (Default)")
    parser.add_argument("--cli", action="store_true", help="Start in interactive CLI REPL mode")
    parser.add_argument("--project", metavar="NAME", help="Work on this project instead of the one in the current directory")
    parser.add_argument("--here", action="store_true", help="Skip the project list: open the current directory's project, or create one there")
    parser.add_argument(
        "-p", "--prompt", metavar="TEXT",
        help="Run a single prompt and exit, instead of starting the REPL. Reads stdin when TEXT is '-'.",
    )
    return parser


def _read_stdin_prompt() -> str:
    data = sys.stdin.read()
    return data.strip()


def gui_bundle_path() -> str:
    """Absolute path of the built React front-end inside the package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui", "index.html")


def _start_gui_or_explain() -> None:
    """Launch the desktop window, or say plainly why this install cannot.

    The graphical interface needs the ``gui`` extra and the built front-end
    bundle, which a command-line install carries neither of. Starting anyway
    would leave ``start_gui_server`` serving its API with no pages and opening a
    browser on a blank one; falling back to the REPL instead would substitute a
    different product for the one that was asked for. Both are worse than saying
    what is missing.

    A missing *pywebview* is deliberately not checked here: the server's own
    fallback opens the built front-end in the default browser, which is still the
    graphical interface.
    """
    bundle = gui_bundle_path()
    if not os.path.exists(bundle):
        T.error(_("cli_gui_unavailable"))
        T.console.print(f"[dim]Front-end bundle not found at {_escape(bundle)}[/dim]")
        sys.exit(2)
    from .ide_server import start_gui_server

    start_gui_server(host="127.0.0.1", port=3000)


def _monitors_argv(argv: list[str]) -> list[str] | None:
    """The arguments of ``gilzabots monitors ...``, or None for any other command."""
    if argv and argv[0] == "monitors":
        return argv[1:]
    return None


def main() -> None:
    monitors_args = _monitors_argv(sys.argv[1:])
    if monitors_args is not None:
        from .monitors.cli import main as monitors_main

        sys.exit(monitors_main(monitors_args))
    if sys.argv[1:2] == ["transfer"]:
        from .transfer_cli import main as transfer_main

        sys.exit(transfer_main(sys.argv[2:]))

    parser = build_parser()
    args = parser.parse_args()

    # Installed before anything else runs: a crash in the embedded browser kills
    # this process without raising a Python exception, and this is what leaves a
    # record of it behind (see gilzabots/crash_report.py).
    from .crash_report import install as install_crash_reporting
    install_crash_reporting()

    if args.debug:
        from gilzabots.config import setup_debug_logging
        setup_debug_logging()
        os.environ["GILZABOTS_DEBUG"] = "1"

    set_lang(args.lang)

    if getattr(args, "stdin", False):
        from .agent_stdin import start_stdin_server
        start_stdin_server()
        sys.exit(0)

    store = ProjectStore(db_path=args.db)

    if getattr(args, "list_projects", False):
        projects = store.list_projects()
        if not projects:
            T.info("No projects found.")
        else:
            T.section("Existing Projects")
            for p in projects:
                pname = p["project_name"] or p["name"]
                T.console.print(
                    f"  [cyan]{_escape(p['name'])}[/cyan]  [bold]{_escape(pname)}[/bold]  "
                    f"[dim]{_escape(p['project_path'])}  {p['updated_at'][:10]}[/dim]"
                )
        sys.exit(0)

    if getattr(args, "delete", False):
        if store.exists(args.delete):
            store.delete(args.delete)
            T.success(f"Project '{args.delete}' deleted.")
        else:
            T.error(f"Project '{args.delete}' not found.")
        sys.exit(0)

    # A prompt is a command-line run by definition; the REPL and the window are
    # both interactive, and neither can carry one.
    if args.prompt:
        prompt = _read_stdin_prompt() if args.prompt == "-" else args.prompt
        if not prompt:
            T.error("Empty prompt.")
            sys.exit(2)
        try:
            project = asyncio.run(resolve_project(store, args))
        except T.AppExit:
            sys.exit(2)
        if prompt.startswith("/"):
            sys.exit(asyncio.run(run_command_once(project, store, prompt, args)))
        sys.exit(asyncio.run(run_once(project, store, prompt, args)))

    # Default to launching the GUI server unless --cli is explicitly passed
    if not getattr(args, "cli", False):
        _start_gui_or_explain()
        sys.exit(0)

    try:
        project = asyncio.run(resolve_project(store, args, pick=True))
        # After resolution, so the banner reports the mode this session will
        # actually run in rather than the configured default.
        T.print_banner(version=__version__, mode=project.mode)
        asyncio.run(repl_loop(project, store, args))
    except KeyboardInterrupt:
        T.warning(_("repl_interrupted"))
        sys.exit(0)
    except T.AppExit:
        T.info(_("exiting"))
        sys.exit(0)


if __name__ == "__main__":
    main()
