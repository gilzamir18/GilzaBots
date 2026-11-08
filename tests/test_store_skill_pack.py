"""The engineering-workflow skill pack in the Asset Store (assetstore_sources/skills/).

Adapted from Matt Pocock's skills (MIT) to GilzaBots in Claude Code's format:
YAML frontmatter, `disable-model-invocation` for the skills the user runs with
`/name`, skills that load each other with `load_skill`, and read-only
subagents through `run_agent`. These tests hold the pack to what makes it
work after installation, not just to what it says.
"""
import os
import py_compile
import re

import pytest
import yaml

import gilzabots.agent_stdin as agent_stdin
import gilzabots.memgpt_runtime as runtime
import gilzabots.subagents as subagents
from gilzabots.assetstore import find_assets, install_skill_with_requirements, skill_requirements
from gilzabots.project import ProjectData
from gilzabots.skill_invocation import resolve_skill_invocation
from gilzabots.skills import parse_skill_md, skill_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "assetstore_sources", "skills")

with open(os.path.join(SOURCES, "manifest.yaml"), encoding="utf-8") as _handle:
    MANIFEST = yaml.safe_load(_handle)
PACK = list(MANIFEST["skills"])

#: The skills that orchestrate a flow: only the user starts them, with /name.
USER_INVOKED = {
    "grill-me", "handoff", "teach", "to-questionnaire", "wait-what", "which-skill",
    "setup-engineering-skills", "grill-with-docs", "improve-codebase-architecture",
    "to-spec", "to-tickets", "implement", "wayfinder", "retro", "triage",
}

#: load_skill calls a skill makes only when the other skill happens to be active.
OPTIONAL_LOADS = {("tdd", "codebase-design")}


def _requires(name):
    return set((MANIFEST["skills"][name] or {}).get("requires", []))


def _closure(name):
    seen, stack = set(), list(_requires(name))
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(_requires(current))
    return seen


def _texts(name):
    """Every Markdown file of a skill, as (relative path, text)."""
    found = []
    for dirpath, _dirs, files in os.walk(os.path.join(SOURCES, name)):
        for filename in files:
            if filename.endswith(".md") and filename != "ATTRIBUTION.md":
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8") as handle:
                    found.append((os.path.relpath(path, SOURCES), handle.read()))
    return found


def test_the_store_zips_are_built_from_the_current_sources():
    import scripts.build_store_skills as build

    stale = [name for name in PACK if not build.zip_matches(name)]
    assert not stale, f"run `python scripts/build_store_skills.py`: out of date {stale}"


@pytest.mark.parametrize("name", PACK)
def test_each_skill_is_in_the_store_with_its_requirements(name):
    matches = find_assets("skill", name)
    assert len(matches) == 1
    assert set(matches[0].get("requires") or []) == _requires(name)
    assert matches[0]["desc"] == parse_skill_md(os.path.join(SOURCES, name))["description"]


@pytest.mark.parametrize("name", PACK)
def test_each_skill_parses_as_claude_code_frontmatter(name):
    meta = parse_skill_md(os.path.join(SOURCES, name))
    assert skill_error(meta) == ""
    assert meta["name"] == name
    assert meta["description"]
    assert meta["disable_model_invocation"] is (name in USER_INVOKED)
    assert meta["role"] == "instructions"


@pytest.mark.parametrize("name", sorted(set(PACK) - USER_INVOKED))
def test_a_model_invoked_skill_says_when_to_reach_for_it(name):
    assert "Use when" in parse_skill_md(os.path.join(SOURCES, name))["description"]


@pytest.mark.parametrize("name", PACK)
def test_every_skill_a_skill_loads_is_installed_with_it(name):
    loaded = set()
    for _path, text in _texts(name):
        loaded |= set(re.findall(r'load_skill\("([a-z0-9-]+)"\)', text))
    for target in loaded:
        assert target in MANIFEST["skills"], (name, target)
        if (name, target) not in OPTIONAL_LOADS:
            assert target in _closure(name), f"{name} loads {target} but does not require it"
    # A user-invoked skill cannot be loaded, so nothing may try.
    assert not loaded & USER_INVOKED


@pytest.mark.parametrize("name", PACK)
def test_each_skill_carries_its_attribution_and_licence(name):
    with open(os.path.join(SOURCES, name, "ATTRIBUTION.md"), encoding="utf-8") as handle:
        text = handle.read()
    assert "Matt Pocock" in text and "MIT" in text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text


