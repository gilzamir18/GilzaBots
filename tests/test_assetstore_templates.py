"""Tests for LaTeX template assets in the Asset Store.

A template is a zipped directory of .tex files and their assets, described by a
YAML sidecar with the same stem. Installing unpacks it at the project root, which
is the one install target that can collide with the user's own files.
"""

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer

from gilzabots.assetstore import (
    VALID_TYPES,
    install_asset,
    list_assets,
    template_conflicts,
    template_entries,
    template_is_installed,
)


SIDECAR = """template:
  name: "Demo Template"
  version: "2.1.0"
  active: true
  Description: "A demo LaTeX template"
"""


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)


@pytest.fixture
def template(tmp_path, monkeypatch):
    """Publish a template into an isolated store and return its metadata."""
    import gilzabots.assetstore as store

    store_dir = tmp_path / "store" / "templates"
    store_dir.mkdir(parents=True)
    monkeypatch.setattr(store, "_STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path / "norepo")

    (store_dir / "demo-template.yaml").write_text(SIDECAR, encoding="utf-8")
    _write_zip(store_dir / "demo-template.zip", {
        "demo-template/main.tex": "\\documentclass{article}\n",
        "demo-template/figs/plot.png": "PNG",
        # macOS resource forks ride along in zips built on a Mac; they are not
        # template content and must not land in the user's project.
        "__MACOSX/demo-template/._main.tex": "junk",
        "demo-template/._figs": "junk",
    })
    return next(a for a in list_assets("template") if a["id"] == "demo-template")


def _project(tmp_path) -> str:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    return str(project)


def test_template_is_a_valid_asset_type():
    assert "template" in VALID_TYPES


def test_sidecar_fields_feed_the_store_listing(template):
    assert template["name"] == "Demo Template"
    assert template["version"] == "2.1.0"
    assert template["desc"] == "A demo LaTeX template"
    assert template["type"] == "template"


def test_inactive_template_is_hidden_from_the_store(tmp_path, monkeypatch, template):
    meta_path: Path = template["_meta"]
    meta_path.write_text(SIDECAR.replace("active: true", "active: false"), encoding="utf-8")

    assert [a for a in list_assets("template") if a["id"] == "demo-template"] == []


def test_sidecar_without_a_package_is_not_listed(template):
    store_dir: Path = template["_meta"].parent
    (store_dir / "orphan.yaml").write_text("template:\n  name: Orphan\n", encoding="utf-8")

    assert [a for a in list_assets("template") if a["id"] == "orphan"] == []


def test_install_unpacks_at_the_project_root_without_archive_junk(tmp_path, template):
    project = Path(_project(tmp_path))

    message = install_asset(template, str(project))

    assert (project / "demo-template" / "main.tex").is_file()
    assert (project / "demo-template" / "figs" / "plot.png").is_file()
    assert not (project / "__MACOSX").exists()
    assert not (project / "demo-template" / "._figs").exists()
    assert "Demo Template" in message
    assert template_entries(template) == [
        "demo-template/main.tex",
        "demo-template/figs/plot.png",
    ]
    assert template_is_installed(template, str(project)) is True


def test_install_refuses_to_overwrite_existing_files(tmp_path, template):
    project = Path(_project(tmp_path))
    (project / "demo-template").mkdir()
    (project / "demo-template" / "main.tex").write_text("my own work\n", encoding="utf-8")

    assert template_conflicts(template, str(project)) == ["demo-template/main.tex"]
    with pytest.raises(FileExistsError):
        install_asset(template, str(project))
    assert (project / "demo-template" / "main.tex").read_text(encoding="utf-8") == "my own work\n"


def test_install_with_overwrite_replaces_the_existing_files(tmp_path, template):
    project = Path(_project(tmp_path))
    (project / "demo-template").mkdir()
    (project / "demo-template" / "main.tex").write_text("my own work\n", encoding="utf-8")

    install_asset(template, str(project), overwrite=True)

    assert (project / "demo-template" / "main.tex").read_text(encoding="utf-8").startswith("\\documentclass")


def test_partially_present_template_is_not_reported_as_installed(tmp_path, template):
    project = Path(_project(tmp_path))
    (project / "demo-template").mkdir()
    (project / "demo-template" / "main.tex").write_text("x\n", encoding="utf-8")

    assert template_is_installed(template, str(project)) is False


def test_entry_escaping_the_project_is_rejected(tmp_path, monkeypatch, template):
    store_dir: Path = template["_meta"].parent
    _write_zip(store_dir / "demo-template.zip", {"../escaped.tex": "nope"})
    meta = next(a for a in list_assets("template") if a["id"] == "demo-template")
    project = Path(_project(tmp_path))

    with pytest.raises(ValueError):
        install_asset(meta, str(project), overwrite=True)
    assert not (project.parent / "escaped.tex").exists()


def test_bundled_example_template_is_visible_in_the_repo_store():
    """The repo's `templates/` directory is the shipped catalog, so it must load."""
    ids = {a["id"] for a in list_assets("template")}
    assert "template-sibgrapi-2024" in ids


def test_package_store_templates_are_discovered_when_repo_root_absent(tmp_path, monkeypatch):
    """When running from a package where repo root templates/ is missing, templates under _STORE_ROOT/templates load."""
    import gilzabots.assetstore as store

    pkg_templates_dir = tmp_path / "gilzabots" / "assetstore" / "templates"
    pkg_templates_dir.mkdir(parents=True)
    monkeypatch.setattr(store, "_STORE_ROOT", tmp_path / "gilzabots" / "assetstore")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path / "site-packages")

    (pkg_templates_dir / "packaged-template.yaml").write_text(SIDECAR, encoding="utf-8")
    _write_zip(pkg_templates_dir / "packaged-template.zip", {
        "packaged-template/main.tex": "\\documentclass{article}\n",
    })

    assets = list_assets("template")
    assert any(a["id"] == "packaged-template" for a in assets)


