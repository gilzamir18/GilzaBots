"""JSON-based stdin/stdout protocol for calling OpalaTex agents."""

import sys
import json
import asyncio
import os
import time
import io
import re

# ── Force UTF-8 on all I/O streams (critical for PyInstaller --windowed) ─────
os.environ["PYTHONUTF8"] = "1"

def _force_utf8_stream(stream):
    """Return a UTF-8 stream, or a safe fallback wrapper."""
    if stream is None:
        return stream
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return stream
    except Exception:
        pass
    try:
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            wrapper = io.TextIOWrapper(binary, encoding="utf-8", errors="replace", line_buffering=True)
            wrapper.mode = getattr(stream, "mode", "w")
            return wrapper
    except Exception:
        pass
    class _UnicodeSafeStream:
        encoding = "utf-8"
        def __init__(self, s): self._stream = s
        def write(self, text):
            try: self._stream.write(text)
            except UnicodeEncodeError:
                try: self._stream.write(text.encode("utf-8", "replace").decode("ascii", "replace"))
                except Exception: pass
            except Exception: pass
        def flush(self):
            try: self._stream.flush()
            except Exception: pass
        def __getattr__(self, name): return getattr(self._stream, name)
    return _UnicodeSafeStream(stream)

sys.stdout = _force_utf8_stream(sys.stdout)
sys.stderr = _force_utf8_stream(sys.stderr)


# The line protocol owns stdout, so the descriptor is captured before anything
# can replace it. The redirection of `sys.stdout` that keeps stray prints out of
# that protocol belongs to the protocol itself and is installed by
# `claim_stdout_for_protocol()`, not by importing this module: the CLI REPL
# imports it (through `build_chat_orchestrator`) only to reuse the turn engine,
# and an import-time redirection sent the whole REPL -- Rich included -- to
# stderr, so `opalatex --cli > log.txt` captured nothing.
_real_stdout = sys.stdout


def claim_stdout_for_protocol() -> None:
    """Route stray prints to stderr so only the JSON protocol reaches stdout.

    Called by the stdin server. Idempotent.
    """
    if sys.stdout is not sys.stderr:
        sys.stdout = sys.stderr

from agenticblocks.blocks.llm.inbox import (
    InboxClosedError,
    InboxError,
    InboxFullError,
    InboxItem,
    MessageInbox,
)

# Hook to intercept event prints (e.g. for Python GUI server)
event_hook = None

# Worker reports are emitted as info events. Keep the latest worker messages
# only as context for corrective retries when the orchestrator finishes
# silently; they must not become a final assistant response by themselves.
_LAST_WORKER_MESSAGES: list[str] = []
_LAST_INTERMEDIATE_AGENT_RESPONSE = ""
EMPTY_RESPONSE_MAX_CORRECTION_ATTEMPTS = 2
# Bounded like the empty-response breaker above: a model that cannot issue native
# tool calls on the current route will not learn to mid-turn, so retrying past this
# only burns tokens before the same failure.
SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS = 2
_ACTIVE_THOUGHT_CHUNKS: list[str] | None = None
# The visible text this turn has streamed so far. A turn that dies mid-stream --
# a provider network error, a standby that kills the socket -- never reaches the
# code that persists its answer, so without this the work was recoverable only
# as activity fragments and the conversation lost it entirely. Reset per turn
# beside `_ACTIVE_THOUGHT_CHUNKS` and read by `_persist_unfinished_turn`.
_ACTIVE_VISIBLE_CHUNKS: list[str] | None = None
# Token count of the active turn's reasoning (opalatex.thought_context). Nothing
# is discarded: every chunk is streamed and stored, and the configured size only
# decides what the chat shows and what a resumed turn replays.
_ACTIVE_THOUGHT_METER = None

# Pending GUI input requests: maps request-id -> asyncio.Future so that the
# /api/opalatex/input_response endpoint can resolve them.
_gui_input_pending: dict = {}


# A front-end that answers `input_request` itself instead of calling back over
# HTTP. The GUI leaves this unset: it receives the event on its stream and
# resolves the future through /api/opalatex/input_response. The CLI sets it so
# the same request is asked in the terminal (opalatex/cli_render.py).
#
# It exists because the two things the agent has to ask a human -- permission to
# run an unsafe tool, and approval of a plan -- were written straight against
# `_gui_input_pending`, which only the HTTP endpoint resolves. Under `--cli` and
# under `--stdin` nothing could answer, so both waited out the 24h timeout.
_input_transport = None  # type: Callable[[dict], Coroutine[Any, Any, str]] | None


def set_input_transport(transport) -> None:
    """Install (or clear, with ``None``) the front-end that answers input requests."""
    global _input_transport
    _input_transport = transport


# A front-end that runs `run_background_command` itself. The GUI leaves this
# unset: the tool sends the command to the IDE's main terminal over HTTP. The CLI
# has no such terminal (and no server to post to), so it installs a runner that
# starts the command as a background process of its own (opalatex/cli_render.py).
_background_command_runner = None  # type: Callable[[str, str], Coroutine[Any, Any, str]] | None


def set_background_command_runner(runner) -> None:
    """Install (or clear, with ``None``) the front-end that runs background commands."""
    global _background_command_runner
    _background_command_runner = runner


def background_command_runner():
    return _background_command_runner


# Where the orchestrator's replies are read. ``None`` is the desktop chat, which
# renders Markdown and typesets math, so a reply written as Markdown with LaTeX
# math reads as formatted text. The CLI sets "terminal": nothing is rendered
# there, and the same reply arrives as a wall of asterisks, `$\alpha$` and
# `![..](..)`. The system prompt reads this to tell the model which surface it
# is writing for (`memgpt_runtime.chat_orchestrator_system_prompt`).
REPLY_SURFACE_TERMINAL = "terminal"
# A monitor's wake-up run by the daemon (PROJECT_DESIGN 2.24): the reply is read
# later in the desktop chat, so it is written as for the desktop, but no window
# is open while the turn runs and nobody is watching it.
REPLY_SURFACE_BACKGROUND = "background"
_reply_surface: str | None = None


def set_reply_surface(surface: str | None) -> None:
    """Install (or clear, with ``None``) the surface the replies are shown on."""
    global _reply_surface
    _reply_surface = surface


def reply_surface() -> str | None:
    return _reply_surface


# A turn a monitor started (PROJECT_DESIGN 2.24) runs with nobody present, and
# its only channel to the user is its reply in its own chat. It names the
# monitor while such a turn runs, and every way a tool asks a person -- tool
# permission, plan approval, ask_question, an interactive terminal -- refuses
# with `unattended_refusal` instead of opening a question outside the chat.
_unattended_monitor: str | None = None
_unattended_monitor_id: str | None = None


class UnattendedTurnError(RuntimeError):
    """A question asked during a turn nobody is present to answer."""


def unattended_turn() -> str | None:
    """The monitor whose wake-up is running, or None in an ordinary turn."""
    return _unattended_monitor


def unattended_monitor_id() -> str | None:
    """The id of the monitor whose wake-up is running, or None when unknown."""
    return _unattended_monitor_id


def unattended_refusal(what: str) -> str:
    """The tool result for an attempt to ask the user during a monitor's turn."""
    return (
        f"Not done: {what}. This turn was started by the monitor \"{_unattended_monitor}\" and nobody "
        "is present to answer outside the chat. Do not retry it in this turn. Carry out the task with the "
        "information you have; if something is missing or needs the user's approval, say so in your reply "
        "in this chat and end the turn -- the user answers there."
    )


async def request_user_input(
    prompt: str,
    *,
    options: list[str] | None = None,
    default: str | None = None,
    input_type: str = "confirm",
    markdown_content: str | None = None,
    agent: str = "",
    timeout: float = 86400.0,
    is_multi_select: bool | None = None,
    extra: dict | None = None,
) -> str:
    """Ask the human a question from inside a tool and return the raw answer.

    The request is always published as an `input_request` event, so every
    front-end watching the stream can show it. How it is *answered* depends on
    the front-end: the GUI posts to /api/opalatex/input_response, the stdin
    protocol sends an ``input_response`` command, and a transport installed with
    :func:`set_input_transport` answers in-process.

    Raises ``asyncio.TimeoutError`` after *timeout*; callers decide what an
    unanswered question means, because the two of them disagree (a tool defaults
    to refusing, a plan defaults to not approved).

    A confirmation carries yes/no choices and a default unless others are given.
    A free-form request (``ask``, ``interactive_terminal``) carries neither unless
    given: the desktop app's contract is that an ask never inherits the
    confirmation's choices (``normalizeInputRequest`` in askQuestion.js).
    ``extra`` adds type-specific fields, such as the interactive terminal's
    ``command`` and ``term_id``.
    """
    import uuid

    if _unattended_monitor is not None:
        # Every caller in a turn refuses before reaching here; one that does
        # not fails loudly instead of waiting for an answer that cannot come.
        raise UnattendedTurnError(unattended_refusal(f"the question {prompt[:120]!r} was not asked"))

    loop = asyncio.get_event_loop()
    req_id = str(uuid.uuid4())
    fut = loop.create_future()
    _gui_input_pending[req_id] = fut

    free_form = input_type in ("ask", "interactive_terminal")
    request = {
        "id": req_id,
        "prompt": prompt,
        "type": input_type,
    }
    if options or not free_form:
        request["options"] = list(options or ["yes", "no"])
    if default is not None or not free_form:
        request["default"] = default if default is not None else "yes"
    if is_multi_select is not None:
        request["is_multi_select"] = bool(is_multi_select)
    if extra:
        request.update(extra)
    if markdown_content is not None:
        request["markdown_content"] = markdown_content
    if agent:
        request["agent"] = agent

    transport_task = None
    try:
        print_event("input_request", dict(request))
        if _input_transport is not None:
            async def _ask_front_end() -> None:
                try:
                    answer = await _input_transport(dict(request))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _logging.getLogger(__name__).warning(
                        "Input transport failed for %s: %s", req_id, exc
                    )
                    return
                if not fut.done():
                    fut.set_result(answer)

            transport_task = loop.create_task(_ask_front_end())
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    finally:
        _gui_input_pending.pop(req_id, None)
        if transport_task is not None and not transport_task.done():
            transport_task.cancel()
        if not fut.done():
            fut.cancel()
        print_event("input_request_closed", {"id": req_id})

# The out-of-band message inbox of the agent turn currently running, and the
# project/chat pair it belongs to. `/api/opalatex/message` submits here; the turn
# opens it in handle_run and closes it in the same turn's cleanup. This is a
# separate channel from `_gui_input_pending` on purpose: that one resolves a
# question the agent asked, this one carries a message the agent did not ask for,
# and routing one into the other would answer a pending `ask_question` with text
# the user wrote for the conversation (PROJECT_DESIGN 2.7).
_active_inbox: "MessageInbox | None" = None
_active_inbox_scope: tuple[str, str] = ("", "")


_TURN_INPUT_HOOK_NAMES = (
    "_async_confirm_hook",
    "_async_ask_hook",
    "_async_interactive_terminal_hook",
)


def _install_turn_input_hooks(confirm_hook, ask_hook, interactive_terminal_hook):
    """Install one turn's GUI input hooks and return the function that removes them.

    The hooks are module globals of ``opalatex.terminal``. Removal puts back a
    hook only while this turn's own hook is still the installed one: a turn whose
    cleanup runs after another turn has installed its hooks must not reset them.
    Resetting them sent the running turn's ``ask_question`` to a blocking
    ``input()`` on the server console, where the GUI user never sees it.
    """
    import opalatex.terminal as T

    installed = dict(zip(
        _TURN_INPUT_HOOK_NAMES,
        (confirm_hook, ask_hook, interactive_terminal_hook),
    ))
    previous = {name: getattr(T, name, None) for name in installed}
    for name, hook in installed.items():
        setattr(T, name, hook)

    def restore():
        for name, hook in installed.items():
            if getattr(T, name, None) is hook:
                setattr(T, name, previous[name])

    return restore


# The turn's questions go through request_user_input, the one channel every
# front-end answers: the GUI over /api/opalatex/input_response, the stdin
# protocol with an `input_response` command, the CLI in the terminal
# (set_input_transport). Registering the future by hand, as these hooks used
# to, left the CLI with nothing that could answer, so `ask_question` waited
# out the 24h timeout. The published event is unchanged for the GUI.
async def _turn_confirm_hook(prompt_text: str, default: bool = True) -> bool:
    try:
        raw = await request_user_input(
            prompt_text,
            options=["yes", "no"],
            default="yes" if default else "no",
            input_type="confirm",
        )
    except asyncio.TimeoutError:
        return default
    return str(raw).strip().lower() in ("yes", "y", "s", "sim", "true", "1")


async def _turn_ask_hook(prompt_text: str, options: list[str] | None = None, is_multi_select: bool = False) -> str:
    try:
        raw = await request_user_input(
            prompt_text,
            options=list(options) if options else None,
            input_type="ask",
            is_multi_select=bool(is_multi_select) if options else None,
        )
    except asyncio.TimeoutError:
        return ""
    return str(raw).strip()


async def _turn_interactive_terminal_hook(command: str, term_id: str) -> str:
    try:
        raw = await request_user_input(
            "Interactive terminal spawned",
            input_type="interactive_terminal",
            extra={"command": command, "term_id": term_id},
        )
    except asyncio.TimeoutError:
        return ""
    return str(raw).strip()


class InboxScopeError(RuntimeError):
    """Raised when a message is submitted to a turn of a different project/chat."""


def active_inbox_scope() -> dict | None:
    """Describe the running turn's inbox, or None when no turn accepts messages."""
    if _active_inbox is None or _active_inbox.closed:
        return None
    project_path, chat_id = _active_inbox_scope
    return {
        "project_path": project_path,
        "chat_id": chat_id,
        "pending": len(_active_inbox),
        "max_pending": _active_inbox.max_pending,
    }


def submit_chat_message(
    content: str,
    *,
    project_path: str = "",
    chat_id: str = "",
    attachments: list | None = None,
    client_message_id: str = "",
    display_text: str = "",
) -> dict:
    """Queue a message for the agent turn already running.

    Raises `InboxClosedError` when no turn is accepting messages, `InboxFullError`
    when too many are already waiting, and `InboxScopeError` when the caller is
    talking to a different conversation than the one running. Each is a clear
    diagnostic the caller must surface: a message that is accepted and never
    delivered is worse than one that is refused.
    """
    inbox = _active_inbox
    if inbox is None or inbox.closed:
        raise InboxClosedError("no agent turn is currently accepting messages")

    scope_project, scope_chat = _active_inbox_scope
    requested_project = str(project_path or "").strip()
    requested_chat = str(chat_id or "").strip()
    if requested_project and scope_project and requested_project != scope_project:
        raise InboxScopeError("the running agent turn belongs to another project")
    if requested_chat and scope_chat and requested_chat != scope_chat:
        raise InboxScopeError("the running agent turn belongs to another chat")

    item = inbox.submit(
        content,
        attachments=attachments,
        metadata={
            "client_message_id": str(client_message_id or "").strip(),
            "display_text": str(display_text or "").strip(),
        },
    )
    return {
        "item_id": item.item_id,
        "client_message_id": item.metadata.get("client_message_id", ""),
        "pending": len(inbox),
    }


def cancel_chat_message(item_id: str) -> bool:
    """Drop a queued message. False means it was already delivered or unknown."""
    inbox = _active_inbox
    if inbox is None:
        return False
    return inbox.cancel(str(item_id or ""))

import litellm

# Ollama (and some other local providers) don't support params like
# presence_penalty. Drop unsupported params silently instead of crashing.
litellm.drop_params = True
# OpalaTex agents use Chat Completions-style tool loops. Do not let LiteLLM
# globally reroute OpenAI chat calls to the stateful Responses API.
litellm.route_all_chat_openai_to_responses = False

