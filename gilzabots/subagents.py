"""Read-only subagents the main agent starts with `run_agent` (PROJECT_DESIGN 2.30).

Claude Code's subagent, within GilzaBots' rule that only the main agent changes
the project (PROJECT_DESIGN 2.23): the main agent writes a task, and a fresh
agent -- no conversation, no skill body, only the task -- reads, searches and
researches with read tools only, then returns one report. Several
`run_agent` calls issued in one response run at the same time
(`concurrency_safe`, honoured by `MemGPTAgentBlock`), and a call with
`background=True` returns at once and is collected later with `wait_agents`.

A helper (`run_skill`) is the other kind of auxiliary agent: it runs a skill's
own instructions and may run that skill's scripts. A subagent runs none of that,
which is what makes it safe to run several at once, in every mode, without a
single confirmation dialog.
"""
from __future__ import annotations

import asyncio
import itertools
import os

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.core.function_block import as_tool

SUBAGENT_MODEL_WORKER = "worker"
SUBAGENT_MODEL_MAIN = "main"
SUBAGENT_MODELS = (SUBAGENT_MODEL_WORKER, SUBAGENT_MODEL_MAIN)

_ids = itertools.count(1)

#: Background subagents of the turn that is running: id -> (label, task).
_BACKGROUND: dict[str, tuple[str, asyncio.Task]] = {}


def _subagent_intro() -> str:
    return (
        "You are a subagent. The main agent -- the one that does the user's task and talks to the "
        "user -- started you with a fresh context for one job: the task below. You do not see its "
        "conversation, so the task is everything you know about the job.\n"
        "Your tools only read: you read files, search the project and the web, and inspect Git. You "
        "change nothing and run nothing. Where the job shows that something should change, describe "
        "the change in your report (path, passage, new text); the main agent decides and applies it.\n"
        "Finish with one report in normal text. It is the only thing the main agent receives, so it "
        "answers the task in full: findings with their source (file:line for project text, the URL "
        "for web results), and a plain statement of what you could not determine and why. Report "
        "only what a tool returned to you.\n"
        "Use provider-native tool calls for actions; never write a tool call as text.\n\n"
    )


def _model_for(project, choice: str) -> tuple[str, dict, dict]:
    """(catalog model id, LiteLLM kwargs, vision params) for a subagent on *choice*."""
    from .config import get_agent_llm_kwargs, get_agent_model, session_main_model, session_worker_model, DEFAULT_MODEL

    effort = getattr(project, "chat_reasoning_effort", "") or None
    project_model = session_main_model(project)
    if choice == SUBAGENT_MODEL_MAIN:
        model = get_agent_model("memgpt", get_agent_model("chat_agent", project_model))
        kwargs = get_agent_llm_kwargs("memgpt", reasoning_effort_override=effort)
        vision_params = dict(getattr(project, "model_params", None) or {})
        return model, kwargs, vision_params
    from .memgpt_runtime import _helper_vision_params

    worker = session_worker_model(project) or project_model or DEFAULT_MODEL
    model = get_agent_model("worker", worker)
    kwargs = get_agent_llm_kwargs("worker", reasoning_effort_override=effort)
    return model, kwargs, _helper_vision_params(project, model)


def _route(model: str, kwargs: dict) -> str:
    """The LiteLLM route for *model*, with the Ollama root URL the native route expects."""
    from .config import resolve_model_route

    routed = resolve_model_route(model, kwargs)
    base = kwargs.get("api_base")
    if base and (routed.startswith("ollama/") or routed.startswith("ollama_chat/")):
        if base.endswith("/v1/"):
            kwargs["api_base"] = base[:-4]
        elif base.endswith("/v1"):
            kwargs["api_base"] = base[:-3]
    return routed


