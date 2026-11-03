"""Keep each front-end's own affordances out of the other one's prompt.

The chat-orchestrator's SKILL bodies and tool list are written once and read by
both front-ends, but some of what they describe exists in only one of them:

* the **terminal commands** the desktop chat refuses outright
  (``cli_commands.CommandRegistry`` marks them ``cli_only``), and
* the **window tools**: ``get_editor_state`` reports what the user has open in
  the IDE; ``open_file``/``close_file``, ``reveal_in_pdf`` and ``set_layout``
  change what the window shows; ``get_problems`` and ``read_terminal`` read its
  Problems panel and its main terminal. The terminal has no editor, so under
  the command-line interface the first can only answer "nothing was staged" --
  or, worse, hand back the snapshot a desktop session left on disk earlier, as
  though it were what the user is looking at now -- and the others would wait
  for a window, or read a terminal tab, that does not exist.

Offering either where it cannot work spends context describing it and invites
the model to call it and then report a capability the user does not have. The
surface is decided by ``agent_stdin.reply_surface``: the command-line interface
installs it (``cli_render.install``), and the desktop app leaves it unset.

Filtering is by the *subject* of each item -- the first backticked name in a
bullet, a table row or a comma-separated list entry -- because that is how the
list shapes in the bodies are written (a routing-table row names its correct
route first, after a request written in plain quotes). A line that merely
mentions a name in passing keeps its own subject, so nothing that belongs to
this surface is dropped.
"""

from __future__ import annotations

import re

#: Tools that only mean something in the desktop window.
GUI_ONLY_TOOLS = frozenset({
    "get_editor_state", "open_file", "close_file", "reveal_in_pdf", "set_layout",
    "get_problems", "read_terminal",
})

#: The first backticked name of an item: `/project [new]`, `get_editor_state`.
_SUBJECT_RE = re.compile(r"`([A-Za-z_/][\w./-]*)[^`]*`")

_BULLET_RE = re.compile(r"^\s*[*-]\s")

_TABLE_ROW_RE = re.compile(r"^\s*\|")


def is_terminal_surface() -> bool:
    """Whether the replies of this run are read in a terminal."""
    from .agent_stdin import REPLY_SURFACE_TERMINAL, reply_surface

    return reply_surface() == REPLY_SURFACE_TERMINAL


def has_window() -> bool:
    """Whether a desktop window is attached to this run (its tools can work)."""
    from .agent_stdin import REPLY_SURFACE_BACKGROUND, REPLY_SURFACE_TERMINAL, reply_surface

    return reply_surface() not in (REPLY_SURFACE_TERMINAL, REPLY_SURFACE_BACKGROUND)


def cli_only_command_names() -> frozenset[str]:
    """Every command name the desktop chat refuses, aliases included."""
    from .cli_commands import _registry

    return frozenset(name for name in _registry.names() if _registry.is_cli_only(name))


def _subject(text: str) -> str:
    match = _SUBJECT_RE.search(text)
    return match.group(1) if match else ""


def _split_top_level(line: str) -> list[str]:
    """Split a comma-separated list, ignoring commas inside brackets."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(line):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(line[start:index])
            start = index + 1
    parts.append(line[start:])
    return parts


def strip_items(text: str, removed: frozenset[str] | set[str]) -> str:
    """Remove every bullet and list entry whose subject is in *removed*.

    Fenced code blocks are left alone: an example of what to type is not a list
    of what is available.
    """
    if not removed:
        return text
    lines, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence or "`" not in line:
            lines.append(line)
            continue
        # A bullet or table row whose subject is removed goes entirely; one that
        # survives is still filtered inside, because a bullet can carry a
        # comma-separated list of its own ("neither is any tool of yours:
        # `read_file`, …").
        if (_BULLET_RE.match(line) or _TABLE_ROW_RE.match(line)) and _subject(line) in removed:
            continue
        segments = _split_top_level(line)
        if len(segments) < 2:
            lines.append(line)
            continue
        kept = [s for s in segments if _subject(s) not in removed]
        if len(kept) == len(segments):
            lines.append(line)
            continue
        joined = ",".join(kept).strip()
        # Keeping a separator's spacing: entries are written ", " apart, and the
        # split above cuts before the space.
        lines.append(re.sub(r"\s*,\s*", ", ", joined) if joined else "")
    return "\n".join(lines)


def body_for_surface(body: str) -> str:
    """The orchestrator body with the other front-end's affordances removed."""
    if not has_window():
        text = strip_items(body, GUI_ONLY_TOOLS)
        if not is_terminal_surface():
            # A background turn's reply is read in the desktop chat, where the
            # terminal's commands do not exist either.
            text = strip_items(text, cli_only_command_names())
        # The fallback body lists its tools as bare prose rather than a list, so
        # the subject rule above cannot see them.
        for name in GUI_ONLY_TOOLS:
            text = text.replace(f"{name}, ", "").replace(f", {name}", "")
        return text
    return strip_items(body, cli_only_command_names())


def tools_for_surface(tools: list) -> list:
    """The tool list with the tools this front-end cannot serve removed."""
    if has_window():
        return list(tools)
    return [tool for tool in tools if str(getattr(tool, "name", "")) not in GUI_ONLY_TOOLS]
