"""`get_file_tree` walks with os.scandir and must answer exactly as before.

The previous implementation (os.listdir + os.path.isdir per entry) is kept here
as the oracle. scandir avoids the per-entry stat on Windows, where it is the
expensive part of the walk; nothing about the result may change, including the
edge cases os.path.isdir decided silently: symlinks, broken links, link loops
and unreadable directories.
"""

import os
import sys

import pytest

from gilzabots.ide_server import (
    _is_gilzabots_hidden_artifact,
    _is_workspace_hidden_by_extension,
    get_file_tree,
)


def _reference_tree(dir_path, root_path=None, show_hidden_files=False, hidden_extensions=None):
    if root_path is None:
        root_path = dir_path
    files = []
    try:
        items = os.listdir(dir_path)
    except Exception:
        return []
    for item in items:
        if item in ['node_modules', '.git', '.venv', '.env', '__pycache__', '.pytest_cache']:
            continue
        if _is_gilzabots_hidden_artifact(item):
            continue
        full_path = os.path.join(dir_path, item)
        rel_path = os.path.relpath(full_path, root_path)
        if os.path.isdir(full_path):
            files.append({
                "name": item,
                "path": rel_path,
                "isDirectory": True,
                "children": _reference_tree(full_path, root_path, show_hidden_files, hidden_extensions),
            })
        else:
            if not show_hidden_files and _is_workspace_hidden_by_extension(item, hidden_extensions):
                continue
            files.append({"name": item, "path": rel_path, "isDirectory": False})
    files.sort(key=lambda x: (not x["isDirectory"], x["name"].lower()))
    return files


def _build_project(root):
    (root / "chapters" / "deep" / "deeper").mkdir(parents=True)
    (root / "figures").mkdir()
    for name in ("main.tex", "main.aux", "main.log", "main.synctex.gz", "refs.bib", "Notes.md", "notes.md"):
        (root / name).write_text(name, encoding="utf-8")
    (root / "chapters" / "intro.tex").write_text("i", encoding="utf-8")
    (root / "chapters" / "deep" / "deeper" / "x.tex").write_text("x", encoding="utf-8")
    (root / "figures" / "plot.pdf").write_bytes(b"%PDF")
    (root / "figures" / "ação ü.png").write_bytes(b"png")
    for skipped in ("node_modules", ".git", ".venv", "__pycache__", ".pytest_cache"):
        (root / skipped).mkdir()
        (root / skipped / "inner.txt").write_text("skip", encoding="utf-8")
    (root / ".env").write_text("KEY=1", encoding="utf-8")
    (root / "gilzabots_partial_main.tex").write_text("hidden artifact", encoding="utf-8")
    (root / "empty").mkdir()


def _assert_same(root):
    for show_hidden in (False, True):
        assert get_file_tree(str(root), show_hidden_files=show_hidden) == _reference_tree(
            str(root), show_hidden_files=show_hidden
        )
    # A caller-supplied extension list is honoured unchanged.
    assert get_file_tree(str(root), hidden_extensions=[".tex"]) == _reference_tree(
        str(root), hidden_extensions=[".tex"]
    )


def test_matches_the_listdir_walk_on_an_ordinary_project(tmp_path):
    _build_project(tmp_path)
    _assert_same(tmp_path)


def test_matches_with_a_trailing_separator_and_a_relative_root(tmp_path, monkeypatch):
    _build_project(tmp_path)
    with_sep = str(tmp_path) + os.sep
    assert get_file_tree(with_sep) == _reference_tree(with_sep)
    monkeypatch.chdir(tmp_path)
    assert get_file_tree(".") == _reference_tree(".")


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs privileges on Windows")
def test_matches_on_symlinks_broken_links_and_loops(tmp_path):
    _build_project(tmp_path)
    os.symlink(tmp_path / "chapters", tmp_path / "linked_dir")
    os.symlink(tmp_path / "main.tex", tmp_path / "linked_file.tex")
    os.symlink(tmp_path / "missing", tmp_path / "broken_link")
    os.symlink(tmp_path / "loop_b", tmp_path / "loop_a")
    os.symlink(tmp_path / "loop_a", tmp_path / "loop_b")
    _assert_same(tmp_path)
    names = {entry["name"]: entry["isDirectory"] for entry in get_file_tree(str(tmp_path))}
    assert names["linked_dir"] is True
    assert names["broken_link"] is False
    assert names["loop_a"] is False


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_matches_on_an_unreadable_directory(tmp_path):
    _build_project(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.tex").write_text("s", encoding="utf-8")
    locked.chmod(0)
    try:
        _assert_same(tmp_path)
        entry = next(e for e in get_file_tree(str(tmp_path)) if e["name"] == "locked")
        assert entry["isDirectory"] is True and entry["children"] == []
    finally:
        locked.chmod(0o755)


def test_a_missing_directory_is_an_empty_tree(tmp_path):
    assert get_file_tree(str(tmp_path / "nope")) == [] == _reference_tree(str(tmp_path / "nope"))