@pytest.mark.parametrize("name", PACK)
def test_the_files_a_skill_links_to_exist(name):
    for rel, text in _texts(name):
        base = os.path.dirname(os.path.join(SOURCES, rel))
        # Links inside fenced blocks are sample content (a glossary map's
        # entries), not links of the skill.
        prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in re.findall(r"\]\(([^)#:]+\.(?:md|py))\)", prose):
            assert os.path.isfile(os.path.join(base, target)), (rel, target)


def test_no_skill_tells_the_user_to_clear():
    """GilzaBots' /clear erases every chat of the project, not the context."""
    for name in PACK:
        for rel, text in _texts(name):
            assert "`/clear`" not in text and "/clear " not in text, rel
            assert "Skill tool" not in text, rel


def test_the_tools_the_skills_name_exist(tmp_path, monkeypatch):
    from gilzabots import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    monkeypatch.setattr(agent_stdin, "print_event", lambda *_a, **_k: None)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    project = ProjectData(name="t", project_name="t", project_path=str(project_dir),
                          model="ollama/main-model", worker_model="ollama/small-model", mode="auto")
    memgpt = runtime.build_chat_orchestrator(project)
    known = {tool.name for tool in memgpt.tools} | {"run_skill", "create_plan"}
    # The wizard template's own helpers, named in the wizard skill.
    known |= {"stage", "say", "step", "note", "open_url", "ask", "ask_secret", "write_env",
              "set_secret", "set_var", "pause", "confirm", "run_wizard", "finish", "capture"}
    subagents.cancel_background_agents()

    named = set()
    for name in PACK:
        for _rel, text in _texts(name):
            named |= set(re.findall(r"`([a-z][a-z_]+)\(", text))
    assert named - known == set()


@pytest.mark.parametrize("template", [
    os.path.join("diagnosing-bugs", "templates", "hitl_loop.py"),
    os.path.join("wizard", "templates", "wizard.py"),
])
def test_the_script_templates_compile(template):
    py_compile.compile(os.path.join(SOURCES, template), doraise=True)


def test_the_router_names_every_skill_of_the_pack():
    text = dict(_texts("which-skill"))[os.path.join("which-skill", "SKILL.md")]
    missing = [name for name in PACK if name != "which-skill" and f"`{name}`" not in text
               and f"`/{name}`" not in text]
    assert not missing


def test_installing_a_skill_brings_what_it_loads_and_it_runs(tmp_path):
    meta = find_assets("skill", "improve-codebase-architecture")[0]
    assert [r["id"] for r in skill_requirements(meta)] == ["codebase-design", "grilling", "domain-modeling"]

    installed = install_skill_with_requirements(meta, str(tmp_path))
    names = [name for name, _ in installed]
    assert names == ["codebase-design", "grilling", "domain-modeling", "improve-codebase-architecture"]
    for name in names:
        assert (tmp_path / ".gilzabots" / "skills" / name / "SKILL.md").is_file()

    with open(tmp_path / "skills.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump({"skills": names}, handle)
    invocation = resolve_skill_invocation(str(tmp_path), "/improve-codebase-architecture the parser")
    assert 'load_skill("codebase-design")' in invocation.prompt
    assert invocation.prompt.rstrip().endswith("ARGUMENTS: the parser")

    import asyncio
    load = runtime.build_load_skill_tool(str(tmp_path))._func
    assert "loaded" in asyncio.run(load("grilling"))
    assert asyncio.run(load("improve-codebase-architecture")).startswith("[ERROR]")


def test_a_requirement_the_project_already_has_is_left_alone(tmp_path):
    meta = find_assets("skill", "grill-me")[0]
    own = tmp_path / ".gilzabots" / "skills" / "grilling"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: grilling\ndescription: Mine.\n---\nMy own edits.\n", encoding="utf-8")

    installed = install_skill_with_requirements(meta, str(tmp_path))

    assert [name for name, _ in installed] == ["grill-me"]
    assert "My own edits." in (own / "SKILL.md").read_text(encoding="utf-8")


def test_a_missing_requirement_fails_before_anything_is_installed(tmp_path):
    meta =dict(find_assets("skill", "grill-me")[0], requires=["not-in-the-store"])
    with pytest.raises(ValueError, match="not-in-the-store"):
        install_skill_with_requirements(meta, str(tmp_path))
    assert not (tmp_path / ".gilzabots").exists()