# ── Register model cost for the OpalaTex Cloud proxy model ──────────────────
# The Cloud proxy routes approved openai/gemini-* aliases to Google Gemini via
# opalacoder.com. LiteLLM has no built-in cost entry for this custom routing,
# so register_model emits a DeprecationWarning about missing cache cost fields
# on every Router deployment (once per session, per model_id hash).
#
# We register the canonical provider-prefixed name with cache cost fields set
# to 0.0 (billing is handled server-side via token_balance, not via LiteLLM's
# cost map), which silences the warning for the alias entry.
#
# The Router also registers under a SHA-256 model_id hash (derived from
# litellm_params including api_key), which is unstable across sessions and
# cannot be pre-registered. We suppress that specific warning via a logging
# filter so it doesn't pollute the console without affecting other warnings.
try:
    litellm.register_model({
        "openai/gemini-3.5-flash-lite": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "cache_creation_input_token_cost": 0.0,
            "cache_read_input_token_cost": 0.0,
        },
        "openai/gemini-3.1-flash-lite": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "cache_creation_input_token_cost": 0.0,
            "cache_read_input_token_cost": 0.0,
        },
        "openai/gemini-3.5-flash": {
            "litellm_provider": "openai",
            "mode": "chat",
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "cache_creation_input_token_cost": 0.0,
            "cache_read_input_token_cost": 0.0,
        },
    })
except Exception:
    pass

import logging as _logging

class _RegisterModelCostFilter(_logging.Filter):
    """Suppress only the register_model cache-cost warning (hash-based id).

    The hash-based model_id is unstable (depends on api_key/api_base in
    litellm_params), so it cannot be pre-registered. This filter silences
    that specific message without affecting other LiteLLM warnings.
    """
    def filter(self, record: _logging.LogRecord) -> bool:
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record)
        if "not in built-in cost map and no prefix/region variant matched" in msg:
            return False
        return True

try:
    litellm.verbose_logger.addFilter(_RegisterModelCostFilter())
except Exception:
    pass

from agenticblocks.blocks.llm.tokens import count_message_tokens
from opalatex.chat_meta_params import parse_meta_params, apply_meta_params
from opalatex.think_stream import InlineReasoningStreamSplitter
from opalatex.token_usage import (
    CONTEXT_AGENTS as TOKEN_CONTEXT_AGENTS,
    attach_usage_tracking,
    context_scope_key,
    get_context_prompt_tokens,
    record_context_tokens,
    set_context_scope,
    set_context_window,
    set_usage_listener,
)

