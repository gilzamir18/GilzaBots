"""Tests for the skills-oriented loader (target design, docs/specs/06).

Covers the new directory/SKILL.md format:
  - parse_skill_md reads name/description/model from frontmatter
  - discover_skills finds skill directories and shadows by priority
  - skills.yaml filtering (absent → all; present → mandatory + declared)
  - mandatory chat-orchestrator is always active
  - level1_metadata renders name + description
"""

import os
import textwrap

from gilzabots.skills import (
    parse_skill_md,
    discover_skills,
    read_skills_yaml,
    active_skills,
    level1_metadata,
    find_skill_dir,
    skill_search_dirs,
    resolve_skill_icon_path,
    MANDATORY_SKILLS,
)


def _write_skill(base: str, name: str, frontmatter: str, body: str = "Body.") -> str:
    """Create <base>/<name>/SKILL.md and return the skill directory path."""
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(frontmatter).strip() + "\n\n" + body + "\n")
    return d


# ---------------------------------------------------------------------------
# parse_skill_md
# ---------------------------------------------------------------------------

def test_parse_skill_md_extracts_fields(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo", """\
        ---
        name: demo
        description: A demo skill. Use when testing.
        model: worker
        ---
    """, "These are the instructions.")
    meta = parse_skill_md(d)
    assert meta is not None
    assert meta["name"] == "demo"
    assert meta["description"] == "A demo skill. Use when testing."
    assert meta["model"] == "worker"
    assert "instructions" in meta["body"]


def test_parse_skill_md_light_profile_uses_variant_body(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo", """\
        ---
        name: demo
        description: A demo skill.
        ---
    """, "Full instructions.")
    with open(os.path.join(d, "SKILL.light.md"), "w", encoding="utf-8") as f:
        f.write("Condensed instructions.")

    full = parse_skill_md(d, profile="full")
    light = parse_skill_md(d, profile="light")

    assert full["body"] == "Full instructions."
    assert light["body"] == "Condensed instructions."
    # Frontmatter always comes from the canonical SKILL.md, regardless of profile.
    assert light["name"] == full["name"] == "demo"
    assert light["description"] == full["description"]


def test_parse_skill_md_light_profile_falls_back_without_variant_file(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo", """\
        ---
        name: demo
        description: A demo skill.
        ---
    """, "Only body available.")

    light = parse_skill_md(d, profile="light")

    assert light["body"] == "Only body available."


def test_parse_skill_md_name_defaults_to_dir(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "no-name", """\
        ---
        description: No explicit name.
        ---
    """)
    meta = parse_skill_md(d)
    assert meta["name"] == "no-name"
    assert meta["model"] == ""


def test_parse_skill_md_missing_manifest_returns_none(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert parse_skill_md(str(d)) is None


def test_parse_skill_md_extracts_icon_field(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo", """\
        ---
        name: demo
        description: A demo skill.
        icon: icon.png
        ---
    """)
    assert parse_skill_md(d)["icon"] == "icon.png"


def test_parse_skill_md_icon_defaults_to_empty(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "no-icon", """\
        ---
        name: no-icon
        description: No icon declared.
        ---
    """)
    assert parse_skill_md(d)["icon"] == ""


# ---------------------------------------------------------------------------
# resolve_skill_icon_path
# ---------------------------------------------------------------------------

def test_resolve_skill_icon_path_missing_field_returns_none(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo", "---\nname: demo\ndescription: D.\n---")
    meta = parse_skill_md(d)
    assert resolve_skill_icon_path(meta) is None


def test_resolve_skill_icon_path_missing_file_returns_none(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo",
                      "---\nname: demo\ndescription: D.\nicon: icon.png\n---")
    meta = parse_skill_md(d)
    assert resolve_skill_icon_path(meta) is None


def test_resolve_skill_icon_path_returns_path_when_present(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo",
                      "---\nname: demo\ndescription: D.\nicon: icon.png\n---")
    icon_file = os.path.join(d, "icon.png")
    with open(icon_file, "wb") as f:
        f.write(b"fake-png")
    meta = parse_skill_md(d)
    assert resolve_skill_icon_path(meta) == os.path.abspath(icon_file)


def test_resolve_skill_icon_path_rejects_path_traversal(tmp_path):
    d = _write_skill(str(tmp_path / "skills"), "demo",
                      "---\nname: demo\ndescription: D.\nicon: ../secret.png\n---")
    outside = os.path.join(str(tmp_path / "skills"), "secret.png")
    with open(outside, "wb") as f:
        f.write(b"fake-png")
    meta = parse_skill_md(d)
    assert resolve_skill_icon_path(meta) is None


# ---------------------------------------------------------------------------
# discover_skills + shadowing
# ---------------------------------------------------------------------------

def test_discover_skills_finds_directories(tmp_path, monkeypatch):
    base = str(tmp_path / "project" / "skills")
    _write_skill(base, "alpha", "---\nname: alpha\ndescription: A.\n---")
    _write_skill(base, "beta", "---\nname: beta\ndescription: B.\n---")

    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])
    names = {s["name"] for s in discover_skills(str(tmp_path / "project"))}
    assert names == {"alpha", "beta"}


def test_discover_skills_project_shadows_bundled(tmp_path, monkeypatch):
    proj = str(tmp_path / "proj" / "skills")
    bundled = str(tmp_path / "pkg" / "skills")
    _write_skill(proj, "shared", "---\nname: shared\ndescription: project version.\n---")
    _write_skill(bundled, "shared", "---\nname: shared\ndescription: bundled version.\n---")

    # project dir comes first → wins
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [proj, bundled])
    skills = discover_skills(str(tmp_path / "proj"))
    shared = [s for s in skills if s["name"] == "shared"]
    assert len(shared) == 1
    assert shared[0]["description"] == "project version."


# ---------------------------------------------------------------------------
# skills.yaml filtering
# ---------------------------------------------------------------------------

def test_read_skills_yaml_absent_returns_none(tmp_path):
    assert read_skills_yaml(str(tmp_path)) is None


def test_read_skills_yaml_lists_declared(tmp_path):
    (tmp_path / "skills.yaml").write_text("skills:\n  - foo\n  - bar\n", encoding="utf-8")
    assert read_skills_yaml(str(tmp_path)) == ["foo", "bar"]


def test_active_skills_without_yaml_loads_only_mandatory(tmp_path, monkeypatch):
    """A new project (no skills.yaml) loads ONLY the mandatory skills — dev skills
    are not auto-loaded; the user opts in with /addskill."""
    base = str(tmp_path / "proj" / "skills")
    _write_skill(base, "chat-orchestrator", "---\nname: chat-orchestrator\ndescription: core.\n---")
    _write_skill(base, "foo", "---\nname: foo\ndescription: F.\n---")
    _write_skill(base, "bar", "---\nname: bar\ndescription: B.\n---")
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])

    names = {s["name"] for s in active_skills(str(tmp_path / "proj"))}
    assert names == {"chat-orchestrator"}  # foo/bar NOT auto-loaded


def test_active_skills_with_yaml_filters_to_declared_plus_mandatory(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    base = str(proj / "skills")
    _write_skill(base, "chat-orchestrator", "---\nname: chat-orchestrator\ndescription: core.\n---")
    _write_skill(base, "foo", "---\nname: foo\ndescription: F.\n---")
    _write_skill(base, "bar", "---\nname: bar\ndescription: B.\n---")
    (proj / "skills.yaml").write_text("skills:\n  - foo\n", encoding="utf-8")
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])

    names = {s["name"] for s in active_skills(str(proj))}
    # mandatory chat-orchestrator + declared foo; bar excluded
    assert names == {"chat-orchestrator", "foo"}
    assert "chat-orchestrator" in MANDATORY_SKILLS


# ---------------------------------------------------------------------------
# level1_metadata + find_skill_dir
# ---------------------------------------------------------------------------

def test_level1_metadata_renders_name_and_description():
    skills = [
        {"name": "a", "description": "Does A."},
        {"name": "b", "description": "Does B."},
    ]
    out = level1_metadata(skills)
    assert "- a: Does A." in out
    assert "- b: Does B." in out


def test_find_skill_dir_returns_path(tmp_path, monkeypatch):
    base = str(tmp_path / "skills")
    d = _write_skill(base, "target", "---\nname: target\ndescription: T.\n---")
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])
    assert find_skill_dir("target", str(tmp_path)) == os.path.abspath(d)
    assert find_skill_dir("nope", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Real bundled skills load (chat-orchestrator must exist as a directory)
# ---------------------------------------------------------------------------

def test_bundled_chat_orchestrator_discoverable():
    skills = discover_skills("")
    names = {s["name"] for s in skills}
    assert "chat-orchestrator" in names


# ---------------------------------------------------------------------------
# skills.yaml editing (add/remove) — single source of truth for the active set
# ---------------------------------------------------------------------------

def test_add_skill_writes_skills_yaml_and_filters_active(tmp_path, monkeypatch):
    from gilzabots.skills import (
        add_skill_to_project, active_skills, read_skills_yaml,
    )
    base = str(tmp_path / "proj" / "skills")
    _write_skill(base, "chat-orchestrator", "---\nname: chat-orchestrator\ndescription: core.\n---")
    _write_skill(base, "foo", "---\nname: foo\ndescription: F.\n---")
    _write_skill(base, "bar", "---\nname: bar\ndescription: B.\n---")
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])
    proj = str(tmp_path / "proj")

    # No skills.yaml yet → adding seeds it with the discovered set; here we just
    # verify the unknown-skill guard and that a real add persists + activates.
    changed, _ = add_skill_to_project(proj, "does-not-exist")
    assert changed is False

    # Seed an explicit skills.yaml with only foo, then add bar.
    (tmp_path / "proj" / "skills.yaml").write_text("skills:\n  - foo\n", encoding="utf-8")
    changed, _ = add_skill_to_project(proj, "bar")
    assert changed is True
    assert set(read_skills_yaml(proj)) == {"foo", "bar"}
    names = {s["name"] for s in active_skills(proj)}
    assert names == {"chat-orchestrator", "foo", "bar"}


def test_remove_skill_updates_active_and_protects_mandatory(tmp_path, monkeypatch):
    from gilzabots.skills import remove_skill_from_project, active_skills
    base = str(tmp_path / "proj" / "skills")
    _write_skill(base, "chat-orchestrator", "---\nname: chat-orchestrator\ndescription: core.\n---")
    _write_skill(base, "foo", "---\nname: foo\ndescription: F.\n---")
    monkeypatch.setattr("gilzabots.skills.skill_search_dirs",
                        lambda project_path="": [base])
    proj = str(tmp_path / "proj")
    (tmp_path / "proj" / "skills.yaml").write_text("skills:\n  - foo\n", encoding="utf-8")

    # Mandatory skill cannot be removed.
    changed, _ = remove_skill_from_project(proj, "chat-orchestrator")
    assert changed is False

    changed, _ = remove_skill_from_project(proj, "foo")
    assert changed is True
    names = {s["name"] for s in active_skills(proj)}
    assert names == {"chat-orchestrator"}  # foo gone, mandatory stays
