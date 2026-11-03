"""Tests for the main agent's helper policy (direct/delegate).

The main agent does the user's task, so it holds the workspace action tools --
file writes *and* command execution -- under both policies (PROJECT_DESIGN
2.23). The policy decides only how much auxiliary work it hands to helpers
through `run_skill`, and that is a block of its prompt. It is a per-model catalog
field (`gilzabots/models_store.py`) resolved by `config.model_orchestrator_policy`,
deliberately separate from `prompt_profile`: "light prompt + delegate" has to
remain expressible.

What the mode withholds (writes and commands in plan mode) is still enforced by
tool-list composition in `build_chat_orchestrator` and by the `gilzabots_tool`
gate, independently of the policy.
"""

import pytest

from gilzabots.memgpt_runtime import (
    build_chat_orchestrator,
    _orchestrator_body_variant,
)
from gilzabots.project import ProjectData


WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "write_content_pos",
    "replace_content_range",
    "create_docx_file",
    "create_pptx_file",
    # Generating an image produces a project file like any other write.
    "generate_image",
}

# Command execution is part of the same authority as writing: an agent allowed
# to rewrite main.tex but not to run pdflatex on it holds half an authority.
EXEC_TOOLS = {
    # Runs Tectonic and writes the PDF: execution authority, like a shell command.
    "compile_latex",
    "run_command",
    "run_python_script",
    "run_background_command",
    "run_interactive_command",
}


@pytest.fixture(autouse=True)
def _restore_terminal_flag():
    """Keep the global terminal-access flag from leaking between tests."""
    from gilzabots import tools

    original = tools._ORCHESTRATOR_HAS_TERMINAL
    yield
    tools.set_orchestrator_terminal_access(original)


def _catalog(tmp_path, monkeypatch, model_id, **fields):
    """Register *model_id* in an isolated model catalog and return a project."""
    from gilzabots import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    models_store.add_or_update_connection(
        {"id": "c1", "label": "c", "provider": "ollama", "api_key": "", "api_base": ""}
    )
    models_store.add_or_update_model(
        {"id": model_id, "connection_id": "c1", "name": model_id.split("/", 1)[-1], **fields}
    )
    return ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model=model_id, mode="auto",
    )


def _tool_names(project):
    return {getattr(t, "name", None) for t in build_chat_orchestrator(project, None).tools}


# ── The bug this policy work started from ────────────────────────────────────

def test_direct_orchestrator_can_create_a_file(tmp_path, monkeypatch):
    """Regression: the orchestrator had no way to create a file.

    With only write_content_pos and replace_content_range -- both of which
    require an existing file -- a model asked to create one had no valid call to
    make, and reached for write_content_pos, which fails with "file not found".
    """
    project = _catalog(tmp_path, monkeypatch, "ollama/direct-model")
    assert "write_file" in _tool_names(project)


def test_unregistered_model_defaults_to_direct_with_write_file(tmp_path, monkeypatch):
    """A model absent from the catalog keeps the shipped behavior, plus write_file."""
    from gilzabots import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/never-registered", mode="auto",
    )
    assert WRITE_TOOLS <= _tool_names(project)


# ── Tool composition: the same under both policies ───────────────────────────

@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_every_policy_keeps_every_write_and_execution_tool(tmp_path, monkeypatch, policy):
    """The main agent does the task, so delegate no longer withholds anything."""
    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    names = _tool_names(project)
    assert (WRITE_TOOLS | EXEC_TOOLS) <= names


@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_every_policy_keeps_reading_skills_and_helpers(tmp_path, monkeypatch, policy):
    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    names = _tool_names(project)
    assert {
        "run_skill", "load_skill", "read_file", "read_content_pos", "search_code",
        "get_project_overview", "ask_question",
    } <= names
    # Core-memory writes are not file writes and stay available.
    assert {"read_core_memory", "append_core_memory"} <= names