def _prompt_budget_from_error(msg: str) -> tuple[int, int] | None:
    """Extract (prompt_tokens, server_max_tokens) from a prompt-too-long error.

    OpenAI-compatible runtimes (vLLM, llama.cpp servers, and the FreeToken-style
    local hosts) reject an oversized request with a plain-text message such as
    ``prompt is too long: 13557 tokens > 8221 maximum (prompt + generation)``.
    LiteLLM wraps it in an ``APIConnectionError``, so without this the generic
    connection branch below reports an unreachable server for a server that
    answered. The wording never contains "context", so the context-window branch
    does not catch it either.
    """
    import re

    match = re.search(
        r"prompt is too long:\s*(\d+)\s*tokens?\s*>\s*(\d+)\s*maximum",
        msg,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(\d+)\s*tokens?[^\d]{0,40}?>\s*(\d+)\s*maximum\s*\(prompt",
            msg,
            re.IGNORECASE,
        )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _friendly_llm_error(exc: Exception, project=None) -> str:
    """Convert a LiteLLM/agent exception into a user-friendly message."""
    msg = str(exc)
    #print(f"\n[DEBUG ERROR TRACE] Caught Exception: {type(exc).__name__}")
    #print(f"[DEBUG ERROR TRACE] Raw Exception Message: {msg}\n")
    low = msg.lower()
    model = getattr(project, "model", None) or "the configured model"
    api_base = getattr(project, "api_base", "") or ""

    if (
        "opalatex_ollama_tool_call_json_escape" in low
        or "error parsing tool call" in low
        or "tool-call json was invalid" in low
        or "returned invalid tool call arguments" in low
    ):
        return (
            f"Ollama rejected a tool call from {model}: the model emitted invalid "
            "JSON inside a tool argument. The tool-call JSON was invalid, commonly "
            "from unescaped LaTeX backslashes or raw newlines. Automatic correction attempts were exhausted."
        )

    # Checked before the connection branch: OpenAI-compatible servers raise this
    # as an APIConnectionError even though the server was reached and answered.
    prompt_budget = _prompt_budget_from_error(msg)
    if prompt_budget is not None:
        from opalatex.i18n import _
        prompt_tokens, max_tokens = prompt_budget
        return _(
            "err_prompt_exceeds_server_budget",
            model=model,
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens,
        )

    if "exceed_context_size_error" in low or (
        "context" in low and ("length" in low or "window" in low or "exceed" in low)
    ):
        from opalatex.i18n import _
        return _("err_context_exceeded").format(model=model)

    # Checked before the Ollama-status and connection branches: LiteLLM words
    # every timeout as a connection problem ("Connection timed out", or an
    # APIConnectionError for a stream that stalls mid-response), and a timeout
    # value such as "500.0 seconds" would be read as an HTTP status.
    timeout_kind = _llm_timeout_kind(low)
    if timeout_kind is not None:
        from opalatex.i18n import _
        return _(timeout_kind, model=model)

    ollama_status = _ollama_http_status_from_error(msg)
    if ollama_status is not None:
        return (
            f"Ollama returned HTTP {ollama_status} while generating a response from "
            f"{model}. The server was reached, so this is not a connection error. "
            "Reproduce it while checking the Ollama server log. If it happens only "
            "with one type of prompt, isolate model/runtime parameters such as "
            "think, num_ctx, top_k, and reasoning_effort."
        )

    if "unexpected keyword argument" in low or "got an unexpected" in low:
        # Extract the parameter name from messages like: "got an unexpected keyword argument 'reasoning_effort'"
        import re
        m = re.search(r"unexpected keyword argument ['\"]([^'\"]+)['\"]", msg)
        param = m.group(1) if m else "unknown"
        return (
            f"Parameter '{param}' is not supported by {model}. "
            "Clear it where it is configured: inference parameters (temperature, "
            "top_k, reasoning_effort, ...) and additional model parameters live on "
            "the model's catalog entry (Edit Models, or /models); per-project "
            f"parameters are cleared with /set-model-param {param} null."
        )

    from opalatex.i18n import _

    # Checked before the generic parameter-rejection branch below: providers
    # report this as a BadRequestError/invalid_request_error, so the generic
    # branch would swallow it and hide the actionable diagnostic.
    if "system message must be at the beginning" in low:
        from opalatex.config import model_requires_single_system_message
        if model_requires_single_system_message(model):
            return _("err_system_message_order_unresolved", model=model)
        return _("err_system_message_order", model=model)

    if "invalid value for" in low or "invalid_request_error" in low or "badrequest" in low:
        return f"The model rejected a parameter value: {msg}"

    if "404 page not found" in low:
        return _("err_connection_failed").format(model=model)

    if "not found" in low or "pull" in low or "try pulling it first" in low:
        from opalatex.config import is_local_model
        if model.startswith("ollama/") and is_local_model(model, api_base):
            return _("err_ollama_model_not_found", model=model.replace("ollama/", ""))
        return _("err_model_not_found", model=model)

    if "authentication" in low or "api key" in low or "unauthorized" in low:
        return _("err_auth_failed").format(model=model)

    if "retired" in low:
        return f"O modelo {model} foi descontinuado (retired) e não pode mais ser utilizado."

    if "connection" in low or "connect" in low:
        return _("err_connection_failed").format(model=model)

    if "insufficient_quota" in low or "insufficient quota" in low or "billing" in low or "credit" in low or "balance" in low:
        return _("err_insufficient_quota", model=model)

    if "ratelimit" in low or "rate limit" in low or "429" in low or "quota exceeded" in low or "resource_exhausted" in low:
        import re

        delay_match = re.search(r"please retry in ([\d\.]+)s", low)
        delay_str = f" Aguarde cerca de {round(float(delay_match.group(1)))} segundos e tente novamente." if delay_match else " Aguarde alguns instantes e tente novamente."

        return f"Você atingiu o limite de requisições (Rate Limit) do provedor para o modelo {model}.{delay_str}"

    return msg


def _apply_model_thinking_capability(model: str, model_kwargs: dict) -> dict:
    """Drop thinking params unless the selected model explicitly supports them."""
    from opalatex.config import model_supports_thinking

    cleaned = dict(model_kwargs or {})
    if "think" in cleaned and not model_supports_thinking(model):
        cleaned.pop("think", None)
    return cleaned


def clear_worker_message_buffer() -> None:
    """Clear worker messages captured outside the final agent_response path."""
    global _LAST_INTERMEDIATE_AGENT_RESPONSE
    _LAST_WORKER_MESSAGES.clear()
    _LAST_INTERMEDIATE_AGENT_RESPONSE = ""


def record_worker_message(message: str) -> None:
    """Record a worker report for corrective retry context."""
    text = str(message or "").strip()
    if text:
        _LAST_WORKER_MESSAGES.append(text)


def _worker_summary_response(agent) -> str:
    """Return the latest worker-facing summary for corrective retry context."""
    if _LAST_INTERMEDIATE_AGENT_RESPONSE.strip():
        return _LAST_INTERMEDIATE_AGENT_RESPONSE.strip()

    messages = [
        str(m).strip()
        for m in getattr(agent, "_current_worker_messages", []) or []
        if str(m).strip()
    ]
    if messages:
        return "\n".join(messages)

    summary = str(getattr(agent, "_last_worker_summary", "") or "").strip()
    if summary:
        return summary

    if _LAST_WORKER_MESSAGES:
        return "\n".join(_LAST_WORKER_MESSAGES)

    for item in reversed(getattr(agent, "internal_history", []) or []):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        summary = _extract_worker_summary_from_tool_result(content)
        if summary:
            return summary

    return ""


def _extract_worker_summary_from_tool_result(content: str) -> str:
    """Extract a helper's report from a run_skill tool result.

    Chats stored before helpers existed hold results with the worker marker,
    so both are read.
    """
    from opalatex.memgpt_runtime import HELPER_REPORT_MARKER, LEGACY_WORKER_REPORT_MARKER

    marker = next(
        (m for m in (HELPER_REPORT_MARKER, LEGACY_WORKER_REPORT_MARKER) if m in content), None
    )
    if marker is None:
        return ""

    summary = content.split(marker, 1)[1].strip()
    lines = summary.splitlines()
    if lines and lines[0].strip().startswith(("(Tools used by worker:", "(Tool calls made by the helper:")):
        summary = "\n".join(lines[1:]).strip()
    # The runtime's tool-error list is not part of the helper's report.
    summary = summary.split("\n\n[Tool errors in this helper run", 1)[0].strip()
    return summary


def _response_with_thought(response: str, thought_chunks: list[str]) -> str:
    """Return the content safe to persist in chat history."""
    text = _strip_empty_think_blocks(str(response or "")).strip()
    return _visible_chat_response(text)


def _mark_turn_without_answer(resp_obj, response: str) -> str:
    """Say so when the run stopped before the model ever answered.

    A run can end with only what the model said on the way -- "I will inspect
    now." and nothing else. That text is real and must not be dropped, but it is
    an announcement, and persisting it as the assistant's reply hands the user
    work nobody did. The block reports the difference (`final_text` empty while
    `narration` is not), so the turn is delivered with its progress intact and
    labelled for what it is.

    *Why* it ended decides the label, because the two causes have different
    remedies. A spent step budget (`budget_exhausted`) is fixed by a larger one.
    A model that stopped acting with steps still left -- the idle allowance ran
    out, or it returned nothing -- would do the same under any budget, and
    telling the user to raise it sends them after a setting that cannot help.
    """
    if not str(response or "").strip():
        return response
    if str(getattr(resp_obj, "final_text", "") or "").strip():
        return response
    if not (getattr(resp_obj, "narration", None) or []):
        return response
    marker = (
        TURN_CUT_SHORT_MARKER
        if getattr(resp_obj, "budget_exhausted", False)
        else TURN_NO_ANSWER_MARKER
    )
    return f"{response}\n\n{marker}"


PROGRESS_FENCE_LANG = "opalatex-progress"
"""Deliberately not ``progress``: the fence is stripped before an assistant turn
is replayed to the model, and a bare ``progress`` tag is something a model can
legitimately write when a user asks it for one. The namespaced tag makes the
markup this host's own, so stripping it can never eat the model's content."""


def _fold_turn_progress(resp_obj, response: str) -> str:
    """Group mid-turn progress into a collapsed block above the answer.

    The model speaks while it works and again when it is done, and both reach the
    user -- that is the whole point of the single text channel. This is layout on
    top of that, never a filter: every character of *response* survives, the
    progress simply stops sitting between the reader and the deliverable.

    The split is applied to the already-sanitized text and only where the boundary
    is still verifiable in it, so nothing bypasses sanitization. If the tail no
    longer matches (a correction rewrote it) or the progress carries a fence of
    its own, the response is returned whole: showing it unfolded is a cosmetic
    loss, and dropping it is the defect this protocol was rewritten to remove.
    """
    narration = [
        str(t).strip()
        for t in (getattr(resp_obj, "narration", None) or [])
        if str(t).strip()
    ]
    final_text = str(getattr(resp_obj, "final_text", "") or "")
    text = str(response or "")
    # Compared raw, never stripped: the answer may legitimately open with
    # indentation (a Markdown code block), and trimming it here would move the
    # boundary and hand that indentation to the progress block instead.
    if not narration or not final_text.strip() or not text.endswith(final_text):
        return response
    progress = text[: len(text) - len(final_text)].strip()
    if not progress or "```" in progress:
        return response
    return f"```{PROGRESS_FENCE_LANG}\n{progress}\n```\n\n{final_text}"


def _visible_chat_response(response: str) -> str:
    """Return the only content allowed in chat history or user-visible chat."""
    visible, _thoughts = _split_channel_markup(response)
    return _strip_empty_think_blocks(visible).strip()


def _strip_empty_think_blocks(content: str) -> str:
    """Remove empty thinking sections produced by local reasoning models."""
    return re.sub(r"<think>\s*</think>\s*", "", str(content or ""), flags=re.IGNORECASE)


def _strip_chat_control_tokens(content: str) -> str:
    """Remove raw chat-template control tokens from model output."""
    from agenticblocks.utils.parsers import strip_chat_control_tokens

    return strip_chat_control_tokens(content)


def _split_channel_markup(content: str) -> tuple[str, list[str]]:
    """Split gpt-oss/Ollama channel markup into visible text and thought text.

    The splitting itself lives in ``agenticblocks.utils.parsers`` next to the
    ``<think>`` parsing it falls back to: a reasoning model reaches the content
    channel in one shape or the other depending on the provider, so every
    surface that publishes model text needs both, not just the chat. This
    wrapper only keeps the argument order the chat loop uses.
    """
    from agenticblocks.utils.parsers import split_channel_markup

    thoughts, visible = split_channel_markup(content)
    return _strip_empty_think_blocks(visible).strip(), thoughts


def _sanitize_model_response(response: str, thought_chunks: list[str]) -> str:
    """Move raw reasoning-channel output into thought storage and return visible text."""
    visible, channel_thoughts = _split_channel_markup(response)
    for thought in channel_thoughts:
        cleaned = thought.strip()
        if cleaned:
            thought_chunks.append(cleaned)
    return visible.strip()


def _normalize_inline_fenced_replacement(response: str) -> str:
    """Return only source content from an optional fenced inline response."""
    text = str(response or "").strip()
    orphan_parts = text.rsplit("</think>", 1)
    if len(orphan_parts) == 2 and orphan_parts[1].lstrip().startswith(("```", "~~~")):
        text = orphan_parts[1].lstrip()
    blocks = list(re.finditer(r"^(?: {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)\r?\n(?P<body>.*?)^(?: {0,3})(?P=fence)[ \t]*$", text, flags=re.MULTILINE | re.DOTALL))
    if not blocks:
        return text
    content_block = next((block for block in blocks if block.group("info").strip().lower() == "content"), None)
    outer_block = next((block for block in blocks if len(block.group("fence")) >= 4), None)
    block = content_block or outer_block or blocks[-1]
    body = block.group("body").rstrip("\r\n")
    if block.group("info").strip().lower() != "content" and body.startswith("content\n"):
        # Recover only the protocol label when it was placed inside a language fence.
        body = body[len("content\n"):]

    longest_fence = max((len(run) for run in re.findall(r"(?m)^(?: {0,3})(`+)", body)), default=3) + 1
    fence = "`" * max(4, longest_fence)
    return body


def _compact_inline_scope_text(content: str) -> str:
    """Normalize source text for bounded inline replacement scope checks."""
    return re.sub(r"\s+", "", str(content or ""))


def _validate_inline_replacement_scope(
    response: str,
    editor_content: str,
    selected_text: str,
) -> None:
    """Reject a full-document response when inline editing targets a smaller scope."""
    source = _compact_inline_scope_text(editor_content)
    replacement = _compact_inline_scope_text(response)
    selection = _compact_inline_scope_text(selected_text)
    if len(source) < 192 or not replacement or selection == source:
        return

    if selection:
        selection_offset = source.find(selection)
        if selection_offset >= 0:
            before_selection = source[max(0, selection_offset - 96):selection_offset]
            after_selection = source[
                selection_offset + len(selection):selection_offset + len(selection) + 96
            ]
            anchors = [anchor for anchor in (before_selection, after_selection) if len(anchor) >= 48]
            if len(anchors) >= 2 and all(anchor in replacement for anchor in anchors):
                raise RuntimeError(
                    "Inline editor returned surrounding document context instead of the selected replacement."
                )

    source_start = source[:96]
    source_end = source[-96:]
    if source_start in replacement and source_end in replacement:
        raise RuntimeError(
            "Inline editor returned a copy of the full document instead of an inline replacement."
        )


def _looks_like_plain_tool_call_text(content: str) -> bool:
    """Return True when a model emitted a tool call as plain JSON text."""
    text = str(content or "").strip()
    lowered = text.lower()
    if lowered.startswith("```json"):
        lowered = lowered.split("\n", 1)[1] if "\n" in lowered else lowered
    return (
        lowered.startswith("{")
        and '"name"' in lowered
        and '"arguments"' in lowered
    )


def _compact_tool_argument_summary(arguments: object) -> str:
    """Return a human-readable argument summary for diagnostic thoughts."""
    if not isinstance(arguments, dict) or not arguments:
        return ""

    preferred_keys = (
        "path",
        "file_path",
        "query",
        "command",
        "pattern",
        "root_dir",
        "start_line",
        "end_line",
        "max_depth",
        "max_results",
    )
    parts: list[str] = []
    for key in preferred_keys:
        if key not in arguments:
            continue
        value = arguments.get(key)
        if value is None or value == "":
            continue
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if len(text) > 80:
            text = f"{text[:77]}..."
        parts.append(f"{key}={text}")
        if len(parts) >= 3:
            break

    if parts:
        return "; ".join(parts)

    names = [str(key) for key, value in arguments.items() if value not in (None, "")]
    if not names:
        return ""
    shown = ", ".join(names[:3])
    if len(names) > 3:
        shown += f", and {len(names) - 3} more"
    return f"parameters: {shown}"


def _auxiliary_tool_call_thought(tool_name: str, arguments: object) -> str:
    """Build the Thinking-panel sentence for a tool call without raw JSON."""
    summary = _compact_tool_argument_summary(arguments)
    if summary:
        return f"Decided to execute tool '{tool_name}' ({summary})."
    return f"Decided to execute tool '{tool_name}'."


def _should_emit_iteration_reflection(last_message: dict) -> bool:
    """Only assistant prose/reasoning belongs in the reflection panel."""
    if not isinstance(last_message, dict):
        return False
    if last_message.get("role") != "assistant":
        return False
    content = str(last_message.get("content") or "").strip()
    if not content:
        return False
    if _looks_like_plain_tool_call_text(content):
        return False
    return True


def _record_turn_thought(content: str) -> bool:
    """Record a thought chunk for the active chat turn; empty chunks carry nothing."""
    text = str(content or "")
    if not text or _ACTIVE_THOUGHT_CHUNKS is None:
        return bool(text)
    _ACTIVE_THOUGHT_CHUNKS.append(text)
    if _ACTIVE_THOUGHT_METER is not None:
        _ACTIVE_THOUGHT_METER.add(text)
    return True


def publish_thought(chunk: str, agent_type: str) -> None:
    """Record a reasoning chunk for the turn and publish it to the front-end."""
    if _record_turn_thought(chunk):
        print_event("thought", {"content": chunk, "agent": agent_type, "_thought_recorded": True})


def inline_reasoning_splitter(agent_type: str) -> "InlineReasoningStreamSplitter":
    """Route a streamed content channel into answer text and reasoning.

    The three callbacks are one contract, which is why they are wired in one
    place:

    * visible text is published as it streams;
    * an orphan ``</think>`` proves the text already published was reasoning, so
      ``stream_retract`` tells the front-end to drop it from the tail of the
      live response (the desktop chat and the terminal renderer both do);
    * whatever the splitter took out of the answer -- removed before it was
      published (``<think>…</think>``) or retracted after -- is republished as a
      ``thought``.

    That last step is never gated on ``publish_reasoning``. That flag says
    whether the provider was asked to isolate a reasoning channel, and this text
    came through the *content* channel precisely because it was not. Gating it
    dropped the reasoning entirely for a model whose catalog entry declares no
    thinking support, and in the retracted shape the desktop chat wiped text it
    was promised would come back.
    """
    from .think_stream import InlineReasoningStreamSplitter

    return InlineReasoningStreamSplitter(
        on_visible=lambda text: print_event("stream_chunk", {"content": text, "agent": agent_type}),
        on_thinking=lambda text: publish_thought(text, agent_type),
        on_retract=lambda text: print_event("stream_retract", {"content": text, "agent": agent_type}),
    )


EMPTY_RESPONSE_NUDGE = (
    "Your last run ended without a user-facing response. Reply now with a non-empty "
    "final response: report what was actually done and verified, and state plainly "
    "anything that is still unfinished. Use a tool only if more work is required."
)
"""Model-facing, so English whatever language the interface uses, like every
other instruction the agent reads. It deliberately does not say the work is
finished: the run can end silently in the middle of it, and telling the model it
completed the request invites exactly the false success report the rest of the
turn protocol exists to prevent."""

SERIALIZED_TOOL_CALL_NUDGE = (
    "Your last message contained a tool call written as text (JSON, or "
    "<tool_call>/<function=...> markup). Text is never a tool call and nothing "
    "executes it. If you still need that action, issue it now through the provider's "
    "native tool-calling protocol. If the work is already done, reply with the final "
    "user-facing answer as plain text."
)


def _empty_response_retry_prompt(worker_summary: str = "") -> str:
    """Build the corrective prompt used when the orchestrator ends silently."""
    prompt = EMPTY_RESPONSE_NUDGE
    summary = str(worker_summary or "").strip()
    if summary:
        prompt += (
            "\n\nA helper already did its job and returned this report. "
            "Use it to produce the final user-facing message; do not repeat the job:\n"
            f"{summary}"
        )
    return prompt


def _empty_response_failure_message() -> str:
    """Return the localized hard failure for a turn that produced no answer."""
    from opalatex.i18n import _

    return _("empty_response_unresolved_error")


_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def _has_unfenced_tool_call_payload(response: str, tool_schemas=()) -> bool:
    """True when tool-call JSON appears outside every fenced code block.

    A model that *shows* JSON to the user fences it; a model that failed to issue
    a native tool call prints it raw. Scanning only the unfenced text is what
    keeps a legitimate "give me a JSON example" answer deliverable, because the
    payload detector matches any object carrying `name` plus `arguments` and that
    shape occurs in real data too.

    *tool_schemas* are the OpenAI-format schemas of the tools the agent was
    offered. With them, a third encoding is caught: the arguments object alone,
    with no name around it. Observed with ``ollama_chat/gpt-oss:20b``, which
    issued ``ask_question`` under a garbled name, got "not found" back, and then
    printed the question's arguments as its answer. Such an object carries no
    tool-call markers at all, so it is recognized by fitting a tool's parameter
    schema exactly (`tools_accepting_arguments`) -- and, like the other two
    encodings, it is never executed on that basis.
    """
    from agenticblocks.utils.tool_calls import tools_accepting_arguments
    from opalatex.memgpt_runtime import _iter_json_spans, _strip_serialized_tool_calls

    unfenced = _FENCED_BLOCK_RE.sub(" ", str(response or ""))
    _, had_payload = _strip_serialized_tool_calls(unfenced)
    if had_payload:
        return True
    schemas = list(tool_schemas or ())
    if not schemas:
        return False
    for start, end in _iter_json_spans(unfenced):
        try:
            parsed = json.loads(unfenced[start:end])
        except Exception:
            continue
        if tools_accepting_arguments(parsed, schemas):
            return True
    return False


def _agent_tool_schemas(agent) -> list[dict]:
    """Return the schemas of the tools *agent* last offered its model.

    ``tools`` is refreshed from ``tools_provider`` before every request, so it is
    the snapshot the model actually saw. A wrapper exposing the block as
    ``.agent`` is unwrapped the same way the callback wiring does it.
    """
    from agenticblocks.tools.a2a_bridge import block_to_tool_schema

    target = agent if hasattr(agent, "tools") else getattr(agent, "agent", None)
    return [block_to_tool_schema(tool) for tool in (getattr(target, "tools", None) or [])]


def _serialized_tool_call_retry_prompt() -> str:
    """Build the corrective prompt for a tool call written as text."""
    return SERIALIZED_TOOL_CALL_NUDGE


def _serialized_tool_call_failure_message() -> str:
    """Return the localized hard failure for an unrecoverable serialized call."""
    from opalatex.i18n import _

    return _("serialized_tool_call_unresolved_error")


def _report_rejected_serialized_response(response: str, limit: int = 400) -> None:
    """Send the rejected text to the Problems panel, not to the chat.

    Without this the response is discarded with no trace, leaving no way to tell
    a model that really printed a tool call from a false positive in the
    detector. The chat still gets the clean error; the raw payload belongs in
    diagnostics, where it cannot be mistaken for an answer.
    """
    from opalatex.i18n import _

    excerpt = str(response or "").strip()[:limit]
    print_event("problem", {
        "tool": "serialized tool call",
        "severity": "error",
        "message": _("serialized_tool_call_rejected_payload", n=limit, excerpt=excerpt),
    })


def _report_unknown_tool_call(name: str) -> None:
    """List a call to a tool the agent was not offered in the Problems panel.

    No tool ran, so wrap_tool emitted nothing and the call would leave no trace.
    Problems only, not Thinking: the model usually recovers within the turn, and
    a line in the reasoning stream would read as the model's own thought.
    """
    from opalatex.i18n import _

    print_event("problem", {
        "tool": name,
        "severity": "error",
        "message": _("unknown_tool_called", tool=name),
    })


async def _correct_serialized_tool_calls(agent, response, thought_chunks, meta_overrides, resp_obj=None):
    """Return ``(response, resp_obj)``, corrected when the response is a tool call written as text.

    *resp_obj* is the run output the response came from. A correction re-runs the
    agent, and the output of that run is returned with its response: the caller
    labels the turn from ``final_text`` and ``budget_exhausted``, and reading
    them off the run that was pushed back would describe a different run.

    A tool call printed as text executes nothing, and unlike a worker report
    (sanitized in `memgpt_runtime`) the orchestrator's own output goes straight to
    the user -- so the turn ends looking answered while nothing happened. Push it
    back with a diagnostic and let the model issue the call properly.

    The payload is never parsed into a real call: doing so would invent an action
    the model never actually issued, exactly the silent substitution CLAUDE.md
    rule 1.1 forbids. When the bounded breaker is spent, this raises instead of
    handing the user raw JSON that implies work nobody did.
    """
    from opalatex.i18n import _

    attempts = 0
    while (
        response
        and _has_unfenced_tool_call_payload(response, _agent_tool_schemas(agent))
        and attempts < SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS
    ):
        attempts += 1
        print_event("info", {"message": _("serialized_tool_call_retry_info")})
        nudge = _serialized_tool_call_retry_prompt()
        # System role, not user: this is runtime feedback, not something the user
        # typed (PROJECT_DESIGN 2.6). Agents with no conversation history reject a
        # non-user role, so they keep the default.
        retry_input = (
            AgentInput(prompt=nudge, role="system")
            if hasattr(agent, "internal_history")
            else AgentInput(prompt=nudge)
        )
        with apply_meta_params(agent, meta_overrides):
            resp_obj = await agent.run(retry_input)
        response = _sanitize_model_response(resp_obj.response or "", thought_chunks)

    if response and _has_unfenced_tool_call_payload(response, _agent_tool_schemas(agent)):
        _report_rejected_serialized_response(response)
        raise RuntimeError(_serialized_tool_call_failure_message())
    return response, resp_obj


# Panel diagnostics kept across reloads. "error" is included so a failed turn still
# shows its error bubble after the chat is reopened: it is UI-visible state that must
# not enter project_history, because the model must not read its own failures back as
# conversation (PROJECT_DESIGN 2.5).
_PERSISTED_ACTIVITY_EVENTS = {"thought", "reflection", "stream_chunk", "error"}
INTERRUPTED_AGENT_HISTORY_MARKER = "[INTERRUPTED] The user interrupted the agent execution."
TURN_CUT_SHORT_MARKER = (
    "[TURN-CUT-SHORT] The runaway guardrail stopped this turn before the model "
    "gave a final answer. The text above is work in progress, not a reply."
)
"""Stable, unlocalised, appended after the progress a cut-short turn produced.

Mirrors ``INTERRUPTED_AGENT_HISTORY_MARKER``: the marker is what gets persisted
and what the model reads back, while the front-end matches it and renders the
localised notice plus the continue action. Persisting the translated prose
instead would make the match depend on which language was active when the turn
ran, so the button would vanish for anyone who later switched languages."""


TURN_NO_ANSWER_MARKER = (
    "[TURN-NO-ANSWER] The model stopped before giving a final answer, with steps "
    "still left. The text above is work in progress, not a reply."
)
"""Stable and unlocalised, like the markers above.

The turn ended without an answer while its step budget still had room, so the
notice the front-end shows for it must not point at the budget: it is the same
kind of unfinished turn as a cut-short one, with a different cause."""


TURN_FAILED_MARKER = (
    "[TURN-FAILED] This turn stopped on an error before the model gave a final "
    "answer. The text above is work in progress, not a reply."
)
"""Stable and unlocalised, for the same reason as the two markers above.

A turn can die on something that is nobody's decision -- the provider becoming
unreachable, which a system standby causes by killing the connection mid-stream.
That path used to persist *nothing*: the error reached the panel, the streamed
answer stayed only as activity fragments, and the conversation kept just the
user's question. Reopening the chat then showed an empty-looking exchange and
"Continue" had no assistant turn to resume from. What the model wrote before the
failure is real work and is now stored like any other partial turn."""


#: Every marker that says an assistant turn ended without an answer. Mirrors
#: ``gui_src/src/utils/turnMarkers.js``; both front-ends decide the same way
#: whether a turn can be continued.
TURN_MARKERS = (
    INTERRUPTED_AGENT_HISTORY_MARKER,
    TURN_CUT_SHORT_MARKER,
    TURN_FAILED_MARKER,
    TURN_NO_ANSWER_MARKER,
)


def unfinished_turn_content(history: list[dict] | None) -> str:
    """Return the last assistant message when it ended without an answer, else "".

    A marker is a *suffix*, not the whole message: `_persist_unfinished_turn`
    stores what the model had written and appends the marker, so this matches by
    containment the way the front-end does.
    """
    for message in reversed(history or []):
        role = str(message.get("role") or "")
        if role == "user":
            return ""
        if role != "assistant":
            continue
        content = str(message.get("content") or "")
        return content if any(marker in content for marker in TURN_MARKERS) else ""
    return ""


def _turn_visible_text() -> str:
    """Return the visible text streamed by the turn so far."""
    return "".join(_ACTIVE_VISIBLE_CHUNKS or []).strip()


def _persist_unfinished_turn(
    agent_type: str,
    marker: str,
    *,
    only_with_work: bool = False,
) -> None:
    """Persist what an interrupted or failed turn produced, plus *marker*.

    The marker alone used to be stored, which is the same loss in a smaller
    shape: the partial answer was on screen, was never a message, and vanished
    on the next reload. Whatever the model actually wrote is kept above the
    marker, exactly as the cut-short path keeps it.

    ``only_with_work`` is what a *failure* passes. A turn that died having
    written nothing has nothing to lose, and a bare marker would add a failure
    row to the conversation the model reads back -- which is precisely why
    errors are panel activity rather than history (PROJECT_DESIGN 2.5), and why
    a run whose model returned nothing must not have an answer invented for it.
    An interruption still records itself unconditionally: the user stopping the
    agent is context the model needs, not noise.
    """
    if agent_type not in ("orchestrator", "chat_orchestrator"):
        return
    store = globals().get("current_store")
    project = globals().get("current_project")
    if not store or not project:
        return
    visible = _turn_visible_text()
    if only_with_work and not visible:
        return
    content = f"{visible}\n\n{marker}" if visible else marker
    try:
        store.append_message(project, "assistant", content)
        store.save(project)
    except Exception:
        pass


def _record_interrupted_agent_turn(agent_type: str) -> None:
    """Persist the interruption as agent context, without using UI-facing prose."""
    _persist_unfinished_turn(agent_type, INTERRUPTED_AGENT_HISTORY_MARKER)


def _persist_activity_event(event: str, data: dict) -> None:
    """Persist panel diagnostic events outside chat message history."""
    if event not in _PERSISTED_ACTIVITY_EVENTS:
        return
    store = globals().get("current_store")
    project = globals().get("current_project")
    if not store or not project:
        return
    content = data.get("content", data.get("message", ""))
    if not str(content or "").strip():
        return
    try:
        store.append_activity(
            project,
            event,
            content=str(content),
            agent=str(data.get("agent") or ""),
            # "trace" is a developer-only stack trace: it stays on the live event
            # stream but is not persisted, so it can never resurface in a panel.
            payload={k: v for k, v in data.items() if k not in {"content", "trace"}},
        )
    except Exception:
        pass


def _provider_wait_payload(wait, agent: str) -> dict:
    """The `provider_wait` event for one `ResponseWait` record.

    Reported from a real session: a pooled connection to the provider died while
    a request was being sent, the turn sat on the 600 s HTTP timeout with only
    "Analyzing the obtained result..." on screen, and nothing told the user that
    stopping and continuing would have recovered it in seconds. The event only
    informs; the request, its timeout and its retries are unchanged.
    """
    return {
        "agent": agent,
        "model": wait.model,
        "phase": wait.phase,
        "elapsed_seconds": round(float(wait.elapsed_seconds), 1),
        "waiting": bool(wait.waiting),
    }


def print_event(event: str, data: dict):
    global _LAST_INTERMEDIATE_AGENT_RESPONSE
    if event == "agent_response" and data.get("intermediate"):
        response = str(data.get("response") or "").strip()
        if response:
            _LAST_INTERMEDIATE_AGENT_RESPONSE = response
            record_worker_message(response)
        event = "info"
        data = {
            "message": response,
            "agent": data.get("agent"),
        }

    already_recorded_thought = bool(data.pop("_thought_recorded", False))
    if event == "thought" and not already_recorded_thought:
        if not _record_turn_thought(data.get("content", "")):
            return
    if event == "thought" and _ACTIVE_THOUGHT_METER is not None:
        # Reasoning tokens of the turn so far, including this chunk: the chat
        # keeps the most recent reasoning up to the configured size by it.
        data["thought_tokens"] = _ACTIVE_THOUGHT_METER.total

    # One choke point for every visible chunk the turn streams, so a turn that
    # never reaches its own persistence still knows what it wrote.
    if event == "stream_chunk" and _ACTIVE_VISIBLE_CHUNKS is not None:
        _ACTIVE_VISIBLE_CHUNKS.append(str(data.get("content") or ""))

    payload = {"event": event, **data}
    _persist_activity_event(event, data)
    hook = event_hook
    if not hook:
        hook = getattr(litellm, "event_hook", None)
    # Write to stdout only in CLI/stdin mode (no server hook active)
    if not hook:
        try:
            if _real_stdout and not getattr(_real_stdout, 'closed', False):
                _real_stdout.write(json.dumps(payload) + "\n")
                _real_stdout.flush()
        except (ValueError, OSError, AttributeError):
            pass
    if hook:
        try:
            hook(payload)
            
            # Emit auxiliary thoughts to keep the Thinking tab active and alive
            thought_content = None
            if event == "agent_started":
                thought_content = f"Starting execution of agent '{data.get('agent', '')}' using model '{data.get('model', '')}'..."
            elif event == "tool_call":
                thought_content = _auxiliary_tool_call_thought(data.get("tool", ""), data.get("arguments", {}))
            elif event == "tool_result":
                if data.get("is_error"):
                    thought_content = f"Tool '{data.get('tool', '')}' returned an error. Analyzing the failure..."
                else:
                    thought_content = f"Received successful return from tool '{data.get('tool', '')}'. Analyzing the obtained result..."
            elif event == "error":
                thought_content = f"Alert: An error occurred during execution: {data.get('message', '')}"
                
            if thought_content and _record_turn_thought(thought_content):
                # Marked as auxiliary: this is narration of an event the
                # front-end already received, not something the model wrote. A
                # front-end that shows tool calls on their own (the terminal
                # renderer does) would otherwise print each of them twice.
                thought_payload = {
                    "content": thought_content,
                    "agent": data.get("agent"),
                    "auxiliary": True,
                }
                _persist_activity_event("thought", thought_payload)
                hook({"event": "thought", **thought_payload})
        except Exception as ex:
            _logging.getLogger(__name__).warning("Error invoking event hook: %s", ex)


def _attachment_alias(index: int, att: dict) -> str:
    name = att.get("name") or "attachment"
    _, ext = os.path.splitext(name)
    if not ext:
        mime = att.get("mime", "")
        if mime == "application/pdf":
            ext = ".pdf"
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            ext = ".docx"
        elif mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
            ext = ".pptx"
        elif mime.startswith("image/"):
            ext = "." + mime.split("/", 1)[1].replace("jpeg", "jpg")
    return f"input_file_{index}{ext or ''}"


def _apply_document_budget(
    attachments: list,
    free_chars: int,
    truncate_pct: int,
) -> list:
    """Fit every extracted document of a turn into ONE shared slice of context.

    The slice used to be recomputed for each attachment, so N documents each
    took `truncate_pct` of the free window and any N > 1 overflowed it. It is
    now computed once and divided between the documents, and whatever a short
    document leaves unused rolls over to the ones after it.

    A non-positive budget means the window is already full (or was never
    measured). The attachments are then returned untouched, exactly as before:
    the request is doomed either way and silently blanking the documents would
    hide the real problem.
    """
    doc_indexes = [
        index for index, att in enumerate(attachments)
        if att.get("type") == "pdf_text"
    ]
    if not doc_indexes:
        return list(attachments)

    remaining_chars = int(free_chars * truncate_pct / 100)
    if remaining_chars <= 0:
        return list(attachments)

    budgeted = list(attachments)
    remaining_docs = len(doc_indexes)
    for index in doc_indexes:
        att = budgeted[index]
        data = att.get("data", "") or ""
        share = remaining_chars // remaining_docs
        remaining_docs -= 1
        if len(data) <= share:
            remaining_chars -= len(data)
            continue
        shared_note = (
            f" shared across {len(doc_indexes)} documents"
            if len(doc_indexes) > 1 else ""
        )
        truncated = data[:share] + (
            f"\n\n[PDF truncated: {len(data):,} chars total, "
            f"{share:,} shown ({truncate_pct}% of free context{shared_note})]"
        )
        budgeted[index] = {**att, "data": truncated}
        remaining_chars -= share
    return budgeted


def _attachment_summary(att: dict, alias: str) -> str:
    name = att.get("name") or alias
    att_type = att.get("type", "")
    mime = att.get("mime", "")
    if att_type == "image":
        return f"- {alias}: image attachment '{name}' ({mime or 'image'})"
    if att_type == "pdf_text":
        chars = len(att.get("data", "") or "")
        label = "PDF" if mime == "application/pdf" else "document"
        return f"- {alias}: {label} attachment '{name}' ({chars} extracted text chars)"
    return f"- {alias}: attachment '{name}' ({mime or att_type or 'unknown'})"


def _recent_history_attachments(history: list, *, limit: int = 3) -> list[dict]:
    attachments: list[dict] = []
    for msg in reversed(history or []):
        if msg.get("role") != "user":
            continue
        for att in reversed(msg.get("_attachments") or []):
            if att.get("type") in ("image", "pdf_text"):
                attachments.append(att)
                if len(attachments) >= limit:
                    return list(reversed(attachments))
    return list(reversed(attachments))


def _deliver_inbox_message(item: "InboxItem", agent_type: str) -> None:
    """Finalize and record one out-of-band message as it enters the running turn.

    Called by the agent loop immediately before the message joins the history, so
    everything here describes a message the model is about to read:

    - the attachments go through the same vision gate, document budget and alias
      registration as the message that opened the turn, budgeted against the
      context measured so far in this turn rather than at submission time;
    - the message is written to `project_history` now, not when the user sent it,
      so the stored conversation keeps the order the model actually saw
      (PROJECT_DESIGN 2.6);
    - `user_message_delivered` always reports the delivery, while
      `user_message_saved` reports the stored identity only when there is one.
    """
    import opalatex.tools as tools_mod

    client_message_id = str(item.metadata.get("client_message_id") or "")
    display_text = str(item.metadata.get("display_text") or "").strip() or item.content
    raw_attachments = list(item.attachments or [])

    content, final_attachments, aliases = _prepare_turn_attachments(
        item.content,
        raw_attachments,
        alias_offset=tools_mod.recent_file_attachment_count(),
        history_fallback=False,
    )
    item.content = content
    item.attachments = final_attachments
    if aliases:
        tools_mod.add_recent_file_attachments(aliases)

    message_id = None
    if current_store is not None and current_project is not None:
        try:
            message_id = current_store.append_message(
                current_project,
                "user",
                display_text,
                attachments=raw_attachments,
                client_message_id=client_message_id,
            )
            current_store.save(current_project)
        except Exception as exc:
            # The model is about to read this message either way — the loop has
            # already taken it off the inbox. Report the divergence instead of
            # hiding it: the turn continues, but the next turn will not replay
            # this message, and the user has to know that now.
            message_id = None
            print_event("error", {
                "agent": agent_type,
                "message": (
                    "A message delivered to the running agent could not be stored "
                    f"in the chat history: {exc}"
                ),
                "trace": "",
            })

    print_event("user_message_delivered", {
        "agent": agent_type,
        "item_id": item.item_id,
        "client_message_id": client_message_id,
        "message_id": message_id,
    })
    if message_id is not None:
        print_event("user_message_saved", {
            "message_id": message_id,
            "client_message_id": client_message_id,
        })


def _open_turn_inbox(
    agent,
    agent_type: str,
    *,
    project_path: str,
    chat_id: str,
) -> "MessageInbox":
    """Attach a fresh inbox to the agent for the duration of this turn."""
    global _active_inbox, _active_inbox_scope

    inbox = MessageInbox()
    agent.inbox = inbox
    agent.on_message_delivery = lambda item: _deliver_inbox_message(item, agent_type)
    _active_inbox = inbox
    _active_inbox_scope = (project_path, chat_id)
    return inbox


def _close_turn_inbox(agent, inbox: "MessageInbox | None", agent_type: str) -> None:
    """Close the turn's inbox and report whatever was never delivered.

    The chat orchestrator block outlives the turn, so the inbox is detached here:
    leaving a closed one attached would make the next turn start with a channel
    that rejects everything.
    """
    global _active_inbox, _active_inbox_scope

    if inbox is None:
        return
    try:
        agent.inbox = None
        agent.on_message_delivery = None
    except Exception:
        pass
    if _active_inbox is inbox:
        _active_inbox = None
        _active_inbox_scope = ("", "")

    undelivered = inbox.close()
    if not undelivered:
        return
    # The turn ended before these reached the model — the loop breaks as soon as
    # the model produces its final response, and a message submitted during it
    # misses the last boundary. They are handed back to the front-end, which
    # resends them as an ordinary next turn, rather than being dropped.
    print_event("user_message_backlog", {
        "agent": agent_type,
        "items": [
            {
                "item_id": item.item_id,
                "client_message_id": str(item.metadata.get("client_message_id") or ""),
            }
            for item in undelivered
        ],
    })


def _prepare_turn_attachments(
    prompt: str,
    raw_attachments: list,
    *,
    alias_offset: int = 0,
    history_fallback: bool = True,
) -> tuple[str, list, dict]:
    """Apply the vision gate and the document budget to one message's attachments.

    Returns the prompt with the attachment context appended, the attachments the
    request will actually carry, and the alias -> descriptor map the file tools
    resolve. A message delivered mid-turn from the agent inbox goes through this
    same function, so a file attached to it is budgeted, gated and aliased
    exactly like a file attached to the message that opened the turn.

    ``alias_offset`` continues the alias numbering of a turn that already
    registered attachments, so a mid-turn message cannot claim an alias the model
    was already given. ``history_fallback`` is for the opening message only: a
    turn with no attachments of its own reuses the recent ones from the chat, but
    a mid-turn message must not re-attach what the turn already carries.
    """
    from opalatex.config import model_supports_vision as _model_supports_vision

    _model_name = (current_project.model if current_project else None) or ""
    _mp = getattr(current_project, "model_params", {}) or {}
    # LiteLLM's registry, or the user's `force_vision` for a local vision model
    # it does not list -- one rule, shared with the helper catalog.
    model_supports_vision = _model_supports_vision(_model_name, _mp)

    pdf_truncate_enabled = _mp.get("pdf_truncate", True)
    # The documents are fitted into the window only when it is known; an
    # unknown window leaves them whole for the provider to accept or refuse.
    pdf_truncate_pct = int(_mp.get("pdf_truncate_pct", 50))
    from opalatex.config import resolve_effective_num_ctx
    num_ctx = resolve_effective_num_ctx("memgpt", _model_name)

    # Budget the attachment against what the provider charged for the previous
    # request of this chat. project_history holds neither the system prompt, nor
    # the tool schemas, nor any tool result, so its JSON length understates the
    # occupied window by a wide margin and lets a truncated PDF still overflow
    # the context. The char/4 estimate remains only as the pre-measurement
    # fallback, on the very first request of a conversation.
    history_tokens = get_context_prompt_tokens()
    if history_tokens is None:
        _hist = getattr(current_project, "history", []) or []
        history_tokens = len(json.dumps(_hist)) // 4
    free_tokens = max(0, num_ctx - history_tokens) if num_ctx else None
    free_chars = free_tokens * 4 if free_tokens is not None else None  # back to chars

    history_attachments = []
    if history_fallback and not raw_attachments and current_project:
        history_attachments = _recent_history_attachments(getattr(current_project, "history", []) or [])

    attachments_for_turn = list(raw_attachments or history_attachments)
    if pdf_truncate_enabled and free_chars is not None:
        attachments_for_turn = _apply_document_budget(
            attachments_for_turn, free_chars, pdf_truncate_pct
        )
    final_attachments = []
    for att in attachments_for_turn:
        if att.get("type") == "image" and not model_supports_vision:
            prompt += (
                f"\n\n[Note: The user attached image '{att.get('name', 'image')}' "
                f"but the active model does not support vision. The image was not analysed.]"
            )
            continue
        final_attachments.append(att)

    attachment_aliases = {
        _attachment_alias(alias_offset + idx, att): att
        for idx, att in enumerate(final_attachments)
        if att.get("type") in ("image", "pdf_text")
    }

    if final_attachments:
        source = "current message" if raw_attachments else "recent chat history"
        summaries = [
            _attachment_summary(att, _attachment_alias(alias_offset + idx, att))
            for idx, att in enumerate(final_attachments)
        ]
        prompt += (
            f"\n\n[Attachment context from {source}. These files are attached to this LLM message. "
            f"Use the visual/document content directly when possible. If a tool asks for a file path, "
            f"use the listed alias exactly. For document aliases, read_file(alias) extracts the original attached file.]\n"
            + "\n".join(summaries)
        )

    return prompt, final_attachments, attachment_aliases

from opalatex.config import DEFAULT_MODEL, DEFAULT_DB_PATH, sanitize_model_params
from opalatex.project import ProjectStore, ProjectData
from opalatex.memgpt_runtime import build_chat_orchestrator
from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock

# Import all tools
from opalatex.tools import (
    read_file as raw_read_file_base,
    write_file,
    run_command,
    run_python_script,
    run_interactive_command,
    compile_latex,
    search_code,
    ask_question,
    ask_human,
    get_project_overview,
    write_content_pos,
    replace_content_range,
    edit_file,
    read_content_pos,
    set_project_context,
    web_search,
    analyze_image,
    generate_image,
    get_editor_state,
)

# Global map of available tools by name
ALL_TOOLS_MAP = {
    "read_file": raw_read_file_base,
    "write_file": write_file,
    "run_command": run_command,
    "run_python_script": run_python_script,
    "run_interactive_command": run_interactive_command,
    "compile_latex": compile_latex,
    "search_code": search_code,
    "ask_question": ask_question,
    "ask_human": ask_human,
    "get_project_overview": get_project_overview,
    "write_content_pos": write_content_pos,
    "replace_content_range": replace_content_range,
    "edit_file": edit_file,
    "read_content_pos": read_content_pos,
    "web_search": web_search,
    "analyze_image": analyze_image,
    "generate_image": generate_image,
    "get_editor_state": get_editor_state,
}

def stage_editor_state(project_path: str, data: dict) -> dict:
    """Persist what the IDE reported about its editor, for get_editor_state.

    The front-end sends the editor snapshot with every run; staging it to
    `<project>/.opalatex/_editor_state.json` is what lets a safe tool answer
    "what is the user looking at?" without the agent touching the GUI.

    Returns the staged dict (also on write failure, so callers see what was
    intended).
    """
    from opalatex.tools import editor_state_path

    state_file = editor_state_path(project_path)
    editor_state = {
        "current_file": data.get("current_file", ""),
        "editor_content": data.get("editor_content", ""),
        "selected_text": data.get("selected_text", ""),
        # The tab the buffer and selection belong to. open_file/close_file move
        # `current_file` during the turn; this one stays (tools.get_editor_state).
        "editor_content_file": data.get("current_file", ""),
    }
    # Not every caller tracks the tab list: the inline editor posts only the file
    # it is editing. An absent "open_files" means "this caller does not know",
    # not "no tabs are open", so the previously staged list is carried forward
    # instead of being replaced by an empty one that get_editor_state would then
    # report as fact.
    open_files = data.get("open_files")
    if open_files is None:
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                open_files = (json.load(f) or {}).get("open_files") or []
        except (OSError, ValueError):
            open_files = []
    editor_state["open_files"] = [str(p) for p in open_files if str(p).strip()]

    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(editor_state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: Failed to save editor state: {e}", file=sys.stderr)
    return editor_state


# State for persistent session
current_project = None
current_store = None
current_memgpt = None

def wrap_tool(original_tool):
    """Wrap a tool to output events on call and completion."""
    from pydantic import BaseModel
    from agenticblocks.core.function_block import FunctionBlock
    
    if getattr(original_tool, "_is_wrapped", False):
        return original_tool

    if not hasattr(original_tool, "run"):
        name = getattr(original_tool, "name", None) or getattr(original_tool, "__name__", None)
        desc = getattr(original_tool, "description", None) or getattr(original_tool, "__doc__", None)
        original_tool = FunctionBlock(original_tool, name=name, description=desc)
        
    name = getattr(original_tool, "name", None) or getattr(original_tool, "__name__", None)
    original_run = original_tool.run
    
    async def wrapped_run(*args, **kwargs) -> BaseModel:
        # Resolve the input_data (BaseModel) correctly
        if args and isinstance(args[0], BaseModel):
            input_data = args[0]
        elif "input" in kwargs and isinstance(kwargs["input"], BaseModel):
            input_data = kwargs["input"]
        else:
            # Fallback if args/kwargs don't match expected pattern
            raise TypeError("wrapped_run expects a Pydantic BaseModel as its first argument or 'input' keyword argument.")
            
        dump_kwargs = input_data.model_dump()
        print_event("tool_call", {"tool": name, "arguments": dump_kwargs})
        
        # Repeated-call loop breaking lives in the agent blocks themselves
        # (`loop_detection` / `loop_detection_limit`), so it covers workers too --
        # worker tools never pass through wrap_tool. The previous copy here used a
        # module-global window that was never reset between turns, so a turn could
        # start already "in a loop" because of the previous one.
        is_error = False
        res_val = "Execution cancelled or failed unexpectedly."
        try:
            result = await original_run(input_data)
            if hasattr(result, "result"):
                res_val = result.result
            else:
                res_val = result.model_dump()
        except Exception as e:
            is_error = True
            res_val = f"Error: {e}"
            print_event("problem", {"tool": name, "message": str(e), "severity": "error"})
            raise
        finally:
            print_event("tool_result", {"tool": name, "result": str(res_val), "is_error": is_error})
        return result
        
    object.__setattr__(original_tool, "run", wrapped_run)
    object.__setattr__(original_tool, "_is_wrapped", True)
    return original_tool



from opalatex.tools import get_helper_tools

# Monkey-patch get_helper_tools to automatically wrap the tools handed to helpers
original_get_helper_tools = get_helper_tools

def patched_get_helper_tools(vision: bool = False):
    tools = original_get_helper_tools(vision=vision)
    return [wrap_tool(t) for t in tools]

import opalatex.tools
import opalatex.memgpt_runtime
opalatex.tools.get_helper_tools = patched_get_helper_tools
opalatex.memgpt_runtime.get_helper_tools = patched_get_helper_tools

async def handle_load_project(data: dict):
    global current_project, current_store, current_memgpt
    project_name = data.get("project_name") or "stdin_project"
    project_path = data.get("project_path") or os.getcwd()
    db_path = data.get("db") or DEFAULT_DB_PATH
    
    current_store = ProjectStore(db_path=db_path)
    # No chat id means "the project's default chat" (the most recent one, or
    # none when the project has no chat); a literal sentinel here would
    # silently resolve to whatever chat happened to be stored last.
    chat_id = data.get("chat_id") or None
    if current_store.exists(project_name):
        current_project = current_store.load(project_name, chat_id=chat_id)
        if data.get("project_path"):
            current_project.project_path = os.path.abspath(project_path)
    else:
        current_project = ProjectData(
            name=project_name,
            project_name=project_name,
            project_path=os.path.abspath(project_path),
        )

    if data.get("model"):
        current_project.model = data["model"]
    if "worker_model" in data:
        current_project.worker_model = data.get("worker_model") or ""
    if "api_key" in data:
        current_project.api_key = data.get("api_key") or ""
    if "api_base" in data:
        current_project.api_base = data.get("api_base") or ""
    if "worker_api_key" in data:
        current_project.worker_api_key = data.get("worker_api_key") or ""
    if "worker_api_base" in data:
        current_project.worker_api_base = data.get("worker_api_base") or ""
    # Sanitized here too, not only on the ide_server settings route: these IPC
    # writes are the second door into the same stored dict, and an unguarded one
    # is how dead keys accumulated there in the first place.
    if isinstance(data.get("model_params"), dict):
        current_project.model_params = sanitize_model_params(data["model_params"])
    if isinstance(data.get("worker_model_params"), dict):
        current_project.worker_model_params = sanitize_model_params(data["worker_model_params"])
    
    # Initialize workspace context
    set_project_context(current_project, current_store)
    
    # Rebuild orchestrator
    current_memgpt = build_chat_orchestrator(current_project, current_store)
    
    print_event("project_loaded", {
        "project_name": current_project.project_name,
        "project_path": current_project.project_path,
        "skills": current_project.skills,
        "model": current_project.model,
        "worker_model": current_project.worker_model,
    })

async def handle_slash_command(data: dict) -> dict:
    """Execute a slash command and return a plain JSON result (no streaming).

    Returns:
        {"status": "done", "messages": [...]}  when the command completes.
        {"status": "confirm", "id": ..., "prompt": ..., "options": [...], "default": ...}
            when the command needs user confirmation before continuing.
    """
    import re
    import uuid

    prompt = data.get("prompt", "")
    cmd, *args = prompt.split(maxsplit=1)

    # Load project state needed by commands
    global current_project, current_store, current_memgpt
    if "project_name" in data or "project_path" in data:
        await handle_load_project(data)

    from opalatex.cli_commands import REPLState, _registry
    import opalatex.terminal as T

    if cmd not in _registry:
        return {"status": "done", "messages": [f"🔴 Comando desconhecido: {cmd}. Digite /help para ajuda."]}

    # A terminal-only command reaching the GUI would run against a REPLState with
    # no renderer and report success for something that did not happen.
    if _registry.is_cli_only(cmd):
        from opalatex.i18n import _ as _translate
        return {"status": "done", "messages": [f"🔴 {_translate('cli_command_cli_only', cmd=cmd)}"]}

    messages = []

    # ---- Capture terminal output helpers ----
    orig_success         = T.success
    orig_error           = T.error
    orig_info            = T.info
    orig_warning         = T.warning
    orig_confirm         = T.confirm
    orig_ask             = T.ask
    orig_async_confirm_hook = T._async_confirm_hook
    orig_console_print   = T.console.print

    def _capture(*a, prefix=""):
        messages.append(f"{prefix}{' '.join(str(x) for x in a)}")

    T.success = lambda *a, **k: _capture(*a, prefix="🟢 ")
    T.error   = lambda *a, **k: _capture(*a, prefix="🔴 ")
    T.info    = lambda *a, **k: _capture(*a, prefix="ℹ️ ")
    T.warning = lambda *a, **k: _capture(*a, prefix="⚠️ ")
    T.confirm = lambda p, default=True: default   # sync fallback never used in GUI
    T.ask     = lambda p: ""

    def _render_rich(obj) -> str:
        from rich.console import Console as _Console
        from rich.table import Table as _Table
        from io import StringIO
        if isinstance(obj, _Table):
            buf = StringIO()
            c = _Console(file=buf, highlight=False, no_color=True)
            c.print(obj)
            return buf.getvalue()
        return str(obj)

    # A run of literal lines is fenced, so the chat renders it preformatted
    # instead of collapsing the alignment that makes a usage block readable.
    literal_run = {"open": False}

    def _close_literal_run():
        if literal_run["open"]:
            messages.append("```")
            literal_run["open"] = False

    def _console_print(*args_c, **kwargs_c):
        if not args_c:
            messages.append("")
            return
        raw = " ".join(_render_rich(x) for x in args_c)
        # `markup=False` is the caller saying this text is literal. The
        # heuristics below re-read console output as command listings, which
        # turned `/help`'s worked examples into bullets and ate the argument
        # shapes they exist to show.
        if kwargs_c.get("markup") is False:
            if not literal_run["open"]:
                messages.append("```")
                literal_run["open"] = True
            messages.extend(raw.split("\n"))
            return
        _close_literal_run()
        for line in raw.split("\n"):
            stripped = line.strip()
            if not stripped:
                messages.append("")
                continue
            if "Comandos Disponíveis" in stripped or "Available commands" in stripped:
                messages.append("### 🛠️ Comandos Disponíveis\n"); continue
            if "Active skills for this project" in stripped:
                messages.append("### 🧠 Active skills for this project\n"); continue
            if "Available skills" in stripped:
                messages.append("### 📚 Available skills\n"); continue
            clean = re.sub(r'\[/?[\w\s]+\]', '', stripped)
            if clean and clean == clean.upper() and clean.replace(" ", "").isalpha():
                messages.append(f"### {clean}\n"); continue
            m = re.match(r'\s*(?:\*\s*)?\[(green|cyan)\]\s*(.*?)\s*\[/\1\]\s*(.*)', line)
            if m:
                name = m.group(2).strip()
                desc = re.sub(r'\[/?\w+\]', '', m.group(3).strip())
                star = "⭐ " if "*" in line.split("[cyan]")[0] and "[cyan]" in line else "🔹 "
                messages.append(f"{star}**`{name}`** — {desc}"); continue
            cm = re.match(r'^\s*(/[a-zA-Z0-9_<> \-\[\]]+?)\s{2,}(.*)$', line)
            if cm:
                messages.append(f"🔹 **`{cm.group(1).strip()}`** — {cm.group(2).strip()}"); continue
            has_star = bool(re.match(r'^\s*\*\s*', line))
            sm = re.match(r'^\s*(?:\*\s*)?([a-zA-Z0-9_\-]+)\s{2,}(.*)$', line)
            if sm:
                star = "⭐ " if has_star else "🔹 "
                messages.append(f"{star}**`{sm.group(1).strip()}`** — {sm.group(2).strip()}"); continue
            messages.append(re.sub(r'\[/?[\w\s]+\]', '', line))

    T.console.print = _console_print

    # Future that gets resolved either when command finishes OR when it needs confirm
    loop = asyncio.get_event_loop()
    confirm_future: asyncio.Future = loop.create_future()
    confirm_info: dict = {}

    async def _gui_confirm_hook(prompt_text: str, default: bool = True) -> bool:
        req_id = str(uuid.uuid4())
        fut = loop.create_future()
        _gui_input_pending[req_id] = fut
        confirm_info.update({"id": req_id, "prompt": prompt_text,
                             "options": ["yes", "no"],
                             "default": "yes" if default else "no"})
        if not confirm_future.done():
            confirm_future.set_result({"needs_confirm": True})
        try:
            raw = await asyncio.wait_for(asyncio.shield(fut), timeout=120.0)
            return raw.strip().lower() in ("yes", "y", "s", "sim", "true", "1")
        except asyncio.TimeoutError:
            return default
        finally:
            _gui_input_pending.pop(req_id, None)

    T._async_confirm_hook = _gui_confirm_hook

    def _restore():
        T.success            = orig_success
        T.error              = orig_error
        T.info               = orig_info
        T.warning            = orig_warning
        T.confirm            = orig_confirm
        T.ask                = orig_ask
        T._async_confirm_hook = orig_async_confirm_hook
        T.console.print      = orig_console_print

    state    = REPLState(current_project, current_store)
    cmd_args = args[0].split() if args else []

    async def _run_cmd():
        try:
            await _registry.dispatch(state, cmd, cmd_args)
        except Exception as e:
            messages.append(f"🔴 Erro ao executar comando: {e}")
        finally:
            _restore()
            if not confirm_future.done():
                confirm_future.set_result({"needs_confirm": False})

    cmd_task = asyncio.create_task(_run_cmd())

    # Wait until command finishes OR pauses for confirmation
    signal = await confirm_future

    if signal.get("needs_confirm"):
        # Store the running task so /slash-command/continue can resume it
        _pending_slash_tasks[confirm_info["id"]] = (cmd_task, messages)
        return {"status": "confirm", **confirm_info}

    await cmd_task
    _close_literal_run()
    response_text = "\n".join(m for m in messages if m is not None) or f"Comando {cmd} concluído."
    # A command may have erased the context the panel indicator was describing
    # (/clear, /clear_chat). Return the occupancy that is true now — null once it
    # was reset — so the indicator does not keep reporting a deleted conversation.
    return {
        "status": "done",
        "messages": [response_text],
        "context_usage": _current_context_usage(data.get("chat_id")),
    }


def _current_context_usage(chat_id: str | None) -> dict | None:
    """Return the measured occupancy for the chat a request refers to."""
    from opalatex.token_usage import get_context_usage

    return get_context_usage(context_scope_key(
        getattr(current_project, "project_path", "") or "",
        chat_id or "",
    ))


def _persist_context_usage(chat_id: str | None) -> None:
    """Store this turn's measured occupancy next to the chat it describes.

    The in-process measurement is a single slot bound to one project/chat scope,
    so the next run of any other conversation drops it — and the server process
    keeps nothing across a restart. Without a persisted copy, coming back to a
    chat that had filled most of its window answered ``context_usage: null`` and
    the panel fell back to the character estimate over the visible bubbles,
    reporting a nearly empty context for a conversation the orchestrator restores
    in full from ``agent_state``.

    Only a real measurement is written. A turn that failed before any LLM call
    has nothing to say about the window, and erasing the previous number would
    replace a true value with a guess.
    """
    if current_store is None or current_project is None:
        return
    target_chat = str(chat_id or getattr(current_project, "current_chat_id", "") or "")
    if not target_chat:
        return
    usage = _current_context_usage(chat_id)
    if not usage:
        return
    try:
        current_store.save_chat_context_usage(
            current_project.name, target_chat, usage,
        )
    except Exception:
        pass


# Pending slash-command tasks awaiting user confirmation
_pending_slash_tasks: dict = {}


async def handle_slash_command_continue(data: dict) -> dict:
    """Resume a slash command after the user responded to a confirm dialog."""
    req_id = data.get("id", "")
    value  = data.get("value", "")

    fut = _gui_input_pending.get(req_id)
    if not fut or fut.done():
        return {"status": "error", "message": "No pending request with that id"}

    loop = asyncio.get_event_loop()
    loop.call_soon_threadsafe(fut.set_result, value)

    entry = _pending_slash_tasks.pop(req_id, None)
    if entry:
        cmd_task, messages = entry
        try:
            await cmd_task
        except Exception:
            pass
        response_text = "\n".join(m for m in messages if m is not None) or "Comando executado com sucesso."
        return {"status": "done", "messages": [response_text]}

    return {"status": "done", "messages": []}


def _restore_transient_project_mode(initial_project_mode: str | None, agent_type: str) -> None:
    """Restore mode changes that are allowed only during the active agent turn."""
    if not current_project or not initial_project_mode:
        return
    if agent_type not in ("orchestrator", "chat_orchestrator"):
        return
    if current_project.mode == initial_project_mode:
        return

    previous_mode = current_project.mode
    current_project.mode = initial_project_mode
    if current_store:
        current_store.append_activity(
            current_project,
            "mode",
            content=(
                f"[MODE] Transient mode change ('{previous_mode}') restored to "
                f"'{initial_project_mode}' at end of turn."
            ),
            agent=agent_type,
        )
        current_store.save(current_project)


def _resolve_context_window(agent: object, *model_param_sources: dict) -> int:
    """Return the window size the measured token usage is reported against.

    The agent's own ``max_context_tokens`` wins because MemGPT already resolved
    it from ``num_ctx`` and evicts against that exact number; the raw project
    parameters are only consulted for agents that do not carry one. 0 means the
    window is unknown and is reported as such, never as a guessed size.
    """
    candidates = [
        getattr(agent, "max_context_tokens", None),
        (getattr(agent, "model_kwargs", None) or {}).get("num_ctx"),
    ]
    for source in model_param_sources:
        candidates.append((source or {}).get("num_ctx"))

    for value in candidates:
        try:
            window = int(value)
        except (TypeError, ValueError):
            continue
        if window > 0:
            return window
    return 0


async def _run_off_loop(fn, *args, **kwargs):
    """Run a blocking call in a worker thread, keeping its cancellation timing.

    Saving a chat message also embeds it into archival memory (ChromaDB): the
    first message of a session builds the client, and on a fresh machine that
    downloads the embedding model, all of which used to stall the event loop
    and therefore the whole window. In a worker thread the loop stays free.

    Run inline, the call could not be interrupted: a cancel requested meanwhile
    was delivered at the turn's *next* await, after the code following the call
    had run. This keeps that: the call always completes, and a cancel that
    arrived during it is re-armed, so it still lands at the next await rather
    than here.
    """
    future = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = False
    try:
        while True:
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                if future.cancelled():
                    raise
                cancelled = True
                if future.done():
                    return future.result()
    finally:
        if cancelled:
            task = asyncio.current_task()
            if task is not None:
                # Catching CancelledError leaves the task's cancel count
                # raised; undo that once so re-arming does not count twice.
                uncancel = getattr(task, "uncancel", None)
                if uncancel is not None:
                    uncancel()
                task.cancel()


_TURN_LEASE_AGENTS = ("orchestrator", "chat_orchestrator")


def _take_turn_lease(data: dict):
    """Claim the chat for this process for the length of an orchestrator turn.

    The window, the CLI and the monitor daemon's wake-up turns are separate
    processes sharing one ``sessions.db``, and an orchestrator's working state is
    saved per chat; two turns of the same chat at once would each overwrite the
    other's. Returns ``(project, chat_id)`` when held, ``None`` when this turn
    takes no lease, and ``False`` when another live process holds the chat.
    """
    if (data.get("agent") or "chat_orchestrator") not in _TURN_LEASE_AGENTS:
        return None
    project = str(data.get("project_name") or "").strip()
    chat_id = str(data.get("chat_id") or "").strip()
    if not project or not chat_id:
        return None
    try:
        from opalatex.monitors.store import MonitorStore

        holder = MonitorStore().acquire_lease(project, chat_id, os.getpid(), "turn")
    except Exception as exc:
        # The lease guards against a background turn; being unable to take it
        # must not stop the user's own turn.
        _logging.getLogger(__name__).warning("Could not take the chat's turn lease: %s", exc)
        return None
    if holder is not None:
        from opalatex.i18n import _ as _translate

        print_event("error", {
            "agent": data.get("agent") or "chat_orchestrator",
            "message": _translate("chat_busy_in_background", holder=holder.get("holder", "")),
        })
        print_event("agent_finished", {})
        return False
    return (project, chat_id)


def _release_turn_lease(lease) -> None:
    if not lease:
        return
    try:
        from opalatex.monitors.store import MonitorStore

        MonitorStore().release_lease(lease[0], lease[1], os.getpid())
    except Exception:
        pass


async def handle_run(data: dict):
    """Run one agent turn, holding the chat's cross-process lease while it runs.

    ``monitor_wake`` (the monitor's name) and ``monitor_wake_id`` mark a turn a
    monitor started: it runs unattended (`unattended_turn`), whichever front-end
    runs it.
    """
    global _unattended_monitor, _unattended_monitor_id
    lease = _take_turn_lease(data)
    if lease is False:
        return
    monitor = str(data.get("monitor_wake") or "").strip()
    monitor_id = str(data.get("monitor_wake_id") or "").strip()
    _unattended_monitor = monitor or None
    _unattended_monitor_id = monitor_id or None
    try:
        return await _handle_run(data)
    finally:
        _unattended_monitor = None
        _unattended_monitor_id = None
        _release_turn_lease(lease)


async def _handle_run(data: dict):
    global current_project, current_store, current_memgpt

    import opalatex.tools
    opalatex.tools._DENIED_TOOLS.clear()
    
    agent_type = data.get("agent") or "chat_orchestrator"
    model = data.get("model")
    system_prompt = data.get("system_prompt")
    raw_prompt = data.get("prompt", "")
    prompt, _meta_overrides = parse_meta_params(raw_prompt)
    display_prompt = str(data.get("display_prompt") or "").strip()
    messages_history = data.get("messages", [])
    requested_tools = data.get("tools")
    raw_attachments = data.get("attachments", [])  # [{type, data, mime, name}]
    client_message_id = str(data.get("client_message_id") or "").strip()
    
    # Setup project context if provided
    if "project_path" in data or "project_name" in data:
        await handle_load_project(data)

    # Measured context occupancy belongs to one conversation. Rebinding the
    # scope drops a measurement taken for a different project or chat instead of
    # reporting it against the history now loaded.
    if agent_type in TOKEN_CONTEXT_AGENTS:
        set_context_scope(context_scope_key(
            getattr(current_project, "project_path", "") or "",
            data.get("chat_id") or "",
        ))

    # A project starts with no model configured. Fail fast with a clear diagnostic
    # instead of silently substituting DEFAULT_MODEL for the user's (absent) choice.
    if (
        agent_type in ("orchestrator", "chat_orchestrator")
        and not model
        and current_project is not None
        and not str(getattr(current_project, "model", "") or "").strip()
    ):
        from opalatex.i18n import _ as _translate
        print_event("error", {"agent": agent_type, "message": _translate("no_model_configured")})
        print_event("agent_finished", {"agent": agent_type})
        return

    # A project may have no chat. The front-ends create one before sending the
    # first message; a turn reaching here without one has nowhere to store its
    # conversation, so it fails here rather than on its first write.
    if (
        agent_type in ("orchestrator", "chat_orchestrator")
        and current_project is not None
        and not getattr(current_project, "current_chat_id", "")
    ):
        from opalatex.i18n import _ as _translate
        print_event("error", {"agent": agent_type, "message": _translate("no_chat_open")})
        print_event("agent_finished", {"agent": agent_type})
        return

    # "Continue" on an interrupted turn: the prompt is built here from what the
    # store holds for that turn, so it survives a reloaded window or a restarted
    # app. Must run before this turn's own messages are appended.
    if (
        data.get("resume_interrupted")
        and agent_type in ("orchestrator", "chat_orchestrator")
        and current_store is not None
        and current_project is not None
    ):
        from opalatex.thought_context import ThoughtSummaryError, resume_prompt_for_chat

        def _announce_summary(tokens: int) -> None:
            print_event("info", {
                "agent": agent_type,
                "message": f"Summarizing {tokens} tokens of reasoning to resume the interrupted turn...",
            })

        try:
            prompt = await resume_prompt_for_chat(
                current_store,
                current_project,
                model=str(model or getattr(current_project, "model", "") or ""),
                on_summarizing=_announce_summary,
            )
        except ThoughtSummaryError as exc:
            print_event("error", {"agent": agent_type, "message": str(exc)})
            print_event("agent_finished", {"agent": agent_type})
            return
        except Exception as exc:
            print_event("error", {
                "agent": agent_type,
                "message": f"Could not prepare the resumed turn: {_friendly_llm_error(exc, current_project)}",
            })
            print_event("agent_finished", {"agent": agent_type})
            return

    initial_project_mode = None
    if current_project:
        initial_project_mode = current_project.mode
        # Record the mode at the start of this turn as diagnostic activity. It is an
        # audit entry, not part of the conversation: keeping it out of
        # project_history stops it from consuming the model's context window and
        # from injecting a mid-history system message (PROJECT_DESIGN 2.5/2.7).
        if current_store and agent_type in ("orchestrator", "chat_orchestrator"):
            current_store.append_activity(
                current_project,
                "mode",
                content=f"[MODE] Agent turn started. Current mode: '{initial_project_mode}'.",
                agent=agent_type,
            )

    if current_project and current_project.project_path:
        stage_editor_state(current_project.project_path, data)
    

    # Build agent
    agent = None
    if agent_type == "chat_orchestrator":
        if not current_memgpt:
            # Autoload default project
            await handle_load_project(data)
        agent = current_memgpt
        if system_prompt:
            agent.system_prompt = system_prompt

        # Apply heartbeat override for this run (e.g. low=20, medium=50, high=100, custom)
        req_hb = data.get("max_heartbeats")
        if req_hb is None and isinstance(data.get("model_params"), dict):
            req_hb = data["model_params"].get("max_heartbeats")
        if req_hb is not None:
            try:
                hb_val = int(req_hb)
                if hb_val > 0:
                    agent.max_heartbeats = hb_val
            except (ValueError, TypeError):
                pass
        else:
            from opalatex.config import get_project_agent_params, get_agent_max_heartbeats
            _agent_params = get_project_agent_params()
            agent.max_heartbeats = int(_agent_params.get("max_heartbeats", get_agent_max_heartbeats("memgpt", 50)))
    else:
        # Custom LLMAgentBlock
        if not system_prompt:
            print_event("error", {"message": "system_prompt is required for custom agent"})
            return
            
        # >> LOAD CONTEXTUAL SKILL START <<
        if current_project and current_project.project_path:
            current_file = data.get("current_file", "")
            ext = ""
            if current_file:
                _, ext = os.path.splitext(current_file)
                ext = ext.lstrip('.').lower()
            
            contextual_dir = os.path.join(current_project.project_path, ".opalatex", "contextual")
            
            target_skill = os.path.join(contextual_dir, f"contextual_skill_{ext}.skill")
            generic_skill = os.path.join(contextual_dir, "contextual_skill_.skill")
            
            if not os.path.exists(target_skill) or not ext:
                target_skill = generic_skill
                
            try:
                if os.path.exists(target_skill):
                    with open(target_skill, "r", encoding="utf-8") as f:
                        skill_content = f.read().strip()
                    if skill_content:
                        system_prompt += "\n\n# Contextual Instructions:\n" + skill_content
            except Exception as e:
                print(f"Warning: Failed to load contextual skill: {e}", file=sys.stderr)
        # >> LOAD CONTEXTUAL SKILL END <<
        if agent_type == "inline_editor":
            system_prompt += (
                "\n\n# Inline Output Contract\n"
                "The response format specified by the original system prompt is authoritative. "
                "Contextual instructions guide only the generated source content and must not "
                "change the required response wrapper or add analysis."
            )

        
        # Resolve tools
        tools_list = []
        if requested_tools:
            if requested_tools == "all":
                tools_list = [wrap_tool(t) for t in ALL_TOOLS_MAP.values()]
            else:
                for tname in requested_tools:
                    if tname in ALL_TOOLS_MAP:
                        tools_list.append(wrap_tool(ALL_TOOLS_MAP[tname]))
                    else:
                        print_event("error", {"message": f"Tool '{tname}' not found"})
        
        model_params = data.get("model_params") or {}
        model_kwargs = {}
        if model_params.get("max_tokens") is not None:
            model_kwargs["max_tokens"] = int(model_params["max_tokens"])
        if model_params.get("num_ctx") is not None:
            model_kwargs["num_ctx"] = int(model_params["num_ctx"])
        if model_params.get("temperature") is not None:
            model_kwargs["temperature"] = float(model_params["temperature"])
        if model_params.get("reasoning_effort"):
            model_kwargs["reasoning_effort"] = model_params["reasoning_effort"]
        
        # `think` is deliberately absent here. It is resolved from the selected
        # model's catalog entry by `get_agent_llm_kwargs` below
        # (config.resolve_think_request), which is also where a configured
        # reasoning effort becomes the level sent to the provider. Hardcoding
        # `True` at this layer overrode that resolution -- these kwargs are
        # merged *over* the resolved ones -- so every custom agent asked for
        # full thinking no matter what the catalog said. The resolved value
        # still gates the Thinking panel (`publish_reasoning`), and
        # `_apply_model_thinking_capability` still drops it for a model that
        # does not declare support.
        # Inline editing applies a single final replacement. It must never expose
        # partial model output, regardless of a global streaming default.
        model_kwargs["stream"] = False if agent_type == "inline_editor" else bool(model_params.get("stream", True))

        agent_kwargs = {}
        if model_params.get("max_iterations") is not None:
            agent_kwargs["max_iterations"] = int(model_params["max_iterations"])
        if model_params.get("max_tool_calls") is not None:
            agent_kwargs["max_tool_calls"] = int(model_params["max_tool_calls"])

        from opalatex.config import (
            resolve_model_route,
            get_agent_model,
            get_agent_llm_kwargs,
            normalize_ollama_api_base_for_litellm,
            sanitize_litellm_kwargs_for_model,
        )
        
        _model = model or DEFAULT_MODEL
        _agent_name = agent_type or "custom_agent"
        _model = get_agent_model(_agent_name, _model)
        
        # Merge global kwargs so we get the cloud API base and keys
        # The model that will actually run is `_model`, so its own catalog entry
        # is the one whose parameters (and credentials) must be resolved -- the
        # role's default model may be a different entry entirely.
        _global_kwargs = get_agent_llm_kwargs(_agent_name, model_override=_model)
        _global_kwargs.update(model_kwargs)
        _global_kwargs = _apply_model_thinking_capability(_model, _global_kwargs)
        _model = resolve_model_route(_model, _global_kwargs)
        model_kwargs = sanitize_litellm_kwargs_for_model(_model, _global_kwargs)
        if model_kwargs.get("api_base"):
            model_kwargs["api_base"] = normalize_ollama_api_base_for_litellm(
                _model,
                model_kwargs["api_base"],
            )

        if _agent_name == "worker":
            agent_kwargs["use_shared_router"] = False
        agent = LLMAgentBlock(
            name=agent_type or "custom_agent",
            system_prompt=system_prompt,
            model=_model,
            tools=tools_list,
            model_kwargs=model_kwargs,
            **agent_kwargs
        )
    from opalatex.litellm_compat import wrap_agent_litellm_compat
    agent.user_prompt_prefix = getattr(current_project, "user_prompt_prefix", "")
    wrap_agent_litellm_compat(agent)
    attach_usage_tracking(agent)

    # Whether the model's reasoning is published at all (thought events, Thinking
    # panel). Read off the resolved wire flag rather than any user setting: the
    # provider is asked to isolate the reasoning channel exactly when the model's
    # catalog entry declares `supports_thinking` (config.resolve_think_request), so
    # the reasoning that exists to be shown and the decision to show it come from
    # one place. A model without the capability has no isolated channel at all --
    # its inline `<think>` tags are still handled by the splitter below.
    publish_reasoning = bool((getattr(agent, "model_kargs", None) or {}).get("think"))

    # Setup message history if provided, for an agent whose history is not owned
    # by the store. The chat orchestrator's is: `build_chat_orchestrator` restored
    # its working memory for this chat -- tool calls and results included -- or
    # seeded it from persisted history. Overwriting that with client-sent text is
    # what made every "Continue" start from truncated bubbles and redo the work
    # the stopped turn had already done (PROJECT_DESIGN 2.6).
    if (
        messages_history
        and agent_type not in ("orchestrator", "chat_orchestrator")
        and hasattr(agent, "internal_history")
    ):
        agent.internal_history.clear()
        for msg in messages_history:
            agent.internal_history.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })
            
    global _ACTIVE_THOUGHT_CHUNKS, _ACTIVE_THOUGHT_METER, _ACTIVE_VISIBLE_CHUNKS
    from opalatex.thought_context import ThoughtTokenMeter
    thought_chunks = []
    _ACTIVE_THOUGHT_CHUNKS = thought_chunks
    # Opened here rather than at the first chunk so a turn that fails before
    # streaming anything still has an empty list instead of the previous turn's.
    _ACTIVE_VISIBLE_CHUNKS = []
    _ACTIVE_THOUGHT_METER = ThoughtTokenMeter(getattr(agent, "model", "") or model or "")
    stream_probe_pending = [""]
    stream_probe_decided = [False]
    suppress_plain_tool_json_stream = [False]

    import time as _time
    _chunk_timing_state = {"last": None, "n": 0}

    def _log_chunk_timing(label: str, chunk: str) -> None:
        now = _time.monotonic()
        last = _chunk_timing_state["last"]
        gap_ms = (now - last) * 1000 if last is not None else 0.0
        _chunk_timing_state["last"] = now
        _chunk_timing_state["n"] += 1
        #print(
        #    f"[STREAM-TIMING] #{_chunk_timing_state['n']:04d} {label} "
        #    f"gap={gap_ms:8.1f}ms len={len(chunk):4d} preview={chunk[:40]!r}",
        #    flush=True,
        #)

    def _should_probe_plain_tool_json_stream() -> bool:
        return agent_type in ("orchestrator", "chat_orchestrator")

    def _is_possible_json_fence_prefix(text: str) -> bool:
        stripped = text.lstrip().lower()
        if not stripped:
            return True
        return "```json".startswith(stripped) or "```".startswith(stripped)

    def _looks_like_plain_tool_json_stream(text: str) -> bool:
        stripped = text.lstrip().lower()
        return stripped.startswith("{") or stripped.startswith("```json")

    def _on_thinking(chunk: str) -> None:
        _log_chunk_timing("thinking", chunk)
        # A model whose catalog entry does not declare thinking support has no
        # isolated reasoning channel to publish. The callback stays wired either
        # way, so an agent can always call it.
        if not publish_reasoning:
            return
        publish_thought(chunk, agent_type)

    _think_splitter = inline_reasoning_splitter(agent_type)

    def _process_visible_chunk(chunk: str) -> None:
        _think_splitter.feed(chunk)

    def _on_chunk(chunk: str) -> None:
        _log_chunk_timing("content", chunk)
        if not _should_probe_plain_tool_json_stream():
            _process_visible_chunk(chunk)
            return

        if suppress_plain_tool_json_stream[0]:
            return

        if not stream_probe_decided[0]:
            stream_probe_pending[0] += str(chunk or "")
            if _looks_like_plain_tool_json_stream(stream_probe_pending[0]):
                suppress_plain_tool_json_stream[0] = True
                stream_probe_pending[0] = ""
                return
            if _is_possible_json_fence_prefix(stream_probe_pending[0]):
                return
            stream_probe_decided[0] = True
            pending = stream_probe_pending[0]
            stream_probe_pending[0] = ""
            if pending:
                _process_visible_chunk(pending)
            return

        _process_visible_chunk(chunk)

    def _on_iteration(_step: int, messages: list) -> None:
        # Bound the retraction window to this response: an orphan </think> in the
        # answer that follows must never take back text streamed by an earlier
        # heartbeat.
        _think_splitter.begin_response()

        # Fires just before each LLM call, so the request assembled here already
        # carries the tool results produced since the previous call. Counting it
        # now is what makes the context indicator move while the agent is still
        # working, instead of only when the provider reports usage afterwards.
        if agent_type in TOKEN_CONTEXT_AGENTS:
            try:
                assembled = count_message_tokens(agent.model, messages)
                record_context_tokens(assembled)
                print_event("token_usage", {
                    "agent": agent_type,
                    "context_window": _context_window,
                    "prompt_tokens": assembled,
                    "source": "local",
                })
            except Exception:
                pass

        last = messages[-1] if messages else {}
        content = last.get("content") or ""
        # A bare arguments object is a tool call written as text too, and the
        # Reflection panel is for prose, so it is filtered like the other shapes.
        if _should_emit_iteration_reflection(last) and not _has_unfenced_tool_call_payload(
            str(content), _agent_tool_schemas(agent)
        ):
            print_event("reflection", {"content": str(content), "agent": agent_type})

        raw_max_steps = (
            getattr(agent, "max_heartbeats", None)
            or getattr(getattr(agent, "agent", None), "max_heartbeats", None)
            or getattr(agent, "max_iterations", None)
            or getattr(getattr(agent, "agent", None), "max_iterations", None)
        )
        try:
            max_steps = int(raw_max_steps) if raw_max_steps is not None else None
        except (ValueError, TypeError):
            max_steps = None

        print_event("agent_step", {
            "step": _step,
            "max_steps": max_steps,
            "agent": agent_type,
        })

    def _on_unknown_tool(name: str, _arguments: str) -> None:
        _report_unknown_tool_call(name)

    def _on_response_wait(wait) -> None:
        print_event("provider_wait", _provider_wait_payload(wait, agent_type))

    # Inline editing has a final-response-only transport contract. Do not bind
    # callbacks that could emit intermediate model content.
    if agent_type != "inline_editor":
        if hasattr(agent, "on_thinking"):
            agent.on_thinking = _on_thinking
        elif hasattr(agent, "agent") and hasattr(agent.agent, "on_thinking"):
            agent.agent.on_thinking = _on_thinking

        if hasattr(agent, "on_chunk"):
            agent.on_chunk = _on_chunk
        elif hasattr(agent, "agent") and hasattr(agent.agent, "on_chunk"):
            agent.agent.on_chunk = _on_chunk

        if hasattr(agent, "on_iteration"):
            agent.on_iteration = _on_iteration
        elif hasattr(agent, "agent") and hasattr(agent.agent, "on_iteration"):
            agent.agent.on_iteration = _on_iteration

        if hasattr(agent, "on_unknown_tool"):
            agent.on_unknown_tool = _on_unknown_tool
        elif hasattr(agent, "agent") and hasattr(agent.agent, "on_unknown_tool"):
            agent.agent.on_unknown_tool = _on_unknown_tool

        if hasattr(agent, "on_response_wait"):
            agent.on_response_wait = _on_response_wait
        elif hasattr(agent, "agent") and hasattr(agent.agent, "on_response_wait"):
            agent.agent.on_response_wait = _on_response_wait

    if agent_type != "inline_editor":
        print_event("agent_started", {"agent": agent_type, "model": agent.model})

    from opalatex.tools import TURN_ACHIEVEMENTS
    import opalatex.tools as tools_mod

    if agent_type == "chat_orchestrator":
        tools_mod.TURN_ACHIEVEMENTS = ""

    turn_checkpoint = None
    if agent_type in ("orchestrator", "chat_orchestrator") and current_project and current_project.project_path:
        try:
            from opalatex.config import get_git_strategy
            if get_git_strategy().lower() != "none":
                from opalatex.vcs import AsyncAgentTurnCheckpoint
                turn_checkpoint = AsyncAgentTurnCheckpoint(current_project.project_path)
                await turn_checkpoint.begin()
        except Exception:
            turn_checkpoint = None

    # Open the out-of-band inbox only after the cancellation-safe checkpoint has
    # started.  A cancellation while Git is taking the initial snapshot therefore
    # cannot leave a live inbox attached to a turn that never began execution.
    turn_inbox = None
    if agent_type in ("orchestrator", "chat_orchestrator"):
        turn_inbox = _open_turn_inbox(
            agent,
            agent_type,
            project_path=getattr(current_project, "project_path", "") or "",
            chat_id=str(data.get("chat_id") or ""),
        )

    import opalatex.terminal as T
    loop = asyncio.get_event_loop()
    import uuid

    restore_turn_input_hooks = _install_turn_input_hooks(
        _turn_confirm_hook,
        _turn_ask_hook,
        _turn_interactive_terminal_hook,
    )

    # Report what the provider charges for each request of this turn, so the
    # panel indicator tracks the real window instead of a char count over the
    # visible bubbles. Only the chat orchestrator is reported: sub-agents run
    # their own histories and do not describe the conversation's window.
    _context_window = _resolve_context_window(
        agent,
        data.get("model_params") or {},
        getattr(current_project, "model_params", {}) or {},
    )

    if agent_type in TOKEN_CONTEXT_AGENTS:
        set_context_window(_context_window)

    def _report_token_usage(usage_agent: str, record: dict) -> None:
        if usage_agent not in TOKEN_CONTEXT_AGENTS:
            return
        print_event("token_usage", {
            "agent": usage_agent,
            "context_window": _context_window,
            **record,
        })

    set_usage_listener(_report_token_usage)

    try:
        try:
            # --- Attachment processing: vision gate + smart PDF truncation ---
            user_history_content = display_prompt or prompt
            prompt, final_attachments, attachment_aliases = _prepare_turn_attachments(
                prompt, raw_attachments
            )
            tools_mod.set_recent_file_attachments(attachment_aliases)

            # Save user message to store immediately so it's not lost if the agent crashes
            if agent_type in ("orchestrator", "chat_orchestrator") and current_store and current_project:
                user_message_id = None
                try:
                    user_message_id = await _run_off_loop(
                        current_store.append_message,
                        current_project,
                        "user",
                        user_history_content,
                        attachments=raw_attachments,
                        client_message_id=client_message_id,
                    )
                except TypeError:
                    user_message_id = await _run_off_loop(
                        current_store.append_message,
                        current_project, "user", user_history_content, attachments=raw_attachments
                    )
                current_store.save(current_project)
                # Hand the stored id back to the UI. Without it the front-end has no
                # stable anchor for this message during the session, and any edit or
                # branch would have to guess its position in the stored history.
                if user_message_id is not None:
                    print_event("user_message_saved", {
                        "message_id": user_message_id,
                        "client_message_id": client_message_id,
                    })

            if agent_type in ("orchestrator", "chat_orchestrator"):
                clear_worker_message_buffer()
                agent._current_worker_messages = []
                agent._last_worker_summary = ""

            #print(f"\n{'='*30} [DIAGNOSTIC: ORCHESTRATOR TURN START] {'='*30}")
            #print(f"[DIAGNOSTIC] Agent: {agent_type} | Model: {getattr(agent, 'model', 'unknown')}")
            #print(f"[DIAGNOSTIC] Incoming Prompt: {prompt[:300]!r}")
            if hasattr(agent, "tools"):
                tool_names = [getattr(t, "name", str(t)) for t in agent.tools]
                #print(f"[DIAGNOSTIC] Registered Tools ({len(tool_names)}): {tool_names}")
            # The separator of the commented-out diagnostic block above. It went
            # to the server console under the GUI, where nobody saw it; under the
            # CLI it printed a rule into the user's terminal on every turn.
            #print(f"{'='*80}\n")

            with apply_meta_params(agent, _meta_overrides):
                resp_obj = await agent.run(AgentInput(prompt=prompt, attachments=final_attachments))
            response = _sanitize_model_response(resp_obj.response or "", thought_chunks)
            if response and data.get("inline_response_contract") == "replacement_only":
                response = _normalize_inline_fenced_replacement(response)
                _validate_inline_replacement_scope(
                    response,
                    data.get("editor_content", ""),
                    data.get("selected_text", ""),
                )
            
            empty_response_attempts = 0
            while not response and empty_response_attempts < EMPTY_RESPONSE_MAX_CORRECTION_ATTEMPTS:
                empty_response_attempts += 1
                worker_summary = _worker_summary_response(agent)
                from opalatex.i18n import _
                print_event("info", {"message": _("empty_response_retry_info")})
                retry_prompt = _empty_response_retry_prompt(worker_summary)
                # This is framework feedback, not something the user typed. Sending it
                # with the system role keeps runtime corrections out of the user's
                # voice (PROJECT_DESIGN 2.6). Agents without a conversation history
                # reject a non-user role, so they keep the default.
                retry_input = (
                    AgentInput(prompt=retry_prompt, role="system")
                    if hasattr(agent, "internal_history")
                    else AgentInput(prompt=retry_prompt)
                )
                with apply_meta_params(agent, _meta_overrides):
                    resp_obj = await agent.run(retry_input)
                response = _sanitize_model_response(resp_obj.response or "", thought_chunks)
                if response and data.get("inline_response_contract") == "replacement_only":
                    response = _normalize_inline_fenced_replacement(response)
                    _validate_inline_replacement_scope(
                        response,
                        data.get("editor_content", ""),
                        data.get("selected_text", ""),
                    )

            if agent_type in ("orchestrator", "chat_orchestrator"):
                response, resp_obj = await _correct_serialized_tool_calls(
                    agent, response, thought_chunks, _meta_overrides, resp_obj
                )

            if not response:
                raise RuntimeError(_empty_response_failure_message())

            if agent_type in ("orchestrator", "chat_orchestrator") and not data.get(
                "inline_response_contract"
            ):
                response = _mark_turn_without_answer(resp_obj, response)
                response = _fold_turn_progress(resp_obj, response)

            persisted_response = _response_with_thought(response, thought_chunks)
            assistant_message_id = None

            # Save assistant response and achievements
            if agent_type in ("orchestrator", "chat_orchestrator") and current_store and current_project:
                if tools_mod.TURN_ACHIEVEMENTS:
                    current_store.append_activity(
                        current_project,
                        "achievements",
                        content=f"Achievements logged during this turn:\n{tools_mod.TURN_ACHIEVEMENTS}",
                        agent=agent_type,
                    )
                if persisted_response:
                    assistant_message_id = await _run_off_loop(
                        current_store.append_message, current_project, "assistant", persisted_response
                    )
                _restore_transient_project_mode(initial_project_mode, agent_type)
                current_store.save(current_project)

            response_payload = {"response": response}
            if agent_type in ("orchestrator", "chat_orchestrator"):
                response_payload["persisted_response"] = persisted_response
                if assistant_message_id is not None:
                    response_payload["message_id"] = assistant_message_id
            print_event("agent_response", response_payload)
        except opalatex.tools.UserCancelException as e:
            # The user denied a tool operation; gracefully abort the turn without feeding an error back to the LLM.
            print_event("agent_response", {"response": "Turno cancelado pelo usuário."})
            print_event("error", {"message": str(e), "trace": ""})
        except asyncio.CancelledError:
            _record_interrupted_agent_turn(agent_type)
            raise
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc()
            user_msg = _friendly_llm_error(e, current_project)
            print_event("error", {"message": user_msg, "trace": err_msg})
            # The conversation keeps what the model wrote before the failure.
            # Reported from a real session: a standby killed the connection to
            # the provider mid-answer, this handler stored nothing, and the chat
            # was left holding only the user's question -- with no assistant turn
            # for "Continue" to resume from.
            _persist_unfinished_turn(
                agent_type, TURN_FAILED_MARKER, only_with_work=True
            )
    finally:
        checkpoint_cancelled_during_cleanup = False
        # Close the message channel before anything else in the cleanup: from
        # here on nothing will drain it, so a message still accepted would be
        # accepted into a turn that can no longer deliver it.
        _close_turn_inbox(agent, turn_inbox, agent_type)
        # Persist the orchestrator's working memory even when the turn failed or was
        # interrupted: the partial context is what a resume needs, and the
        # compatibility layer repairs any tool call left without its result.
        if agent_type in ("orchestrator", "chat_orchestrator") and current_store and current_project:
            try:
                from opalatex.memgpt_runtime import save_chat_orchestrator_state
                save_chat_orchestrator_state(agent, current_project, current_store)
            except Exception:
                pass
        # The occupancy describes the working state saved just above, so it is
        # persisted in the same place and for the same reason: an interrupted or
        # failed turn already consumed the window it reports.
        if agent_type in TOKEN_CONTEXT_AGENTS:
            _persist_context_usage(data.get("chat_id"))
        if turn_checkpoint is not None and turn_checkpoint.start_checkpoint:
            try:
                checkpoint_saved = await turn_checkpoint.finalize()
                print_event("checkpoint_finalized", {
                    "agent": agent_type,
                    "success": checkpoint_saved,
                    "changed_files": list(turn_checkpoint.changed_files),
                    "project_path": turn_checkpoint.project_path,
                })
                if not checkpoint_saved:
                    from opalatex.i18n import _
                    print_event("problem", {
                        "agent": agent_type,
                        "severity": "error",
                        "message": _("checkpoint_finalize_failed"),
                    })
            except asyncio.CancelledError:
                # AsyncAgentTurnCheckpoint propagates cancellation only after its
                # shielded Git cleanup has completed.  Defer propagation until the
                # remaining synchronous cleanup below has also run.
                print_event("checkpoint_finalized", {
                    "agent": agent_type,
                    "success": bool(turn_checkpoint.finalize_result),
                    "changed_files": list(turn_checkpoint.changed_files),
                    "project_path": turn_checkpoint.project_path,
                })
                if not turn_checkpoint.finalize_result:
                    from opalatex.i18n import _
                    print_event("problem", {
                        "agent": agent_type,
                        "severity": "error",
                        "message": _("checkpoint_finalize_failed"),
                    })
                checkpoint_cancelled_during_cleanup = True
            except Exception:
                from opalatex.i18n import _
                print_event("checkpoint_finalized", {
                    "agent": agent_type,
                    "success": False,
                    "changed_files": [],
                    "project_path": turn_checkpoint.project_path,
                })
                print_event("problem", {
                    "agent": agent_type,
                    "severity": "error",
                    "message": _("checkpoint_finalize_failed"),
                })
        # The activity connection is kept open for the whole turn because it is
        # written once per streamed token; outside a turn there is nothing to
        # gain from holding the database file open, so release it here.
        if current_store is not None:
            try:
                current_store.close_activity_connection()
            except Exception:
                pass
        restore_turn_input_hooks = locals().get("restore_turn_input_hooks")
        if restore_turn_input_hooks is not None:
            restore_turn_input_hooks()
        set_usage_listener(None)
        _ACTIVE_THOUGHT_CHUNKS = None
        _ACTIVE_THOUGHT_METER = None
        _restore_transient_project_mode(
            locals().get("initial_project_mode"),
            locals().get("agent_type", ""),
        )
        if checkpoint_cancelled_during_cleanup:
            raise asyncio.CancelledError

    if agent_type != "inline_editor":
        print_event("agent_finished", {})

