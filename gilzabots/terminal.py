"""Elegant terminal output utilities for GilzaBots using Rich."""

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table
from rich.rule import Rule
from rich import print as rprint
from contextlib import contextmanager
import os
import re
import sys

from .i18n import _

# Matches a model-provided "other"/"none of the above" style choice (en/pt/es)
# so it can be filtered out before the UI appends its own free-text "Other" option.
_OTHER_OPTION_PATTERN = re.compile(r"^(outr[oa]s?|other|otro)\b", re.IGNORECASE)

try:
    import readline
except ImportError:
    pass

console = Console(highlight=False)

class UserCancelled(Exception):
    """Raised when the user wants to cancel the current operation."""
    pass

class AppExit(Exception):
    """Raised when the user wants to exit the application completely."""
    pass

_CANCEL_WORDS = {"cancelar", "abortar", "/cancel", "/abort", "cancel", "abort"}
_EXIT_WORDS = {"/exit", "/quit", "/sair", "sair", "exit", "quit"}

def _check_cancel(text: str) -> None:
    text_lower = text.strip().lower()
    if text_lower in _CANCEL_WORDS:
        raise UserCancelled()
    if text_lower in _EXIT_WORDS:
        raise AppExit()


# ─── Branding ─────────────────────────────────────────────────────────────────

BANNER = r"""
  ___               _       ____          _           
 / _ \ _ __   __ _ | | __ _/ ___|___   __| | ___ _ __ 
| | | | '_ \ / _` || |/ _` | |   / _ \ / _` |/ _ \ '__|
| |_| | |_) | (_| || | (_| | |__| (_) | (_| |  __/ |   
 \___/| .__/ \__,_||_|\__,_|\____\___/ \__,_|\___|_|   
      |_|                                             
"""


def print_banner(version: str = "0.1.0", mode: str = "plan") -> None:
    text = Text(BANNER, style="bold cyan")
    console.print(text)
    console.print(
        f"  [dim]version {version}[/dim]  "
        f"[bold]mode:[/bold] [yellow]{mode}[/yellow]"
    )
    console.print()


# ─── Section headers ──────────────────────────────────────────────────────────

def section(title: str, style: str = "bold blue") -> None:
    console.print(Rule(f"[{style}]{title}[/{style}]", style="dim"))


def subsection(title: str) -> None:
    console.print(f"\n[bold cyan]▶ {title}[/bold cyan]")


# ─── Info / status ────────────────────────────────────────────────────────────

def info(msg: str) -> None:
    console.print(f"[dim]  {msg}[/dim]")


def success(msg: str) -> None:
    console.print(f"[bold green]  ✓ {msg}[/bold green]")


def warning(msg: str) -> None:
    console.print(f"[bold yellow]  ⚠ {msg}[/bold yellow]")


def error(msg: str) -> None:
    console.print(f"[bold red]  ✗ {msg}[/bold red]")


def thinking(msg: str) -> None:
    console.print(f"[italic dim cyan]  💭 {msg}[/italic dim cyan]")


# ─── Panels ───────────────────────────────────────────────────────────────────

def show_plan(plan_text: str, title: str = None) -> None:
    title = title or _("generated_plan")
    console.print(
        Panel(plan_text, title=f"[bold]{title}[/bold]", border_style="cyan", expand=False)
    )


def show_result(text: str, title: str = None) -> None:
    title = title or _("final_result")
    console.print(
        Panel(text, title=f"[bold green]{title}[/bold green]", border_style="green")
    )


def show_error_report(errors: list[tuple[str, str]]) -> None:
    table = Table(title=_("exec_errors"), border_style="red", show_lines=True)
    table.add_column(_("subplan"), style="bold yellow")
    table.add_column(_("error"), style="red")
    for sp_id, err in errors:
        table.add_row(sp_id, err[:300])
    console.print(table)


# ─── Spinner context ──────────────────────────────────────────────────────────

