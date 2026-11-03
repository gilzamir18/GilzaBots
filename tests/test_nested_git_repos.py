"""Detection of nested Git repositories, the shadow-git blind spot.

A sub-directory carrying its own `.git` is recorded by the shadow repository as
a gitlink, so agent edits inside it never reach a turn checkpoint and the turn
is discarded as a no-op. `find_nested_git_repos` names those directories so the
Review UI and the log can say why entries are missing.
"""

import shutil
import subprocess

import pytest

from gilzabots.vcs import (
    NESTED_REPO_SCAN_MAX_DEPTH,
    find_nested_git_repos,
)


def _mark_as_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()


def test_project_without_nested_repos_reports_nothing(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "cap1.tex").write_text("x\n", encoding="utf-8")

    assert find_nested_git_repos(str(tmp_path)) == []


def test_project_own_repository_is_not_a_nested_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "book.tex").write_text("x\n", encoding="utf-8")

    assert find_nested_git_repos(str(tmp_path)) == []


def test_nested_repository_is_reported_with_relative_path(tmp_path):
    _mark_as_repo(tmp_path / "AgenticAIBook")

    assert find_nested_git_repos(str(tmp_path)) == ["AgenticAIBook"]


def test_nested_repository_below_the_root_is_reported(tmp_path):
    _mark_as_repo(tmp_path / "src" / "vendor")

    assert find_nested_git_repos(str(tmp_path)) == ["src/vendor"]


def test_git_as_a_file_counts_as_a_repository(tmp_path):
    # A worktree or a submodule checkout carries a `.git` *file*; it is a
    # gitlink for the shadow repository just the same.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")

    assert find_nested_git_repos(str(tmp_path)) == ["worktree"]


def test_repository_inside_a_repository_reports_only_the_outermost(tmp_path):
    _mark_as_repo(tmp_path / "book")
    _mark_as_repo(tmp_path / "book" / "figures")

    assert find_nested_git_repos(str(tmp_path)) == ["book"]


def test_skipped_directories_are_not_scanned(tmp_path):
    for skipped in ("node_modules", ".venv", "__pycache__", ".gilzabots"):
        _mark_as_repo(tmp_path / skipped / "pkg")
    _mark_as_repo(tmp_path / "book")

    assert find_nested_git_repos(str(tmp_path)) == ["book"]


def test_scan_stops_at_the_depth_limit(tmp_path):
    deep = tmp_path
    for level in range(NESTED_REPO_SCAN_MAX_DEPTH + 1):
        deep = deep / f"level{level}"
    _mark_as_repo(deep)

    assert find_nested_git_repos(str(tmp_path)) == []
    assert find_nested_git_repos(str(tmp_path), max_depth=NESTED_REPO_SCAN_MAX_DEPTH + 2)


def test_limit_caps_the_number_of_reported_repositories(tmp_path):
    for index in range(5):
        _mark_as_repo(tmp_path / f"repo{index}")

    assert len(find_nested_git_repos(str(tmp_path), limit=3)) == 3


def test_missing_project_path_reports_nothing(tmp_path):
    assert find_nested_git_repos(str(tmp_path / "does-not-exist")) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_edits_inside_a_nested_repo_leave_no_turn_checkpoint(tmp_path):
    """The symptom the detection exists to explain.

    An agent turn that edits only files inside a nested repository produces no
    net diff, so both checkpoints are discarded and the Review UI shows nothing
    -- while `find_nested_git_repos` names the folder responsible.
    """
    from gilzabots.vcs import begin_agent_turn_checkpoint, finalize_agent_turn_checkpoint

    inner = tmp_path / "book"
    inner.mkdir()
    subprocess.run(["git", "init"], cwd=inner, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=inner, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=inner, check=True, capture_output=True)
    chapter = inner / "cap1.tex"
    chapter.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=inner, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=inner, check=True, capture_output=True)

    start_checkpoint = begin_agent_turn_checkpoint(str(tmp_path))
    assert start_checkpoint
    chapter.write_text("edited by the agent\n", encoding="utf-8")
    finalize_agent_turn_checkpoint(str(tmp_path), start_checkpoint)

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" not in log
    assert "Agent turn end checkpoint" not in log
    assert find_nested_git_repos(str(tmp_path)) == ["book"]


@pytest.mark.asyncio
async def test_nested_repos_endpoint_lists_the_blind_spots(tmp_path):
    import json
    from unittest.mock import AsyncMock

    from gilzabots.ide_server import AsyncHTTPServer

    _mark_as_repo(tmp_path / "book")

    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, code, body, ctype="text/plain": responses.append(
        (code, json.loads(body.decode("utf-8")))
    )

    await server.route_api(
        "GET",
        "/api/git/nested-repos",
        {"projectPath": [str(tmp_path)]},
        {},
        b"",
        AsyncMock(),
    )

    assert responses == [(200, {"nested_repos": ["book"]})]


@pytest.mark.asyncio
async def test_nested_repos_endpoint_rejects_an_invalid_project_path(tmp_path):
    import json
    from unittest.mock import AsyncMock

    from gilzabots.ide_server import AsyncHTTPServer

    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, code, body, ctype="text/plain": responses.append(
        (code, json.loads(body.decode("utf-8")))
    )

    await server.route_api(
        "GET",
        "/api/git/nested-repos",
        {"projectPath": [str(tmp_path / "missing")]},
        {},
        b"",
        AsyncMock(),
    )

    assert responses[0][0] == 400