async def handle_list_projects(data: dict):
    db_path = data.get("db") or DEFAULT_DB_PATH
    store = ProjectStore(db_path=db_path)
    projects = store.list_projects()
    print_event("projects_list", {"projects": projects})

async def handle_create_project(data: dict):
    from opalatex.i18n import _

    db_path = data.get("db") or DEFAULT_DB_PATH
    store = ProjectStore(db_path=db_path)

    project_name = data.get("project_name")
    if not project_name:
        print_event("error", {"message": "project_name is required"})
        return
    
    project_path = data.get("project_path") or os.getcwd()
    description = data.get("description", "")
    # No implicit default: a project starts with no model configured.
    model = str(data.get("model") or "")
    mode = data.get("mode") or "auto"
    skills = data.get("skills", [])
    api_key = data.get("api_key")
    api_base = data.get("api_base")
    worker_api_key = data.get("worker_api_key")
    worker_api_base = data.get("worker_api_base")
    model_params = data.get("model_params")
    worker_model_params = data.get("worker_model_params")
    
    abs_proj_path = os.path.abspath(project_path)
    existing_name = store.find_by_path(abs_proj_path)
    
    if existing_name:
        existing_proj = store.load(existing_name)
        existing_project_name = existing_proj.project_name if existing_proj else existing_name
        print_event("error", {"message": _("project_exists_in_folder", name=existing_project_name)})
        return
    else:
        db_key = project_name.replace(" ", "_").lower()
        if store.exists(db_key):
            db_key = f"{db_key}_{int(time.time())}"
            
        project = store.create(
            name=db_key,
            mode=mode,
            model=model,
            project_name=project_name,
            project_path=abs_proj_path,
            skills=skills,
            description=description,
            api_key=api_key,
            api_base=api_base,
            worker_api_key=worker_api_key,
            worker_api_base=worker_api_base,
            model_params=model_params,
            worker_model_params=worker_model_params,
        )
    print_event("project_created", {
        "project_name": project.project_name,
        "project_path": project.project_path,
        "skills": project.skills,
        "api_key": project.api_key,
        "api_base": project.api_base,
        "worker_api_key": project.worker_api_key,
        "worker_api_base": project.worker_api_base,
    })