def test_pyproject_force_includes_templates():
    """pyproject.toml must force-include the templates directory into the wheel."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    force_include = (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    assert "templates" in force_include
    assert force_include["templates"] == "gilzabots/assetstore/templates"


# ---------------------------------------------------------------------------
# Asset Store endpoints
# ---------------------------------------------------------------------------


def _api_harness():
    """A server whose responses are collected instead of written to a socket."""
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8"))))

    server.send_response = mock_send_response
    return server, responses


async def _get(server, path, query):
    await server.route_api("GET", path, query, {}, b"", AsyncMock())


async def _post(server, path, payload):
    await server.route_api(
        "POST", path, {}, {}, json.dumps(payload).encode("utf-8"), AsyncMock()
    )


@pytest.mark.asyncio
async def test_assets_endpoint_reports_template_install_state(tmp_path, template):
    server, responses = _api_harness()
    project = Path(_project(tmp_path))

    await _get(server, "/api/assets", {"type": ["template"], "projectPath": [str(project)]})
    status, payload = responses[-1]
    assert status == 200
    entry = next(a for a in payload["assets"] if a["id"] == "demo-template")
    assert entry["version"] == "2.1.0"
    assert entry["installed"] is False
    assert entry["conflicts"] == []

    await _post(server, "/api/assets/install",
                {"id": "demo-template", "type": "template", "projectPath": str(project)})
    assert responses[-1][0] == 200

    await _get(server, "/api/assets", {"type": ["template"], "projectPath": [str(project)]})
    entry = next(a for a in responses[-1][1]["assets"] if a["id"] == "demo-template")
    assert entry["installed"] is True
    assert entry["conflicts"] == ["demo-template/figs/plot.png", "demo-template/main.tex"]


@pytest.mark.asyncio
async def test_install_endpoint_reports_what_it_wrote(tmp_path, template):
    """The UI turns this into the install confirmation, so it must name the
    template and count only the files that really landed in the project."""
    server, responses = _api_harness()
    project = Path(_project(tmp_path))

    await _post(server, "/api/assets/install",
                {"id": "demo-template", "type": "template", "projectPath": str(project)})

    status, payload = responses[-1]
    assert status == 200
    assert payload["success"] is True
    assert "Demo Template" in payload["message"]
    # main.tex and figs/plot.png — the archive junk in the zip is not counted.
    assert payload["files"] == 2


@pytest.mark.asyncio
async def test_install_endpoint_asks_before_replacing_project_files(tmp_path, template):
    server, responses = _api_harness()
    project = Path(_project(tmp_path))
    (project / "demo-template").mkdir()
    (project / "demo-template" / "main.tex").write_text("my own work\n", encoding="utf-8")

    await _post(server, "/api/assets/install",
                {"id": "demo-template", "type": "template", "projectPath": str(project)})
    status, payload = responses[-1]
    assert status == 409
    assert payload["conflicts"] == ["demo-template/main.tex"]
    assert (project / "demo-template" / "main.tex").read_text(encoding="utf-8") == "my own work\n"

    await _post(server, "/api/assets/install",
                {"id": "demo-template", "type": "template", "projectPath": str(project),
                 "overwrite": True})
    assert responses[-1][0] == 200
    assert (project / "demo-template" / "main.tex").read_text(encoding="utf-8").startswith("\\documentclass")


@pytest.mark.asyncio
async def test_install_endpoint_rejects_an_unknown_asset(tmp_path, template):
    server, responses = _api_harness()

    await _post(server, "/api/assets/install",
                {"id": "nope", "type": "template", "projectPath": _project(tmp_path)})
    assert responses[-1][0] == 404