@contextmanager
def spinner(label: str):
    with Progress(
        SpinnerColumn(),
        TextColumn(f"[cyan]{label}[/cyan]"),
        transient=True,
        console=console,
    ) as progress:
        progress.add_task("", total=None)
        yield progress


# ─── User prompts ─────────────────────────────────────────────────────────────

def ask(prompt: str) -> str:
    from rich.markup import escape
    console.print(f"\n[bold yellow]?[/bold yellow] {escape(prompt)}")
    ans = input("  → ").strip()
    _check_cancel(ans)
    return ans


def confirm(prompt: str, default: bool = True) -> bool:
    from rich.markup import escape
    hint = "[Y/n]" if default else "[y/N]"
    console.print(f"\n[bold yellow]?[/bold yellow] {escape(prompt)} {hint}")
    raw = input("  → ").strip().lower()
    _check_cancel(raw)
    if not raw:
        return default
    return raw in _("yes_hints")


# Optional async hook for GUI mode. When set, aconfirm() awaits it instead of
# calling the blocking sync confirm(). Set by agent_stdin in GUI context.
_async_confirm_hook = None  # type: Callable[[str, bool], Coroutine[Any, Any, bool]] | None


async def aconfirm(prompt: str, default: bool = True) -> bool:
    """Async-capable confirm: delegates to GUI hook if available, sync otherwise."""
    if _async_confirm_hook is not None:
        return await _async_confirm_hook(prompt, default)
    # Fallback: run the blocking sync version in the default executor so the
    # event loop is not stalled when running in terminal mode.
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, confirm, prompt, default)


# Optional async hook for GUI mode text input.
_async_ask_hook = None  # type: Callable[[str], Coroutine[Any, Any, str]] | None

async def aask(prompt: str, options: list[str] | None = None, is_multi_select: bool = False) -> str:
    """Async-capable ask: delegates to GUI hook if available, sync otherwise."""
    if _async_ask_hook is not None:
        try:
            return await _async_ask_hook(prompt, options=options, is_multi_select=is_multi_select)
        except TypeError:
            return await _async_ask_hook(prompt)
    import asyncio
    loop = asyncio.get_event_loop()
    if options and not is_multi_select:
        # The UI always appends its own "Other" write-in choice, so drop any
        # equivalent catch-all the model may have added on its own to avoid
        # showing two "other" entries.
        filtered = [o for o in options if not _OTHER_OPTION_PATTERN.match(str(o).strip())]
        return await loop.run_in_executor(None, choose, prompt, filtered + ["Other / Custom input..."])
    return await loop.run_in_executor(None, ask, prompt)

# Optional async hook for interactive embedded terminal
_async_interactive_terminal_hook = None

async def a_interactive_terminal(command: str, term_id: str) -> str:
    if _async_interactive_terminal_hook is not None:
        return await _async_interactive_terminal_hook(command, term_id)
    return "Interactive terminal is not supported in this environment."


def choose(prompt: str, options: list[str], default: str | None = None) -> str:
    """Let user pick from a numbered list; returns chosen option string.

    With ``default`` (one of ``options``), an empty answer picks it; its number
    is shown in the prompt.
    """
    from rich.markup import escape
    console.print(f"\n[bold yellow]?[/bold yellow] {escape(prompt)}")
    for i, opt in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]) {escape(opt)}")
    default_hint = f"[{options.index(default) + 1}] " if default in options else ""
    while True:
        raw = input(f"  {default_hint}→ ").strip()
        _check_cancel(raw)
        if not raw and default_hint:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        # accept text match too
        matches = [o for o in options if o.lower().startswith(raw.lower())]
        if len(matches) == 1:
            return matches[0]
        console.print(f"  [red]{_('invalid_option')}[/red]")


# ─── Subplan progress table ───────────────────────────────────────────────────

# ─── Workflow debug output (enabled via GILZABOTS_WORKFLOW_DEBUG=1) ──────────

# Re-read at call time so setup_debug_logging() setting os.environ after import works.
def _workflow_debug() -> bool:
    return os.environ.get("GILZABOTS_WORKFLOW_DEBUG", "0") == "1"