async def handle_update_project(data: dict):
    db_path = data.get("db") or DEFAULT_DB_PATH
    store = ProjectStore(db_path=db_path)
    
    project_name = data.get("project_name")
    if not project_name:
        print_event("error", {"message": "project_name is required"})
        return
    
    if not store.exists(project_name):
        print_event("error", {"message": f"Project '{project_name}' not found"})
        return
        
    project = store.load(project_name)
    if "display_name" in data:
        project.project_name = data["display_name"]
    if "model" in data:
        project.model = str(data["model"] or "")
    if "worker_model" in data:
        project.worker_model = str(data["worker_model"] or "")
    if "description" in data:
        project.description = data["description"]
    if "mode" in data and data["mode"]:
        project.mode = data["mode"]
    if "project_path" in data and data["project_path"]:
        project.project_path = os.path.abspath(data["project_path"])
    if "api_key" in data:
        project.api_key = data["api_key"]
    if "api_base" in data:
        project.api_base = data["api_base"]
    if "worker_api_key" in data:
        project.worker_api_key = data["worker_api_key"]
    if "worker_api_base" in data:
        project.worker_api_base = data["worker_api_base"]
    if "model_params" in data:
        project.model_params = sanitize_model_params(data["model_params"])
    if "worker_model_params" in data:
        project.worker_model_params = sanitize_model_params(data["worker_model_params"])
        
    store.save(project)
    print_event("project_updated", {
        "name": project.name,
        "project_name": project.project_name,
        "project_path": project.project_path,
        "model": project.model,
        "worker_model": project.worker_model,
        "mode": project.mode,
        "description": project.description,
        "api_key": project.api_key,
        "api_base": project.api_base,
        "worker_api_key": project.worker_api_key,
        "worker_api_base": project.worker_api_base,
        "model_params": project.model_params,
    })

