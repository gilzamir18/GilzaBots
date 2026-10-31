"""The prompts every agent reads must agree with each other and with the runtime.

An audit of all prompt versions (the four chat-orchestrator variants, the full
and light profiles, the worker preamble, the framework rules, the runtime
corrections and the inline editor) found instructions that contradicted the
code or each other, several of which cost heartbeats in real turns:

* the idle-allowance alert told a model that had just run tools that "no tool
  call was ever issued";
* the empty-response correction told the model it had "finished the requested
  work", and arrived in Portuguese or English depending on the UI language;
* the orchestrator was told "at most 1–3 tool calls per query" under a runtime
  budget of 20–100 heartbeats;
* achievements bookkeeping was a "MUST", every iteration -- the last heartbeat
  of two traced cut-short turns went to it;
* edit mode said "ask for permission first" over a gate that already asks;
* the worker was told its tools were 14 names while it held 24;
* the command list omitted `/clear_chat` and described `/clear` as far less
  destructive than it is;
* the framework prompt carried Portuguese fragments;
* the inline editor forbade fences and then showed fenced examples.

These tests pin the agreed rules so the versions cannot drift apart again.
"""
import asyncio
import os
import re
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ORCHESTRATOR_DIR = os.path.join(_REPO_ROOT, "skills", "chat-orchestrator")
_VARIANTS = ("SKILL.md", "SKILL.light.md")


def _variant(name):
    with open(os.path.join(_ORCHESTRATOR_DIR, name), encoding="utf-8") as f:
        return f.read()


# ── Runtime alerts and corrections ───────────────────────────────────────────

