"""Tests for refreshing project-local skill copies (Asset Store update button).

A skill installed into `<project>/.gilzabots/skills/<name>/` shadows the bundled
copy of the same name, so it silently stops receiving updates. These cover the
detection of that drift and the two ways out: reinstall from the catalog, or drop
the local copy so the bundled one runs again.
"""

from pathlib import Path

import pytest

from gilzabots.assetstore import (
    asset_matches_install,
    install_asset,
    installed_skill_dir,
    register_asset,
)
from gilzabots.skills import local_skill_dir, shadowed_skill_dirs, MANDATORY_SKILLS


@pytest.fixture
def asset(tmp_path, monkeypatch):
    """Register a one-file skill asset in an isolated store and return its meta."""
    import gilzabots.assetstore as store

    monkeypatch.setattr(store, "_STORE_ROOT", tmp_path / "store")
    source = tmp_path / "src" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: Demo.\n---\nv1\n", encoding="utf-8")
    (source / "helper.py").write_text("# v1\n", encoding="utf-8")
    register_asset("skill", str(source), {"id": "demo", "type": "skill", "name": "demo", "desc": "Demo."})
    return next(a for a in store.list_assets("skill") if a["name"] == "demo")


def _project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return str(project)


def test_fresh_install_matches_the_catalog(tmp_path, asset):
    project = _project(tmp_path)
    install_asset(asset, project)

    assert installed_skill_dir(asset, project) == Path(project, ".gilzabots", "skills", "demo")
    assert asset_matches_install(asset, project) is True


def test_edited_local_copy_counts_as_drift(tmp_path, asset):
    project = _project(tmp_path)
    install_asset(asset, project)
    (Path(project) / ".gilzabots" / "skills" / "demo" / "helper.py").write_text("# hand-edited\n", encoding="utf-8")

    assert asset_matches_install(asset, project) is False


def test_extra_local_file_counts_as_drift(tmp_path, asset):
    project = _project(tmp_path)
    install_asset(asset, project)
    (Path(project) / ".gilzabots" / "skills" / "demo" / "stale.py").write_text("# left over\n", encoding="utf-8")

    assert asset_matches_install(asset, project) is False


def test_update_replaces_the_local_copy_and_drops_stale_files(tmp_path, asset):
    project = _project(tmp_path)
    install_asset(asset, project)
    local = Path(project) / ".gilzabots" / "skills" / "demo"
    (local / "stale.py").write_text("# removed upstream\n", encoding="utf-8")
    (local / "helper.py").write_text("# hand-edited\n", encoding="utf-8")

    message = install_asset(asset, project, replace=True)

    assert "updated" in message
    assert not (local / "stale.py").exists()
    assert (local / "helper.py").read_text(encoding="utf-8") == "# v1\n"
    assert asset_matches_install(asset, project) is True


def test_no_local_copy_is_not_a_match(tmp_path, asset):
    project = _project(tmp_path)
    assert installed_skill_dir(asset, project) is None
    assert asset_matches_install(asset, project) is False


def test_install_dir_rejects_a_name_escaping_the_skills_folder(tmp_path, asset):
    project = _project(tmp_path)
    # The update path deletes whatever this resolves to, so a traversing name
    # must resolve to nothing rather than to a directory outside the project.
    assert installed_skill_dir({**asset, "name": "../../etc"}, project) is None


def test_local_copy_shadowing_a_bundled_skill_is_detected(tmp_path):
    project = _project(tmp_path)
    for base in (Path(project) / ".gilzabots" / "skills" / "command-line",
                 Path(project) / "skills" / "command-line"):
        base.mkdir(parents=True)
        (base / "SKILL.md").write_text("---\nname: command-line\n---\n", encoding="utf-8")

    assert local_skill_dir("command-line", project) is not None
    # `<project>/skills` wins over `<project>/.gilzabots/skills`, so the local
    # copy here is the shadowed one -- either way the pair is reported.
    assert shadowed_skill_dirs("command-line", project)


def test_unshadowed_skill_reports_nothing_to_restore(tmp_path):
    project = _project(tmp_path)
    only = Path(project) / ".gilzabots" / "skills" / "solo"
    only.mkdir(parents=True)
    (only / "SKILL.md").write_text("---\nname: solo\n---\n", encoding="utf-8")

    assert shadowed_skill_dirs("solo", project) == []


def test_view_editor_is_no_longer_a_mandatory_skill():
    # Reading the editor is a native safe tool now (get_editor_state); the skill
    # could never do it in plan mode because it ran through run_python_script.
    assert "view-editor" not in MANDATORY_SKILLS
