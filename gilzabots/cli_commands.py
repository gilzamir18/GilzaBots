"""REPL command registry and handlers for GilzaBots CLI."""

import asyncio
import os

from .project import ProjectStore, ProjectData
from . import terminal as T
from .i18n import _
from rich.markup import escape as _escape


# ─── REPL state container ─────────────────────────────────────────────────────

class REPLState:
    """The project a command operates on, plus the orchestrator it may need.

    ``renderer`` is the terminal event renderer when the command-line interface
    built this state, and ``None`` when the GUI did (it renders the same events
    itself). Commands that only make sense in a terminal check it.
    """

    def __init__(
        self,
        project: ProjectData,
        store: ProjectStore,
        renderer: object | None = None,
    ):
        self.project = project
        self.store = store
        self.renderer = renderer
        self._memgpt = None

    @property
    def memgpt(self):
        """The fixed MemGPT chat-orchestrator, built on first use.

        Skills-oriented architecture: it both converses and delegates to skills
        via run_skill. Building it seeds memory from the project's history and
        reads every active skill, which is real work -- and most commands never
        touch it. The GUI builds one REPLState per slash command, so `/help` used
        to construct a whole orchestrator to print a list.
        """
        if self._memgpt is None:
            from .memgpt_runtime import build_chat_orchestrator
            self._memgpt = build_chat_orchestrator(self.project, self.store)
        return self._memgpt

    @memgpt.setter
    def memgpt(self, value) -> None:
        self._memgpt = value

    def invalidate_memgpt(self) -> None:
        """Drop the built orchestrator so the next use reflects changed state."""
        self._memgpt = None

    @property
    def display_name(self) -> str:
        return self.project.project_name or self.project.name


# ─── Command registry ─────────────────────────────────────────────────────────

class CommandRegistry:
    """The slash commands, shared by the CLI REPL and the GUI chat.

    Both front-ends dispatch through this one registry
    (``agent_stdin.handle_slash_command`` is the GUI's door), so a command added
    here appears in both. ``cli_only`` marks the ones that do not: a command that
    needs a terminal renderer, or that changes state the GUI tracks in its own
    UI, would be a no-op reporting success there.
    """

    def __init__(self):
        self._cmds: dict[str, tuple] = {}

    def register(self, *names: str, usage: str = "", description: str = "",
                 cli_only: bool = False, details: str = ""):
        """Register *names* for one handler.

        ``usage`` is the argument shape, ``details`` the worked examples shown
        by ``/help <command>``. A command with subcommands and ``key=value``
        fields cannot be learned from a one-line listing, so the listing points
        at the details instead of trying to carry them.
        """
        def decorator(fn):
            for name in names:
                self._cmds[name] = (fn, usage, description, cli_only, details)
            return fn
        return decorator

    def __contains__(self, cmd: str) -> bool:
        return cmd in self._cmds

    def is_cli_only(self, cmd: str) -> bool:
        entry = self._cmds.get(cmd)
        return bool(entry and entry[3])

    def names(self) -> list[str]:
        """Every registered command name, for shell completion."""
        return sorted(self._cmds)

    def aliases(self, cmd: str) -> list[str]:
        """Every name that reaches the same handler as *cmd*, primary first."""
        handler = self._cmds[cmd][0]
        return [name for name, entry in self._cmds.items() if entry[0] is handler]

    def entry(self, cmd: str) -> dict | None:
        """The full record behind a command name."""
        record = self._cmds.get(cmd)
        if record is None:
            return None
        fn, usage, description, cli_only, details = record
        names = self.aliases(cmd)
        return {
            "name": names[0],
            "aliases": names[1:],
            "usage": usage,
            "description": description,
            "cli_only": cli_only,
            "details": details,
        }

    def entries(self, include_cli_only: bool = True) -> list[dict]:
        """One record per handler, in registration order."""
        seen, result = set(), []
        for name, (fn, _usage, _desc, cli_only, _details) in self._cmds.items():
            if fn in seen:
                continue
            seen.add(fn)
            if cli_only and not include_cli_only:
                continue
            result.append(self.entry(name))
        return result

    def help_lines(self, include_cli_only: bool = True) -> list[tuple[str, str]]:
        return [
            (f"{e['name']} {e['usage']}".strip(), e["description"])
            for e in self.entries(include_cli_only)
        ]

    async def dispatch(self, state: REPLState, cmd: str, args: list[str]) -> str | None:
        fn = self._cmds[cmd][0]
        return await fn(state, args)


_registry = CommandRegistry()


# ─── Command handlers ─────────────────────────────────────────────────────────

#: Which section of `/help` each command belongs to, in display order.
#:
#: Kept here rather than on each decorator so the order of the listing is
#: explicit and does not depend on which module imported first. A command
#: missing from this map is a bug the help would hide, so a test fails on it.
COMMAND_GROUPS: dict[str, tuple[str, ...]] = {
    "help_group_session": ("/help", "/mode", "/cost", "/resume", "/thoughts", "/tools", "/exit"),
    "help_group_projects": ("/project", "/list", "/load", "/rename", "/delete"),
    "help_group_chats": ("/chat", "/effort", "/history", "/clear_chat", "/clear"),
    "help_group_models": (
        "/add-provider", "/add-model", "/models", "/providers",
        "/set-provider-field", "/set-model-field", "/remove-provider", "/remove-model",
        "/set-main-model", "/set-worker-model", "/set-model-param",
    ),
    "help_group_skills": ("/skills", "/lsskills", "/addskill", "/rmskill",
                          "/list_assets", "/load_asset"),
    "help_group_vcs": ("/commit", "/undo", "/checkpoints", "/restoreckp", "/removechk"),
    "help_group_build": ("/compile",),
    "help_group_monitors": ("/monitors",),
    "help_group_connectors": ("/connectors",),
}


