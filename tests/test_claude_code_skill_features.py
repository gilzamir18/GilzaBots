"""Skills as Claude Code has them (PROJECT_DESIGN 2.23, 2.29-2.31).

* Frontmatter is YAML, with Claude Code's `disable-model-invocation`,
  `user-invocable` and `argument-hint`; a file that is not valid YAML is listed
  as unusable with the parser's reason, never read by a second, guessing reader.
* `/name [arguments]` runs a skill the user may invoke: the model receives the
  skill's instructions and the arguments, the chat stores what was typed.
* `run_agent` starts read-only subagents, concurrently and in the background.
* `AGENTS.md` at the project root reaches the main agent and its subagents.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
import yaml

import gilzabots.agent_stdin as agent_stdin
import gilzabots.memgpt_runtime as runtime
import gilzabots.subagents as subagents
import gilzabots.tools as tools
from gilzabots.project import ProjectData, ProjectStore
from gilzabots.skill_invocation import (
    SkillInvocationError,
    invocable_skills,
    parse_invocation,
    resolve_skill_invocation,
)
from gilzabots.skills import is_model_invocable, parse_skill_md, skill_error


def _write_skill(project_path, name, front: str, body="Do the thing."):
    skill_dir = os.path.join(project_path, "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\n{front.strip()}\n---\n{body}\n")
    return skill_dir


def _activate(project_path, *names):
    with open(os.path.join(project_path, "skills.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"skills": list(names)}, f)


@pytest.fixture
def project(tmp_path, monkeypatch):
    from gilzabots import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    monkeypatch.setattr(agent_stdin, "print_event", lambda *_a, **_k: None)
    path = tmp_path / "proj"
    path.mkdir()
    yield ProjectData(
        name="t", project_name="t", project_path=str(path),
        model="ollama/main-model", worker_model="ollama/small-model", mode="auto",
    )
    subagents.cancel_background_agents()


# ── frontmatter ──────────────────────────────────────────────────────────────

def test_frontmatter_is_read_as_yaml(tmp_path):
    skill_dir = _write_skill(str(tmp_path), "grill", '''
name: grill
description: "Interview the user: one round at a time."
disable-model-invocation: true
argument-hint: What should we stress-test?
''')
    meta = parse_skill_md(skill_dir)
    # Quoted, and containing ': ' -- the old line reader kept the quotes.
    assert meta["description"] == "Interview the user: one round at a time."
    assert meta["disable_model_invocation"] is True
    assert meta["user_invocable"] is True
    assert meta["argument_hint"] == "What should we stress-test?"
    assert meta["error"] == ""


def test_a_block_scalar_description_spans_lines(tmp_path):
    skill_dir = _write_skill(str(tmp_path), "multi", '''
name: multi
description: >
  First line
  and second line.
''')
    assert parse_skill_md(skill_dir)["description"] == "First line and second line."


def test_invalid_yaml_is_reported_not_guessed(tmp_path):
    skill_dir = _write_skill(str(tmp_path), "broken", '''
name: broken
description: Use when: the user asks
''')
    meta = parse_skill_md(skill_dir)
    assert "not valid YAML" in skill_error(meta)
    assert meta["name"] == "broken"
    assert meta["body"] == ""
    assert not is_model_invocable(meta)


def test_a_flag_that_is_not_a_boolean_is_an_error(tmp_path):
    skill_dir = _write_skill(str(tmp_path), "flag", '''
name: flag
description: x
disable-model-invocation: sometimes
''')
    assert "true or false" in skill_error(parse_skill_md(skill_dir))


def test_every_skill_shipped_today_still_parses_cleanly():
    from gilzabots.skills import discover_skills

    for skill in discover_skills(""):
        assert skill_error(skill) == "", (skill["name"], skill_error(skill))


# ── the catalog and load_skill ───────────────────────────────────────────────

def test_a_user_only_skill_is_left_out_of_the_catalog_and_refused_by_load_skill(project):
    _write_skill(project.project_path, "grill", "name: grill\ndescription: Interview.\ndisable-model-invocation: true")
    _write_skill(project.project_path, "tdd", "name: tdd\ndescription: Red, green, refactor.")
    _activate(project.project_path, "grill", "tdd")

    prompt = runtime.chat_orchestrator_system_prompt(project)
    assert "- tdd:" in prompt
    assert "- grill:" not in prompt

    load = runtime.build_load_skill_tool(project.project_path)._func
    refused = asyncio.run(load("grill"))
    assert refused.startswith("[ERROR]") and "/grill" in refused
    assert "Red" not in refused
    assert "loaded" in asyncio.run(load("tdd"))


def test_a_broken_skill_is_listed_as_unusable_with_the_parser_reason(project):
    _write_skill(project.project_path, "broken", "name: broken\ndescription: Use when: x")
    _activate(project.project_path, "broken")

    prompt = runtime.chat_orchestrator_system_prompt(project)
    assert "## Skills that cannot be used" in prompt
    assert "not valid YAML" in prompt


# ── /name [arguments] ────────────────────────────────────────────────────────

def test_parse_invocation_reads_the_name_and_the_arguments():
    assert parse_invocation("/grill-me my plan") == ("grill-me", "my plan")
    assert parse_invocation("/grill-me\nline one\nline two") == ("grill-me", "line one\nline two")
    assert parse_invocation("/handoff") == ("handoff", "")
    assert parse_invocation("/etc/hosts is wrong") is None
    assert parse_invocation("please /grill-me") is None


def test_running_a_skill_hands_its_body_and_arguments_to_the_model(project):
    skill_dir = _write_skill(
        project.project_path, "handoff",
        "name: handoff\ndescription: Write a handoff.\ndisable-model-invocation: true",
        body="Write the handoff document. Read FORMAT.md first.",
    )
    _activate(project.project_path, "handoff")

    invocation = resolve_skill_invocation(project.project_path, "/handoff next: the parser")
    assert invocation.name == "handoff"
    assert invocation.arguments == "next: the parser"
    assert invocation.prompt.startswith("/handoff next: the parser")
    assert "Write the handoff document." in invocation.prompt
    assert f"Skill directory: {skill_dir}" in invocation.prompt
    assert invocation.prompt.rstrip().endswith("ARGUMENTS: next: the parser")


def test_the_arguments_placeholder_is_replaced_where_the_body_puts_it(project):
    _write_skill(project.project_path, "teach", "name: teach\ndescription: Teach.",
                 body="Teach the user about $ARGUMENTS, one lesson at a time.")
    _activate(project.project_path, "teach")

    prompt = resolve_skill_invocation(project.project_path, "/teach Rust lifetimes").prompt
    assert "Teach the user about Rust lifetimes, one lesson at a time." in prompt
    assert "ARGUMENTS:" not in prompt


def test_names_that_are_not_runnable_skills(project):
    _write_skill(project.project_path, "idle", "name: idle\ndescription: Installed, not active.")
    _write_skill(project.project_path, "reader", "name: reader\ndescription: Helper.\nrole: helper")
    _write_skill(project.project_path, "hidden", "name: hidden\ndescription: Agent only.\nuser-invocable: false")
    _write_skill(project.project_path, "help", "name: help\ndescription: Shadowed by the command.")
    _activate(project.project_path, "reader", "hidden", "help")

    with pytest.raises(SkillInvocationError, match="not active"):
        resolve_skill_invocation(project.project_path, "/idle")
    with pytest.raises(SkillInvocationError, match="helper"):
        resolve_skill_invocation(project.project_path, "/reader")
    with pytest.raises(SkillInvocationError, match="user-invocable"):
        resolve_skill_invocation(project.project_path, "/hidden")
    # A built-in command keeps its name, and a word that names no skill is text.
    assert resolve_skill_invocation(project.project_path, "/help") is None
    assert resolve_skill_invocation(project.project_path, "/nothing-here") is None
    assert {s["name"] for s in invocable_skills(project.project_path)} == set()


def test_the_completion_menu_lists_the_runnable_skills(project):
    _write_skill(project.project_path, "handoff",
                 "name: handoff\ndescription: Write a handoff.\nargument-hint: What is next?")
    _activate(project.project_path, "handoff")

    entries = {c["name"]: c for c in agent_stdin.list_slash_commands(project.project_path)}
    assert entries["/handoff"]["kind"] == "skill"
    assert entries["/handoff"]["usage"] == "[What is next?]"
    assert entries["/help"]["kind"] == "command"
    assert "/handoff" not in {c["name"] for c in agent_stdin.list_slash_commands()}


def test_the_gui_door_answers_skill_for_a_runnable_skill(project, monkeypatch):
    _write_skill(project.project_path, "handoff", "name: handoff\ndescription: Write a handoff.")
    _activate(project.project_path, "handoff")
    monkeypatch.setattr(agent_stdin, "current_project", project, raising=False)
    monkeypatch.setattr(agent_stdin, "current_store", None, raising=False)

    result = asyncio.run(agent_stdin.handle_slash_command({"prompt": "/handoff later"}))
    assert result == {"status": "skill", "name": "handoff"}

    unknown = asyncio.run(agent_stdin.handle_slash_command({"prompt": "/nothing-here"}))
    assert unknown["status"] == "error"


@pytest.mark.asyncio
async def test_a_turn_stores_what_was_typed_and_runs_the_skill(monkeypatch, tmp_path):
    events, ran, stored = [], [], []

    class FakeMemGPT:
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        model = "ollama/qwen3.5:cloud"

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            ran.append(agent_input.prompt)
            return SimpleNamespace(response="ok")

    if True:
        tmp = str(tmp_path)
        store = ProjectStore(db_path=os.path.join(tmp, "sessions.db"))
        path = os.path.join(tmp, "p1")
        os.makedirs(path)
        _write_skill(path, "grill-me", "name: grill-me\ndescription: Interview.\ndisable-model-invocation: true",
                     body="Ask one round of questions.")
        store.create(name="p1", mode="auto", model="ollama/qwen3.5:cloud", project_name="P1", project_path=path)
        # After create, which writes a starter skills.yaml of its own.
        _activate(path, "grill-me")
        store.create_chat("p1", "chat-1", "Chat")
        project = store.load("p1")

        original_append = store.append_message

        def recording_append(proj, role, content, *args, **kwargs):
            stored.append((role, content))
            return original_append(proj, role, content, *args, **kwargs)

        monkeypatch.setattr(store, "append_message", recording_append)
        monkeypatch.setattr(agent_stdin, "print_event", lambda event, data: events.append((event, data)))
        monkeypatch.setattr(agent_stdin, "current_project", project)
        monkeypatch.setattr(agent_stdin, "current_store", store)
        monkeypatch.setattr(agent_stdin, "current_memgpt", FakeMemGPT())

        await agent_stdin.handle_run({"agent": "chat_orchestrator", "prompt": "/grill-me the cache plan"})

    assert ("user", "/grill-me the cache plan") in stored
    assert "Ask one round of questions." in ran[0]
    assert "ARGUMENTS: the cache plan" in ran[0]


@pytest.mark.asyncio
async def test_a_skill_that_cannot_run_fails_the_turn_with_the_reason(monkeypatch, tmp_path):
    events = []

    class FakeMemGPT:
        async def run(self, agent_input):  # pragma: no cover - must not be reached
            raise AssertionError("the agent must not run")

    if True:
        tmp = str(tmp_path)
        store = ProjectStore(db_path=os.path.join(tmp, "sessions.db"))
        path = os.path.join(tmp, "p1")
        os.makedirs(path)
        _write_skill(path, "idle", "name: idle\ndescription: Not active.")
        store.create(name="p1", mode="auto", model="ollama/qwen3.5:cloud", project_name="P1", project_path=path)
        store.create_chat("p1", "chat-1", "Chat")
        monkeypatch.setattr(agent_stdin, "print_event", lambda event, data: events.append((event, data)))
        monkeypatch.setattr(agent_stdin, "current_project", store.load("p1"))
        monkeypatch.setattr(agent_stdin, "current_store", store)
        monkeypatch.setattr(agent_stdin, "current_memgpt", FakeMemGPT())

        await agent_stdin.handle_run({"agent": "chat_orchestrator", "prompt": "/idle"})

    errors = [data["message"] for event, data in events if event == "error"]
    assert errors and "not active" in errors[0]


def test_the_terminal_runs_a_skill_line_as_a_turn(project):
    from gilzabots.cli import _classify_skill_line

    _write_skill(project.project_path, "handoff", "name: handoff\ndescription: Write a handoff.")
    _activate(project.project_path, "handoff")
    assert _classify_skill_line(project, "/handoff soon") == "skill"
    assert _classify_skill_line(project, "/nothing-here") == "unknown"


# ── caller context ───────────────────────────────────────────────────────────

def test_one_helper_finishing_does_not_lift_another_ones_confinement():
    """The race the context variable exists for: with a module global, the
    first agent to finish cleared the flag for every agent still running."""
    seen = {}

    async def long_helper():
        tools.set_worker_context(True, "m")
        await asyncio.sleep(0.05)
        seen["still_helper"] = tools.in_helper_context()

    async def short_helper():
        tools.set_worker_context(True, "m")
        await asyncio.sleep(0.01)
        tools.set_worker_context(False)

    async def main():
        await asyncio.gather(long_helper(), short_helper())
        seen["main_after"] = tools.in_helper_context()

    asyncio.run(main())
    assert seen == {"still_helper": True, "main_after": False}


def test_subagent_context_restores_what_was_there():
    assert not tools.in_subagent_context()
    with tools.subagent_context("ollama/m"):
        assert tools.in_subagent_context()
        assert not tools.caller_has_terminal()
    assert not tools.in_subagent_context()


# ── run_agent ────────────────────────────────────────────────────────────────

class _FakeAgent:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_iteration = None
        _FakeAgent.instances.append(self)

    async def run(self, agent_input):
        await asyncio.sleep(float(getattr(self, "delay", 0)))
        # The tools the subagent runs with see the subagent context.
        assert tools.in_subagent_context()
        return SimpleNamespace(response=f"report: {agent_input.prompt}", tool_calls=[])


@pytest.fixture
def fake_subagent(monkeypatch):
    _FakeAgent.instances = []
    monkeypatch.setattr(subagents, "LLMAgentBlock", _FakeAgent)
    import gilzabots.litellm_compat as compat
    import gilzabots.token_usage as usage
    monkeypatch.setattr(compat, "wrap_agent_litellm_compat", lambda agent: agent)
    monkeypatch.setattr(usage, "attach_usage_tracking", lambda agent: agent)
    return _FakeAgent


def _subagent_tool(project, name):
    return next(t for t in subagents.build_subagent_tools(project) if t.name == name)._func


def test_run_agent_is_concurrency_safe_and_in_both_tool_lists(project):
    run_agent = next(t for t in subagents.build_subagent_tools(project) if t.name == "run_agent")
    assert run_agent.concurrency_safe is True

    memgpt = runtime.build_chat_orchestrator(project)
    assert "run_agent" in {t.name for t in memgpt.tools}
    project.mode = "plan"
    assert "run_agent" in {t.name for t in memgpt.tools_provider()}


def test_a_subagent_holds_read_tools_only(project, fake_subagent):
    report = asyncio.run(_subagent_tool(project, "run_agent")("Find where X is defined.", "find X"))

    assert report.startswith("[subagent a") and "report: Find where X is defined." in report
    names = {t.name for t in fake_subagent.instances[0].kwargs["tools"]}
    assert {"read_file", "search_code", "web_search", "inspect_git"} <= names
    assert not names & {
        "write_file", "edit_file", "run_command", "run_python_script", "ask_question",
        "run_skill", "run_agent", "search_conversation_history",
    }
    system =fake_subagent.instances[0].kwargs["system_prompt"]
    assert "You are a subagent" in system and project.project_path in system


def test_a_background_subagent_is_collected_with_wait_agents(project, fake_subagent):
    tools_by_name = {t.name: t._func for t in subagents.build_subagent_tools(project)}

    async def main():
        started = await tools_by_name["run_agent"]("Research the API.", "research", background=True)
        agent_id = started.split()[1]
        report = await tools_by_name["wait_agents"]([agent_id])
        with pytest.raises(ValueError, match="already collected"):
            await tools_by_name["wait_agents"]([agent_id])
        return started, report

    started, report = asyncio.run(main())
    assert "started in the background" in started
    assert "report: Research the API." in report


def test_background_subagents_left_running_are_stopped(project, fake_subagent):
    tools_by_name = {t.name: t._func for t in subagents.build_subagent_tools(project)}

    async def main():
        _FakeAgent.delay = 5
        try:
            await tools_by_name["run_agent"]("Slow job.", background=True)
            await asyncio.sleep(0)
            return subagents.cancel_background_agents()
        finally:
            _FakeAgent.delay = 0

    stopped = asyncio.run(main())
    assert len(stopped) == 1


def test_run_agent_rejects_an_unknown_model_and_an_empty_task(project):
    run_agent = _subagent_tool(project, "run_agent")
    with pytest.raises(ValueError, match="model"):
        asyncio.run(run_agent("job", model="huge"))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(run_agent("   "))


# ── AGENTS.md ────────────────────────────────────────────────────────────────

def test_agents_md_reaches_the_main_agent_and_subagents(project, fake_subagent):
    with open(os.path.join(project.project_path, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("Run the tests with `make check`.\n")

    assert "Run the tests with `make check`." in runtime.chat_orchestrator_system_prompt(project)
    asyncio.run(_subagent_tool(project, "run_agent")("job"))
    assert "Run the tests with `make check`." in fake_subagent.instances[0].kwargs["system_prompt"]


def test_no_agents_md_adds_nothing(project):
    assert "## Project instructions" not in runtime.chat_orchestrator_system_prompt(project)


def test_a_long_agents_md_is_cut_and_says_where_the_rest_is(project, monkeypatch):
    monkeypatch.setattr(runtime, "PROJECT_INSTRUCTIONS_MAX_CHARS", 20)
    with open(os.path.join(project.project_path, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("a" * 100)
    block = runtime.project_instructions_block(project.project_path)
    assert "a" * 21 not in block
    assert "Only the first 20 of 100 characters" in block
    assert "read_file(" in block
