"""Commands the agent sends to the desktop IDE, and the IDE's answers.

The agent's file and compile tools act on the disk; these act on the *window*:
which tabs are open and which one is focused. The window is the only party that
knows its own state, so a command is a round trip rather than a fire-and-forget
event: `send_ide_command` publishes an `ide_command` event on the turn's stream,
the desktop app carries it out with the same handlers a click uses
(`gui_src/src/utils/ideCommands.js`) and posts the outcome, together with the
resulting tab state, to `/api/gilzabots/ide_command_result`, which resolves the
pending future here.

This is not the `input_request` channel (`agent_stdin.request_user_input`):
that one waits for a *person* and may wait a day; this one waits for the
application, which answers in milliseconds or not at all. An unanswered command
therefore fails after a short timeout with a diagnostic -- the window may be
closed or disconnected -- instead of pretending the command took effect.

Only the desktop app answers these commands, so the tools that send them are
withheld from the command-line interface (`prompt_surface.GUI_ONLY_TOOLS`).
"""

from __future__ import annotations

import asyncio
import uuid

#: Seconds the window gets to carry out a command. Opening a file reads it over
#: the local API, which is milliseconds; anything near this means no window is
#: listening.
IDE_COMMAND_TIMEOUT = 15.0

_pending: dict[str, asyncio.Future] = {}


class IdeCommandError(ValueError):
    """The IDE did not carry out a command; the message says why."""


async def send_ide_command(
    action: str,
    args: dict,
    *,
    project_path: str,
    timeout: float = IDE_COMMAND_TIMEOUT,
) -> dict:
    """Ask the desktop window to perform *action* and return its answer.

    The answer is the dict the window posted, always with ``ok`` true; a refusal
    (``ok`` false) or no answer raises :class:`IdeCommandError` carrying the
    window's own reason, so the caller reports it rather than guessing.
    """
    from gilzabots.agent_stdin import print_event

    loop = asyncio.get_running_loop()
    command_id = str(uuid.uuid4())
    future = loop.create_future()
    _pending[command_id] = future
    try:
        print_event("ide_command", {
            "id": command_id,
            "action": action,
            "args": dict(args),
            "project_path": project_path,
        })
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError:
            raise IdeCommandError(
                f"The IDE window did not answer the '{action}' command within {timeout:.0f} s: "
                "it may be closed or disconnected from this turn (for example after the computer "
                "slept). Do not retry it; tell the user what you meant to show them instead."
            ) from None
    finally:
        _pending.pop(command_id, None)
        if not future.done():
            future.cancel()

    if not isinstance(result, dict):
        raise IdeCommandError(f"The IDE window sent a malformed answer to '{action}'.")
    if not result.get("ok"):
        reason = str(result.get("error") or "").strip()
        raise IdeCommandError(reason or f"The IDE window refused '{action}' without giving a reason.")
    return result


def resolve_ide_command(command_id: str, result: dict) -> bool:
    """Deliver the window's answer to a pending command.

    Returns False when no command with that id is waiting (it timed out, or the
    id is unknown). Safe to call from any thread.
    """
    future = _pending.get(command_id)
    if future is None or future.done():
        return False

    def _deliver() -> None:
        if not future.done():
            future.set_result(result)

    future.get_loop().call_soon_threadsafe(_deliver)
    return True