async def handle_delete_project(data: dict):
    db_path = data.get("db") or DEFAULT_DB_PATH
    store = ProjectStore(db_path=db_path)
    project_name = data.get("project_name")
    if not project_name:
        print_event("error", {"message": "project_name is required"})
        return
    
    if store.exists(project_name):
        store.delete(project_name)
        print_event("project_deleted", {"project_name": project_name})
    else:
        print_event("error", {"message": f"Project '{project_name}' not found"})

def handle_input_response(data: dict) -> None:
    """Answer a pending `input_request` over the stdin protocol.

    The GUI has /api/opalatex/input_response for this; without the same command
    here, a `--stdin` run that hit a tool-permission prompt or a plan approval
    had no way to reply and waited out the timeout.
    """
    req_id = str(data.get("id") or "")
    fut = _gui_input_pending.get(req_id)
    if fut is None or fut.done():
        print_event("error", {"message": f"No pending input request '{req_id}'"})
        return
    fut.set_result(str(data.get("response", "")))


async def handle_stdin_run(data: dict) -> None:
    """`run` as a client driving OpalaTex over stdin sends it.

    A project may have no chat, and in every front-end the first message is
    what starts one (PROJECT_DESIGN 2.8). The desktop window and the REPL create
    it before they call `handle_run`, which stays strict because the window
    shares it; this front-end does the same here. The new chat is announced
    with `chat_created`, so the client can name it with `chat_id` later.
    """
    global current_project, current_memgpt
    agent_type = data.get("agent") or "chat_orchestrator"
    if (
        agent_type not in ("orchestrator", "chat_orchestrator")
        or data.get("chat_id")
        or data.get("resume_interrupted")
    ):
        await handle_run(data)
        return

    names_project = "project_path" in data or "project_name" in data
    if names_project:
        store = ProjectStore(db_path=data.get("db") or DEFAULT_DB_PATH)
        name = data.get("project_name") or "stdin_project"
    elif current_project is not None and current_store is not None:
        store, name = current_store, current_project.name
    else:
        store, name = None, ""

    from opalatex.i18n import _ as _translate

    if store is not None and not store.exists(name):
        # Only this front-end can name a project the store does not hold; its
        # turn would fail on its first write (the history tables reference
        # `projects`), so it is refused with the actual cause.
        print_event("error", {"agent": agent_type, "message": _translate("project_not_registered", name=name)})
        print_event("agent_finished", {"agent": agent_type})
        return

    if store is not None and not store.load(name).current_chat_id:
        import uuid

        chat_id = str(uuid.uuid4())
        chat_name = _translate("default_chat_name")
        store.create_chat(name, chat_id, chat_name)
        print_event("chat_created", {"project_name": name, "chat_id": chat_id, "name": chat_name})
        if names_project:
            # `handle_run` reloads the named project, now on this chat.
            data = {**data, "chat_id": chat_id}
        else:
            # The project `load_project` opened keeps its in-memory overrides;
            # only its chat view moves to the new chat.
            opened = store.load(name, chat_id=chat_id)
            for field_name in ("chats", "current_chat_id", "chat_reasoning_effort", "history"):
                setattr(current_project, field_name, getattr(opened, field_name))
            set_project_context(current_project, store)
            current_memgpt = build_chat_orchestrator(current_project, store)

    await handle_run(data)


