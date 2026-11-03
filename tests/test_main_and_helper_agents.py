"""The main agent / helper split (PROJECT_DESIGN 2.23).

The main agent does the user's task and makes every change. A skill is either
one it carries out itself (`role: instructions`, the default, read with
`load_skill`) or a helper (`role: helper`): an ephemeral agent `run_skill`
spawns for an auxiliary job, which returns data and never changes project
files. The main agent is told each helper's model and whether that model sees
images, and receives, beside the helper's report, the tool calls that failed.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import yaml

import gilzabots.agent_stdin as agent_stdin
import gilzabots.memgpt_runtime as runtime
import gilzabots.tools as tools
from agenticblocks.blocks.llm.agent import LLMAgentBlock, ToolCallRecord
from gilzabots.project import ProjectData
from gilzabots.skills import HELPER_ROLE, INSTRUCTIONS_ROLE, parse_skill_md, skill_role


# ── fixtures ─────────────────────────────────────────────────────────────────

def _skill(project_path, name, role=None, model=None, body="Do the thing.", extends=None, script=None):
    """Write a project-local skill and return its directory."""
    skill_dir = os.path.join(project_path, "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    front = [f"name: {name}", f"description: The {name} skill."]
    if model:
        front.append(f"model: {model}")
    if role:
        front.append(f"role: {role}")
    if extends:
        front.append(f"extends: {extends}")
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n" + body + "\n")
    if script:
        os.makedirs(os.path.join(skill_dir, "scripts"), exist_ok=True)
        with open(os.path.join(skill_dir, "scripts", script), "w", encoding="utf-8") as f:
            f.write("print('ok')\n")
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
    (path / "main5.tex").write_text("\\section{Intro}\\label{sec:intro}\nText.\n", encoding="utf-8")
    proj = ProjectData(
        name="t", project_name="t", project_path=str(path),
        model="ollama/main-model", worker_model="ollama/small-model", mode="auto",
    )
    yield proj
    tools.set_worker_context(False)


def _raw(tool):
    return getattr(tool, "_func", None) or tool


# ── roles ────────────────────────────────────────────────────────────────────

def test_a_skill_without_a_role_is_carried_out_by_the_main_agent(tmp_path):
    assert skill_role(parse_skill_md(_skill(str(tmp_path), "plain"))) == INSTRUCTIONS_ROLE
    assert skill_role(parse_skill_md(_skill(str(tmp_path), "aux", role="helper"))) == HELPER_ROLE


def test_the_bundled_auxiliary_skills_are_helpers_and_the_rest_are_not():
    from gilzabots.skills import discover_skills

    roles = {s["name"]: skill_role(s) for s in discover_skills("")}
    for name in ("staged-reader", "elementary-ops", "log-table-condenser", "web-search"):
        assert roles[name] == HELPER_ROLE, name
    for name in ("command-line", "humanize", "latex-assistant", "tex-to-jpt"):
        assert roles[name] == INSTRUCTIONS_ROLE, name


@pytest.mark.parametrize("name", ["staged-reader", "elementary-ops", "log-table-condenser"])
def test_the_asset_store_copy_of_a_helper_is_a_helper_too(name):
    """An installed store copy shadows the bundled skill, so it must agree."""
    import zipfile
    from gilzabots.skills import _parse_frontmatter

    archive = os.path.join(os.path.dirname(tools.__file__), "assetstore", "skills", f"{name}.zip")
    with zipfile.ZipFile(archive) as z:
        front, _ = _parse_frontmatter(z.read(f"{name}/SKILL.md").decode("utf-8"))
    assert front.get("role") == HELPER_ROLE


# ── the catalog ──────────────────────────────────────────────────────────────

def test_the_catalog_lists_each_skill_under_the_tool_that_accepts_it(project):
    _skill(project.project_path, "reader", role="helper", model="worker")
    _skill(project.project_path, "writer")
    _activate(project.project_path, "reader", "writer")

    prompt = runtime.chat_orchestrator_system_prompt(project)
    helpers = prompt.index("## Helpers (delegate with run_skill)")
    loadable = prompt.index("## Skills you carry out yourself (load with load_skill)")

    assert helpers < prompt.index("- reader:") < loadable < prompt.index("- writer:")
    assert "[model: ollama/small-model; vision: no]" in prompt


def test_vision_is_declared_by_the_parameters_of_the_model_that_runs(project):
    """force_vision on the worker parameters is about the worker model, so a
    helper that runs on the main model does not inherit it."""
    _skill(project.project_path, "on-worker", role="helper", model="worker")
    _skill(project.project_path, "on-main", role="helper", model="default")
    _activate(project.project_path, "on-worker", "on-main")
    project.worker_model_params = {"force_vision": True}

    prompt = runtime.chat_orchestrator_system_prompt(project)

    assert "- on-worker: The on-worker skill. [model: ollama/small-model; vision: yes]" in prompt
    assert "- on-main: The on-main skill. [model: ollama/main-model; vision: no]" in prompt


def test_a_skill_with_an_unknown_role_is_listed_as_unusable_and_refused(project):
    _skill(project.project_path, "odd", role="sidekick")
    _activate(project.project_path, "odd")

    prompt = runtime.chat_orchestrator_system_prompt(project)
    assert "## Skills that cannot be used" in prompt
    assert "role 'sidekick'" in prompt

    memgpt = runtime.build_chat_orchestrator(project, None)
    run_skill = runtime.build_run_skill_tool(memgpt, project.project_path, _project_ref=project)
    load_skill = runtime.build_load_skill_tool(project.project_path)
    assert "role 'sidekick'" in asyncio.run(_raw(run_skill)("odd", "x"))
    assert "role 'sidekick'" in asyncio.run(_raw(load_skill)("odd"))


# ── load_skill ───────────────────────────────────────────────────────────────

def test_load_skill_returns_the_instructions_and_the_scripts(project):
    _skill(project.project_path, "base", body="PARENT RULES")
    _skill(project.project_path, "writer", body="CHILD RULES", extends="base", script="fix.py")
    _activate(project.project_path, "base", "writer")

    out = asyncio.run(_raw(runtime.build_load_skill_tool(project.project_path))("writer"))

    assert out.startswith("[skill 'writer' loaded] Carry out these instructions yourself")
    assert out.index("PARENT RULES") < out.index("CHILD RULES")
    assert os.path.join("writer", "scripts", "fix.py") in out


def test_load_skill_sends_a_helper_to_run_skill_and_refuses_unknown_names(project):
    _skill(project.project_path, "reader", role="helper")
    _activate(project.project_path, "reader")
    load = _raw(runtime.build_load_skill_tool(project.project_path))

    assert "run_skill('reader'" in asyncio.run(load("reader"))
    unknown = asyncio.run(load("read_file"))
    assert "was not found / is not active" in unknown and "'reader'" in unknown
    assert "is you" in asyncio.run(load(runtime.CHAT_ORCHESTRATOR_SKILL))


def test_run_skill_sends_an_instructions_skill_to_load_skill(project):
    memgpt = runtime.build_chat_orchestrator(project, None)
    run_skill = runtime.build_run_skill_tool(memgpt, project.project_path, _project_ref=project)

    out = asyncio.run(_raw(run_skill)("command-line", "rename the figures"))

    assert out.startswith("[ERROR] 'command-line' is not a helper")
    assert "load_skill('command-line')" in out


# ── run_skill, end to end with a scripted helper model ───────────────────────

def _tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _run_scripted_helper(project, monkeypatch, steps, report):
    seen = {}

    class ScriptedHelper(LLMAgentBlock):
        async def _acompletion(self, messages, **kwargs):
            seen["tools"] = {t["function"]["name"] for t in kwargs.get("tools", [])}
            step = sum(1 for m in messages if m.get("role") == "assistant")
            if step < len(steps):
                return _response("", [steps[step]])
            return _response(report)

    monkeypatch.setattr(runtime, "LLMAgentBlock", ScriptedHelper)
    memgpt = runtime.build_chat_orchestrator(project, None)
    run_skill = runtime.build_run_skill_tool(
        memgpt, project.project_path, project_model=project.model,
        project_worker=project.worker_model, _project_ref=project,
    )
    result = asyncio.run(_raw(run_skill)("reader", "find the intro label in main5.tex"))
    return result, seen, memgpt


def test_a_helper_holds_data_tools_only(project, monkeypatch):
    _skill(project.project_path, "reader", role="helper", model="worker")
    _activate(project.project_path, "reader")

    _result, seen, _ = _run_scripted_helper(project, monkeypatch, [], "nothing to do")

    assert {"read_file", "search_code", "web_search", "write_file", "run_python_script"} <= seen["tools"]
    assert not {
        "generate_image", "compile_latex", "create_presentation", "create_docx_file",
        "run_background_command", "run_interactive_command", "run_skill", "load_skill",
    } & seen["tools"]
    assert "analyze_image" not in seen["tools"], "its model has no vision"


def test_a_helper_whose_model_sees_images_gets_analyze_image(project, monkeypatch):
    _skill(project.project_path, "reader", role="helper", model="worker")
    _activate(project.project_path, "reader")
    project.worker_model_params = {"force_vision": True}

    _result, seen, _ = _run_scripted_helper(project, monkeypatch, [], "nothing to do")

    assert "analyze_image" in seen["tools"]


def test_the_main_agent_receives_the_report_and_the_failed_calls(project, monkeypatch):
    _skill(project.project_path, "reader", role="helper", model="worker")
    _activate(project.project_path, "reader")
    steps = [
        _tool_call("c1", "search_code", {"query": r"\label{sec:intro}", "path": "main5.tex", "use_regex": True}),
        _tool_call("c2", "search_code", {"query": r"\label{sec:intro}", "path": "main5.tex"}),
        _tool_call("c3", "write_file", {"path": "main5.tex", "content": "oops"}),
        _tool_call("c4", "write_file", {"path": ".gilzabots/notes.md", "content": "intro at 1"}),
    ]

    result, _seen, memgpt = _run_scripted_helper(
        project, monkeypatch, steps, "The label is at main5.tex:1.",
    )

    assert result.startswith("[helper 'reader' finished] Helper's report:")
    assert "(Tool calls made by the helper: 4)" in result
    assert "The label is at main5.tex:1." in result
    assert "- call 1 search_code failed: Error: invalid regular expression" in result
    assert "1 of them a successful search_code" in result
    assert "- call 3 write_file failed: Error: helpers do not change project files" in result
    # The helper's scratch note was written; the project file was not touched.
    assert os.path.isfile(os.path.join(project.project_path, ".gilzabots", "notes.md"))
    with open(os.path.join(project.project_path, "main5.tex"), encoding="utf-8") as f:
        assert f.read().startswith("\\section{Intro}")
    # The loop breakers classify the report only, never the runtime's error list.
    assert "Tool errors" not in memgpt._skill_run_history[-1]["result"]


def test_a_run_without_failed_calls_adds_no_error_list(project, monkeypatch):
    _skill(project.project_path, "reader", role="helper")
    _activate(project.project_path, "reader")
    steps = [_tool_call("c1", "search_code", {"query": "Intro", "path": "main5.tex"})]

    result, _seen, _ = _run_scripted_helper(project, monkeypatch, steps, "Found it at main5.tex:1.")

    assert "Tool errors" not in result


# ── the failed-call list ─────────────────────────────────────────────────────

def test_the_error_list_states_facts_and_bounds_itself():
    records = [ToolCallRecord(index=i, tool="search_code", ok=False, error=f"bad {i}") for i in range(1, 8)]
    notes = runtime._helper_tool_error_notes(records, limit=5)

    assert notes.count("\n- call ") == 5
    assert "- and 2 more failed tool call(s)" in notes
    assert "- call 7" not in notes
    assert "it was the helper's last tool call" not in notes  # call 7 is not listed
    assert runtime._helper_tool_error_notes([ToolCallRecord(index=1, tool="x", ok=True)]) == ""


def test_a_failure_that_ended_the_run_says_so():
    notes = runtime._helper_tool_error_notes([
        ToolCallRecord(index=1, tool="read_file", ok=True),
        ToolCallRecord(index=2, tool="web_search", ok=False, error="timeout"),
    ])
    assert "- call 2 web_search failed: timeout -- it was the helper's last tool call" in notes


def test_the_report_extractor_reads_both_markers_and_drops_the_error_list():
    new = (
        "[helper 'r' finished] Helper's report:\n(Tool calls made by the helper: 2)\nREPORT"
        "\n\n[Tool errors in this helper run -- recorded facts]\n- call 1 x failed: y"
    )
    legacy = "[skill 'r' finished] Worker's summary/report:\n(Tools used by worker: 1)\nOLD REPORT"

    assert agent_stdin._extract_worker_summary_from_tool_result(new) == "REPORT"
    assert agent_stdin._extract_worker_summary_from_tool_result(legacy) == "OLD REPORT"


# ── the helper's scratch area ────────────────────────────────────────────────

def _write_file(path, content="x"):
    return asyncio.run(_raw(tools.write_file)(path, content))


def test_a_helper_writes_only_under_the_scratch_area(project):
    tools.set_project_context(project)
    tools.set_worker_context(True, "ollama/small-model")

    assert "Successfully" in _write_file(".gilzabots/notes/a.md")
    for outside in ("main5.tex", ".gilzabots/../main5.tex", "new_chapter.tex"):
        with pytest.raises(ValueError, match="helpers do not change project files"):
            _write_file(outside)


def test_the_main_agent_is_not_confined(project):
    tools.set_project_context(project)
    tools.set_worker_context(False)

    assert "Successfully" in _write_file("new_chapter.tex")


def test_analyze_image_in_a_helper_asks_the_helper_model(project, monkeypatch):
    import litellm

    tools.set_project_context(project)
    image = os.path.join(project.project_path, "fig.png")
    with open(image, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
    asked = []
    monkeypatch.setattr(litellm, "completion", lambda model, **_k: asked.append(model) or SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="a chart"))]))

    tools.set_worker_context(True, "ollama/vision-helper")
    assert asyncio.run(_raw(tools.analyze_image)("fig.png")) == "a chart"
    tools.set_worker_context(False)
    asyncio.run(_raw(tools.analyze_image)("fig.png"))

    assert asked[0] == "ollama/vision-helper"
    assert asked[1] != "ollama/vision-helper"


# ── search_code says how it read the query ───────────────────────────────────

def _search(project, *args, **kwargs):
    tools.set_project_context(project)
    return asyncio.run(_raw(tools.search_code)(*args, **kwargs))


def test_a_literal_query_with_a_pipe_explains_why_it_found_nothing(project):
    out = _search(project, "Intro|Conclusion", "main5.tex")
    assert out.startswith("No matches for 'Intro|Conclusion'")
    assert "use_regex=true" in out
    # The search itself is not changed into a regex behind the caller's back.
    assert "main5.tex:1" not in out
    assert "main5.tex:1" in _search(project, "Intro|Conclusion", "main5.tex", use_regex=True)


def test_a_literal_query_without_a_pipe_gets_no_regex_note(project):
    assert "use_regex" not in _search(project, "Conclusion", "main5.tex")


def test_an_invalid_regex_fails_with_what_is_wrong_with_it(project):
    with pytest.raises(ValueError) as excinfo:
        _search(project, r"\section{Intro}\label", "main5.tex", use_regex=True)
    message = str(excinfo.value)
    assert message.startswith("Error: invalid regular expression (use_regex=true): bad escape \\l")
    assert "use_regex=false" in message
    assert "searching code in" not in message


# ── vision ───────────────────────────────────────────────────────────────────

def test_model_supports_vision():
    from gilzabots.config import model_supports_vision

    assert model_supports_vision("ollama/anything", {"force_vision": True})
    assert not model_supports_vision("ollama/unknown-local-model", {})
    assert not model_supports_vision("", None)