async def run_subagent(project, task: str, label: str, model_choice: str, agent_id: str) -> str:
    """Run one subagent to completion and return its report for the main agent."""
    from .agent_stdin import print_event
    from .config import get_project_agent_params, model_prompt_profile, model_supports_vision
    from .litellm_compat import wrap_agent_litellm_compat
    from .memgpt_runtime import (
        _current_date_instruction,
        _helper_tool_error_notes,
        _NATIVE_TOOL_CALL_REMINDER,
        _needs_native_tool_call_reminder,
        _strip_serialized_tool_calls,
        project_instructions_block,
    )
    from .prompt_profiles import get_profile
    from .prompt_surface import tools_for_surface
    from .token_usage import attach_usage_tracking
    from .tools import get_subagent_tools, subagent_context
    from .agent_stdin import wrap_tool

    tag = f"subagent:{agent_id}" + (f" {label}" if label else "")
    model, kwargs, vision_params = _model_for(project, model_choice)
    vision = model_supports_vision(model, vision_params)
    profile = get_profile(model_prompt_profile(model))
    tools = tools_for_surface([wrap_tool(tool) for tool in get_subagent_tools(vision=vision)])
    project_path = getattr(project, "project_path", "") or os.getcwd()
    routed = _route(model, kwargs)

    system = (
        f"{_current_date_instruction()}\n\n"
        + _subagent_intro()
        + profile["worker_tools_block"](tools)
        + f"\nThe project directory is: {project_path}\n"
        + project_instructions_block(project_path)
        + (_NATIVE_TOOL_CALL_REMINDER if _needs_native_tool_call_reminder(routed) else "")
    )
    params = get_project_agent_params("worker")
    agent = LLMAgentBlock(
        name=f"subagent_{agent_id}",
        user_prompt_prefix=getattr(project, "user_prompt_prefix", ""),
        system_prompt=system,
        model=routed,
        tools=tools,
        model_kwargs=kwargs,
        use_shared_router=False,
        max_iterations=params.get("max_iterations", None),
        max_tool_calls=params.get("max_tool_calls", 40),
        loop_detection=params.get("loop_detection", True),
        loop_detection_limit=params.get("loop_detection_limit", 3),
    )
    wrap_agent_litellm_compat(agent)
    attach_usage_tracking(agent)
    object.__setattr__(agent, "model", routed)
    object.__setattr__(agent, "model_kargs", kwargs)
    object.__setattr__(agent, "model_kwargs", kwargs)

    # Its narration goes to the activity log only. Streaming it into the chat, as
    # helpers do, would interleave the text of subagents running at once.
    def _on_iteration(_step: int, messages: list) -> None:
        last = messages[-1] if messages else {}
        content = last.get("content") or ""
        if content:
            print_event("reflection", {"content": str(content), "agent": tag})

    agent.on_iteration = _on_iteration
    print_event("info", {"agent": tag, "message": f"Subagent {agent_id} started on {model}."})

    records = []
    try:
        with subagent_context(model):
            out = await agent.run(AgentInput(prompt=task))
        report = out.response if hasattr(out, "response") else str(out)
        records = list(getattr(out, "tool_calls", None) or [])
    except asyncio.CancelledError:
        print_event("info", {"agent": tag, "message": f"Subagent {agent_id} stopped."})
        raise
    except Exception as exc:  # the main agent decides what a failed subagent means
        print_event("problem", {"agent": tag, "severity": "error", "message": str(exc)})
        return f"[subagent {agent_id} failed] {exc}"

    report, _ = _strip_serialized_tool_calls(str(report or "").strip())
    print_event("info", {"agent": tag, "message": f"Subagent {agent_id} finished."})
    if not report:
        report = "(The subagent returned no report.)"
    title = f"[subagent {agent_id}" + (f" '{label}'" if label else "") + " finished]"
    return f"{title}\n{report}{_helper_tool_error_notes(records)}"


def build_subagent_tools(project) -> list:
    """`run_agent` and `wait_agents`, bound to *project*."""

    @as_tool(
        name="run_agent",
        concurrency_safe=True,
        description=(
            "Start a read-only subagent: a fresh agent that does one self-contained job and returns a "
            "report. It sees only the task you write -- not this conversation -- so put everything it "
            "needs in `task`: the question, paths, what to look for, the shape of the report. It reads "
            "files, searches the project and the web and inspects Git; it never changes files or runs "
            "commands, and it cannot ask the user anything. Use it to keep long reading out of your own "
            "context, to look at one question from independent angles, or to research while you work. "
            "Calls you issue in the same response run at the same time. `description` is a 3-5 word "
            "label shown to the user. `model` is 'worker' (default: the helper model) or 'main' (your "
            "own model, for judgement-heavy work such as a review). With `background=true` the call "
            "returns an id at once: keep working, then collect the report with wait_agents before you "
            "answer; a background subagent still running when your turn ends is stopped."
        ),
    )
    async def run_agent(task: str, description: str = "", model: str = SUBAGENT_MODEL_WORKER,
                        background: bool = False) -> str:
        choice = str(model or SUBAGENT_MODEL_WORKER).strip().lower()
        if choice not in SUBAGENT_MODELS:
            raise ValueError(f"model must be one of {list(SUBAGENT_MODELS)}, not {model!r}.")
        if not str(task or "").strip():
            raise ValueError("task is empty: write the whole job the subagent must do.")
        agent_id = f"a{next(_ids)}"
        label = " ".join(str(description or "").split())[:60]
        if not background:
            return await run_subagent(project, task, label, choice, agent_id)
        _BACKGROUND[agent_id] = (
            label, asyncio.create_task(run_subagent(project, task, label, choice, agent_id))
        )
        return (
            f"[subagent {agent_id} started in the background] Collect its report with "
            f"wait_agents(['{agent_id}']) before you answer."
        )

    @as_tool(
        name="wait_agents",
        description=(
            "Wait for background subagents started with run_agent(background=true) and return their "
            "reports. `agent_ids` lists the ids run_agent returned; empty waits for all of them."
        ),
    )
    async def wait_agents(agent_ids: list[str] | None = None) -> str:
        wanted = [str(i).strip() for i in (agent_ids or []) if str(i).strip()] or list(_BACKGROUND)
        if not wanted:
            return "No background subagent is running."
        unknown = [i for i in wanted if i not in _BACKGROUND]
        if unknown:
            raise ValueError(
                f"No background subagent has the id(s) {unknown}. Running: {sorted(_BACKGROUND)}. "
                "A report already collected is not returned twice."
            )
        tasks = [_BACKGROUND[i][1] for i in wanted]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        reports = []
        for agent_id, result in zip(wanted, results):
            _BACKGROUND.pop(agent_id, None)
            if isinstance(result, BaseException):
                reports.append(f"[subagent {agent_id} failed] {result}")
            else:
                reports.append(str(result))
        return "\n\n".join(reports)

    return [run_agent, wait_agents]


def cancel_background_agents() -> list[str]:
    """Stop the background subagents still running; return their ids.

    Called when a turn ends: the report of a subagent nobody collected has no
    reader, and one left running would keep spending the user's tokens and keep
    publishing events into a turn that is over.
    """
    stopped = []
    for agent_id, (_label, task) in list(_BACKGROUND.items()):
        if not task.done():
            task.cancel()
            stopped.append(agent_id)
    _BACKGROUND.clear()
    return stopped