@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_plan_mode_withholds_writes_under_every_policy(tmp_path, monkeypatch, policy):
    """The mode decides whether writes exist; the policy never does."""
    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    assert "create_plan" not in _tool_names(project)

    project.mode = "plan"
    names = _tool_names(project)
    assert "create_plan" in names
    assert "load_skill" in names, "loading instructions only reads"
    assert not ((WRITE_TOOLS | EXEC_TOOLS) & names)


def test_the_main_agent_holds_the_whole_action_set(tmp_path, monkeypatch):
    """Composed from `tools.get_workspace_action_tools()`, asserted as a set
    identity so a tool added there can never silently skip the main agent."""
    from gilzabots.tools import get_workspace_action_tools

    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy="delegate")
    action_names = {t.name for t in get_workspace_action_tools()}

    assert action_names == WRITE_TOOLS | EXEC_TOOLS | {"export_tex_to_docx"}
    assert action_names <= _tool_names(project)


@pytest.mark.parametrize("tool_name,args", [
    ("run_command", ("rm -rf build",)),
    ("run_python_script", ("script.py",)),
    ("run_background_command", ("npm run dev",)),
    ("run_interactive_command", ("npm init",)),
])
def test_execution_tools_stay_unsafe_for_the_orchestrator(tmp_path, tool_name, args):
    """Granting the tools must not weaken the mode gate.

    The gate in `gilzabots_tool` wraps the same function objects for both roles,
    so a direct orchestrator inherits plan-mode refusal and edit-mode
    confirmation unchanged -- these tools are declared is_safe=False.
    """
    import asyncio
    from types import SimpleNamespace
    import gilzabots.tools as tools

    tools.set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan"))
    tool = getattr(tools, tool_name)
    raw = getattr(tool, "_func", None) or tool
    result = asyncio.run(raw(*args))

    assert result.startswith("Execution blocked: In 'plan' mode")


# ── Recovery advice follows the toolset, not the role ────────────────────────

@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_main_agent_is_told_to_convert_documents_itself(tmp_path, monkeypatch, policy):
    """Guidance must name a route this caller can take (PROJECT_DESIGN 2.6)."""
    from gilzabots.tools import _binary_read_error

    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    build_chat_orchestrator(project, None)

    message = _binary_read_error(str(tmp_path / "data.xls"))
    assert "run_command" in message
    assert "command-line" not in message


def test_main_agent_in_plan_mode_is_not_sent_to_a_skill(tmp_path, monkeypatch):
    """In plan mode commands and helpers are both blocked: asking is the route."""
    from gilzabots.tools import _binary_read_error

    project = _catalog(tmp_path, monkeypatch, "ollama/m")
    project.mode = "plan"
    build_chat_orchestrator(project, None)

    message = _binary_read_error(str(tmp_path / "data.xls"))
    assert "plan mode" in message
    assert "run_skill" not in message and "command-line" not in message


def test_helper_advice_ignores_the_main_agent_mode(tmp_path, monkeypatch):
    """A helper always has run_command, whatever the main agent holds now."""
    import gilzabots.tools as tools

    project = _catalog(tmp_path, monkeypatch, "ollama/m")
    project.mode = "plan"
    build_chat_orchestrator(project, None)
    tools.set_worker_context(True)
    try:
        message = tools._binary_read_error(str(tmp_path / "data.xls"))
    finally:
        tools.set_worker_context(False)

    assert "You have run_command" in message


# ── The two axes stay independent ────────────────────────────────────────────

def test_policy_and_profile_are_independent(tmp_path, monkeypatch):
    """"light prompt + delegate" is the combination worth protecting."""
    project = _catalog(
        tmp_path, monkeypatch, "ollama/small-local",
        prompt_profile="light", orchestrator_policy="delegate",
    )
    agent = build_chat_orchestrator(project, None)
    names = {getattr(t, "name", None) for t in agent.tools}

    assert WRITE_TOOLS <= names
    assert "Your helper policy: delegate" in agent.system_prompt
    assert "Mode: auto." in agent.system_prompt  # light mode-instructions
    assert "\U0001F6A8 **SYSTEM ALERT" not in agent.system_prompt