def _tc(call_id, name, arguments="{}"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _resp(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _idle_alert_after(script):
    from agenticblocks.blocks.llm.agent import AgentInput
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
    from agenticblocks.core.function_block import as_tool

    @as_tool(name="write_file", description="Write a file.")
    def write_file(path: str) -> str:
        return "written"

    agent = MemGPTAgentBlock(name="o", system_prompt="s", tools=[write_file],
                             max_heartbeats=30, max_idle_heartbeats=2)
    alerts = []

    async def fake(messages, **_kw):
        alerts.append([m["content"] for m in messages
                       if m.get("role") == "system" and "Your turn is ending now" in str(m.get("content"))])
        return script[min(len(alerts) - 1, len(script) - 1)]

    agent._acompletion = fake
    asyncio.run(agent.run(AgentInput(prompt="go")))
    return alerts[-1]


def test_the_idle_alert_does_not_deny_actions_that_ran():
    from agenticblocks.blocks.llm.memgpt_agent import HEARTBEAT_TOOL_NAME

    alert = _idle_alert_after([
        _resp("Editing.", [_tc("1", "write_file", '{"path": "a"}')]),
        _resp("Next.", [_tc("h1", HEARTBEAT_TOOL_NAME)]),
        _resp("Next again.", [_tc("h2", HEARTBEAT_TOOL_NAME)]),
        _resp("Wrote a."),
    ])

    assert len(alert) == 1
    assert "never issued" not in alert[0]
    assert "did run" in alert[0]


def test_the_idle_alert_still_says_so_when_nothing_ran():
    from agenticblocks.blocks.llm.memgpt_agent import HEARTBEAT_TOOL_NAME

    alert = _idle_alert_after([
        _resp("Next.", [_tc("h1", HEARTBEAT_TOOL_NAME)]),
        _resp("Next again.", [_tc("h2", HEARTBEAT_TOOL_NAME)]),
        _resp("I could not act."),
    ])

    assert "no tool call was ever issued" in alert[0]


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_runtime_corrections_are_english_and_claim_nothing(lang):
    from opalatex.agent_stdin import _empty_response_retry_prompt, _serialized_tool_call_retry_prompt
    from opalatex.i18n import set_lang

    set_lang(lang)
    try:
        empty = _empty_response_retry_prompt()
        serialized = _serialized_tool_call_retry_prompt()
    finally:
        set_lang("en")

    assert empty.startswith("Your last run ended without a user-facing response")
    assert "finished the requested work" not in empty, "the run may have stopped mid-work"
    assert "unfinished" in empty
    assert serialized.startswith("Your last message contained a tool call written as text")


# ── Framework rules ──────────────────────────────────────────────────────────

def test_the_framework_prompt_is_english_and_defines_the_heartbeat():
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
    from agenticblocks.core.function_block import as_tool

    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        return ""

    prompt = MemGPTAgentBlock(name="o", system_prompt="s", tools=[read_file],
                              tool_call_limits={"read_file": 2})._build_system_prompt()

    for fragment in ("Sem descri", "REGRAS", "chamada", "permitida"):
        assert fragment not in prompt
    assert "## AVAILABLE TOOLS" in prompt and "MEMORY TOOLS" not in prompt
    assert "[LIMIT: at most 2 call(s) per turn]" in prompt
    assert "each response of yours that carries tool calls spends exactly one" in prompt


def test_the_pressure_alert_counts_in_heartbeats():
    from agenticblocks.blocks.llm.memgpt_agent import _heartbeat_pressure_alert

    alert = _heartbeat_pressure_alert(3, 50)
    assert "3 of 50 heartbeats remain" in alert


# ── Orchestrator variants ────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_delegation_budget_counts_delegations_not_tool_calls(variant):
    text = _variant(variant)

    assert not re.search(r"1–3\*{0,2} tool calls", text), variant
    assert "1–3 call budget" not in text, variant
    assert re.search(r"1–3\*{0,2} `run_skill` calls", text), variant


@pytest.mark.parametrize("variant", _VARIANTS)
def test_achievements_are_never_a_step_of_their_own(variant):
    text = _variant(variant)

    assert "FREQUENTLY" not in text and "iteration concluded" not in text, variant
    assert "update_achievements_memory" in text
    assert re.search(r"only (in the same response as|alongside) the action it records", text), variant


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_command_list_matches_the_registry(variant):
    from opalatex.cli_commands import _registry

    text = _variant(variant)
    missing = [
        name for name in _registry._cmds
        if name not in ("/h", "/quit") and f"`{name}" not in text
    ]
    assert missing == [], f"{variant} omits {missing}"
    assert "/clear_chat" in text and "every" in text.split("/clear_chat", 1)[1][:400], (
        f"{variant} must say /clear wipes every chat and point at /clear_chat"
    )


def test_the_worker_and_fallback_achievements_policy_is_shared():
    import opalatex.memgpt_runtime as runtime
    from opalatex.prompt_profiles import ACHIEVEMENTS_POLICY, get_profile

    for profile in ("full", "light"):
        text = get_profile(profile)["achievements_instructions"]()
        assert ACHIEVEMENTS_POLICY in text
        assert "FREQUENTLY" not in text and "MUST record" not in text

    fallback_source = open(runtime.__file__, encoding="utf-8").read()
    assert "Use it FREQUENTLY" not in fallback_source


# ── Profiles ────────────────────────────────────────────────────────────────

def test_edit_mode_does_not_ask_for_a_permission_the_gate_already_asks():
    from opalatex.prompt_profiles import get_profile

    for profile in ("full", "light"):
        text = get_profile(profile)["mode_instructions"]("edit")
        assert "ask the user for permission first" not in text and "ask before terminal" not in text, profile
        assert "confirm" in text and "call them directly" in text, profile


def test_plan_mode_names_the_same_tools_in_both_profiles():
    from opalatex.prompt_profiles import get_profile

    tools = ("inspect_git", "inspect_project", "read_document", "search_code", "read_content_pos")
    for profile in ("full", "light"):
        text = get_profile(profile)["mode_instructions"]("plan")
        assert all(tool in text for tool in tools), profile


@pytest.mark.parametrize("profile", ["full", "light"])
def test_the_worker_tool_list_is_the_toolset_it_receives(profile):
    from opalatex.prompt_profiles import get_profile
    from opalatex.tools import get_helper_tools

    tools = get_helper_tools(vision=True)
    block = get_profile(profile)["worker_tools_block"](tools)

    missing = [t.name for t in tools if t.name not in block]
    assert missing == [], missing
    assert "Your specific tools" not in block
    assert "(e.g.\n" not in block, "a description must not be cut at an abbreviation"


def test_the_worker_intro_does_not_name_the_skill_in_either_profile():
    """The shared tail of the worker prompt names the skill once, for both profiles."""
    import inspect

    from opalatex.prompt_profiles import get_profile

    for profile in ("full", "light"):
        intro = get_profile(profile)["worker_intro"]
        assert not inspect.signature(intro).parameters, profile
        assert "Executing the" not in intro(), profile


def test_the_helper_write_rule_names_only_its_scratch_area():
    """A helper writes notes, never project files (PROJECT_DESIGN 2.23), so its
    prompt must not teach it to edit existing project files."""
    source = open(os.path.join(_REPO_ROOT, "opalatex", "memgpt_runtime.py"), encoding="utf-8").read()

    assert "ALWAYS use the write_file tool" not in source
    assert "Write scratch files (notes, plans, JSON) only through the file tools" in source
    assert "replace_content_range or write_content_pos to change part of an existing file" not in source


# ── Inline editor ────────────────────────────────────────────────────────────

def _inline_editor_prompt():
    app = open(os.path.join(_REPO_ROOT, "gui_src", "src", "App.jsx"), encoding="utf-8").read()
    start = app.index('"You are a precise inline content editor')
    end = app.index('"Your entire output must contain only the replacement source."', start)
    return app[start:end]


def test_inline_editor_examples_follow_its_own_fence_rule():
    prompt = _inline_editor_prompt()

    assert "Legacy fence" not in prompt
    # Only the example whose replacement contains a fenced block uses the envelope.
    examples = [line for line in prompt.splitlines() if "Original:" in line]
    fenced = [line for line in examples if "````content" in line]
    assert len(examples) >= 3 and len(fenced) == 1
    assert "```" in fenced[0].split("→", 1)[0], "the envelope is shown only for content with its own fence"
    assert "The one exception" in prompt


@pytest.mark.parametrize("reply, expected", [
    ("The system is functional.", "The system is functional."),
    ("$$\nE = mc^2\n$$", "$$\nE = mc^2\n$$"),
    ("````content\nRun the test suite:\n```bash\nnpm test\n```\n````", "Run the test suite:\n```bash\nnpm test\n```"),
])
def test_inline_editor_example_shapes_survive_the_backend(reply, expected):
    from opalatex.agent_stdin import _normalize_inline_fenced_replacement

    assert _normalize_inline_fenced_replacement(reply) == expected
