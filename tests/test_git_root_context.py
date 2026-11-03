import shutil
import subprocess
import json
import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import (
    AsyncHTTPServer,
    GitContextError,
    _discard_git_path,
    _ensure_gilzabots_git_excludes,
    _is_gilzabots_hidden_artifact,
    _is_workspace_hidden_by_extension,
    _project_path_to_repo_path,
    _repo_path_to_project_path,
    _resolve_git_context,
    get_file_tree,
)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


@pytest.mark.asyncio
async def test_git_status_endpoint_handles_project_without_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "GET",
        "/api/git/status",
        {"projectPath": [str(project)], "shadow": ["false"]},
        {},
        b"",
        writer,
    )

    assert responses == [
        (
            200,
            {
                "files": [],
                "git_available": False,
                "error": "Selected Git root does not contain a .git repository",
            },
            "application/json",
        )
    ]


@pytest.mark.asyncio
async def test_file_at_head_endpoint_handles_project_without_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("hello", encoding="utf-8")
    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "GET",
        "/api/git/file-at-head",
        {"projectPath": [str(project)], "shadow": ["false"], "filePath": ["main.tex"]},
        {},
        b"",
        writer,
    )

    assert responses == [
        (
            200,
            {
                "content": "",
                "source": "none",
                "git_available": False,
                "error": "Selected Git root does not contain a .git repository",
            },
            "application/json",
        )
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_user_git_root_inside_project_maps_paths_to_project_relative(tmp_path):
    project = tmp_path / "project"
    inner = project / "inner"
    inner.mkdir(parents=True)
    (project / "root.txt").write_text("root", encoding="utf-8")
    (inner / "a.txt").write_text("a", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "root")

    _init_repo(inner)
    _git(inner, "add", ".")
    _git(inner, "commit", "-m", "inner")

    ctx = _resolve_git_context(str(project), use_shadow=False, git_root_path=str(inner))

    assert _repo_path_to_project_path("a.txt", ctx) == "inner/a.txt"
    assert _project_path_to_repo_path("inner/a.txt", ctx) == "a.txt"

    (inner / "a.txt").write_text("changed", encoding="utf-8")
    status = subprocess.run(
        ctx["git_cmd"] + ["status", "--porcelain"],
        cwd=ctx["cwd"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    repo_path = status.splitlines()[0][3:].strip()
    assert repo_path == "a.txt"
    assert _repo_path_to_project_path(repo_path, ctx) == "inner/a.txt"


def test_user_git_root_must_stay_inside_project(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    with pytest.raises(GitContextError):
        _resolve_git_context(str(project), use_shadow=False, git_root_path=str(outside))


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_discard_restores_staged_modified_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "a.txt"
    file_path.write_text("original", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    file_path.write_text("changed", encoding="utf-8")
    _git(project, "add", "a.txt")

    ctx = _resolve_git_context(str(project), use_shadow=False)
    _discard_git_path(ctx, "a.txt")

    assert file_path.read_text(encoding="utf-8") == "original"
    assert _git(project, "status", "--porcelain").stdout == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_discard_removes_staged_new_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "base.txt").write_text("base", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    new_file = project / "new.txt"
    new_file.write_text("new", encoding="utf-8")
    _git(project, "add", "new.txt")

    ctx = _resolve_git_context(str(project), use_shadow=False)
    _discard_git_path(ctx, "new.txt")

    assert not new_file.exists()
    assert _git(project, "status", "--porcelain").stdout == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_discard_removes_untracked_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "base.txt").write_text("base", encoding="utf-8")
    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")
    untracked = project / "scratch.txt"
    untracked.write_text("scratch", encoding="utf-8")

    ctx = _resolve_git_context(str(project), use_shadow=False)
    _discard_git_path(ctx, "scratch.txt")

    assert not untracked.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_discard_keeps_the_file_when_git_cannot_answer(tmp_path):
    # A corrupt index makes `git ls-files` exit 128. That is not "untracked":
    # the tracked file the user asked to restore must not be deleted.
    project = tmp_path / "project"
    project.mkdir()
    tracked = project / "a.txt"
    tracked.write_text("original", encoding="utf-8")
    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")
    tracked.write_text("changed", encoding="utf-8")

    ctx = _resolve_git_context(str(project), use_shadow=False)
    (project / ".git" / "index").write_bytes(b"garbage")

    with pytest.raises(RuntimeError):
        _discard_git_path(ctx, "a.txt")
    assert tracked.read_text(encoding="utf-8") == "changed"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_git_stage_endpoint_stages_all_changes_in_one_request(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("a", encoding="utf-8")
    (project / "b.txt").write_text("b", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    (project / "a.txt").write_text("changed", encoding="utf-8")
    (project / "b.txt").unlink()
    (project / "c.txt").write_text("new", encoding="utf-8")

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "POST",
        "/api/git/stage",
        {},
        {},
        json.dumps({
            "projectPath": str(project),
            "filePath": "__all__",
            "action": "stage",
        }).encode("utf-8"),
        writer,
    )

    assert responses == [(200, {"success": True}, "application/json")]
    assert set(_git(project, "status", "--porcelain").stdout.splitlines()) == {
        "M  a.txt",
        "D  b.txt",
        "A  c.txt",
    }


def test_gilzabots_partial_artifacts_are_hidden_from_file_tree(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "chapter.tex").write_text("chapter", encoding="utf-8")
    (project / "gilzabots_partial_chapter.tex").write_text("generated", encoding="utf-8")
    (project / "gilzabots_partial_chapter.pdf").write_text("generated", encoding="utf-8")

    tree = get_file_tree(str(project))

    paths = {item["path"].replace("\\", "/") for item in tree}
    assert "chapter.tex" in paths
    assert "gilzabots_partial_chapter.tex" not in paths
    assert "gilzabots_partial_chapter.pdf" not in paths
    assert _is_gilzabots_hidden_artifact("nested/gilzabots_partial_chapter.aux")


def test_latex_auxiliary_files_are_hidden_from_file_tree_by_default(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("main", encoding="utf-8")
    (project / "main.aux").write_text("aux", encoding="utf-8")
    (project / "main.log").write_text("log", encoding="utf-8")
    (project / "main.synctex.gz").write_text("synctex", encoding="utf-8")
    (project / "main.run.xml").write_text("run xml", encoding="utf-8")

    tree = get_file_tree(str(project))
    paths = {item["path"].replace("\\", "/") for item in tree}

    assert paths == {"main.tex"}
    assert _is_workspace_hidden_by_extension("main.synctex.gz", [".synctex.gz"])
    assert _is_workspace_hidden_by_extension("main.run.xml", [".run.xml"])


def test_latex_auxiliary_files_can_be_shown_in_file_tree(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("main", encoding="utf-8")
    (project / "main.aux").write_text("aux", encoding="utf-8")

    tree = get_file_tree(str(project), show_hidden_files=True)
    paths = {item["path"].replace("\\", "/") for item in tree}

    assert paths == {"main.aux", "main.tex"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_gilzabots_partial_artifacts_are_excluded_from_user_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("main", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    ctx = _resolve_git_context(str(project), use_shadow=False)
    _ensure_gilzabots_git_excludes(ctx)
    (project / "gilzabots_partial_main.tex").write_text("generated", encoding="utf-8")
    (project / "gilzabots_partial_main.pdf").write_text("generated", encoding="utf-8")

    assert _git(project, "status", "--porcelain").stdout == ""


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_git_archive_endpoint_downloads_checkpoint_zip(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "main.tex"
    file_path.write_text("before", encoding="utf-8")

    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    file_path.write_text("after", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "second")
    commit = _git(project, "rev-parse", "HEAD").stdout.strip()

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response_with_headers(_writer, status_code, body, content_type="text/plain", extra_headers=None):
        responses.append((status_code, body, content_type, extra_headers or {}))

    server.send_response_with_headers = mock_send_response_with_headers

    await server.route_api(
        "GET",
        "/api/git/archive",
        {"projectPath": [str(project)], "shadow": ["false"], "commit": [commit]},
        {},
        b"",
        writer,
    )

    assert len(responses) == 1
    status_code, body, content_type, headers = responses[0]
    assert status_code == 200
    assert content_type == "application/zip"
    assert headers["Content-Disposition"].endswith(f'checkpoint-{commit[:12]}.zip"')

    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.read("main.tex").decode("utf-8") == "after"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_git_archive_endpoint_rejects_invalid_commit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("content", encoding="utf-8")
    _init_repo(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "GET",
        "/api/git/archive",
        {"projectPath": [str(project)], "shadow": ["false"], "commit": ["does-not-exist"]},
        {},
        b"",
        writer,
    )

    assert responses == [(400, {"error": "Invalid commit"}, "application/json")]


def test_repo_path_to_project_path_unquotes_and_unescapes():
    ctx = {
        "project_path": "/dummy/project",
        "repo_root": "/dummy/project",
        "repo_prefix": "",
    }
    # Standard quoted path with spaces/parentheses
    assert _repo_path_to_project_path('"file (1).pdf"', ctx) == "file (1).pdf"
    # Quoted path with UTF-8 octal escape sequences
    assert _repo_path_to_project_path('"Diret\\303\\263rio/A\\303\\247\\303\\272car.pdf"', ctx) == "Diretório/Açúcar.pdf"