def _grouped_entries(include_cli_only: bool) -> list[tuple[str, list[dict]]]:
    """`/help` sections, each with the entries that belong to it."""
    available = {e["name"]: e for e in _registry.entries(include_cli_only)}
    groups = []
    for group_key, names in COMMAND_GROUPS.items():
        members = [available.pop(name) for name in names if name in available]
        if members:
            groups.append((group_key, members))
    if available:
        # Anything not placed still has to be reachable.
        groups.append(("help_group_other", list(available.values())))
    return groups


def _print_command(entry: dict, indent: str = "    ") -> None:
    """Name and argument shape on one line, description under it.

    Everything is escaped: a usage string like `[list | show <id>]` is Rich
    markup by accident, and printing it unescaped silently swallowed the
    argument shape of every command that had one -- which is most of the
    commands anyone needs to look up.
    """
    signature = f"{entry['name']} {entry['usage']}".strip()
    alias_note = f"  (also {', '.join(entry['aliases'])})" if entry["aliases"] else ""
    T.console.print(
        f"{indent}{signature}{alias_note}",
        markup=False, highlight=False, soft_wrap=True, style="green",
    )
    if entry["description"]:
        T.console.print(
            f"{indent}    {entry['description']}",
            markup=False, highlight=False, style="dim",
        )


@_registry.register(
    "/help", "/h",
    usage="[command]",
    description="List the commands, or show one command's arguments and examples",
)
async def cmd_help(state: REPLState, args: list[str]) -> str | None:
    include_cli_only = state.renderer is not None
    wanted = " ".join(args).split()

    if wanted:
        name = wanted[0]
        if not name.startswith("/"):
            name = "/" + name
        entry = _registry.entry(name)
        if entry is None:
            T.error(_("unknown_command", cmd=name))
            return "continue"
        if entry["cli_only"] and not include_cli_only:
            # It exists; it just does nothing here. Saying "unknown" would send
            # the reader looking for a typo they did not make.
            T.error(_("cli_command_cli_only", cmd=name))
            return "continue"
        T.console.print()
        _print_command(entry, indent="  ")
        if entry["details"]:
            T.console.print()
            for line in entry["details"].strip("\n").split("\n"):
                # soft_wrap leaves wrapping to the terminal instead of inserting
                # a break, so a long example line stays one line when copied.
                T.console.print(
                    f"  {line}" if line.strip() else "",
                    markup=False, highlight=False, soft_wrap=True,
                )
        T.console.print()
        return "continue"

    T.console.print(f"\n[cyan]{_('available_commands')}[/cyan]")
    for group_key, members in _grouped_entries(include_cli_only):
        T.console.print(f"\n  [bold]{_(group_key)}[/bold]")
        for entry in members:
            _print_command(entry)
    T.console.print(f"\n  [dim]{_escape(_('help_more'))}[/dim]\n")
    return "continue"


@_registry.register("/clear", description="Clear project memory and ALL chats histories")
async def cmd_clear(state: REPLState, _args: list[str]) -> None:
    if await T.aconfirm("Are you sure you want to clear this project's global memory and ALL chats?"):
        state.project = state.store.overwrite(
            state.project.name, state.project.mode, state.project.model,
            state.project.project_name, state.project.project_path,
            state.project.skills, state.project.description,
            worker_model=state.project.worker_model,
            api_key=state.project.api_key,
            api_base=state.project.api_base,
            worker_api_key=state.project.worker_api_key,
            worker_api_base=state.project.worker_api_base,
            model_params=state.project.model_params,
            use_shared_memory=state.project.use_shared_memory,
        )
        state.invalidate_memgpt()
        from .archival import clear_archival
        clear_archival(state.project.name)
        # Every chat is gone, so no stored measurement describes anything.
        from .token_usage import reset_context_usage
        reset_context_usage()
        T.success("Project global memory and all chats cleared.")
    else:
        T.warning(_("cli_action_cancelled"))

@_registry.register("/clear_chat", description="Clear only the current chat's history and its isolated memory (if any)")
async def cmd_clear_chat(state: REPLState, _args: list[str]) -> None:
    chat_id = getattr(state.project, "current_chat_id", "")
    if not chat_id:
        T.info(_("cli_no_chats"))
        return
    if await T.aconfirm(f"Are you sure you want to clear chat '{chat_id}' history?"):
        from .chat_ops import clear_chat
        state.project = clear_chat(state.project, state.store, chat_id)
        state.invalidate_memgpt()
        T.success(f"Chat '{chat_id}' cleared.")
    else:
        T.warning(_("cli_action_cancelled"))



