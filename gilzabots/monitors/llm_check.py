"""The "level 2" check: a model answers whether a monitor's condition holds.

Used only for conditions a deterministic check cannot express ("tell me when
there is news about X that involves Y"). It runs in a child process of the
daemon (``gilzabots monitors llm-check <id>``) because building an agent binds
the project's model and credentials in module globals, which the long-lived
scheduler must not carry from one project to the next.

The agent is read-only: ``web_search`` and a bounded page fetch, nothing that
writes or executes. It must answer with a JSON object; an answer that is not
one is a check error with the text quoted, never a guessed ``met``.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

from .checks import CheckError
from .process import gilzabots_command

LLM_CHECK_MAX_TOOL_CALLS = 8
LLM_CHECK_MAX_ITERATIONS = 10
FETCH_LIMIT_CHARS = 8000

SYSTEM_PROMPT = """You evaluate one condition for a background monitor in GilzaBots.
You are not talking to a person. Research the condition with the tools you have, then answer with exactly one JSON object and nothing else:
{"met": true or false, "value": "<the observation the decision rests on, one line>", "summary": "<one or two sentences a person can read in a desktop notification>"}
"met" is true only when the condition clearly holds now. When the evidence is missing or ambiguous, answer false and say why in "summary".
"""


def _strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_page_tool():
    from agenticblocks.core.function_block import as_tool

    @as_tool(
        name="fetch_page",
        description=(
            "Download a web page (http/https) and return its visible text, at most "
            f"{FETCH_LIMIT_CHARS} characters. Read-only."
        ),
    )
    def fetch_page(url: str) -> str:
        if not re.match(r"^https?://", str(url or ""), re.IGNORECASE):
            return "fetch_page: the URL must start with http:// or https://."
        request = urllib.request.Request(url, headers={"User-Agent": "GilzaBots-Monitor"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return f"fetch_page: could not download {url}: {exc}"
        text = _strip_tags(body)
        return text[:FETCH_LIMIT_CHARS] or "fetch_page: the page has no visible text."

    return fetch_page


def parse_answer(raw: str) -> dict:
    """The model's JSON verdict. Raises CheckError quoting what it wrote."""
    from gilzabots.translation import strip_model_reasoning

    text = strip_model_reasoning(str(raw or "")).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except ValueError:
        raise CheckError(f"The model did not answer with the JSON verdict: {text[:300]!r}") from None
    if not isinstance(data, dict) or not isinstance(data.get("met"), bool):
        raise CheckError(f"The model's verdict has no boolean 'met': {text[:300]!r}")
    return {
        "met": data["met"],
        "value": str(data.get("value", ""))[:500],
        "summary": str(data.get("summary", ""))[:1000],
    }


async def evaluate_in_process(monitor: dict) -> dict:
    """Ask the project's worker model. Runs inside the child process."""
    import agenticblocks.blocks.llm.agent as agent_mod
    from gilzabots.config import (
        get_agent_llm_kwargs,
        normalize_ollama_api_base_for_litellm,
        resolve_agent_model,
        resolve_model_route,
    )
    from gilzabots.litellm_compat import wrap_agent_litellm_compat
    from gilzabots.tools import set_project_context

    if monitor.get("project"):
        from gilzabots.project import ProjectStore

        store = ProjectStore(db_path=monitor.get("sessions_db") or None) if monitor.get("sessions_db") else ProjectStore()
        project = store.load(monitor["project"])
        if project is None:
            raise CheckError(f"The monitor's project '{monitor['project']}' no longer exists.")
        set_project_context(project, store)

    model = resolve_agent_model("worker")
    if not model:
        raise CheckError("No model is configured for the monitor's project.")
    kwargs = get_agent_llm_kwargs("worker", reasoning_effort_override="low")
    model = resolve_model_route(model, kwargs)
    if kwargs.get("api_base"):
        kwargs["api_base"] = normalize_ollama_api_base_for_litellm(model, kwargs["api_base"])

    tools = []
    if monitor["check"].get("use_web", True):
        from gilzabots.tools import web_search

        tools = [web_search, _fetch_page_tool()]
    previous = monitor.get("last_value")
    prompt = f"Condition to evaluate: {monitor['check']['question']}"
    if isinstance(previous, dict) and previous.get("summary"):
        prompt += f"\nWhat the previous check found: {previous['summary']}"
    agent = agent_mod.LLMAgentBlock(
        name="monitor_llm_check",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        model_kwargs=kwargs,
        tools=tools,
        max_tool_calls=LLM_CHECK_MAX_TOOL_CALLS,
        max_iterations=LLM_CHECK_MAX_ITERATIONS,
        on_max_iterations="return_last",
        final_answer_prompt="Answer now with the JSON verdict only.",
    )
    wrap_agent_litellm_compat(agent)
    result = await agent.run(agent_mod.AgentInput(prompt=prompt))
    return parse_answer(getattr(result, "final_text", "") or "")


async def run_llm_check_subprocess(monitor: dict) -> dict:
    """The daemon's runner: evaluate in a child process and read its verdict."""
    argv = gilzabots_command() + ["monitors", "llm-check", monitor["id"]]
    timeout = int(monitor["check"].get("timeout_seconds", 120)) + 30
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, **kwargs)

    try:
        completed = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        raise CheckError(f"The model did not decide within {timeout} s.") from None
    except OSError as exc:
        raise CheckError(f"The model check could not start: {exc}") from None
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise CheckError(
            "The model check produced no verdict"
            + (f": {completed.stderr.strip()[-500:]}" if completed.stderr.strip() else ".")
        )
    try:
        reply = json.loads(lines[-1])
    except ValueError:
        raise CheckError(f"The model check answered with something other than JSON: {lines[-1][:300]!r}") from None
    if not reply.get("ok"):
        raise CheckError(str(reply.get("error") or "The model check failed."))
    return reply["value"]
