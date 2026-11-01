"""MemGPT runtime for the skills-oriented architecture (docs/specs/02, 06).

This module assembles the **main agent** -- the fixed MemGPT chat-orchestrator,
which does the user's task -- and the two ways skills extend it (PROJECT_DESIGN
2.23):

  - ``build_load_skill_tool(project_path)`` returns a ``load_skill`` tool: the
    instructions of a skill the main agent carries out itself, with its own tools.
  - ``build_run_skill_tool(memgpt, project_path)`` returns a ``run_skill`` tool
    bound to a MemGPT instance. When the MemGPT calls it, an ephemeral helper
    (LLMAgentBlock) is spawned with the helper skill's SKILL.md body as its system
    prompt and the helper toolset. The helper returns data as normal text, which
    ``run_skill`` hands back with the tool calls that failed on the way.
  - ``build_chat_orchestrator(project, store)`` builds the MemGPT itself: the
    framework ``MemGPTAgentBlock`` primed with the ``chat-orchestrator`` SKILL.md,
    the catalog of the active skills, the workspace tools, ``load_skill``,
    ``run_skill``, and the memory tools.

The module is additive: the legacy intent-routing path in cli.py is untouched
until the REPL is switched over (Phase 4).
"""
from __future__ import annotations
from datetime import datetime
from opalatex.tools import read_file
from opalatex.tools import get_project_overview
import asyncio
import json
import os
import re
from typing import Any

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.blocks.llm.memgpt_agent import (
    MemGPTAgentBlock,
    is_empty_response_placeholder,
)
# pyrefly: ignore [missing-import]
from agenticblocks.core.function_block import as_tool

from . import terminal as T

from .tools import (
    #ask_human,
    read_core_memory,
    append_core_memory,
    search_conversation_history,
    search_code,
    web_search,
    analyze_image,
    read_content_pos,
    get_editor_state,
    set_project_context,
)
from .config import (
    DEFAULT_MODEL,
    WORKER_MODEL,
    get_agent_llm_kwargs,
    get_agent_max_heartbeats,
    get_agent_model,
    get_project_agent_params,
)
from .skills import (
    HELPER_ROLE,
    INSTRUCTIONS_ROLE,
    skill_role,
    active_skills,
    find_skill_dir,
    level1_metadata,
    parse_skill_md,
    MANDATORY_SKILLS,
)

from .tools import get_helper_tools
from .project import tutorial_chat_id
from .prompt_profiles import ACHIEVEMENTS_POLICY
from .think_stream import InlineReasoningStreamSplitter

CHAT_ORCHESTRATOR_SKILL = "chat-orchestrator"
MAX_FAILED_SKILL_ATTEMPTS = 2
MAX_FAILED_DELEGATIONS = 3


_SERIALIZED_TOOL_CALL_MARKER = (
    "[WORKER PROTOCOL ERROR: the worker serialized a tool call as text instead of "
    "invoking it, so this attempt produced no result.]"
)
_BROKEN_SERIALIZATION_MARKER = (
    "[WORKER PROTOCOL ERROR: the worker emitted a structured payload as text and "
    "did not close it, so this attempt produced no usable report.]"
)