async def stdin_server_loop():
    print_event("server_ready", {})
    
    # Read line by line from stdin
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    
    while True:
        try:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()
            if not line:
                continue
            
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                print_event("error", {"message": "Invalid JSON format"})
                continue
                
            cmd = data.get("command")
            if cmd == "exit":
                print_event("exited", {})
                break
            elif cmd == "load_project":
                await handle_load_project(data)
            elif cmd == "run":
                await handle_stdin_run(data)
            elif cmd == "list_projects":
                await handle_list_projects(data)
            elif cmd == "create_project":
                await handle_create_project(data)
            elif cmd == "update_project":
                await handle_update_project(data)
            elif cmd == "delete_project":
                await handle_delete_project(data)
            elif cmd == "input_response":
                handle_input_response(data)
            elif cmd == "slash_command":
                # The whole command registry, rather than one protocol command
                # per feature. A text front-end driving OpalaTex over stdin has
                # the same commands the REPL and the desktop chat have.
                result = await handle_slash_command(data)
                print_event("slash_command_result", result)
            else:
                print_event("error", {"message": f"Unknown command '{cmd}'"})
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print_event("error", {"message": f"Exception in loop: {e}"})

def start_stdin_server():
    claim_stdout_for_protocol()
    asyncio.run(stdin_server_loop())