def test_light_profile_still_writes_directly_by_default(tmp_path, monkeypatch):
    """Choosing the light profile must not imply delegation."""
    project = _catalog(tmp_path, monkeypatch, "ollama/m", prompt_profile="light")
    assert WRITE_TOOLS <= _tool_names(project)
    assert "Your helper policy: direct" in build_chat_orchestrator(project, None).system_prompt


# ── Body variant selection ───────────────────────────────────────────────────

def _orchestrator_skill_dir():
    from gilzabots.memgpt_runtime import CHAT_ORCHESTRATOR_SKILL
    from gilzabots.skills import find_skill_dir

    skill_dir = find_skill_dir(CHAT_ORCHESTRATOR_SKILL, "")
    assert skill_dir, "chat-orchestrator skill must be discoverable"
    return skill_dir


@pytest.mark.parametrize("profile,expected", [("full", "full"), ("light", "light")])
def test_body_variant_selection(profile, expected):
    assert _orchestrator_body_variant(_orchestrator_skill_dir(), profile) == expected


def test_the_policy_no_longer_selects_a_body():
    """Both policies hold the same tools, so no policy-specific body ships."""
    import os

    skill_dir = _orchestrator_skill_dir()
    assert not os.path.exists(os.path.join(skill_dir, "SKILL.delegate.md"))
    assert not os.path.exists(os.path.join(skill_dir, "SKILL.light-delegate.md"))


def test_body_variant_falls_back_when_the_profile_file_is_missing(tmp_path):
    (tmp_path / "SKILL.md").write_text("canonical", encoding="utf-8")
    assert _orchestrator_body_variant(str(tmp_path), "light") == "full"
    (tmp_path / "SKILL.light.md").write_text("condensed", encoding="utf-8")
    assert _orchestrator_body_variant(str(tmp_path), "light") == "light"


@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_each_policy_renders_its_own_block_and_only_it(tmp_path, monkeypatch, policy):
    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    prompt = build_chat_orchestrator(project, None).system_prompt
    other = "direct" if policy == "delegate" else "delegate"

    assert f"Your helper policy: {policy}" in prompt
    assert f"Your helper policy: {other}" not in prompt
    assert "Writing is currently impossible" not in prompt
    assert "write_file" in prompt


@pytest.mark.parametrize(
    "profile,policy",
    [
        ("full", "direct"),
        ("light", "direct"),
        ("full", "delegate"),
        ("light", "delegate"),
    ],
)
def test_no_orchestrator_prompt_mentions_the_removed_jpt_format(
    tmp_path, monkeypatch, profile, policy
):
    """The native .jpt presentation format and its tools were removed; a prompt
    that still named them would send the model after tools it does not hold."""
    project = _catalog(
        tmp_path, monkeypatch, f"ollama/{profile}-{policy}",
        prompt_profile=profile, orchestrator_policy=policy,
    )
    prompt = build_chat_orchestrator(project, None).system_prompt

    for remnant in ("jpt", "deck.json", "create_presentation", "edit_presentation",
                    "set_presentation_theme", "check_presentation"):
        assert remnant not in prompt.lower(), remnant


def test_fallback_body_makes_the_main_agent_do_the_task(monkeypatch):
    """The no-SKILL.md safety net describes the same roles as the bundled body."""
    import gilzabots.memgpt_runtime as runtime

    monkeypatch.setattr(runtime, "find_skill_dir", lambda *a, **k: None)

    body = runtime._chat_orchestrator_body("/tmp/x", "full")

    assert "write_file is the only tool that creates a new file" in body
    assert "load_skill" in body and "run_skill" in body
    assert "never changes project files" in body


@pytest.mark.parametrize("policy", ["direct", "delegate"])
def test_code_navigation_is_a_read_tool_under_every_policy(tmp_path, monkeypatch, policy):
    """find_symbol and code_outline only read; every policy holds them."""
    project = _catalog(tmp_path, monkeypatch, "ollama/m", orchestrator_policy=policy)
    assert {"find_symbol", "code_outline"} <= _tool_names(project)