# Chat-template markup for a tool call, which weak models emit as plain content
# when their native calls keep being rejected. `<tool_call>` is the Hermes/Qwen
# wrapper; `<function=name>` is the element Qwen-Coder-style templates use inside
# it, and which some models emit on its own. Both are matched lazily so a run of
# several blocks is cut one at a time, and both accept a missing closing tag so a
# truncated block is still recognized.
_MARKUP_TOOL_CALL_RE = re.compile(
    r"<(?P<tag>tool_call|function_call)\b[^>]*>.*?(?:</(?P=tag)\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_MARKUP_FUNCTION_CALL_RE = re.compile(
    r"<function\s*=\s*[^<>\s]+\s*>.*?(?:</function\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _is_tool_call_payload(value: Any) -> bool:
    """True when parsed JSON has the shape of a tool call rather than of data."""
    if isinstance(value, list):
        return any(_is_tool_call_payload(item) for item in value)
    if not isinstance(value, dict):
        return False
    if "tool_calls" in value:
        return True
    function = value.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return True
    if isinstance(value.get("tool_name"), str):
        return True
    return isinstance(value.get("name"), str) and (
        "arguments" in value or "parameters" in value
    )


def _iter_json_spans(text: str):
    """Yield ``(start, end)`` spans of balanced ``{...}``/``[...]`` blocks in ``text``."""
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            if depth == 0:
                start = index
            depth += 1
        elif char in "}]" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield start, index + 1
                start = -1


def _iter_serialized_tool_call_spans(text: str):
    """Yield ``(start, end)`` spans of every tool call written as text.

    A tool call is serialized in one of two encodings, and only the JSON one used
    to be recognized. Qwen-family models served locally fall back to their chat
    template's markup instead -- ``<tool_call>`` wrapping either a JSON payload or
    ``<function=name>``/``<parameter=key>`` elements -- which carries no JSON
    object for `_iter_json_spans` to find. Both encodings are plain text, so
    neither executes, and both must be caught the same way.

    An unterminated block still counts: a truncated ``<tool_call>`` is a call the
    model failed to issue, not prose.
    """
    for pattern in (_MARKUP_TOOL_CALL_RE, _MARKUP_FUNCTION_CALL_RE):
        for match in pattern.finditer(text):
            yield match.start(), match.end()
    for start, end in _iter_json_spans(text):
        try:
            parsed = json.loads(text[start:end])
        except Exception:
            continue
        if _is_tool_call_payload(parsed):
            yield start, end


def _merge_spans(spans) -> list[tuple[int, int]]:
    """Merge overlapping and nested spans so no region is excised twice.

    A ``<tool_call>`` block normally contains a ``<function=...>`` element or a
    JSON payload, so the same text is reported by more than one scanner.
    """
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _strip_serialized_tool_calls(text: str) -> tuple[str, bool]:
    """Replace a tool call emitted as text with a fixed marker.

    Weak local models sometimes print a tool call instead of issuing it, as JSON
    or as chat-template markup. Replaying that payload verbatim into the next
    worker prompt teaches the next model to do the same, so the payload never
    reaches a prompt — only the marker does.
    """
    spans = _merge_spans(_iter_serialized_tool_call_spans(text))
    if not spans:
        return text, False

    kept: list[str] = []
    cursor = 0
    for start, end in spans:
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    cleaned = " ".join(part.strip() for part in kept if part.strip())
    return (f"{cleaned} {_SERIALIZED_TOOL_CALL_MARKER}".strip() if cleaned
            else _SERIALIZED_TOOL_CALL_MARKER), True


def _is_broken_serialization(text: str) -> bool:
    """True when a report opens as a JSON document but does not parse as one.

    That is a failed serialization, not a summary: the model started emitting a
    structured payload as text and truncated it.
    """
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return False
    if not re.search(r'"\s*:', stripped):
        return False
    try:
        json.loads(stripped)
    except Exception:
        return True
    return False


#: Opens the helper's report inside a run_skill result. `agent_stdin` extracts
#: the report by it, and also accepts LEGACY_WORKER_REPORT_MARKER, which the
#: results stored in chats from before helpers existed carry.
HELPER_REPORT_MARKER = "Helper's report:"
LEGACY_WORKER_REPORT_MARKER = "Worker's summary/report:"

#: How many failed tool calls a run_skill result lists before summarising.
MAX_REPORTED_TOOL_ERRORS = 5


def _helper_tool_error_notes(records, limit: int = MAX_REPORTED_TOOL_ERRORS) -> str:
    """The tool calls of a helper run that failed, and what the helper did next.

    The helper's report is its own account, and the error it received told it
    to correct the error silently, so a recovered error is usually missing from
    it -- and an unrecovered one may be too. This lists the facts the runtime
    recorded (`AgentOutput.tool_calls`): each failure, then how many calls the
    helper made after it and how many of those were successful calls of the same
    tool. It does not judge whether the helper recovered; that is the report's
    to say, and the main agent weighs the two.
    """
    records = list(records or [])
    failures = [r for r in records if not getattr(r, "ok", True)]
    if not failures:
        return ""
    lines = []
    for record in failures[:limit]:
        later = [r for r in records if r.index > record.index]
        same_ok = sum(1 for r in later if r.ok and r.tool == record.tool)
        error = " ".join(str(record.error or "").split())
        if len(error) > 240:
            error = error[:240] + "..."
        if later:
            after = (
                f"the helper made {len(later)} more tool call(s) after it, "
                f"{same_ok} of them a successful {record.tool}"
            )
        else:
            after = "it was the helper's last tool call"
        lines.append(f"- call {record.index} {record.tool} failed: {error} -- {after}")
    if len(failures) > limit:
        lines.append(f"- and {len(failures) - limit} more failed tool call(s)")
    return (
        "\n\n[Tool errors in this helper run -- recorded facts, not a verdict; the report above "
        "says what the helper achieved]\n" + "\n".join(lines)
    )


def _is_failed_worker_result(result: Any) -> bool:
    text = str(result or "")
    if not text.strip():
        return True
    lowered = text.lower()
    if (
        "[critical worker crash]" in lowered
        or "[aviso: o worker terminou sem um resumo claro" in lowered
        or "tools used by worker: 0" in lowered
        or "tool calls made by the helper: 0" in lowered
        or "0 chamadas de ferramenta" in lowered
        or "tool-call json parsing failed" in lowered
        or "worker protocol error" in lowered
    ):
        return True
    # A report that is a serialized tool call, or a JSON document the model never
    # closed, carries no summary: the delegation produced nothing usable and must
    # count towards the loop breaker instead of resetting it.
    _, has_tool_call_payload = _strip_serialized_tool_calls(text)
    return has_tool_call_payload or _is_broken_serialization(text)


def _recent_failed_skill_attempts(memgpt: MemGPTAgentBlock, skill_name: str) -> int:
    attempts = 0
    for run in reversed(getattr(memgpt, "_skill_run_history", [])):
        if run.get("skill") != skill_name:
            break
        if not _is_failed_worker_result(run.get("result", "")):
            break
        attempts += 1
    return attempts


def _recent_failed_delegations(memgpt: MemGPTAgentBlock) -> int:
    """Count trailing failed delegations regardless of which skill ran them.

    ``_recent_failed_skill_attempts`` stops at the first entry belonging to another
    skill, so alternating between two skills keeps its count at zero forever. An
    orchestrator that cannot get a usable report is in the same dead end whether it
    reworded the context or switched specialist, so this counter ignores the name.
    """
    attempts = 0
    for run in reversed(getattr(memgpt, "_skill_run_history", [])):
        if not _is_failed_worker_result(run.get("result", "")):
            break
        attempts += 1
    return attempts


def _sanitize_skill_result_for_prompt(result: Any, *, limit: int = 500) -> str:
    text = str(result or "").strip()
    lowered = text.lower()
    if "error parsing tool call" in lowered and "unexpected end of json input" in lowered:
        return "[CRITICAL WORKER CRASH: model produced invalid/truncated JSON tool-call arguments.]"
    if "[aviso: o worker terminou sem um resumo claro" in lowered:
        return "[WORKER NO-ACTION: worker finished without a clear summary or tool calls.]"
    text, had_tool_call_payload = _strip_serialized_tool_calls(text)
    if not had_tool_call_payload and _is_broken_serialization(text):
        return _BROKEN_SERIALIZATION_MARKER
    if len(text) > limit:
        return text[:limit] + "... [truncated]"
    return text


def _tool_calls_count(tool_calls: Any) -> int | None:
    if isinstance(tool_calls, bool):
        return int(tool_calls)
    if isinstance(tool_calls, int):
        return tool_calls
    if isinstance(tool_calls, str) and tool_calls.isdigit():
        return int(tool_calls)
    return None



def _current_date_instruction(now: datetime | None = None) -> str:
    """Return the dynamic date/freshness instruction prepended to the agent prompt."""
    today = now or datetime.now()
    date_text = f"{today.strftime('%B')} {today.day}, {today.year}"
    return (
        f"Today is {date_text}.\n"
        "TOOL USE - web_search: whenever ANY of the conditions below holds, you MUST "
        "use the web_search tool before answering, refusing, or delegating.\n"
        "1. Unknown or low-confidence terms: the request names a concept, entity, "
        "algorithm, framework, acronym, paper, product, or piece of jargon that you do "
        "not confidently recognize, or that looks misspelled or non-standard. Search it, "
        "including the likely corrected spelling.\n"
        "2. Time-sensitive facts: recent events, current facts, latest/last occurrences, "
        "schedules, releases, versions, public facts that may have changed, or dates that "
        "could be after your training data.\n"
        "3. Verification: precise external data you cannot recall exactly, such as API or "
        "library behavior, benchmark numbers, paper or documentation details, and quotes.\n"
        "Never guess, never invent, and never answer that you have no information about a "
        "topic because it is missing from your training data without searching first. Never "
        "claim that a current, recent, or future-dated event did not happen without first "
        "checking the web.\n"
        "Scope: search public/external knowledge only. Questions about this workspace go to "
        "the project read tools, and anything that depends on the user's preference goes to "
        "ask_question. Stop at the first results good enough to answer."
    )



# ---------------------------------------------------------------------------
# Model resolution for a skill's sub-agent
# ---------------------------------------------------------------------------

def resolve_skill_model(skill_meta: dict, project_model: str | None,
                        project_worker: str | None = None) -> str:
    """Resolve the model for a skill's sub-agent from the SKILL.md `model` field.

    Missing/empty or "worker" (or "alternative") → the project's worker model
    (or the project's main model when the worker model is empty).
    "default" → the project's main model.
    """
    raw = (skill_meta.get("model") or "").strip()
    if not raw:
        return project_worker or project_model or DEFAULT_MODEL
    if raw == "default":
        return project_model or DEFAULT_MODEL
    if raw in ("alternative", "worker"):
        return project_worker or project_model or DEFAULT_MODEL
    return raw


def _helper_vision_params(project, model: str) -> dict:
    """The parameters that declare *model*'s vision for a helper running on it.

    `force_vision` is the user's statement about one model, so it is read from
    the parameters that belong to that model: the project's main ones when the
    helper runs on the main model (a skill with `model: default`), its worker
    ones otherwise -- falling back to the main ones exactly as
    `get_agent_llm_kwargs` does for the "worker" role when a project has no
    worker parameters.
    """
    main_params = dict(getattr(project, "model_params", None) or {})
    if model and model == (getattr(project, "model", "") or ""):
        return main_params
    worker_params = getattr(project, "worker_model_params", None) or {}
    return dict(worker_params) if worker_params else main_params


def _helper_model_for(skill_meta: dict, project_model: str | None,
                      project_worker: str | None) -> str:
    """The model a helper skill runs on, before provider routing.

    One resolution for the catalog the main agent reads and for `run_skill`,
    so the model -- and the vision -- it is told about is the one that runs.
    """
    return get_agent_model("worker", resolve_skill_model(skill_meta, project_model, project_worker))


def _skill_catalog(skills: list[dict], project_model: str | None,
                   project_worker: str | None, project) -> str:
    """The two skill lists of the main agent's prompt (PROJECT_DESIGN 2.23).

    Helpers carry their model and whether it accepts images, so the main agent
    sends image work only where it can be seen. A skill whose `role` is neither
    value is listed as unusable, with the reason, instead of being dropped: a
    skill the user enabled that silently vanishes is harder to diagnose than one
    that says what is wrong with it.
    """
    from .config import model_supports_vision

    helpers, instructions, invalid = [], [], []
    for skill in skills:
        role = skill_role(skill)
        description = str(skill.get("description", "") or "").strip()
        if role == HELPER_ROLE:
            model = _helper_model_for(skill, project_model, project_worker)
            vision = "yes" if model_supports_vision(model, _helper_vision_params(project, model)) else "no"
            helpers.append(f"- {skill['name']}: {description} [model: {model or '(none)'}; vision: {vision}]")
        elif role == INSTRUCTIONS_ROLE:
            instructions.append(f"- {skill['name']}: {description}".rstrip())
        else:
            invalid.append(
                f"- {skill['name']}: unusable -- its SKILL.md declares role '{role}', and only "
                f"'{HELPER_ROLE}' or '{INSTRUCTIONS_ROLE}' is accepted."
            )
    blocks = [
        "## Helpers (delegate with run_skill)\n"
        "Each helper is an ephemeral agent that does an auxiliary job and returns data; it never "
        "changes project files. Its model is shown, and whether that model can see images: send "
        "image work only to a helper with `vision: yes`, or look at the image yourself with "
        "`analyze_image`.\n"
        + ("\n".join(helpers) if helpers else "(none active)"),
        "## Skills you carry out yourself (load with load_skill)\n"
        "`load_skill(name)` returns the skill's instructions; you then do the work with your own tools.\n"
        + ("\n".join(instructions) if instructions else "(none active)"),
    ]
    if invalid:
        blocks.append("## Skills that cannot be used\n" + "\n".join(invalid))
    return "\n\n".join(blocks) + "\n"


#: How much auxiliary work the main agent hands to helpers, by policy
#: (`config.model_orchestrator_policy`). Both policies hold the same tools.
_HELPER_POLICY_BLOCKS = {
    "direct": (
        "## Your helper policy: direct\n"
        "You read and search with your own tools. Call a helper when an auxiliary job will not fit "
        "your context or a helper is built for it: full coverage of a file too large to read, "
        "condensing a large log, research across several web sources.\n"
    ),
    "delegate": (
        "## Your helper policy: delegate\n"
        "Hand auxiliary work to helpers by default and keep your own context for the task: reading "
        "or summarising long documents, extracting parts of a file, condensing logs, researching "
        "the web. Helpers usually run on a smaller model, so this saves your tokens. Read yourself "
        "only the exact passages you are about to change or quote. Every change to the project is "
        "still yours to make: helpers return data, never edits.\n"
    ),
}


# ---------------------------------------------------------------------------
# run_skill tool
# ---------------------------------------------------------------------------

WORKER_HISTORY_MESSAGES = 10


def _recent_conversation_for_worker(history, limit: int = WORKER_HISTORY_MESSAGES) -> str:
    """The last *limit* user/assistant messages that carry text, oldest first.

    Counted over conversational messages, not over raw history entries: slicing
    ``history[-10:]`` first let the current turn's tool calls and tool results
    fill the window, so a worker asked to save "the list above" received five
    empty ``ASSISTANT:`` lines (the tool-call entries have no text) and never the
    answer that held the list. Tool-call-only entries are skipped because they
    say nothing to the worker; tool results and system alerts are not
    conversation.
    """
    picked = []
    for message in reversed(history or []):
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", "")) for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(content or "").strip()
        if not text:
            continue
        picked.append(f"{role.upper()}: {text}")
        if len(picked) >= limit:
            break
    return "\n".join(reversed(picked))


def build_run_skill_tool(
    memgpt: MemGPTAgentBlock,
    project_path: str,
    project_model: str | None = None,
    project_worker: str | None = None,
    _project_ref=None,
    _store_ref=None,
):
    """Return a ``run_skill`` tool bound to *memgpt*.

    Calling ``run_skill(skill_name, context, intent)`` resolves the skill directory,
    reads its SKILL.md (Level 2), spawns an ephemeral LLMAgentBlock sub-agent with
    that body as system prompt and the workflow tools,
    runs it with *context* as the prompt, and returns the sub-agent's result.

    *_project_ref* / *_store_ref* let the tool re-assert the project scope on each
    call (so /load mid-session can't leak writes into the previous project).
    """

    @as_tool(
        name="run_skill",
        description=(
            "Delegate an auxiliary job to a helper listed under 'Helpers' AFTER requirements and user intent are clear: "
            "reading or summarising a long document, extracting parts of a file, condensing a log, researching the web. "
            "The helper is an ephemeral agent that returns data and never changes project files; you act on what it returns. "
            "Pass the helper's name and a context string with everything it needs (paths, the exact question, the format to return). "
            "DO NOT call run_skill on broad, generic, or open-ended user requests before asking the user for their focus via ask_question. "
            "CRITICAL: Only names listed under 'Helpers' are accepted. A skill listed for load_skill is carried out by you, not delegated."
        ),
    )
    async def run_skill(skill_name: str, context: str) -> str:
        # Re-assert project scope so the sub-agent's file/terminal tools act inside
        # this project even if a /load changed the global context since build time.
        if _project_ref is not None:
            from .tools import set_project_context as _spc
            _spc(_project_ref, _store_ref)

        if getattr(_project_ref, "mode", "auto") == "plan":
            return (
                "[BLOCKED] run_skill is not available while the project is in plan mode. "
                "Do not delegate to a helper or edit files yet. Gather any remaining context "
                "with safe read-only tools, then call create_plan with the proposed plan. "
                "Only after the user approves the plan may you execute it."
            )
            
        #print(f"\n{'-'*25} [DIAGNOSTIC: RUN_SKILL START] {'-'*25}")
        #print(f"[DIAGNOSTIC] Delegating to skill: '{skill_name}'")
        #print(f"[DIAGNOSTIC] Context passed:\n{context}")
        #print(f"{'-'*75}\n")

        # Delegating to yourself is not delegation: it would spawn a sub-agent
        # carrying this same body, with a helper toolset and no run_skill of its
        # own. It is never what the caller meant.
        def _active_helpers() -> list[str]:
            return [
                s["name"] for s in active_skills(project_path)
                if s["name"] != CHAT_ORCHESTRATOR_SKILL and skill_role(s) == HELPER_ROLE
            ]

        if skill_name == CHAT_ORCHESTRATOR_SKILL:
            return (
                f"[ERROR] '{CHAT_ORCHESTRATOR_SKILL}' is you, not a helper. "
                f"Do the work with your own tools, or delegate an auxiliary job to one of: {_active_helpers()}."
            )

        skill_dir = find_skill_dir(skill_name, project_path)
        if skill_dir is None:
            return (
                f"[ERROR] Skill '{skill_name}' was not found / is not active. "
                f"You MUST NOT invent skill names. Your own tools (read_file, search_code, "
                f"get_project_overview, web_search, ...) are tools you call "
                f"directly — they are never skill names. The only helpers you can "
                f"delegate to are: {_active_helpers()}. "
                # No create_plan here: plan mode returned [BLOCKED] above, so this
                # message is only ever read in modes where the main agent writes
                # and runs commands itself.
                "To list, search, read, write, or run something, use your own tools "
                "(e.g. get_project_overview, search_code, read_file, edit_file, write_file, run_command)."
            )
        meta = parse_skill_md(skill_dir)
        if meta is None:
            return f"[ERROR] skill '{skill_name}' has no valid SKILL.md."


        # >> INHERITANCE LOGIC START <<
        extends_name = meta.get("extends")
        parent_dir = None
        if extends_name:
            active = [s["name"] for s in active_skills(project_path)]
            if extends_name not in active:
                return f"[ERROR] skill '{skill_name}' requires parent '{extends_name}' which is not active."
            parent_dir = find_skill_dir(extends_name, project_path)
            if parent_dir:
                parent_meta = parse_skill_md(parent_dir)
                if parent_meta:
                    meta["body"] = parent_meta["body"] + "\n\n" + meta["body"]
        # >> INHERITANCE LOGIC END <<

        # Only a helper runs here; an instructions skill is the main agent's to
        # carry out (PROJECT_DESIGN 2.23). Refused with the route that works,
        # never run anyway as a helper with the wrong toolset.
        role = skill_role(meta)
        if role == INSTRUCTIONS_ROLE:
            return (
                f"[ERROR] '{skill_name}' is not a helper: it is a skill you carry out yourself. "
                f"Call load_skill('{skill_name}') to read its instructions, then do the work "
                "with your own tools."
            )
        if role != HELPER_ROLE:
            return (
                f"[ERROR] '{skill_name}' cannot run: its SKILL.md declares role '{role}', and only "
                f"'{HELPER_ROLE}' or '{INSTRUCTIONS_ROLE}' is accepted. Tell the user the skill's "
                "SKILL.md needs fixing."
            )

        model = _helper_model_for(meta, project_model, project_worker)

        # Write the request to a fixed temp file so the sub-agent never has to
        # shell-quote a complex request (parens/quotes in the request would break
        # `run_command`'s shell=True). The model's command becomes paren-free:
        #   python <abs>/run_workflow.py --request-file <path>
        request_file = ""
        try:
            _staging = os.path.join(project_path, ".opalatex")
            os.makedirs(_staging, exist_ok=True)
            request_file = os.path.join(_staging, f"_skill_request_{skill_name}.txt")
            with open(request_file, "w", encoding="utf-8") as _rf:
                _rf.write(context)
        except Exception:
            request_file = ""

        # System prompt = SKILL.md body (Level 2) + working dir scope + exact paths.
        script_paths = []
        if parent_dir:
            parent_scripts_dir = os.path.join(parent_dir, "scripts")
            if os.path.isdir(parent_scripts_dir):
                script_paths.extend(
                    os.path.join(parent_scripts_dir, f) 
                    for f in sorted(os.listdir(parent_scripts_dir)) if f.endswith(".py")
                )
                
        scripts_dir = os.path.join(skill_dir, "scripts")
        if os.path.isdir(scripts_dir):
            script_paths.extend(
                os.path.join(scripts_dir, f)
                for f in sorted(os.listdir(scripts_dir)) if f.endswith(".py")
            )
            
        scripts_hint = ""
        if script_paths:
            listing = "\n".join(f'  "{p}"' for p in script_paths)
            scripts_hint = (
                f"\nScripts available in this skill (use the ABSOLUTE path with "
                f"run_command, keeping the double quotes shown below — paths may "
                f"contain spaces):\n{listing}\n"
                f"Example: run_command('python \"{script_paths[0]}\" sample \"<log_file_path>\"')\n"
            )
        request_hint = ""
        if request_file:
            request_hint = (
                f"\nThe user's request has been written to this file:\n  {request_file}\n"
                f"When a script needs the request, pass it as --request-file {request_file} "
                f"(do NOT type the request text into the command — use the file).\n"
            )
        # The chat's reasoning effort covers the whole turn, delegation included:
        # a worker runs at it too. Its own catalog entry still decides whether it
        # reasons at all -- a worker model without `supports_thinking` is sent no
        # effort (`get_agent_llm_kwargs`).
        worker_kwargs = get_agent_llm_kwargs(
            "worker",
            reasoning_effort_override=getattr(_project_ref, "chat_reasoning_effort", "") or None,
        )
        # One decision, one source: `think` is in worker_kwargs only when the
        # worker model's catalog entry declares `supports_thinking`
        # (config.resolve_think_request), and that is exactly when there is an
        # isolated reasoning channel to publish.
        worker_publishes_reasoning = bool(worker_kwargs.get("think"))

        # Decided on the catalog name, before provider routing, exactly as the
        # catalog the main agent read decided it (`_skill_catalog`).
        from .config import model_supports_vision
        helper_vision = model_supports_vision(model, _helper_vision_params(_project_ref, model))

        # Resolved before the ollama_chat/ remap below, so the models_store lookup
        # (keyed on the stored "ollama/<name>" id) still matches.
        from .config import model_prompt_profile
        from .prompt_profiles import get_profile, DEFAULT_PROFILE
        worker_profile = model_prompt_profile(model)
        worker_profile_spec = get_profile(worker_profile)

        # Re-resolve the skill (and parent, if any) body for the profile now that
        # the worker's model is known. Frontmatter (model/extends) was already
        # read above from the canonical SKILL.md and is profile-independent.
        if worker_profile != DEFAULT_PROFILE:
            profiled = parse_skill_md(skill_dir, profile=worker_profile)
            if profiled and profiled.get("body"):
                body_for_profile = profiled["body"]
                if parent_dir:
                    profiled_parent = parse_skill_md(parent_dir, profile=worker_profile)
                    if profiled_parent and profiled_parent.get("body"):
                        body_for_profile = profiled_parent["body"] + "\n\n" + body_for_profile
                meta["body"] = body_for_profile

        from .config import resolve_model_route
        model = resolve_model_route(model, worker_kwargs)
        
        # Strip /v1 from the end because Ollama native providers expect the root URL
        if worker_kwargs.get("api_base"):
            if model.startswith("ollama/") or model.startswith("ollama_chat/"):
                if worker_kwargs["api_base"].endswith("/v1"):
                    worker_kwargs["api_base"] = worker_kwargs["api_base"][:-3]
                elif worker_kwargs["api_base"].endswith("/v1/"):
                    worker_kwargs["api_base"] = worker_kwargs["api_base"][:-4]

        native_tool_instruction = (
            _NATIVE_TOOL_CALL_REMINDER if _needs_native_tool_call_reminder(model) else ""
        )

        # Helpers get the helper toolset and return their report as normal text.
        # Resolved before the prompt, which lists exactly these tools -- including
        # the front-end filter, so the helper's prompt and its toolset agree.
        from .prompt_surface import tools_for_surface
        tools = tools_for_surface(list(get_helper_tools(vision=helper_vision)))

        system = (
            worker_profile_spec["worker_intro"]()
            + worker_profile_spec["worker_tools_block"](tools)
            + "# Metadata: "
            + f"{meta['body']}\n\n"
            + f"You are carrying out the '{skill_name}' helper skill. "
            + f"The project directory is: {project_path}\n"
            + f"Your scratch area is: {os.path.join(project_path, '.opalatex')}\n"
            + f"The skill directory is: {skill_dir}\n"
            + f"{scripts_hint}"
            + f"{request_hint}"
            + "IMPORTANT: Write scratch files (notes, plans, JSON) only through the file tools, "
            + "never by echoing them through run_command. "
            + worker_profile_spec["worker_recommendations"]()
            + worker_profile_spec["achievements_instructions"]()
            + f"{native_tool_instruction}"
        )

        memgpt._current_worker_messages = []

        from .config import get_project_agent_params
        worker_agent_params = get_project_agent_params("worker")

        sub_agent = LLMAgentBlock(
            name=f"skill_{skill_name}",
            user_prompt_prefix=getattr(_project_ref, "user_prompt_prefix", ""),
            system_prompt=system,
            model=model,
            tools=tools,
            model_kwargs=worker_kwargs,
            use_shared_router=False,
            max_iterations=worker_agent_params.get("max_iterations", None),
            max_tool_calls=worker_agent_params.get("max_tool_calls", 40),
            loop_detection=worker_agent_params.get("loop_detection", True),
            loop_detection_limit=worker_agent_params.get("loop_detection_limit", 3),
        )
        from .litellm_compat import wrap_agent_litellm_compat
        from .token_usage import attach_usage_tracking
        wrap_agent_litellm_compat(sub_agent)
        attach_usage_tracking(sub_agent)

        from opalatex.agent_stdin import _provider_wait_payload, _record_turn_thought, print_event

        object.__setattr__(sub_agent, "model", model)
        object.__setattr__(sub_agent, "model_kargs", worker_kwargs)
        object.__setattr__(sub_agent, "model_kwargs", worker_kwargs)
        object.__setattr__(sub_agent, "use_shared_router", False)
        
        worker_think_splitter = None
        if worker_kwargs.get("stream", False):
            thought_chunks = []

            def _worker_on_thinking(chunk: str) -> None:
                if _record_turn_thought(chunk):
                    thought_chunks.append(chunk)
                    print_event("thought", {"content": chunk, "agent": f"worker:{skill_name}", "_thought_recorded": True})

            def _worker_emit_visible(text: str) -> None:
                print_event("stream_chunk", {"content": text, "agent": f"worker:{skill_name}"})

            def _worker_retract_visible(text: str) -> None:
                print_event("stream_retract", {"content": text, "agent": f"worker:{skill_name}"})

            worker_think_splitter = InlineReasoningStreamSplitter(
                on_visible=_worker_emit_visible,
                on_thinking=_worker_on_thinking,
                on_retract=_worker_retract_visible,
            )

            if worker_publishes_reasoning:
                sub_agent.on_thinking = _worker_on_thinking
            sub_agent.on_chunk = worker_think_splitter.feed

        def _worker_on_iteration(_step: int, messages: list) -> None:
            if worker_think_splitter is not None:
                worker_think_splitter.begin_response()
            last = messages[-1] if messages else {}
            content = last.get("content") or ""
            if content:
                print_event("reflection", {"content": str(content), "agent": f"worker:{skill_name}"})

        sub_agent.on_iteration = _worker_on_iteration

        def _worker_on_response_wait(wait) -> None:
            print_event("provider_wait", _provider_wait_payload(wait, f"worker:{skill_name}"))

        sub_agent.on_response_wait = _worker_on_response_wait

        os.environ.setdefault(
            "OPALATEX_ROOT",
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        # Automatically inject recent chat history so MemGPT doesn't have to waste tokens copying it
        recent_history = _recent_conversation_for_worker(memgpt.internal_history)
        
        from .tools import TURN_ACHIEVEMENTS
        achievements_block = f"\n\n[TURN ACHIEVEMENTS MEMORY]\nThe orchestrator has noted the following accomplishments in this turn:\n{TURN_ACHIEVEMENTS}\n" if TURN_ACHIEVEMENTS else ""

        # Inject previous attempts for the same skill to keep a Markovian state
        if not hasattr(memgpt, "_skill_run_history"):
            memgpt._skill_run_history = []

        failed_delegations = _recent_failed_delegations(memgpt)
        if failed_delegations >= MAX_FAILED_DELEGATIONS:
            return (
                f"[SYSTEM ALERT] DELEGATION LOOP BREAKER: the last {failed_delegations} "
                "delegations, across every skill you tried, came back without a usable "
                "report. Switching skills or rewording the context is not fixing this. "
                "Stop delegating for this task: use your direct tools, ask the user what "
                "they need with ask_question, or return a concise normal-text blocker."
            )

        failed_attempts = _recent_failed_skill_attempts(memgpt, skill_name)
        if failed_attempts >= MAX_FAILED_SKILL_ATTEMPTS:
            return (
                f"[SYSTEM ALERT] WORKER LOOP BREAKER: The '{skill_name}' skill has failed "
                f"{failed_attempts} consecutive times without completing useful tool work. "
                "Do NOT call this same worker again for this task. Use your direct tools if "
                "available, reduce the task to a smaller verified action, or return a "
                "concise normal-text blocker explanation."
            )
            
        # Check for macro-loop
        for run in memgpt._skill_run_history:
            if run["skill"] == skill_name and run["context"] == context:
                return f"[SYSTEM ALERT] MACRO-LOOP DETECTED: You already delegated to '{skill_name}' with this EXACT context earlier in the session, and it failed or didn't resolve the issue. You MUST change your plan/context, use different instructions, or return a normal-text request for user input. DO NOT repeat the exact same delegation."
                
        previous_runs = ""
        attempt_count = 1
        for run in memgpt._skill_run_history:
            if run["skill"] == skill_name:
                previous_runs += (
                    f"--- Previous attempt {attempt_count} ---\n"
                    f"Context given: {str(run.get('context', ''))[:200]}...\n"
                    f"Result/Report: {_sanitize_skill_result_for_prompt(run.get('result', ''))}\n\n"
                )
                attempt_count += 1
                
        previous_runs_block = ""
        if previous_runs:
            previous_runs_block = f"\n[PREVIOUS ATTEMPTS HISTORY]\nYou have been called before in this session for the '{skill_name}' skill. Do NOT repeat failed approaches. Here are your previous attempts:\n{previous_runs}"

        prompt = f"RECENT CHAT HISTORY:\n{recent_history}{achievements_block}{previous_runs_block}\n\nMAIN AGENT'S REQUEST:\n{context}"
        worker_checkpoint = None
        try:
            from opalatex.config import get_git_strategy
            if get_git_strategy().lower() != "none":
                from opalatex.vcs import AsyncAgentTurnCheckpoint
                worker_checkpoint = AsyncAgentTurnCheckpoint(
                    project_path,
                    f"worker:{skill_name}",
                )
                await worker_checkpoint.begin()
        except Exception:
            worker_checkpoint = None
        from .tools import set_worker_context
        set_worker_context(True, model)
        tool_records = []
        try:
            out = await sub_agent.run(AgentInput(prompt=prompt))
            out_text = out.response if hasattr(out, "response") else str(out)
            tool_calls = getattr(out, "tool_calls_made", "?")
            tool_records = list(getattr(out, "tool_calls", None) or [])
            #print(f"\n{'-'*25} [DIAGNOSTIC: WORKER FINISHED] {'-'*25}")
            #print(f"[DIAGNOSTIC] Worker '{skill_name}' tool calls made: {tool_calls}")
            #print(f"[DIAGNOSTIC] Worker output preview ({len(out_text)} chars):\n{out_text[:300]}...")
            #print(f"{'-'*75}\n")
        except Exception as e:
            out_text = f"[CRITICAL WORKER CRASH] A exceção não tratada interrompeu o worker: {str(e)}"
            tool_calls = "?"
            print(f"[DIAGNOSTIC WORKER CRASH]: {e}")

        finally:
            set_worker_context(False)
            if worker_checkpoint is not None and worker_checkpoint.start_checkpoint:
                try:
                    checkpoint_saved = await worker_checkpoint.finalize()
                    if not checkpoint_saved:
                        from opalatex.agent_stdin import print_event
                        from opalatex.i18n import _
                        print_event("problem", {
                            "agent": f"worker:{skill_name}",
                            "severity": "error",
                            "message": _("checkpoint_finalize_failed"),
                        })
                except asyncio.CancelledError:
                    if not worker_checkpoint.finalize_result:
                        from opalatex.agent_stdin import print_event
                        from opalatex.i18n import _
                        print_event("problem", {
                            "agent": f"worker:{skill_name}",
                            "severity": "error",
                            "message": _("checkpoint_finalize_failed"),
                        })
                    raise
                except Exception:
                    from opalatex.agent_stdin import print_event
                    from opalatex.i18n import _
                    print_event("problem", {
                        "agent": f"worker:{skill_name}",
                        "severity": "error",
                        "message": _("checkpoint_finalize_failed"),
                    })

        #if "<<NEED_INPUT>>" in out_text:
        #    parts = out_text.split("<<NEED_INPUT>>", 1)
        #    user_prompt = parts[1].strip() if len(parts) > 1 else "Please provide required input:"
        #    user_response = ask_human(user_prompt)
        #    out_text = out_text.replace("<<NEED_INPUT>>", f"[User provided: {user_response}]")

        # Return the summary of what the worker did to MemGPT
        worker_summary = "\n".join(getattr(memgpt, "_current_worker_messages", []))
        if not worker_summary.strip():
            worker_summary = out_text
            tool_call_count = _tool_calls_count(tool_calls)
            # Se o worker calou a boca e o texto for genérico, alertar o orquestrador que ele pode ter estourado o limite.
            if (
                tool_call_count == 0
                or not worker_summary.strip()
                or "max iterations reached" in worker_summary.lower()
            ):
                worker_summary = f"[AVISO: O worker terminou sem um resumo claro. Ele realizou {tool_calls} chamadas de ferramenta e o último texto gerado foi: {out_text}]"

        # A serialized tool call is not a report. Neutralise it here, before the text
        # reaches the orchestrator's context, the stored history, or the next worker
        # prompt — otherwise every consumer downstream learns the broken pattern.
        worker_summary, had_tool_call_payload = _strip_serialized_tool_calls(worker_summary)
        if not had_tool_call_payload and _is_broken_serialization(worker_summary):
            worker_summary = _BROKEN_SERIALIZATION_MARKER

        memgpt._last_worker_summary = worker_summary

        # Record this run
        memgpt._skill_run_history.append({
            "skill": skill_name,
            "context": context,
            "result": worker_summary
        })

        # The report is the helper's own account; the tool errors are what the
        # runtime saw. Kept apart: the failure classification above reads only
        # the report, so an error the helper recovered from cannot turn a
        # successful run into a "failed" one for the loop breakers.
        return (
            f"[helper '{skill_name}' finished] {HELPER_REPORT_MARKER}\n"
            f"(Tool calls made by the helper: {tool_calls})\n{worker_summary}"
            f"{_helper_tool_error_notes(tool_records)}"
        )

    return run_skill


def _skill_script_paths(skill_dirs: list[str]) -> list[str]:
    """The `.py` scripts bundled with *skill_dirs*, in order, as absolute paths."""
    paths = []
    for directory in skill_dirs:
        scripts_dir = os.path.join(directory, "scripts")
        if os.path.isdir(scripts_dir):
            paths.extend(
                os.path.join(scripts_dir, f)
                for f in sorted(os.listdir(scripts_dir)) if f.endswith(".py")
            )
    return paths


def build_load_skill_tool(project_path: str, profile: str = "full"):
    """Return a ``load_skill`` tool: the instructions of a skill the main agent
    carries out itself (PROJECT_DESIGN 2.23).

    The body is rendered for the main agent's prompt *profile* (a skill may ship
    a condensed ``SKILL.<profile>.md``), with its parent's body first when it
    ``extends`` one, followed by the skill directory and its scripts. Loading
    only reads, so it is offered in every mode.
    """

    @as_tool(
        name="load_skill",
        description=(
            "Load the instructions of a skill listed under 'Skills you carry out yourself', then do the "
            "work with your own tools. Call it before starting a task that skill covers. Helpers are not "
            "loaded: delegate to them with run_skill."
        ),
    )
    async def load_skill(skill_name: str) -> str:
        active = [s for s in active_skills(project_path) if s["name"] != CHAT_ORCHESTRATOR_SKILL]
        loadable = [s["name"] for s in active if skill_role(s) == INSTRUCTIONS_ROLE]
        helpers = [s["name"] for s in active if skill_role(s) == HELPER_ROLE]
        if skill_name == CHAT_ORCHESTRATOR_SKILL:
            return f"[ERROR] '{CHAT_ORCHESTRATOR_SKILL}' is you; its instructions are already your own."
        meta_by_name = {s["name"]: s for s in active}
        if skill_name not in meta_by_name:
            return (
                f"[ERROR] Skill '{skill_name}' was not found / is not active. Skills you can load: "
                f"{loadable}. Helpers, delegated with run_skill: {helpers}. Never invent skill names, "
                "and never pass one of your own tools as one."
            )
        role = skill_role(meta_by_name[skill_name])
        if role == HELPER_ROLE:
            return (
                f"[ERROR] '{skill_name}' is a helper, not a skill you carry out: delegate the job "
                f"with run_skill('{skill_name}', context)."
            )
        if role != INSTRUCTIONS_ROLE:
            return (
                f"[ERROR] '{skill_name}' cannot be loaded: its SKILL.md declares role '{role}', and only "
                f"'{HELPER_ROLE}' or '{INSTRUCTIONS_ROLE}' is accepted. Tell the user the skill's "
                "SKILL.md needs fixing."
            )

        skill_dir = meta_by_name[skill_name]["dir"]
        meta = parse_skill_md(skill_dir, profile=profile)
        if meta is None or not meta.get("body"):
            return f"[ERROR] skill '{skill_name}' has no valid SKILL.md."
        body = meta["body"]
        dirs = [skill_dir]
        parent_name = meta.get("extends")
        if parent_name:
            parent_dir = find_skill_dir(parent_name, project_path)
            if parent_name not in meta_by_name or parent_dir is None:
                return f"[ERROR] skill '{skill_name}' requires parent '{parent_name}', which is not active."
            parent = parse_skill_md(parent_dir, profile=profile)
            if parent and parent.get("body"):
                body = parent["body"] + "\n\n" + body
            dirs.insert(0, parent_dir)

        scripts = _skill_script_paths(dirs)
        scripts_block = (
            "Scripts bundled with it (absolute paths; keep the double quotes, paths may contain spaces):\n"
            + "\n".join(f'  "{p}"' for p in scripts) + "\n"
            if scripts else ""
        )
        return (
            f"[skill '{skill_name}' loaded] Carry out these instructions yourself, with your own tools. "
            "Where they speak of the orchestrator's or a worker's context, that is the user's request "
            "in this conversation; where they speak of a report, that is your answer to the user.\n"
            f"Skill directory: {skill_dir}\n"
            f"{scripts_block}\n"
            f"{body}"
        )

    return load_skill


# ---------------------------------------------------------------------------
# Chat-orchestrator (the fixed MemGPT)
# ---------------------------------------------------------------------------

CHAT_HISTORY_SEED_LIMIT = 10
_SEEDED_ROLES = ("user", "assistant")


def restore_chat_orchestrator_state(memgpt: MemGPTAgentBlock, project, store) -> bool:
    """Restore the MemGPT working state saved for this chat.

    The orchestrator is rebuilt on every turn (the system prompt depends on mode,
    core memory and the active skills), so without this its context management —
    FIFO eviction and the recursive summary — would restart from scratch each time
    and only a raw slice of the chat would survive. Returns False when there is no
    saved state and the caller must seed from persisted history instead.
    """
    if store is None:
        return False
    chat_id = getattr(project, "current_chat_id", "") or ""
    if not chat_id:
        return False
    try:
        state = store.get_chat_agent_state(project.name, chat_id)
    except Exception:
        return False
    if not state or not state.get("internal_history"):
        # Seeding replays the visible history, which already holds every turn
        # that arrived from another device, so the queue has nothing to add.
        _discard_external_turns(project, store, chat_id)
        return False
    memgpt.load_state(state)
    _apply_external_turns(memgpt, project, store, chat_id)
    return True


# Turns read from the external queue by the restore of a chat, consumed when
# that chat's state is saved. Keyed by (project, chat): one orchestrator per
# chat runs at a time.
_EXTERNAL_TURNS_APPLIED: dict[tuple[str, str], int] = {}


def _discard_external_turns(project, store, chat_id: str) -> None:
    get = getattr(store, "get_chat_external_turns", None)
    consume = getattr(store, "consume_chat_external_turns", None)
    if get is None or consume is None:
        return
    try:
        count = len(get(project.name, chat_id))
        if count:
            consume(project.name, chat_id, count)
    except Exception:
        pass


def _apply_external_turns(memgpt: MemGPTAgentBlock, project, store, chat_id: str) -> None:
    """Bring turns made elsewhere into a restored context.

    `agent_state` is this machine's working memory; turns of the same chat made
    on another device reach the visible history through the cloud merge, which
    queues them (`cloud/chats.py`). They are appended after the restored history
    and removed from the queue only once the state that holds them is saved, so
    a turn that fails before saving loses nothing.
    """
    get = getattr(store, "get_chat_external_turns", None)
    if get is None:
        return
    try:
        turns = get(project.name, chat_id)
    except Exception:
        return
    if not turns:
        return
    # A restored state can end in a tool call left unanswered by an interrupted
    # turn; the compatibility layer repairs exactly that before every request,
    # and the turns have to land after the repaired unit, not inside it.
    from .litellm_compat import sanitize_agent_state

    sanitize_agent_state(memgpt)
    messages = [
        {"role": str(turn.get("role", "")), "content": _strip_progress_fence(turn.get("content", ""))}
        for turn in turns
        if turn.get("role") in _SEEDED_ROLES and isinstance(turn.get("content"), str)
    ]
    try:
        appended = memgpt.append_external_messages(messages)
    except ValueError:
        return
    if appended or not messages:
        _EXTERNAL_TURNS_APPLIED[(project.name, chat_id)] = len(turns)


def save_chat_orchestrator_state(memgpt: MemGPTAgentBlock, project, store) -> None:
    """Persist the MemGPT working state for this chat (best effort)."""
    if store is None or memgpt is None:
        return
    chat_id = getattr(project, "current_chat_id", "") or ""
    if not chat_id:
        return
    try:
        store.save_chat_agent_state(project.name, chat_id, memgpt.dump_state())
    except Exception:
        return
    applied = _EXTERNAL_TURNS_APPLIED.pop((project.name, chat_id), 0)
    if applied:
        try:
            store.consume_chat_external_turns(project.name, chat_id, applied)
        except Exception:
            pass


def derive_context_usage_from_state(project, store) -> dict | None:
    """Count the window a chat's *restored* context will occupy, or ``None``.

    A conversation that last ran before the measurement was persisted has no
    provider number stored, and the front-end's character estimate over the
    visible bubbles is not a usable stand-in: it cannot see the system prompt,
    the recursive summary, the tool calls or the tool results, so it draws an
    almost full battery for an almost full window.

    What *is* available is the thing that actually fills the window: the working
    state the orchestrator will restore on the next turn. Rebuilding the same
    message list ``MemGPTAgentBlock`` sends (system prompt + recursive summary +
    internal history) and counting it with the model's own tokenizer is the same
    measurement the ``source: "local"`` count already reports before each call --
    not an estimate of a different quantity. It is still not the provider's
    number: the tool JSON schemas travel outside ``messages``, and the probe
    carries no tools so their descriptions are absent from the rendered MemGPT
    rules. Both omissions make it read slightly low, which the ``source`` field
    records so the panel can say where the number came from.

    Returns ``None`` when there is no saved state, which is the honest answer for
    a chat that never ran.
    """
    if store is None or project is None:
        return None
    chat_id = getattr(project, "current_chat_id", "") or ""
    if not chat_id:
        return None
    try:
        state = store.get_chat_agent_state(project.name, chat_id)
    except Exception:
        return None
    if not state or not state.get("internal_history"):
        return None

    project_model = getattr(project, "model", None) or ""
    model = get_agent_model("memgpt", get_agent_model("chat_agent", project_model))
    try:
        probe = MemGPTAgentBlock(
            name="chat_orchestrator",
            system_prompt=chat_orchestrator_system_prompt(project, store),
            user_prompt_prefix=getattr(project, "user_prompt_prefix", ""),
            model=model,
        )
        probe.load_state(state)
        tokens = probe.count_context_tokens()
    except Exception:
        return None
    if tokens <= 0:
        return None

    from .config import resolve_effective_num_ctx

    record = {
        "prompt_tokens": tokens,
        "completion_tokens": 0,
        "total_tokens": tokens,
        "source": "state",
    }
    try:
        window = int(resolve_effective_num_ctx("memgpt", project_model) or 0)
    except Exception:
        window = 0
    if window > 0:
        record["context_window"] = window
    return record


_PROGRESS_FENCE_RE = re.compile(r"\A\s*```opalatex-progress\n.*?\n```\s*", re.DOTALL)


def _strip_progress_fence(content: str) -> str:
    """Drop the host's progress block before replaying an answer to the model.

    The fence is markup ``agent_stdin`` adds when it renders a turn, not anything
    the model wrote. A model reads its own last turn as an example and imitates it
    -- the empty-response marker taught that lesson the expensive way -- so
    replaying the markup would teach it to emit the markup. What it removes is the
    turn's progress chatter, never the answer, and the model's own working state
    keeps the chatter verbatim whenever that state is restored instead of seeded.
    """
    return _PROGRESS_FENCE_RE.sub("", str(content or ""), count=1)


def seed_chat_orchestrator_history(memgpt: MemGPTAgentBlock, project) -> None:
    """Seed the working context from persisted history.

    Only the visible conversation is replayed. ``system``/``tool`` rows are never
    seeded: a mid-history system message breaks chat templates that require the
    system message to come first (PROJECT_DESIGN 2.7), and a stored tool row would
    arrive without its ``tool_call_id``. Message content is replayed verbatim —
    prefixing a timestamp would teach the model to echo that prefix in its own
    answers; the current date is already supplied by the system prompt.
    """
    history = getattr(project, "history", None) or []
    for msg in history[-CHAT_HISTORY_SEED_LIMIT:]:
        role = msg.get("role", "")
        if role not in _SEEDED_ROLES:
            continue
        content = msg.get("content", "")
        if role == "assistant" and str(content).lstrip().startswith("Agent Error:"):
            continue
        # Chats written before the empty-response fix can hold the runtime's own
        # marker as an assistant message. Replaying it teaches the model to answer
        # with it, which is how it got persisted in the first place.
        if role == "assistant" and is_empty_response_placeholder(content):
            continue
        if role == "assistant":
            content = _strip_progress_fence(content)
        memgpt.internal_history.append({"role": role, "content": content})


_NATIVE_TOOL_CALL_REMINDER = (
    "\nUse the provider-native tool-calling protocol for actions. Never write a "
    "tool call, its arguments, or an action request as text: not as JSON, not as "
    "Markdown, and not as <tool_call> or <function=...> markup."
)


# LiteLLM routes that only ever point at a server the user runs themselves.
_SELF_HOSTED_ROUTE_PREFIXES = (
    "hosted_vllm/",
    "vllm/",
    "openai_like/",
    "lm_studio/",
    "llamafile/",
)


def _needs_native_tool_call_reminder(model: str) -> bool:
    """True for self-hosted models, which need the protocol spelled out.

    Self-hosted models are the ones observed printing a tool call as text instead
    of issuing it. Ollama is where it happens most -- LiteLLM's OpenAI-compatible
    `ollama/` route is the one a model without thinking enabled lands on -- but the
    same failure appears on any model the user serves themselves, including the
    vLLM/NIM/LM Studio servers that are reached through the plain `openai/` route
    rather than a dedicated one. Gating on `ollama` alone left those uncovered.

    Both the orchestrator and skill workers use this: the reminder used to exist
    for workers only, which left the agent that talks to the user -- and whose
    serialized output goes straight to the screen -- as the only one without it.
    """
    model = str(model or "")
    if model.startswith("ollama") or model.startswith("ollama_chat") or "local" in model:
        return True
    if model.startswith(_SELF_HOSTED_ROUTE_PREFIXES):
        return True
    # `openai/<vendor>/<model>`: a genuine OpenAI model id carries no second path
    # segment, so an extra one means the OpenAI-compatible route is aimed at a
    # self-hosted server, which names its models after their source repository
    # (`openai/nvidia/Qwen3.6-35B-A3B-NVFP4`). Providers that legitimately use a
    # vendor path have their own prefix (`openrouter/`, `together_ai/`) and so are
    # untouched by this.
    return model.startswith("openai/") and "/" in model[len("openai/"):]


def _orchestrator_body_variant(skill_dir: str, profile: str) -> str:
    """Pick which `SKILL.<variant>.md` body the main agent should load.

    Only the prompt *profile* (full/light) selects the body. The helper policy
    used to select a body too, when "delegate" withheld the write tools and the
    whole prompt had to say so; both policies now hold the same tools, and what
    differs between them is rendered as one block of the prompt
    (`_HELPER_POLICY_BLOCKS`). A missing variant file falls back to `SKILL.md`.
    """
    if profile and profile != "full" and os.path.isfile(os.path.join(skill_dir, f"SKILL.{profile}.md")):
        return profile
    return "full"


def _chat_orchestrator_body(project_path: str, profile: str = "full") -> str:
    """Return the chat-orchestrator SKILL.md body for *profile*, or a fallback.

    *profile* selects an optional `SKILL.<profile>.md` file next to the
    canonical `SKILL.md` (see `_orchestrator_body_variant` and
    `opalatex/prompt_profiles.py`). The fallback text below also doubles as the
    light profile's safety net when no `SKILL.light.md` is bundled yet.
    """
    skill_dir = find_skill_dir(CHAT_ORCHESTRATOR_SKILL, project_path)
    if skill_dir:
        variant = _orchestrator_body_variant(skill_dir, profile)
        meta = parse_skill_md(skill_dir, profile=variant)
        if meta and meta["body"]:
            return meta["body"]
    return (
        "Execute actions only through native tool calls.\n"
        "1. The runtime prepends today's date to this prompt. You MUST use web_search before answering, refusing, or delegating whenever (a) the request is time-sensitive (recent, latest, current, future-dated), (b) it names a concept, entity, acronym, paper or product you do not confidently recognize or that looks misspelled, or (c) it needs exact external data you cannot recall verbatim. You MUST NOT hallucinate dates or assume something did not happen without first searching the web.\n"
        "2. Return the final user-facing answer as normal text. JSON and Markdown in text are answers, never tool calls; use native tool calls only when executing an action.\n"
        "3. You are the main agent: you do the user's task yourself. Use your tools (search_code, find_symbol, code_outline, read_file, read_content_pos, get_editor_state, write_file, edit_file, replace_content_range, write_content_pos, web_search, get_project_overview, search_conversation_history) to investigate and to make every change. "
        "write_file is the only tool that creates a new file; edit_file, write_content_pos and replace_content_range require the file to already exist. "
        "To change existing text, prefer edit_file: it finds the passage by its exact text, so an earlier edit cannot shift it the way line numbers shift. "
        "You also run commands yourself with run_command (non-interactive), run_python_script, run_interactive_command (commands that prompt the user) and run_background_command (servers and other long-running processes). "
        "Compile LaTeX with compile_latex (Tectonic, the IDE's engine), never by looking for pdflatex in the shell.\n"
        "4. If the user asks about something you do not know, search for it with web_search instead of guessing or replying that you have no information about it - a term missing from your training data is a reason to search, not a reason to refuse. If the user asks for something in the project, use get_project_overview to explore the project structure and read_file to read files.\n"
        "5. Whenever the user asks a question involving dates, time, recent events, latest events, sports, news, public figures, APIs, niche terminology, or potentially anachronistic information, you must search the web for updated information. Search public/external knowledge only: workspace questions go to the project read tools, and anything depending on the user's preference goes to ask_question.\n"
        "6. Skills come in two kinds, listed below. A skill under 'Skills you carry out yourself' is loaded with load_skill and then done by you. A helper under 'Helpers' is an ephemeral agent you call with run_skill for an auxiliary job -- reading or summarising a long document, extracting, condensing a log, researching the web; it returns data and never changes project files. CRITICAL: use only names printed in those lists. NEVER invent skill names like 'search_files', 'list_files', 'modify_file', or 'run_cmd'.\n"
        "7. AFTER a helper finishes, you receive its report and, when any of its tool calls failed, the runtime's list of those failures and what the helper did next. The report says what the helper achieved; weigh the failures against it, and check anything you will act on. If the job was NOT done, you MAY call run_skill again with a revised context unless a loop breaker tells you to stop.\n"
        "8. Every invocation of run_skill spawns a completely stateless, ephemeral helper with no memory of prior runs. Provide all instructions and details in a single run_skill call.\n"
        "9. To edit a large file (more than ~100-200 lines), never rewrite it whole with write_file: output limits truncate the tool call. Locate the passage with search_code, then use edit_file, replace_content_range, write_content_pos, or a small Python search-and-replace script run with run_python_script.\n"
        "10. For large structured data or log files (.jsonl, .csv, .tsv, .log), delegate to a data/log helper if one is listed. If none is, never attempt whole-file read_file; sample with read_content_pos or run a short streaming Python script yourself.\n"
        "NEVER assume something didn't happen without first searching the web using the web_search tool.\n"
        "CRITICAL: If you use a <think> block to plan your actions, continue until you either make a native tool call or provide a non-empty final text response.\n\n"
        "ACHIEVEMENTS MEMORY INSTRUCTION:\n"
        f"{ACHIEVEMENTS_POLICY}\n"
    )


def chat_orchestrator_system_prompt(project, store=None) -> str:
    """Render the chat-orchestrator system prompt for a project.

    Extracted from ``build_chat_orchestrator`` so a caller that only needs the
    prompt -- measuring how full a restored context is, for instance -- does not
    have to build the whole agent. Deliberately free of side effects: unlike the
    builder, it never calls ``set_project_context``, which rescopes the global
    file/terminal tools and must not be triggered by a read-only request while
    another project's turn is running.
    """
    project_path = getattr(project, "project_path", "") or os.getcwd()
    project_model = getattr(project, "model", None) or ""
    project_worker = getattr(project, "worker_model", "") or project_model

    # Resolved early because the prompt profile (full/light) depends on the
    # orchestrator's model and, in turn, decides which skill-body variant and
    # mode-instructions rendering to use below.
    model = get_agent_model("memgpt", get_agent_model("chat_agent", project_model))
    from .config import model_prompt_profile, model_orchestrator_policy
    from .prompt_profiles import get_profile
    orchestrator_profile = model_prompt_profile(model)
    orchestrator_policy = model_orchestrator_policy(model)
    profile_spec = get_profile(orchestrator_profile)

    skills = active_skills(project_path)
    # Each front-end is told about its own affordances only: the desktop chat
    # refuses the terminal commands, and the terminal has no editor for
    # `get_editor_state` (opalatex/prompt_surface.py).
    from .prompt_surface import body_for_surface
    body = body_for_surface(_chat_orchestrator_body(project_path, orchestrator_profile))

    # The catalog lists what run_skill and load_skill actually accept: never the
    # main agent's own entry, which is not a target -- listing it once rendered
    # a bare "- chat-orchestrator:" that a model filled in by guessing.
    catalog = _skill_catalog(
        [s for s in skills if s.get("name") != CHAT_ORCHESTRATOR_SKILL],
        project_model,
        getattr(project, "worker_model", "") or "",
        project,
    )
    project_name = getattr(project, "project_name", "") or getattr(project, "name", "(unknown)")
    project_desc = getattr(project, "description", "") or ""
    project_mode = getattr(project, "mode", "auto") or "auto"
    
    # Load the appropriate core memory based on sharing mode
    use_shared = getattr(project, "use_shared_memory", False)
    if use_shared:
        core_memory = getattr(project, "core_memory", "") or ""
    else:
        # Per-chat isolated core memory
        chat_id = getattr(project, "current_chat_id", "main")
        if store:
            core_memory = store.get_chat_core_memory(project.name, chat_id) or ""
        else:
            core_memory = ""

    mode_instructions = profile_spec["mode_instructions"](project_mode)

    project_block = (
        f"## Current Project\n"
        f"- **Name**: {project_name}\n"
        f"- **Path**: {project_path}\n"
        f"- **Model**: {project_model}\n"
        f"- **Helper Model** (helpers that do not name their own): {project_worker}\n"
        f"- **Mode**: {project_mode}\n"
        f"{mode_instructions}"
    )
    if project_desc:
        project_block += f"- **Description**: {project_desc}\n"
    if core_memory:
        project_block += (
            f"\n### Core Memory (persisted facts from previous conversations)\n"
            f"**IMPORTANT**: The entries below represent YOUR persistent memory — things you learned "
            f"from prior conversations with the user across different chat sessions. "
            f"Treat these as facts you already know. When the user asks if you remember something "
            f"or if you have talked before, refer to this section.\n\n"
            f"{core_memory}\n"
        )

    # The tutorial chat carries the OpalaTex usage guide in the system prompt rather
    # than in core memory or a seeded internal_history: per-chat core memory is only
    # read when the project does not share memory, and a seeded history would be
    # evicted and summarized away exactly when the user is still asking questions.
    # Scoped to that one chat, so no other conversation pays context for it.
    tutorial_block = ""
    if getattr(project, "current_chat_id", "") == tutorial_chat_id(project.name):
        try:
            from .tutorial import tutorial_system_block
            from .ui_settings import load_ui_settings
            tutorial_block = "\n" + tutorial_system_block(
                load_ui_settings().get("lang", "pt")
            )
        except Exception:
            tutorial_block = ""

    # The orchestrator's own model decides this, not the worker's: an Ollama-served
    # orchestrator that prints a tool call as text sends that JSON straight to the
    # user, since nothing downstream can turn it back into an action.
    native_tool_block = (
        _NATIVE_TOOL_CALL_REMINDER + "\n"
        if _needs_native_tool_call_reminder(model) else ""
    )

    from .agent_stdin import REPLY_SURFACE_TERMINAL, reply_surface
    surface_block = (
        _TERMINAL_REPLY_BLOCK if reply_surface() == REPLY_SURFACE_TERMINAL
        else desktop_workspace_block()
    )

    from .monitors.agent_tools import MONITORS_PROMPT_BLOCK

    system_prompt = (
        f"{_current_date_instruction()}\n\n"
        f"{body}\n\n"
        f"{project_block}\n"
        f"{catalog}\n"
        f"{_HELPER_POLICY_BLOCKS[orchestrator_policy]}\n"
        f"{MONITORS_PROMPT_BLOCK}"
        f"{native_tool_block}"
        f"{tutorial_block}"
        f"{surface_block}"
    )
    return system_prompt


#: Appended when the replies are read in a terminal (`agent_stdin.reply_surface`).
#: The skill bodies are written for the desktop chat, which renders Markdown and
#: typesets math; in a terminal every `**`, `$\frac{a}{b}$` and `![img](path)`
#: is printed literally. Last in the prompt so it wins over the skill body's
#: "display images with Markdown" instruction. It governs the chat text only:
#: the files the agent writes keep whatever syntax their format requires.
_TERMINAL_REPLY_BLOCK = (
    "\n## Where your replies are read: a plain-text terminal\n"
    "The user is talking to you through the OpalaTex command-line interface. Your text "
    "is printed exactly as written and nothing is rendered: Markdown, HTML and LaTeX math "
    "show up as literal characters. This overrides any instruction above about formatting "
    "replies or displaying images.\n"
    "- Write plain prose. Structure with short paragraphs and simple `-` lists. Do not use "
    "Markdown headings, bold or italics, tables, links, or image embeds (`![...](...)`); "
    "name a file by its project-relative path instead.\n"
    "- Do not typeset mathematics in replies (no `$...$`, `\\(...\\)`, `\\[...\\]`). Write "
    "formulas in readable plain text with Unicode where it helps, e.g. α² + β ≤ 1, "
    "∑ᵢ xᵢ / n, sqrt(x).\n"
    "- When the user has to see source itself (a LaTeX snippet to paste, the exact lines "
    "you changed, a command), put it in a fenced code block; it is shown verbatim.\n"
    "- This is about your chat text only. Files you write keep the syntax their format "
    "requires: .tex stays LaTeX, .md stays Markdown.\n"
)


#: The parts of the desktop window and the orchestrator tool that reaches each,
#: as (tool, line). The skill bodies describe each tool on its own; this is the
#: map of the place they all act on, so the agent knows that compiling reloads
#: the pane the user is looking at and that its terminal is the user's terminal.
_DESKTOP_WORKSPACE_PARTS = (
    ("get_editor_state",
     "**Editor tabs** — what the user has open, which tab is focused and what is selected: "
     "`get_editor_state` reads them."),
    ("open_file",
     "`open_file` puts a file on screen (optionally at a line) or adds a background tab, and "
     "`close_file` closes one. They change the window, never the file."),
    ("compile_latex",
     "**PDF pane** — `compile_latex` is the editor's own Compile: the pane beside the `.tex` "
     "reloads with the new PDF, and a failure fills its error card and the Problems panel, exactly "
     "as when the user clicks Compile."),
    ("reveal_in_pdf",
     "`reveal_in_pdf` scrolls the PDF pane to where a source line was typeset and highlights it "
     "(SyncTeX forward search), with the `.tex` focused at that line beside it."),
    ("get_problems",
     "**Problems panel** — the failures the window shows, including a compile the user ran "
     "themselves: `get_problems` reads it."),
    ("read_terminal",
     "**Main terminal** — the user's own shell tab: `read_terminal` reads its last lines."),
    ("run_background_command",
     "`run_background_command` types a long-running command into that terminal; read its output "
     "with `read_terminal`."),
    ("run_interactive_command",
     "**Interactive terminal popup** — `run_interactive_command` opens one for a command that "
     "asks the user something."),
    ("set_layout",
     "**Layout** — `set_layout` switches the window between its layouts (editor, studio, document, "
     "chat, review)."),
)


def desktop_workspace_block() -> str:
    """The map of the desktop window, for the tools the main agent holds.

    Rendered only on the desktop surface. The main agent holds every part under
    both policies (PROJECT_DESIGN 2.23).
    """
    from .prompt_surface import GUI_ONLY_TOOLS
    from .tools import get_workspace_action_tools

    held = set(GUI_ONLY_TOOLS)
    held |= {str(getattr(tool, "name", "")) for tool in get_workspace_action_tools()}
    parts = [f"- {line}" for tool, line in _DESKTOP_WORKSPACE_PARTS if tool in held]
    return (
        "\n## The IDE you work in\n"
        "You run inside the OpalaTex desktop IDE, and the user is watching the same window while "
        "you work. Its parts are within your reach:\n"
        + "\n".join(parts) + "\n"
        "Use the window the way a colleague at the user's screen would: when you finish changing "
        "a file the user will want to look at, open it focused at the line that matters, or "
        "reveal that line in the PDF when the typeset result is what they care about. Do not "
        "hop between tabs as a way of reading — `read_file` does that without moving the user's "
        "view — and close only tabs you opened or were asked to close. The layout is the user's "
        "choice: change it only when they ask or when what you are showing cannot be seen in the "
        "current one.\n"
    )


def build_chat_orchestrator(project, store=None) -> MemGPTAgentBlock:
    """Build the fixed MemGPT chat-orchestrator for a project.

    The system prompt = chat-orchestrator SKILL.md body + Level-1 metadata of the
    active skills. Tools = run_skill + the memory tools. Uses the framework
    MemGPTAgentBlock (classic memory) per docs/specs/04 §1.
    """
    from .tools import (
        read_core_memory, append_core_memory, search_conversation_history,
        set_project_context,
    )

    project_path = getattr(project, "project_path", "") or os.getcwd()
    # An unconfigured project keeps an empty model here. The orchestrator is still
    # built (so the project can be opened and configured), but handle_run refuses to
    # run it instead of silently substituting DEFAULT_MODEL for the user's choice.
    project_model = getattr(project, "model", None) or ""
    project_worker = getattr(project, "worker_model", "") or project_model

    # Scope all file/terminal tools to the project directory. Without this,
    # get_project_path() falls back to the cwd (the OpalaTex repo root) and the
    # sub-agent's write_file/run_command would act outside the project.
    set_project_context(project, store)

    system_prompt = chat_orchestrator_system_prompt(project, store)
    # Resolved again here (cheap and pure) because the LiteLLM kwargs and the
    # thinking route below need the model, not just the prompt.
    model = get_agent_model("memgpt", get_agent_model("chat_agent", project_model))

    # A chat may ask for a different reasoning effort than its model's catalog
    # entry (the composer's Reasoning selector, `/effort` in the CLI). It applies
    # to this chat's orchestrator here and to its `run_skill` workers in
    # `build_run_skill_tool`.
    _llm_kwargs = get_agent_llm_kwargs(
        "memgpt",
        reasoning_effort_override=getattr(project, "chat_reasoning_effort", "") or None,
    )
    
    model_params = getattr(project, "model_params", {}) or {}
    enable_achievements = model_params.get("enable_achievements", True)
    
    from .agent_stdin import wrap_tool
    from .tools import create_plan, ask_question, get_code_navigation_tools, get_diagnostic_tools, get_ide_control_tools

    # Read/answer tools the main agent holds in every mode. The memory tools
    # write, but only to core memory -- never to the user's files. Neither do
    # the IDE control tools: they move the window, not the project. `load_skill`
    # only reads a skill's instructions, so planning can use it too.
    from .config import model_prompt_profile
    orchestrator_tools = [
        wrap_tool(ask_question),
        wrap_tool(read_core_memory),
        wrap_tool(read_file),
        wrap_tool(read_content_pos),
        wrap_tool(get_editor_state),
        wrap_tool(search_code),
        *(wrap_tool(tool) for tool in get_code_navigation_tools()),
        wrap_tool(get_project_overview),
        wrap_tool(append_core_memory),
        wrap_tool(search_conversation_history),
        wrap_tool(web_search),
        wrap_tool(analyze_image),
        *(wrap_tool(tool) for tool in get_diagnostic_tools()),
        *(wrap_tool(tool) for tool in get_ide_control_tools()),
        wrap_tool(build_load_skill_tool(project_path, model_prompt_profile(model))),
    ]

    # The terminal has no editor to report, and the snapshot a desktop session
    # staged earlier is not what a terminal user is looking at. Filtered here,
    # at the one place both role lists are composed from
    # (opalatex/prompt_surface.py).
    from .prompt_surface import tools_for_surface
    orchestrator_tools = tools_for_surface(orchestrator_tools)

    # Monitors (PROJECT_DESIGN 2.24). Listing, pausing and cancelling are safe;
    # registering is gated like any execution, since a monitor keeps reaching the
    # network or running a command with nobody watching.
    from .monitors.agent_tools import build_monitor_tools
    orchestrator_tools.extend(wrap_tool(tool) for tool in build_monitor_tools())

    # `create_plan` halts the turn on an approval dialog, which only means
    # something where the mode is "propose, then execute". In `auto` the mode's
    # own contract is that action is pre-authorized *without* confirmation
    # dialogs, and in `edit` the affordance for a question is `ask_question`; the
    # tool's description has always said "in plan mode", and a model that reached
    # for it anyway in `auto` answered a request to *show* a plan by opening an
    # approval dialog and then summarising what it had shown. Withheld by
    # composing the list, for the reason given below: the shared mode gate in
    # `opalatex_tool` wraps the same function object the helper uses.
    planning_tools = [*orchestrator_tools, wrap_tool(create_plan)]

    # The main agent does the task, so it holds the workspace action tools under
    # both policies (PROJECT_DESIGN 2.23); the policy only decides how much
    # auxiliary work goes to helpers, which is a matter for its prompt. The list
    # comes from `tools.get_workspace_action_tools()` so it cannot drift from
    # what the tools module calls an action. The command-execution tools are
    # `is_safe=False`, so plan mode still blocks them and edit mode still asks
    # the user.
    #
    # Recovery advice in tools.py branches on whether the caller can run
    # commands (PROJECT_DESIGN 2.6): the main agent can in every mode but plan.
    from .tools import set_orchestrator_terminal_access, get_workspace_action_tools
    set_orchestrator_terminal_access(project.mode != "plan")
    orchestrator_tools.extend(
        wrap_tool(tool) for tool in get_workspace_action_tools()
    )
    if enable_achievements:
        from .tools import update_achievements_memory
        orchestrator_tools.append(wrap_tool(update_achievements_memory))
        planning_tools.append(wrap_tool(update_achievements_memory))

    from .config import resolve_model_route, resolve_effective_num_ctx
    model = resolve_model_route(model, _llm_kwargs)


    # Strip /v1 from the end because Ollama native providers expect the root URL
    if _llm_kwargs.get("api_base"):
        if model.startswith("ollama/") or model.startswith("ollama_chat/"):
            if _llm_kwargs["api_base"].endswith("/v1"):
                _llm_kwargs["api_base"] = _llm_kwargs["api_base"][:-3]
            elif _llm_kwargs["api_base"].endswith("/v1/"):
                _llm_kwargs["api_base"] = _llm_kwargs["api_base"][:-4]
                
    _agent_params = get_project_agent_params()

    memgpt = MemGPTAgentBlock(
        name="chat_orchestrator",
        system_prompt=system_prompt,
        model=model,
        tools=orchestrator_tools,
        model_kwargs=_llm_kwargs,
        # A guardrail against a runaway loop, not a budget the model has to
        # ration: the turn continues for as long as tool calls are pending, and a
        # turn that legitimately speaks before each of several actions costs more
        # steps than the old accounting assumed.
        max_heartbeats=_agent_params.get("max_heartbeats", get_agent_max_heartbeats("memgpt", 50)),
        # Bounds the one thing that can spin under a single-channel protocol: a
        # model asking to keep its turn open without ever acting.
        max_idle_heartbeats=int(
            _agent_params.get(
                "max_idle_heartbeats",
                model_params.get("max_idle_heartbeats", 2),
            )
        ),
        # Not _llm_kwargs.get("num_ctx", ...): that dict is the *sanitized*
        # LiteLLM request, and sanitize_litellm_kwargs_for_model deliberately
        # strips num_ctx for non-Ollama providers before it reaches here (cloud
        # endpoints reject the param). resolve_effective_num_ctx computes the
        # true effective window independent of what is actually sent over the
        # wire, so a cloud model's catalog num_ctx is not silently lost.
        max_context_tokens=_agent_params.get("max_context_tokens", resolve_effective_num_ctx("memgpt")),
        # Start evicting before the window is completely full: at 1.0 there is no
        # headroom left for the model's own answer once the request is assembled.
        eviction_threshold=_agent_params.get("eviction_threshold", 0.85),
        memory_pressure_threshold=_agent_params.get("memory_pressure_threshold", 0.7),
        # Warn the model that its step budget is running out while it still has
        # steps to react: a turn that hits max_heartbeats is cut off mid-work and
        # the user has to resume it by hand.
        heartbeat_pressure_threshold=float(
            _agent_params.get(
                "heartbeat_pressure_threshold",
                model_params.get("heartbeat_pressure_threshold", 0.75),
            )
        ),
        debug=_agent_params.get("debug", False),
        use_shared_router=_agent_params.get("use_shared_router", True),
        loop_detection=_agent_params.get("loop_detection", model_params.get("loop_detection", True)),
        loop_detection_limit=_agent_params.get(
            "loop_detection_limit", model_params.get("loop_detection_limit", 3)
        ),
        # Opt-in: when the model writes its whole answer in the reasoning channel and
        # leaves the visible one empty, publish the reasoning instead of spending a
        # heartbeat asking for it again (PROJECT_DESIGN 2.7).
        empty_response_reasoning_fallback=bool(
            _agent_params.get(
                "empty_response_reasoning_fallback",
                model_params.get("empty_response_reasoning_fallback", False),
            )
        ),
    )
    from .litellm_compat import wrap_agent_litellm_compat
    from .token_usage import attach_usage_tracking
    memgpt.user_prompt_prefix = project.user_prompt_prefix
    wrap_agent_litellm_compat(memgpt)
    # Report real context occupancy for the CLI path too, not only for runs that
    # go through agent_stdin.
    attach_usage_tracking(memgpt)

    from opalatex.agent_stdin import _record_turn_thought, print_event
    
    if _llm_kwargs.get("stream", False):
        orchestrator_thought_chunks = []

        def _orch_on_thinking(chunk: str) -> None:
            if _record_turn_thought(chunk):
                orchestrator_thought_chunks.append(chunk)
                print_event("thought", {"content": chunk, "agent": "orchestrator", "_thought_recorded": True})

        def _orch_emit_visible(text: str) -> None:
            print_event("stream_chunk", {"content": text, "agent": "orchestrator"})

        def _orch_retract_visible(text: str) -> None:
            print_event("stream_retract", {"content": text, "agent": "orchestrator"})

        orch_think_splitter = InlineReasoningStreamSplitter(
            on_visible=_orch_emit_visible,
            on_thinking=_orch_on_thinking,
            on_retract=_orch_retract_visible,
        )

        def _orch_on_iteration(_step: int, _messages: list) -> None:
            orch_think_splitter.begin_response()

        # `_llm_kwargs["think"]` is set only for a model whose catalog entry
        # declares `supports_thinking` (config.resolve_think_request); that is both
        # the provider's parsing switch and the reason there is anything to show.
        if _llm_kwargs.get("think"):
            memgpt.on_thinking = _orch_on_thinking
        memgpt.on_chunk = orch_think_splitter.feed
        memgpt.on_iteration = _orch_on_iteration

    if not restore_chat_orchestrator_state(memgpt, project, store):
        seed_chat_orchestrator_history(memgpt, project)

    # run_skill is bound to this MemGPT instance (interceptor needs its history)
    # and to the project (so it can re-scope file tools on each call).
    run_skill = build_run_skill_tool(
        memgpt, 
        project.project_path,
        project_model=project.model,
        project_worker=project.worker_model,
        _project_ref=project,
        _store_ref=store,
    )
    execution_tools = [*orchestrator_tools, wrap_tool(run_skill)]

    prompt_mode = str(getattr(project, "mode", "auto")).strip().lower()

    def available_tools():
        nonlocal prompt_mode
        mode = str(getattr(project, "mode", "auto")).strip().lower()
        if mode != prompt_mode:
            memgpt.system_prompt = chat_orchestrator_system_prompt(project, store)
            prompt_mode = mode
        planning = mode == "plan"
        set_orchestrator_terminal_access(not planning)
        return planning_tools if planning else execution_tools

    memgpt.tools_provider = available_tools
    memgpt.tools = list(available_tools())

    return memgpt