# A read timeout on an open response: the server answered, then went silent.
_STALLED_RESPONSE_MARKERS = (
    "timeout on reading data",
    "read timed out",
    "the read operation timed out",
    "readtimeout",
)
# Any other request timeout; LiteLLM does not say whether it hit while
# connecting or while waiting for the first byte.
_REQUEST_TIMEOUT_MARKERS = (
    "timed out",
    "litellm.timeout",
    "apitimeouterror",
)


def _llm_timeout_kind(low_message: str) -> str | None:
    """Return the i18n key describing a timed-out LLM request, if it is one."""
    if any(marker in low_message for marker in _STALLED_RESPONSE_MARKERS):
        return "err_response_stalled"
    if any(marker in low_message for marker in _REQUEST_TIMEOUT_MARKERS):
        return "err_request_timeout"
    return None


def _ollama_http_status_from_error(message: str) -> int | None:
    """Return an Ollama HTTP status embedded in a LiteLLM exception message."""
    text = str(message or "")
    low = text.lower()
    if "ollama" not in low:
        return None
    if "404 page not found" in low:
        return None
    if "internal server error" in low:
        return 500
    match = re.search(
        r"(?:http\s*(?:status(?:\s*code)?\s*)?|status(?:\s*code)?\s*[:=]?\s*|\b)([45]\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None