def _log(text: str) -> None:
    """Write plain text to the run log file if one is active."""
    try:
        from .config import get_run_logger
        rl = get_run_logger()
        if rl:
            rl.debug(text)
    except Exception:
        pass


def debug_oracle(schema_name: str, attempt: int, raw_content: str) -> None:
    msg = (
        f"\n┌─ ORACLE [{schema_name}] attempt {attempt + 1} — RAW OUTPUT ─┐\n"
        f"{raw_content}\n"
        f"└──────────────────────────────────────────────────────────┘"
    )
    _log(msg)
    if not _workflow_debug():
        return
    from rich.markup import escape
    console.print(f"\n[bold cyan]┌─ ORACLE [{schema_name}] attempt {attempt + 1} — RAW OUTPUT ─┐[/bold cyan]")
    console.print(escape(raw_content[:2000]))
    console.print("[bold cyan]└──────────────────────────────────────────────────────────┘[/bold cyan]")


def debug_oracle_error(schema_name: str, attempt: int, error: str, raw_content: str) -> None:
    msg = (
        f"\n┌─ ORACLE [{schema_name}] attempt {attempt + 1} — PARSE ERROR ─┐\n"
        f"Error: {error}\n"
        f"Raw content: {raw_content}\n"
        f"└──────────────────────────────────────────────────────────┘"
    )
    _log(msg)
    if not _workflow_debug():
        return
    from rich.markup import escape
    console.print(f"\n[bold red]┌─ ORACLE [{schema_name}] attempt {attempt + 1} — PARSE ERROR ─┐[/bold red]")
    console.print(f"[red]Error:[/red] {escape(error)}")
    console.print(f"[dim]Raw content:[/dim] {escape(raw_content[:1000])}")
    console.print("[bold red]└──────────────────────────────────────────────────────────┘[/bold red]")


def debug_worker_start(task_id: str, description: str, model: str) -> None:
    _log(f"\n┌─ WORKER [{task_id}] model={model} ─┐\nTask: {description}")
    if not _workflow_debug():
        return
    from rich.markup import escape
    console.print(f"\n[bold green]┌─ WORKER [{task_id}] model={model} ─┐[/bold green]")
    console.print(f"[dim]Task:[/dim] {escape(description[:500])}")
    console.print("[bold green]│ executing…[/bold green]")


def debug_worker_project_path(task_id: str, project_path: str) -> None:
    _log(f"│ [{task_id}] project_path: {project_path}")
    if not _workflow_debug():
        return
    from rich.markup import escape
    console.print(f"[bold green]│ project_path:[/bold green] {escape(project_path)}")


def debug_worker_tool_calls(task_id: str, count: int) -> None:
    _log(f"│ [{task_id}] tool_calls_made: {count}")
    if not _workflow_debug():
        return
    color = "green" if count > 0 else "red"
    console.print(f"[bold {color}]│ tool_calls_made: {count}[/bold {color}]")


def debug_worker_result(task_id: str, result: str) -> None:
    _log(f"│ [{task_id}] Result:\n{result}\n└──────────────────────────────────────────────────────────┘")
    if not _workflow_debug():
        return
    from rich.markup import escape
    console.print(f"[bold green]│ Result:[/bold green] {escape(result[:1000])}")
    console.print("[bold green]└──────────────────────────────────────────────────────────┘[/bold green]")


def subplan_status_table(statuses: list[tuple[str, str, str]]) -> None:
    """statuses: [(sp_id, objective, status_label), ...]"""
    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column(_("table_id"), style="cyan", width=6)
    table.add_column(_("table_objective"))
    table.add_column(_("table_status"), width=12)
    for sp_id, obj, status in statuses:
        color = {"OK": "green", "FALHOU": "red", "FAILED": "red", "ERRO": "red", "ERROR": "red", "→": "yellow"}.get(
            status.upper().split()[0], "white"
        )
        table.add_row(sp_id, obj[:70], f"[{color}]{status}[/{color}]")
    console.print(table)