@_registry.register("/rename", usage="<new_name>", description="Rename the current project")
async def cmd_rename(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /rename <new_name>")
        return "continue"
    new_name = args[0].strip('"\'')
    if state.store.rename(state.project.name, new_name):
        state.project.name = new_name
        state.store.save(state.project)
        T.success(f"Project renamed to '{new_name}'.")
    else:
        T.error(f"A project named '{new_name}' already exists.")


@_registry.register("/list", description="List all projects")
async def cmd_list(state: REPLState, _args: list[str]) -> None:
    projects = state.store.list_projects()
    if not projects:
        T.info("No projects found.")
    else:
        T.console.print(f"\n[dim]Existing projects:[/dim]")
        for p in projects:
            pname = p["project_name"] or p["name"]
            T.console.print(
                f"  [cyan]{_escape(p['name'])}[/cyan]  "
                f"[bold]{_escape(pname)}[/bold]  "
                f"[dim]{_escape(p['project_path'])}  {p['updated_at'][:10]}  mode={p['mode']}[/dim]"
            )
        T.console.print()


def warn_if_project_dir_unusable(project: ProjectData) -> bool:
    """Say, once on opening, when the project's folder cannot hold its state.

    A project created before the folder was checked otherwise surfaces only as "Permission denied" on
    ``<folder>/.gilzabots`` during every turn. A folder that no longer exists is
    left alone: probing it would recreate it.
    """
    from .config import project_dir_error
    path = project.project_path or ""
    if not os.path.isdir(path):
        return False
    problem = project_dir_error(path)
    if not problem:
        return False
    T.error(problem)
    T.console.print(f"[dim]{_escape(_('cli_project_dir_unusable_hint'))}[/dim]")
    return True


def _switch_project(state: REPLState, project: ProjectData) -> None:
    from .tools import set_project_context
    state.project = project
    set_project_context(state.project, state.store)
    # Drop the MemGPT so the next turn rebuilds it for the newly loaded
    # project (re-scopes file tools, reseeds memory from its history).
    state.invalidate_memgpt()


@_registry.register("/load", usage="[name]", description="Load another project (bare, in the terminal: choose from the list)")
async def cmd_load(state: REPLState, args: list[str]) -> str | None:
    if not args or not args[0].strip():
        # The terminal gets the project list the window shows; the desktop
        # chat's command box has no keyboard prompt to show it with.
        if state.renderer is None:
            T.error("Usage: /load <name>")
            return "continue"
        return await _load_from_list(state)
    name = args[0].strip('"\'')
    if not state.store.exists(name):
        T.error(f"Project '{name}' not found.")
        return "continue"
    loaded = state.store.load(name)
    if loaded:
        _switch_project(state, loaded)
        T.success(f"Project '{name}' loaded.")
        T.console.print(f"  [dim]Skills: {', '.join(state.project.skills)}[/dim]")
        warn_if_project_dir_unusable(state.project)
        if state.project.request and state.project.plan_text and not state.project.results:
            T.warning(_("pending_demand", request=state.project.request[:50]))
    else:
        T.error(f"Project '{name}' not found.")


async def _load_from_list(state: REPLState) -> str:
    """Pick the project to switch to from the registered ones."""
    entries = {}
    for p in state.store.list_projects():
        if p["name"] == state.project.name:
            continue
        path = os.path.abspath(os.path.expanduser(p.get("project_path") or ""))
        label = f"{p.get('project_name') or p['name']}  —  {path}"
        if not os.path.isdir(path):
            label += f"  {_('cli_picker_folder_missing')}"
        entries[label] = (p["name"], path)
    if not entries:
        T.info(_("cli_load_no_other_project"))
        return "continue"
    try:
        picked = T.choose(_("cli_picker_title"), list(entries))
    except (T.UserCancelled, EOFError):
        T.warning(_("cli_action_cancelled"))
        return "continue"
    name, path = entries[picked]
    if not os.path.isdir(path):
        T.error(_("cli_project_dir_missing", path=path))
        T.console.print(f"[dim]{_escape(_('cli_project_dir_missing_hint', project_key=name))}[/dim]")
        return "continue"
    return await cmd_load(state, [name])


@_registry.register(
    "/project", usage="[new]", cli_only=True,
    description="Show the current project, or create a new one and switch to it",
    details="Examples:\n  /project        name, folder and model of the current project\n"
            "  /project new    create a project interactively and switch to it\n\n"
            "Other projects: /list, /load <name>, /rename, /delete.",
)
async def cmd_project(state: REPLState, args: list[str]) -> str | None:
    sub = args[0].strip().lower() if args and args[0].strip() else ""
    if not sub:
        T.console.print(f"\n[dim]Project '{_escape(state.display_name)}' ({_escape(state.project.name)}):[/dim]")
        T.console.print(f"  [cyan]folder[/cyan]  {_escape(state.project.project_path or '')}")
        T.console.print(f"  [cyan]model[/cyan]   {_escape(state.project.model or _('cli_model_not_set'))}")
        T.console.print(f"  [cyan]mode[/cyan]    {_escape(state.project.mode or '')}\n")
        return "continue"
    if sub != "new":
        T.error("Usage: /project [new]")
        return "continue"
    import types
    from .cli import _create_project
    try:
        created = await _create_project(state.store, types.SimpleNamespace(mode=None, model=None))
    except T.UserCancelled:
        T.warning(_("cli_action_cancelled"))
        return "continue"
    _switch_project(state, created)
    T.console.print(f"  [dim]Path:   {_escape(created.project_path)}[/dim]")
    return "continue"


@_registry.register("/delete", usage="<name>", description="Delete a project")
async def cmd_delete(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /delete <name>")
        return "continue"
    name = args[0].strip('"\'')
    if not state.store.exists(name):
        T.error(f"Project '{name}' not found.")
        return "continue"
    
    project_to_delete = state.store.load(name)
    state.store.delete(name)

    import os
    import shutil
    if project_to_delete and project_to_delete.project_path and os.path.exists(project_to_delete.project_path):
        if await T.aconfirm(_("delete_dir_confirm", path=project_to_delete.project_path), default=False):
            try:
                shutil.rmtree(project_to_delete.project_path)
                T.success(_("dir_deleted", path=project_to_delete.project_path))
            except Exception as e:
                T.error(_("dir_delete_failed", err=str(e)))
        else:
            gilzabots_dir = os.path.join(project_to_delete.project_path, ".gilzabots")
            if os.path.exists(gilzabots_dir):
                try:
                    shutil.rmtree(gilzabots_dir)
                    T.success(_("vcs_deleted"))
                except Exception as e:
                    T.error(_("vcs_delete_failed", err=str(e)))

    T.success(f"Project '{name}' deleted.")
    if state.project.name == name:
        T.info("Current project was deleted. Please restart GilzaBots.")
        return "break"


def _rebuild_memgpt(state: REPLState) -> None:
    """Drop the MemGPT so a skills.yaml or model change takes effect immediately."""
    state.invalidate_memgpt()


@_registry.register("/lsskills", description="List active skills for this project")
async def cmd_lsskills(state: REPLState, _args: list[str]) -> None:
    from .skills import active_skills
    T.console.print(f"\n[dim]Active skills for this project:[/dim]")
    for s in active_skills(state.project.project_path):
        T.console.print(f"  [cyan]{s['name']}[/cyan]  [dim]{s['description']}[/dim]")
    T.console.print()


@_registry.register("/skills", description="List all available skills (active marked with *)")
async def cmd_skills(state: REPLState, _args: list[str]) -> None:
    from .skills import discover_skills, active_skills
    discovered = discover_skills(state.project.project_path)
    active_names = {s["name"] for s in active_skills(state.project.project_path)}
    if not discovered:
        T.info("No skills found.")
        return
    T.console.print(f"\n[dim]Available skills:[/dim]")
    for s in discovered:
        mark = "[green]*[/green] " if s["name"] in active_names else "  "
        T.console.print(f"  {mark}[cyan]{s['name']}[/cyan]  [dim]{s['description']}[/dim]")
    T.console.print(f"\n[dim]([green]*[/green] = active in this project)[/dim]\n")


@_registry.register("/addskill", usage="<name>", description="Add a skill to this project")
async def cmd_addskill(state: REPLState, args: list[str]) -> str | None:
    from .skills import add_skill_to_project
    if not args:
        T.error("Usage: /addskill <skill_name>")
        return "continue"
    skill_name = args[0].strip().lower()
    changed, msg = add_skill_to_project(state.project.project_path, skill_name)
    if changed:
        _rebuild_memgpt(state)
        T.success(msg)
    else:
        T.info(msg)


@_registry.register("/rmskill", usage="<name>", description="Remove a skill from this project")
async def cmd_rmskill(state: REPLState, args: list[str]) -> str | None:
    from .skills import remove_skill_from_project
    if not args:
        T.error("Usage: /rmskill <skill_name>")
        return "continue"
    skill_name = args[0].strip().lower()
    changed, msg = remove_skill_from_project(state.project.project_path, skill_name)
    if changed:
        _rebuild_memgpt(state)
        T.success(msg)
    else:
        T.info(msg)


# ─── Model commands ───────────────────────────────────────────────────────────

_CATALOG_ACTIONS = ("list", "show", "add", "set", "remove")


@_registry.register(
    "/models",
    usage="[list | show <id> | add key=value... | set <id> key=value... | remove <id>]",
    description="Show the models this project uses, or manage the model catalog",
    details='A usable model is two records: a provider connection holding the\ncredentials (/providers), and a catalog entry naming a model under it.\n\nExamples:\n  /models                                  what this project runs on\n  /models list                             every model in the catalog\n  /models show ollama/gemma4:26b           one entry in full\n  /models add name=gemma4:26b connection=ollama-gil num_ctx=65000 supports_thinking=true\n  /models add name=gpt-4o-mini connection=openai temperature=0.3\n  /models set ollama/gemma4:26b profile=light policy=delegate\n  /models remove ollama/gemma4:26b\n\nThen point the project at it with /set-main-model <id>.\n\nFields: name, connection (=connection_id), id, num_ctx (=context),\n  supports_thinking (=thinking), requires_single_system_message,\n  prompt_profile (=profile: full|light), orchestrator_policy (=policy:\n  direct|delegate), temperature, max_tokens, seed, top_p, top_k, min_p,\n  frequency_penalty, presence_penalty, repetition_penalty, reasoning_effort,\n  supports_image_generation, image_route, supports_speech_synthesis, speech_route.\n\nAny other field is refused, so a typo cannot become an invisible setting.\nA parameter you do want sent to the provider is written extra.<name>=<value>,\nfor example extra.keep_alive=30m. Quote values with spaces.',
)
async def cmd_models(state: REPLState, _args: list[str]) -> None:
    # With a subcommand this is the catalog (gilzabots/cli_catalog.py); bare, it
    # answers the question it always answered -- what this project runs on.
    parts = " ".join(_args).split()
    if parts and parts[0].lower() in _CATALOG_ACTIONS:
        from .cli_catalog import catalog_models
        return await catalog_models(state, parts[0].lower(), parts[1:])

    # What a turn actually runs on: no main model means the turn refuses to
    # start (agent_stdin), and an unset worker follows the main model
    # (config.resolve_agent_model). A built-in default shown here instead read
    # as a model the user had configured.
    main_model = state.project.model or _("cli_model_not_set")
    alt_model = state.project.worker_model or main_model
    alt_origin = "project" if state.project.worker_model else _("cli_worker_follows_main")
    T.console.print(f"\n[dim]Models for project '{state.display_name}':[/dim]")
    T.console.print(f"  [cyan]main[/cyan]        {main_model}")
    T.console.print(f"  [cyan]worker[/cyan]      {alt_model}  [dim]({alt_origin})[/dim]")
    params = getattr(state.project, "model_params", {})
    if params:
        T.console.print(f"  [cyan]parameters[/cyan]")
        for k, v in params.items():
            T.console.print(f"    {k}: {v}")
    T.console.print(
        f"\n[dim]Change with /set-main-model <id>, /set-worker-model <id>, "
        f"or /set-model-param <name> <value>.[/dim]"
    )
    T.console.print(
        f"[dim]{_('cli_models_catalog_hint')}[/dim]\n"
    )


def _warn_if_not_in_catalog(model_id: str) -> None:
    """Say when a model id has no catalog entry behind it.

    The id is still stored: a model reachable through credentials in the
    environment runs without one. But a mistyped id used to be accepted in
    silence and fail only on the next message, as a connection error.
    """
    from .models_store import get_model_by_runtime_id
    if get_model_by_runtime_id(model_id) is None:
        from .cli_catalog import report_unknown_model
        report_unknown_model(model_id, _("cli_model_not_in_catalog", id=model_id))


@_registry.register("/set-main-model", usage="<model_id>",
                    description="Set the main model for this project")
async def cmd_set_main_model(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /set-main-model <model_id>  (e.g. ollama/gemma4:latest)")
        return "continue"
    model_id = args[0].strip()
    _warn_if_not_in_catalog(model_id)
    state.project.model = model_id
    state.store.save(state.project)
    _rebuild_memgpt(state)
    T.success(f"Main model set to '{model_id}' for this project.")


@_registry.register("/set-worker-model", usage="<model_id>",
                    description="Set the worker model for this project (the model helpers run on)")
async def cmd_set_worker_model(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /set-worker-model <model_id>  (e.g. gemini/gemini-2.0-flash)")
        return "continue"
    model_id = args[0].strip()
    _warn_if_not_in_catalog(model_id)
    state.project.worker_model = model_id
    state.store.save(state.project)
    _rebuild_memgpt(state)
    T.success(f"Worker model set to '{model_id}' for this project.")


def _parse_model_param_value(val_str: str):
    """Infer the Python type of a model parameter value from its string representation.

    Priority: bool literals → int → float → string.
    Returns the typed value or raises ValueError with a descriptive message.
    """
    low = val_str.lower()
    if low in {"true", "on", "yes"}:
        return True
    if low in {"false", "off", "no"}:
        return False
    if low == "null" or low == "none":
        return None
    try:
        return int(val_str)
    except ValueError:
        pass
    try:
        return float(val_str)
    except ValueError:
        pass
    # Validate: reject values that look like typos (contain spaces, control chars)
    if not val_str.isprintable():
        raise ValueError("Value contains non-printable characters")
    return val_str


# Parameters a project must not carry, with the place that actually owns them.
# Storing one here would be dead configuration: the resolver never reads it back,
# so a silent write would look like a setting the user had made.
_MODEL_PARAMS_OWNED_ELSEWHERE = {
    "think": (
        "thinking is a per-model capability, not a project setting: enable "
        "'supports_thinking' on the model's catalog entry (Edit Models, or "
        "/models) and every project using that model reasons"
    ),
}

# Inference parameters moved to the model catalog. `sanitize_model_params` drops
# them on save, so accepting one here would print a success line for a value that
# never persists -- the silent behavior substitution this map exists to prevent.
# `num_ctx` is absent on purpose: it is still a project-level budgeting override.
for _catalog_owned_param in (
    "temperature", "max_tokens", "seed", "top_p", "top_k", "min_p",
    "frequency_penalty", "presence_penalty", "repetition_penalty",
    "reasoning_effort",
):
    _MODEL_PARAMS_OWNED_ELSEWHERE[_catalog_owned_param] = (
        "it is a per-model inference parameter, not a project setting: set it "
        "on the model's catalog entry (Edit Models, or /models) and every "
        "project using that model inherits it"
    )
del _catalog_owned_param


@_registry.register("/set-model-param", usage="<param_name> <value>",
                    description="Set a per-project model parameter (e.g. num_ctx, stop, stream). Inference parameters such as temperature live on the model's catalog entry.")
async def cmd_set_model_param(state: REPLState, args: list[str]) -> str | None:
    if len(args) < 2:
        T.error("Usage: /set-model-param <param_name> <value>\n"
                "Accepts any parameter supported by LiteLLM/Ollama.\n"
                "Values are auto-typed: integers, floats, booleans (true/false/on/off/yes/no), or strings.")
        return "continue"

    param = args[0].strip()
    val_str = " ".join(args[1:]).strip()

    if not param or not param.replace("_", "").replace("-", "").isalnum():
        T.error(f"Invalid parameter name '{param}'. Use only letters, digits, underscores and hyphens.")
        return "continue"

    if param in _MODEL_PARAMS_OWNED_ELSEWHERE:
        T.error(
            f"'{param}' cannot be set per project: "
            f"{_MODEL_PARAMS_OWNED_ELSEWHERE[param]}."
        )
        return "continue"

    try:
        val = _parse_model_param_value(val_str)
    except ValueError as e:
        T.error(f"Invalid value for '{param}': {e}")
        return "continue"

    if not hasattr(state.project, "model_params") or state.project.model_params is None:
        state.project.model_params = {}

    state.project.model_params[param] = val
    try:
        state.store.save(state.project)
        _rebuild_memgpt(state)
    except Exception as e:
        # Roll back the in-memory change so the project stays consistent
        state.project.model_params.pop(param, None)
        T.error(
            f"Failed to apply parameter '{param}': {e}\n"
            "The parameter was not saved. Check the value and try again."
        )
        return "continue"
    T.success(f"Model parameter '{param}' set to {repr(val)} for this project.")


@_registry.register(
    "/load_asset",
    usage="<type> <desc|id|*>",
    description="Install an asset from the AssetStore into the active project. type=skill|template, desc=id/description or * for all.",
)
async def cmd_load_asset(state: REPLState, args: list[str]) -> str | None:
    from .assetstore import find_assets, install_asset, VALID_TYPES

    if len(args) < 2:
        T.error(
            "Usage: /load_asset <type> <desc|id|*>\n"
            f"  type: {', '.join(sorted(VALID_TYPES))}\n"
            "  desc: asset id, description, or * to install all of the type"
        )
        return "continue"

    asset_type = args[0].strip().lower()
    desc = " ".join(args[1:]).strip()

    if asset_type not in VALID_TYPES:
        T.error(f"Unknown type '{asset_type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}")
        return "continue"

    matches = find_assets(asset_type, desc)
    if not matches:
        if desc == "*":
            T.warning(f"No {asset_type} assets found in the AssetStore.")
        else:
            T.warning(f"No {asset_type} asset matching '{desc}' found in the AssetStore.")
        return "continue"

    project_path = state.project.project_path
    installed = []
    errors = []
    for meta in matches:
        try:
            msg = install_asset(meta, project_path)
            installed.append(msg)
        except Exception as e:
            errors.append(f"{meta.get('id', '?')}: {e}")

    for msg in installed:
        T.success(f"Installed: {msg}")
    for err in errors:
        T.error(f"Failed: {err}")

    if installed and asset_type == "skill":
        T.console.print(
            "[dim]Tip: use /addskill <name> to activate the skill in this project.[/dim]"
        )

    return "continue"


@_registry.register("/list_assets",
    usage="[type]",
    description="List available assets in the AssetStore. type=skill|template (optional).")
async def cmd_list_assets(_state: REPLState, args: list[str]) -> str | None:
    from .assetstore import list_assets, VALID_TYPES

    asset_type = args[0].strip().lower() if args else None
    if asset_type and asset_type not in VALID_TYPES:
        T.error(f"Unknown type '{asset_type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}")
        return "continue"

    assets = list_assets(asset_type)
    if not assets:
        T.warning("No assets found in the AssetStore.")
        return "continue"

    T.console.print(f"\n[cyan]{_('available_commands')}[/cyan]".replace("commands", "assets"))
    for a in sorted(assets, key=lambda x: (x.get("type", ""), x.get("id", ""))):
        label = f"{a.get('id', '?')} [{a.get('type', '')}]"
        T.console.print(f"  [green]{label:<40}[/green] {a.get('desc', '')}")
    T.console.print()
    return "continue"


@_registry.register("/undo", description=_("undo_desc"))
async def cmd_undo(state: REPLState, _args: list[str]) -> str | None:
    from .vcs import get_vcs_strategy
    from .config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    try:
        vcs.setup()
    except Exception as e:
        T.error(f"Failed to setup VCS: {e}")
    success, msg = vcs.undo_last()
    if success:
        T.success(_("undo_success"))
    else:
        T.error(_("undo_fail") + f" ({msg})")
    return "continue"


@_registry.register("/commit", usage="<message>", description=_("commit_desc"))
async def cmd_commit(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /commit <message>")
        return "continue"
    message = " ".join(args).strip('"\'')
    from .vcs import get_vcs_strategy
    from .config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    try:
        vcs.setup()
    except Exception as e:
        T.error(f"Failed to setup VCS: {e}")
    success, msg = vcs.manual_commit(message)
    if success:
        T.success(_("commit_success"))
    else:
        T.error(_("commit_fail", err=msg))
    return "continue"


@_registry.register("/checkpoints", description="List all checkpoints (commits) in the shadow git")
async def cmd_checkpoints(state: REPLState, _args: list[str]) -> str | None:
    from .vcs import get_vcs_strategy
    from .config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    try:
        vcs.setup()
    except Exception as e:
        T.error(f"Failed to setup VCS: {e}")
    success, msg = vcs.list_checkpoints()
    if success:
        T.info("Checkpoints:")
        T.console.print(msg)
    else:
        T.error(f"Failed to list checkpoints: {msg}")
    return "continue"


@_registry.register("/restoreckp", usage="<checkpoint_id>", description="Restore the project to a specific checkpoint")
async def cmd_restoreckp(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /restoreckp <checkpoint_id>")
        return "continue"
    checkpoint_id = args[0].strip()
    from .vcs import get_vcs_strategy
    from .config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    try:
        vcs.setup()
    except Exception as e:
        T.error(f"Failed to setup VCS: {e}")
        
    if await T.aconfirm(f"Are you sure you want to restore to checkpoint '{checkpoint_id}'? This will discard all current changes.", default=False):
        success, msg = vcs.restore_checkpoint(checkpoint_id)
        if success:
            T.success(msg)
        else:
            T.error(msg)
    else:
        T.warning(_("cli_action_cancelled"))
    return "continue"


@_registry.register("/removechk", usage="<checkpoint_id>", description="Remove a specific checkpoint from history")
async def cmd_removechk(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.error("Usage: /removechk <checkpoint_id>")
        return "continue"
    checkpoint_id = args[0].strip()
    from .vcs import get_vcs_strategy
    from .config import get_git_strategy
    vcs = get_vcs_strategy(get_git_strategy(), state.project.project_path)
    try:
        vcs.setup()
    except Exception as e:
        T.error(f"Failed to setup VCS: {e}")
        
    if await T.aconfirm(f"Are you sure you want to remove checkpoint '{checkpoint_id}'?", default=False):
        success, msg = vcs.remove_checkpoint(checkpoint_id)
        if success:
            T.success(msg)
        else:
            T.error(msg)
    else:
        T.warning(_("cli_action_cancelled"))
    return "continue"


@_registry.register("/history", usage="[n]", description="Show conversation history (last n messages, default all)")
async def cmd_history(state: REPLState, args: list[str]) -> str | None:
    limit: int | None = None
    if args:
        try:
            limit = int(args[0])
            if limit <= 0:
                T.error("Usage: /history [n]  — n must be a positive integer")
                return "continue"
        except ValueError:
            T.error("Usage: /history [n]  — n must be a positive integer")
            return "continue"

    history = state.project.history
    if not history:
        T.info("No conversation history for this project.")
        return "continue"

    messages = history[-limit:] if limit else history
    total = len(history)
    showing = len(messages)

    T.console.print(
        f"\n[dim]Conversation history for '{_escape(state.display_name)}' "
        f"— showing {showing} of {total} message(s):[/dim]\n"
    )

    role_styles = {"user": "bold cyan", "assistant": "bold green"}
    role_labels = {"user": "You", "assistant": "GilzaBots"}

    for i, msg in enumerate(messages, start=total - showing + 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        style = role_styles.get(role, "bold yellow")
        label = role_labels.get(role, role.capitalize())

        # Truncate very long messages for readability
        display = content if len(content) <= 500 else content[:497] + "..."
        T.console.print(f"[{style}][{i}] {label}:[/{style}] {_escape(display)}\n")

    T.console.print(f"[dim]{'─' * 40}[/dim]\n")
    return "continue"


# ─── Turn and session commands ────────────────────────────────────────────────

_MODES = ("auto", "plan", "edit")


@_registry.register("/mode", usage="[auto | plan | edit]",
                    description="Show or set how much the agent may do on its own",
                    details='Examples:\n  /mode                 show the current mode\n  /mode auto            act freely, no questions\n  /mode plan            read-only until you approve a plan\n  /mode edit            ask before each change\n\nThe mode is stored on the project, so the desktop app sees the same value.')
async def cmd_mode(state: REPLState, args: list[str]) -> str | None:
    if not args:
        T.info(_("cli_mode_current", mode=state.project.mode))
        return "continue"
    mode = args[0].strip().lower()
    if mode not in _MODES:
        T.error(_("cli_usage_mode"))
        return "continue"
    state.project.mode = mode
    state.store.save(state.project)
    # The mode is read from the live session object by the tool permission gate
    # (gilzabots/tools.py), so the context has to be re-pointed at the saved project.
    from .tools import set_project_context
    set_project_context(state.project, state.store)
    T.success(_("cli_mode_set", mode=mode))
    return "continue"


@_registry.register("/chat", usage="[list | new <name> | switch <name>]",
                    description="List, create, or switch the chat this project is talking in",
                    cli_only=True, details='Examples:\n  /chat                                    list this project\'s chats\n  /chat new "Chapter 3 review"             create one and switch to it\n  /chat switch "Chapter 3"                 switch by name (a prefix is enough)\n  /chat switch 7ac7a997-6a13-435f-8ad1     switch by id\n\nEach chat keeps its own history and working context. To empty the current\none use /clear_chat; /clear wipes every chat in the project.')
async def cmd_chat(state: REPLState, args: list[str]) -> str | None:
    import uuid

    from .tools import set_project_context

    parts = " ".join(args).split()
    action = parts[0].lower() if parts else "list"
    rest = " ".join(parts[1:]).strip()
    chats = list(getattr(state.project, "chats", []) or [])

    if action == "list":
        if not chats:
            T.info(_("cli_no_chats"))
            return "continue"
        T.console.print(f"\n[dim]{_('cli_chats_header')}[/dim]")
        for chat in chats:
            mark = "[green]*[/green]" if chat["id"] == state.project.current_chat_id else " "
            T.console.print(f"  {mark} [cyan]{_escape(chat['name'])}[/cyan]  [dim]{chat['id']}[/dim]")
        T.console.print()
        return "continue"

    if action == "new":
        if not rest:
            T.error(_("cli_usage_chat"))
            return "continue"
        chat_id = str(uuid.uuid4())
        state.store.create_chat(state.project.name, chat_id, rest)
        target = chat_id
    elif action == "switch":
        if not rest:
            T.error(_("cli_usage_chat"))
            return "continue"
        needle = rest.lower()
        match = next(
            (c for c in chats if c["id"] == rest or c["name"].lower() == needle),
            None,
        ) or next((c for c in chats if c["name"].lower().startswith(needle)), None)
        if match is None:
            T.error(_("cli_chat_not_found", name=rest))
            return "continue"
        target = match["id"]
    else:
        T.error(_("cli_usage_chat"))
        return "continue"

    loaded = state.store.load(state.project.name, chat_id=target)
    if loaded is None:
        T.error(_("cli_chat_not_found", name=rest))
        return "continue"
    state.project = loaded
    set_project_context(state.project, state.store)
    state.invalidate_memgpt()
    name = next(
        (c["name"] for c in (loaded.chats or []) if c["id"] == target), rest
    )
    T.success(_("cli_chat_created" if action == "new" else "cli_chat_switched", name=name))
    return "continue"


@_registry.register("/effort", usage="[default | low | medium | high | xhigh]",
                    description="Show or set the reasoning effort of this chat",
                    cli_only=True, details='Examples:\n  /effort            show what this chat uses\n  /effort low        think less in this chat, whatever the model catalog says\n  /effort default    follow the model catalog again\n\nThe setting belongs to the current chat and applies to its orchestrator\nand its workers from the next turn on. It has an effect only on a model\nwhose catalog entry declares thinking support.')
async def cmd_effort(state: REPLState, args: list[str]) -> str | None:
    from .config import get_agent_model
    from .models_store import get_model, get_model_by_runtime_id, resolve_runtime_model_id
    from .project import CHAT_REASONING_EFFORTS

    chat_id = state.project.current_chat_id
    requested = " ".join(args).strip().lower()
    if not chat_id:
        T.info(_("cli_no_chats"))
        return "continue"

    if not requested:
        current = state.store.get_chat_reasoning_effort(state.project.name, chat_id)
        T.info(_("cli_effort_current", effort=current) if current else _("cli_effort_default"))
        return "continue"

    if requested != "default" and requested not in CHAT_REASONING_EFFORTS:
        T.error(_("cli_usage_effort"))
        return "continue"

    stored = state.store.set_chat_reasoning_effort(
        state.project.name, chat_id, "" if requested == "default" else requested
    )
    state.project.chat_reasoning_effort = stored
    state.invalidate_memgpt()
    T.success(_("cli_effort_set", effort=stored) if stored else _("cli_effort_cleared"))
    # Saved either way, but said plainly: on a model that does not reason there
    # is no effort to change, and a setting that silently does nothing reads as
    # a broken one. The models and their catalog entries are resolved the way
    # `build_chat_orchestrator`, `run_skill` and `get_agent_llm_kwargs` resolve them.
    orchestrator = get_agent_model("memgpt", get_agent_model("chat_agent", state.project.model or ""))
    worker = get_agent_model("worker", state.project.worker_model or state.project.model or "")

    def _reasons(model: str) -> bool:
        entry = get_model(model) or get_model_by_runtime_id(resolve_runtime_model_id(model))
        return bool(entry and entry.get("supports_thinking"))

    if stored and (orchestrator or worker) and not any(_reasons(m) for m in (orchestrator, worker) if m):
        T.warning(_("cli_effort_no_thinking"))
    return "continue"


@_registry.register("/cost", "/context",
                    description="Show how much of the context window this chat occupies")
async def cmd_cost(state: REPLState, _args: list[str]) -> str | None:
    from .token_usage import context_scope_key, get_context_usage

    # The in-process measurement belongs to the turn that just ran; the stored
    # one survives a restart and is what a freshly opened session has. Prefer the
    # live value and fall back to the stored one, exactly as the chat panel does.
    usage = get_context_usage(
        context_scope_key(state.project.project_path or "", state.project.current_chat_id)
    ) or state.store.get_chat_context_usage(
        state.project.name, state.project.current_chat_id
    )
    used = int((usage or {}).get("prompt_tokens") or 0)
    if not used:
        T.info(_("cli_cost_none"))
        return "continue"
    window = int((usage or {}).get("context_window") or 0)
    pct = f"{(used / window * 100):.0f}%" if window else "?"
    T.console.print(f"\n[dim]{_('cli_cost_header', chat=state.project.current_chat_id)}[/dim]")
    T.console.print(f"  [cyan]prompt tokens[/cyan]  {used}")
    T.console.print(f"  [cyan]context window[/cyan] {window or '?'}")
    T.console.print(f"  [cyan]occupancy[/cyan]      {pct}\n")
    return "continue"


@_registry.register("/resume", "/continue",
                    description="Continue the turn that was interrupted in this chat",
                    cli_only=True)
async def cmd_resume(state: REPLState, _args: list[str]) -> str | None:
    """Hand the REPL a sentinel: only the loop can start an agent turn."""
    from .agent_stdin import unfinished_turn_content

    if not unfinished_turn_content(getattr(state.project, "history", []) or []):
        T.info(_("cli_resume_nothing"))
        return "continue"
    return "resume"


def _toggle(state: REPLState, args: list[str], attribute: str, feature_key: str) -> str:
    if state.renderer is None:
        T.error(_("cli_renderer_required"))
        return "continue"
    current = bool(getattr(state.renderer, attribute))
    wanted = args[0].strip().lower() if args else ("off" if current else "on")
    if wanted not in ("on", "off"):
        T.error("Usage: on|off")
        return "continue"
    setattr(state.renderer, attribute, wanted == "on")
    feature = _(feature_key)
    T.success(_("cli_toggle_on" if wanted == "on" else "cli_toggle_off", feature=feature))
    return "continue"


@_registry.register("/thoughts", usage="[on|off]",
                    description="Show or hide the model's reasoning while it works",
                    cli_only=True)
async def cmd_thoughts(state: REPLState, args: list[str]) -> str | None:
    return _toggle(state, args, "show_thoughts", "cli_feature_thoughts")


@_registry.register("/tools", usage="[on|off]",
                    description="Show or hide tool calls and their results",
                    cli_only=True)
async def cmd_tools(state: REPLState, args: list[str]) -> str | None:
    return _toggle(state, args, "show_tools", "cli_feature_tools")


@_registry.register("/compile", usage="[file.tex]",
                    description="Compile the project with Tectonic and report the result",
                    cli_only=True)
async def cmd_compile(state: REPLState, args: list[str]) -> str | None:
    import os

    from .latex_compiler import compile_latex, guess_main_file

    project_dir = state.project.project_path
    target = args[0].strip() if args else (
        state.project.main_file or guess_main_file(project_dir)
    )
    if not target:
        T.error("No .tex file found in the project. Pass one: /compile <file.tex>")
        return "continue"
    path = target if os.path.isabs(target) else os.path.join(project_dir, target)
    if not os.path.exists(path):
        T.error(f"File not found: {path}")
        return "continue"

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()

    with T.spinner(f"Compiling {os.path.basename(path)}..."):
        result = await asyncio.to_thread(
            compile_latex,
            content,
            file_path=path,
            main_file=os.path.basename(path),
            project_dir=project_dir,
            include_pdf_base64=False,
        )

    if result.get("success"):
        T.success(f"PDF written to {result.get('pdf_path') or '(unknown path)'}")
    else:
        T.error("Compilation failed.")
        log = str(result.get("log") or "").strip()
        if log:
            T.console.print(f"[dim]{_escape(log[-2000:])}[/dim]")
    return "continue"


#: The `gilzabots monitors` subcommands that make sense inside the REPL. The
#: daemon would block it, and the internal ones are started by the daemon or OS.
_REPL_MONITOR_COMMANDS = ("list", "info", "pause", "resume", "cancel", "delete", "test",
                          "service", "notify-test")


@_registry.register(
    "/monitors", usage="[list|info|pause|resume|cancel|delete|test|service] ...",
    cli_only=True,
    description="Manage background monitors (the same as `gilzabots monitors`)",
    details="Examples:\n  /monitors                  active and paused monitors\n"
            "  /monitors list --all       every monitor, finished ones included\n"
            "  /monitors info <id>        details and recent history\n"
            "  /monitors pause <id>       stop checking until resumed\n"
            "  /monitors cancel <id>      stop it for good\n"
            "  /monitors test <id>        check now, changing nothing\n"
            "  /monitors service status   the login service that starts the daemon",
)
async def cmd_monitors(_state: REPLState, args: list[str]) -> str:
    import shlex

    from .monitors.cli import main as monitors_main

    try:
        argv = shlex.split(" ".join(args)) if args else ["list"]
    except ValueError as exc:
        T.error(f"/monitors: {exc}")
        return "continue"
    if argv[0] not in _REPL_MONITOR_COMMANDS:
        T.error(f"/monitors {argv[0]}: not available here. Use one of: {', '.join(_REPL_MONITOR_COMMANDS)}.")
        return "continue"
    try:
        # Run off the loop: `test` runs its own event loop for the check.
        await asyncio.to_thread(monitors_main, argv)
    except SystemExit:
        pass  # argparse reports a usage error and exits; the REPL keeps going.
    return "continue"


@_registry.register("/exit", "/quit", description=_("exit_desc"))
async def cmd_exit(_state: REPLState, _args: list[str]) -> str:
    T.info(_("exiting"))
    return "break"


# Provider connections and catalog models register themselves into `_registry`.
# Imported at the end so the registry above already exists when they do.
from . import cli_catalog  # noqa: E402,F401  (import for side effect)
from . import cli_connectors  # noqa: E402,F401  (import for side effect)
