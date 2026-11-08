"""Terminal front-end for the agent event stream.

An agent turn publishes everything it does as structured events through
``agent_stdin.print_event``: streamed answer text, reasoning, tool calls and
their results, problems, errors and the final response. The GUI subscribes to
that stream by installing an ``event_hook`` (``ide_server.start_gui_server``);
this module installs one that renders the same stream to a terminal.

Without a hook, ``print_event`` falls back to writing the raw JSON protocol to
stdout -- which is what the CLI used to show, one protocol line per token,
interleaved with a spinner.

Two contracts are carried over from the GUI front-end (PROJECT_DESIGN 2.6):

* Streamed text is printed raw, never parsed as Markdown while partial. The
  final ``agent_response`` is not printed again when the stream already showed
  it: there is exactly one user-facing text channel, so printing both would
  duplicate the answer.
* ``stream_retract`` means text already published turned out to be reasoning.
  A terminal cannot unprint in the general case, so the text is erased when it
  is still the tail of what this renderer wrote and the output is a TTY, and is
  otherwise marked as reasoning. It arrives again as a ``thought`` event either
  way, so nothing is lost.

The module also provides the terminal's answer to ``input_request`` -- the
tool-permission prompt and the plan approval -- through
``agent_stdin.set_input_transport``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys

from . import terminal as T
from .i18n import _

#: Reasoning delimiters a model inlines in its answer. The engine routes the text
#: between them to the reasoning channel, but a tag split across chunks can
#: still reach the visible stream; the desktop app drops them the same way
#: (`sanitizeVisibleStreamChunk` in App.jsx).
_THINK_TAG = re.compile(r"</?think>", re.IGNORECASE)


def _visible_text(value: object) -> str:
    return _THINK_TAG.sub("", str(value or ""))


#: How much of a tool result is shown before it is cut short.
TOOL_RESULT_PREVIEW = 220

#: Events that carry no information a terminal user needs.
_IGNORED_EVENTS = frozenset({
    "server_ready",
    "exited",
    "projects_list",
    "input_request_closed",
    "context_usage",
    "token_usage",
    "agent_step",
    "agent_started",
    "project_loaded",
    "user_message_saved",
})

#: Events after which a held-back reflection has to appear, because the turn
#: visibly went on to do something else. Bookkeeping events (`agent_step`,
#: `token_usage`) are deliberately absent: one of them arrives between the last
#: reflection and the final response, and releasing the reflection there printed
#: the answer a second time.
_RELEASE_REFLECTION_BEFORE = frozenset({
    "stream_chunk",
    "tool_call",
    "tool_result",
    "thought",
    "info",
    "warning",
    "problem",
    "error",
    "agent_finished",
})


def _visible_width() -> int:
    """Terminal width used to work out how many lines a string occupied."""
    try:
        return max(shutil.get_terminal_size().columns, 1)
    except Exception:
        return 80


class TerminalEventRenderer:
    """Render one agent turn's event stream to the terminal.

    A single instance is reused across turns; ``begin_turn`` resets the
    per-turn state. The renderer is intentionally synchronous and reentrant-safe
    for a single event loop: ``print_event`` calls it directly from inside the
    turn.
    """

    def __init__(self, *, show_thoughts: bool = True, show_tools: bool = True) -> None:
        self.show_thoughts = show_thoughts
        self.show_tools = show_tools
        self.streamed_text = ""
        self._visible_run = ""
        self._at_line_start = True
        self._thought_open = False
        self._pending_reflection = ""

    # ── turn lifecycle ───────────────────────────────────────────────────────

    def begin_turn(self) -> None:
        self.streamed_text = ""
        self._visible_run = ""
        self._at_line_start = True
        self._thought_open = False
        self._pending_reflection = ""

    # ── low-level writing ────────────────────────────────────────────────────

    def _raw(self, text: str) -> None:
        """Write model output verbatim: no Rich markup, no highlighting."""
        if not text:
            return
        T.console.print(text, end="", markup=False, highlight=False, soft_wrap=True)
        self._at_line_start = text.endswith("\n")

    def _newline(self) -> None:
        if not self._at_line_start:
            T.console.print()
            self._at_line_start = True

    def _close_visible_run(self) -> None:
        """End the current run of streamed text before printing a status line."""
        if self._visible_run:
            self._newline()
            self._visible_run = ""
        if self._thought_open:
            self._newline()
            self._thought_open = False

    # ── event handling ───────────────────────────────────────────────────────

    def __call__(self, payload: dict) -> None:
        try:
            self.handle(payload)
        except Exception:
            # A rendering fault must never take the agent turn down with it.
            if os.environ.get("GILZABOTS_CLI_RENDER_DEBUG") == "1":
                import traceback

                traceback.print_exc(file=sys.stderr)

    def handle(self, payload: dict) -> None:
        event = str(payload.get("event") or "")
        if event in _IGNORED_EVENTS:
            return
        if event in _RELEASE_REFLECTION_BEFORE:
            self._flush_reflection()
        handler = getattr(self, f"_on_{event}", None)
        if handler is not None:
            handler(payload)

    # ── individual events ────────────────────────────────────────────────────

    def _on_stream_chunk(self, data: dict) -> None:
        text = _visible_text(data.get("content"))
        if not text:
            return
        if self._thought_open:
            self._newline()
            self._thought_open = False
        self.streamed_text += text
        self._visible_run += text
        self._raw(text)

    def _on_stream_retract(self, data: dict) -> None:
        text = _visible_text(data.get("content"))
        if not text:
            return
        if self.streamed_text.endswith(text):
            self.streamed_text = self.streamed_text[: -len(text)]
        if self._visible_run.endswith(text) and self._erase(text):
            self._visible_run = self._visible_run[: -len(text)]
            return
        # Could not unprint it. Say what it was instead of leaving reasoning
        # sitting in the terminal disguised as the answer.
        self._visible_run = ""
        self._newline()
        T.console.print(f"[dim]{_('cli_retracted_reasoning')}[/dim]")

    def _erase(self, text: str) -> bool:
        """Remove *text* from the terminal, when it is still the visible tail."""
        console_file = getattr(T.console, "file", sys.stdout)
        if not getattr(console_file, "isatty", lambda: False)():
            return False
        width = _visible_width()
        # Count the screen lines the text occupied. Only the tail matters, so
        # the run is measured from the start of the line the retraction began on.
        prefix = self._visible_run[: -len(text)]
        start_col = len(prefix) - (prefix.rfind("\n") + 1)
        rows = 0
        col = start_col
        for char in text:
            if char == "\n":
                rows += 1
                col = 0
                continue
            col += 1
            if col >= width:
                rows += 1
                col = 0
        try:
            console_file.write("\r\x1b[2K")
            for _unused in range(rows):
                console_file.write("\x1b[1A\r\x1b[2K")
            console_file.flush()
        except Exception:
            return False
        self._at_line_start = True
        return True

    def _on_thought(self, data: dict) -> None:
        """Model reasoning, streamed as deltas, written as one running block.

        The turn publishes reasoning token by token (``agent_stdin._on_thinking``),
        the way the desktop app concatenates it. Printing each delta as a line of
        its own, stripped, put every word of the reasoning on a separate line.
        Narration the engine writes itself (``auxiliary``) is a whole sentence.
        """
        if not self.show_thoughts:
            return
        if data.get("auxiliary"):
            # Narration of an event this renderer already printed itself.
            if not self.show_tools:
                self._print_thought_block(str(data.get("content") or ""))
            return
        text = str(data.get("content") or "")
        if self._visible_run:
            self._newline()
            self._visible_run = ""
        if not self._thought_open:
            text = text.lstrip()
            if not text:
                return
            self._newline()
            self._thought_open = True
        if not text:
            return
        T.console.print(text, end="", markup=False, highlight=False, soft_wrap=True, style="dim italic")
        self._at_line_start = text.endswith("\n")

    def _print_thought_block(self, content: str) -> None:
        """A complete thought -- narration or a released reflection -- on its own lines."""
        content = content.strip()
        if not content:
            return
        if self._visible_run:
            self._visible_run = ""
        self._newline()
        self._thought_open = True
        T.console.print(content, markup=False, highlight=False, style="dim italic")
        self._at_line_start = True

    def _on_reflection(self, data: dict) -> None:
        """An iteration's last message: the model's prose between tool calls.

        It is held back one event instead of printed at once. The reflection of
        the *last* iteration is the answer itself, and the desktop app can put
        it in a panel of its own; a terminal has one column, so printing it here
        and again as the final response would show the answer twice -- the same
        one-text-channel rule that governs `agent_response`. Anything the turn
        goes on to do releases it; the final response absorbs it.
        """
        content = str(data.get("content") or "").strip()
        if not content or content in self.streamed_text:
            return
        self._flush_reflection()
        self._pending_reflection = content

    def _flush_reflection(self) -> None:
        pending, self._pending_reflection = self._pending_reflection, ""
        if pending and self.show_thoughts:
            self._print_thought_block(pending)

    def _on_tool_call(self, data: dict) -> None:
        if not self.show_tools:
            return
        from .agent_stdin import _compact_tool_argument_summary

        self._close_visible_run()
        tool = str(data.get("tool") or "?")
        summary = _compact_tool_argument_summary(data.get("arguments", {}))
        T.console.print(f"[bold cyan]⏺[/bold cyan] [cyan]{tool}[/cyan][dim]({summary})[/dim]")

    def _on_tool_result(self, data: dict) -> None:
        if not self.show_tools:
            return
        self._close_visible_run()
        result = str(data.get("result") or "").strip().replace("\n", " ")
        if len(result) > TOOL_RESULT_PREVIEW:
            result = result[:TOOL_RESULT_PREVIEW] + "…"
        if data.get("is_error"):
            T.console.print(f"  [red]⌞ {result}[/red]")
        elif result:
            T.console.print(f"  [dim]⌞ {result}[/dim]")

    def _on_info(self, data: dict) -> None:
        self._print_notice(data, "dim")

    def _on_warning(self, data: dict) -> None:
        self._print_notice(data, "yellow")

    def _print_notice(self, data: dict, style: str) -> None:
        """An info or warning line, with the agent that sent it.

        Both texts are escaped: a subagent's label is written by the model, and
        a `[` in it would otherwise be read as Rich markup.
        """
        from rich.markup import escape

        message = str(data.get("message") or "").strip()
        if not message:
            return
        self._close_visible_run()
        agent = str(data.get("agent") or "")
        label = f"[dim]{escape(agent)}:[/dim] " if agent else ""
        T.console.print(f"  {label}[{style}]{escape(message)}[/{style}]")

    def _on_provider_wait(self, data: dict) -> None:
        """The provider has been silent past the threshold, or stopped being.

        Without this the terminal shows nothing while a request waits out its
        HTTP timeout -- ten minutes, in the session that motivated it -- and
        the user cannot tell a dead connection from a slow model.
        """
        self._close_visible_run()
        seconds = int(float(data.get("elapsed_seconds") or 0))
        agent = str(data.get("agent") or "")
        label = f"[dim]{agent}:[/dim] " if agent.startswith("worker:") else ""
        if not data.get("waiting"):
            T.console.print(f"  {label}[dim]{_('cli_provider_wait_over', seconds=seconds)}[/dim]")
            return
        key = "cli_provider_wait_stream" if data.get("phase") == "stream" else "cli_provider_wait"
        T.warning(f"{label}{_(key, seconds=seconds)}")

    def _on_context_compaction(self, data: dict) -> None:
        """The turn compacted its context because the window passed the threshold."""
        self._close_visible_run()
        phase = data.get("phase")
        if phase == "started":
            T.info(_("cli_compact_auto_started", before=int(data.get("pre_tokens") or 0)))
        elif phase == "completed":
            T.success(_(
                "cli_compact_done",
                count=int(data.get("messages_compacted") or 0),
                before=int(data.get("pre_tokens") or 0),
                after=int(data.get("post_tokens") or 0),
            ))
            from .compaction import print_compaction_details

            print_compaction_details(data)
        elif phase == "failed":
            T.warning(_("cli_compact_auto_failed", error=str(data.get("error") or "")))

    def _on_problem(self, data: dict) -> None:
        self._close_visible_run()
        tool = str(data.get("tool") or data.get("agent") or "")
        message = str(data.get("message") or "").strip()
        where = f" ({tool})" if tool else ""
        T.warning(f"{message}{where}")

    def _on_error(self, data: dict) -> None:
        self._close_visible_run()
        T.error(str(data.get("message") or "").strip())
        trace = str(data.get("trace") or "")
        if trace and os.environ.get("GILZABOTS_DEBUG") == "1":
            T.console.print(f"[dim]{trace}[/dim]", markup=False)

    def _on_agent_response(self, data: dict) -> None:
        response = str(data.get("response") or "").strip()
        if not response:
            self._flush_reflection()
            return
        pending, self._pending_reflection = self._pending_reflection, ""
        if pending and pending not in response:
            self._on_thought({"content": pending})
        streamed = self.streamed_text.strip()
        self._close_visible_run()
        # One text channel: if the stream already showed this answer, showing it
        # again would duplicate it. Only the difference is worth printing, and
        # only when the stream is a prefix of the final text.
        if streamed and response.startswith(streamed):
            remainder = response[len(streamed):]
            if remainder.strip():
                self._raw(remainder)
                self._newline()
            return
        if streamed == response:
            return
        self._newline()
        T.console.print(f"[bold green]{_('cli_assistant_label')}[/bold green]")
        self._raw(response)
        self._newline()

    def _on_checkpoint_finalized(self, data: dict) -> None:
        if not data.get("success"):
            return

    def _on_agent_finished(self, _data: dict) -> None:
        self._flush_reflection()
        self._close_visible_run()
        self._newline()


# ── Input requests in the terminal ───────────────────────────────────────────


async def terminal_input_transport(request: dict) -> str:
    """Answer an `input_request` at the terminal.

    Installed with ``agent_stdin.set_input_transport``. Runs the blocking prompt
    in the default executor so the agent turn's event loop keeps streaming while
    the user reads the question. Each request type is answered with the value
    the desktop app's dialog for it would send back, so the tool that asked
    cannot tell the front-ends apart.
    """
    kind = str(request.get("type") or "confirm")
    if kind == "ask":
        ask = _ask_question
    elif kind == "interactive_terminal":
        ask = _run_interactive_command
    else:
        ask = _confirm
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, ask, request)


def _read_answer() -> str:
    return input("  → ").strip()


def _confirm(request: dict) -> str:
    """A choice among fixed options -- tool permission, plan approval."""
    from rich.markup import escape

    prompt = str(request.get("prompt") or "").strip()
    options = [str(o) for o in (request.get("options") or ["yes", "no"])]
    default = str(request.get("default") or "")
    markdown = request.get("markdown_content")

    T.console.print()
    if markdown:
        from rich.markdown import Markdown
        from rich.panel import Panel

        T.console.print(
            Panel(
                Markdown(str(markdown)),
                title=f"[bold]{_('cli_proposed_plan')}[/bold]",
                border_style="cyan",
            )
        )
    hint = "/".join(
        option.upper() if option == default else option for option in options
    )
    T.console.print(f"[bold yellow]?[/bold yellow] {escape(prompt)} [{hint}]")
    while True:
        raw = _read_answer().lower()
        if not raw:
            return default
        for option in options:
            if option.lower().startswith(raw):
                return option
        T.console.print(f"  [red]{_('invalid_option')}[/red]")


def _ask_question(request: dict) -> str:
    """The agent's `ask_question`: free text, one choice, or several.

    Answers take the desktop app's wire format (``formatAskResponse`` in
    askQuestion.js): the chosen label or the typed text for a single answer, and
    a JSON array of the chosen labels plus any write-in for a multi-select. As in
    that dialog, the model's own "Other" choices give way to one write-in.
    """
    import json

    from rich.markup import escape

    prompt = str(request.get("prompt") or "").strip()
    options = [
        str(o) for o in (request.get("options") or [])
        if not T._OTHER_OPTION_PATTERN.match(str(o).strip())
    ]
    multi = bool(request.get("is_multi_select"))

    T.console.print()
    T.console.print(f"[bold yellow]?[/bold yellow] {escape(prompt)}")
    if not options:
        return _read_answer()

    for index, option in enumerate(options, 1):
        T.console.print(f"  [cyan]{index}[/cyan]) {escape(option)}")
    other = len(options) + 1
    T.console.print(f"  [cyan]{other}[/cyan]) {escape(_('cli_ask_other'))}")
    T.console.print(f"[dim]{_('cli_ask_multi_hint' if multi else 'cli_ask_single_hint')}[/dim]")

    while True:
        raw = _read_answer()
        if not multi:
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            if raw == str(other):
                T.console.print(f"[dim]{_('cli_ask_write_in')}[/dim]")
                return _read_answer()
            if raw and not raw.isdigit():
                return raw
        else:
            picks = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
            if picks and all(p.isdigit() and 1 <= int(p) <= other for p in picks):
                chosen = sorted({int(p) for p in picks})
                selected = [options[i - 1] for i in chosen if i != other]
                if other in chosen:
                    T.console.print(f"[dim]{_('cli_ask_write_in')}[/dim]")
                    custom = _read_answer()
                    if custom:
                        selected.append(custom)
                return json.dumps(selected, ensure_ascii=False)
        T.console.print(f"  [red]{_('invalid_option')}[/red]")


def _run_interactive_command(request: dict) -> str:
    """`run_interactive_command`: the command runs in this terminal, with it.

    The desktop app opens a PTY in a dialog and sends back ``yes`` when the user
    says the command finished, ``cancel`` otherwise. A terminal front-end already
    is a terminal: the command inherits it, so its prompts reach the user
    directly, and the user then says how it went.
    """
    import subprocess

    from rich.markup import escape

    from .tools import get_project_path

    command = str(request.get("command") or "").strip()
    if not command:
        return "cancel"
    T.console.print()
    T.console.print(f"[bold cyan]⏺[/bold cyan] {escape(_('cli_interactive_running'))} [bold]{escape(command)}[/bold]")
    try:
        code = subprocess.call(command, shell=True, cwd=get_project_path() or None)
    except OSError as exc:
        T.console.print(f"  [red]{escape(str(exc))}[/red]")
        return "cancel"
    T.console.print(f"[dim]{_('cli_interactive_exit_code', code=code)}[/dim]")
    answer = _confirm({
        "prompt": _("cli_interactive_finished"),
        "options": ["yes", "no"],
        "default": "yes" if code == 0 else "no",
    })
    return "yes" if answer == "yes" else "cancel"


# ── Background commands ──────────────────────────────────────────────────────

#: Processes `run_background_command` started in this session, stopped on exit
#: the way closing the desktop app ends what ran in its terminal.
_background_processes: list = []


async def terminal_background_command(command: str, cwd: str) -> str:
    """`run_background_command` without the IDE's main terminal.

    The command starts as a background process of this session. Its output goes
    to a log file under the project's ``.gilzabots/background/``, since printing it
    would interleave with the conversation, and the result names that file so
    the agent can read it when it needs to.
    """
    import subprocess
    import time

    log_dir = os.path.join(cwd or os.getcwd(), ".gilzabots", "background")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"command-{time.strftime('%Y%m%d-%H%M%S')}.log")
        log = open(log_path, "ab")
        try:
            process = subprocess.Popen(
                command, shell=True, cwd=cwd or None,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
        finally:
            log.close()
    except OSError as exc:
        return f"FAILED to start background command: {exc}"
    _background_processes.append(process)
    T.console.print(f"[dim]  {_('cli_background_started', pid=process.pid, log=log_path)}[/dim]")
    return (
        f"SUCCESS: The command '{command}' is running in the background (pid {process.pid}). "
        f"Its output is written to {log_path}."
    )


def stop_background_commands() -> None:
    while _background_processes:
        process = _background_processes.pop()
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


# ── Installation ─────────────────────────────────────────────────────────────


def install(renderer: TerminalEventRenderer | None = None) -> TerminalEventRenderer:
    """Route the agent event stream and its input requests to the terminal.

    Mirrors what ``ide_server.start_gui_server`` does for the GUI: both hooks are
    set, because the LiteLLM-side stream publishes through ``litellm.event_hook``.
    """
    import litellm

    from . import agent_stdin

    renderer = renderer or TerminalEventRenderer()
    agent_stdin.event_hook = renderer
    litellm.event_hook = renderer
    agent_stdin.set_input_transport(terminal_input_transport)
    agent_stdin.set_background_command_runner(terminal_background_command)
    agent_stdin.set_reply_surface(agent_stdin.REPLY_SURFACE_TERMINAL)
    return renderer


def uninstall() -> None:
    """Remove the terminal hooks, restoring the default JSON protocol output."""
    import litellm

    from . import agent_stdin

    agent_stdin.event_hook = None
    litellm.event_hook = None
    agent_stdin.set_input_transport(None)
    agent_stdin.set_background_command_runner(None)
    agent_stdin.set_reply_surface(None)
    stop_background_commands()
