"""The line diff and the `diff_files` worker tool.

What matters most is that every line number in the output is a line number of
the file it came from: a model reads "a 42" and edits line 42. A diff of two
ranges that restarted numbering at 1 would send that edit to the wrong place.
"""

import asyncio
import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

from gilzabots.text_diff import line_diff, summarize_blocks

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPS = os.path.join(_REPO_ROOT, "skills", "elementary-ops", "scripts", "ops.py")


def _load_ops():
    spec = importlib.util.spec_from_file_location("elementary_ops", _OPS)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(tool, *args, **kwargs):
    raw = getattr(tool, "_func", None) or tool
    result = raw(*args, **kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


@pytest.fixture
def project(tmp_path):
    from gilzabots.tools import set_project_context

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    return tmp_path


# ---------------------------------------------------------------------------
# line_diff
# ---------------------------------------------------------------------------

def test_identical_inputs_report_no_blocks():
    result = line_diff(["a", "b"], ["a", "b"])
    assert result.identical and result.blocks == [] and result.hunks == []


def test_blocks_carry_the_original_line_numbers_of_both_sides():
    a = ["one", "two", "three"]
    b = ["one", "TWO", "extra", "three"]
    result = line_diff(a, b, a_first=40, b_first=100)
    assert result.blocks == [(41, 41, 101, 102)]
    assert (result.added, result.removed) == (2, 1)
    hunk = result.hunks[0]
    assert hunk[0] == "@@ a 41 | b 101-102 @@"
    assert any(line.startswith("- a41") and line.endswith("two") for line in hunk)
    assert any(line.startswith("+") and "b101" in line and line.endswith("TWO") for line in hunk)
    assert any(line.startswith("  a40") and "b100" in line for line in hunk)


def test_a_pure_insertion_says_where_it_goes():
    result = line_diff(["x", "y"], ["x", "new", "y"])
    assert result.blocks == [(2, 1, 2, 2)]
    assert "a none (before 2) -> b 2" == summarize_blocks(result.blocks)


def test_ignore_whitespace_compares_collapsed_spacing_but_shows_the_original():
    assert line_diff(["a  b "], ["a b"], ignore_whitespace=True).identical
    result = line_diff(["a  b", "c"], ["a b", "d"], ignore_whitespace=True)
    assert result.blocks == [(2, 2, 2, 2)]


def test_context_zero_shows_only_changed_lines():
    result = line_diff(["1", "2", "3", "4"], ["1", "2", "X", "4"], context=0)
    assert [line[0] for line in result.hunks[0][1:]] == ["-", "+"]


def test_the_skill_script_renders_exactly_like_the_module():
    """ops.py cannot import the application package, so it carries a copy."""
    ops = _load_ops()
    cases = [
        (["a", "b", "c", "d", "e", "f", "g", "h", "i"], ["a", "B", "c", "d", "e", "f", "g", "h", "i", "j"], 7, 1),
        (["same"], ["same"], 1, 1),
        ([], ["new"], 1, 1),
        (["x  y", "z"], ["x y", "w"], 3, 3),
    ]
    for a, b, a_first, b_first in cases:
        for ignore in (False, True):
            module = line_diff(a, b, a_first, b_first, context=2, ignore_whitespace=ignore)
            added, removed, blocks, hunks = ops.line_diff(a, b, a_first, b_first, 2, ignore)
            assert (added, removed, blocks, hunks) == (
                module.added, module.removed, module.blocks, module.hunks
            )
            assert ops.summarize_blocks(blocks) == summarize_blocks(module.blocks)


# ---------------------------------------------------------------------------
# diff_files tool
# ---------------------------------------------------------------------------

def test_diff_files_is_a_safe_worker_tool():
    from gilzabots.tools import diff_files, get_helper_tools

    assert "diff_files" in {getattr(t, "name", None) for t in get_helper_tools()}
    # Read-only: it must answer in plan mode without a permission prompt.
    assert getattr(diff_files, "name", "") == "diff_files"


def test_diff_files_answers_in_plan_mode(tmp_path):
    from gilzabots.tools import diff_files, set_project_context

    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan"))
    assert "IDENTICAL: no" in _call(diff_files, "a.txt", "b.txt")


def test_diff_files_reports_identical_files_in_one_line(project):
    from gilzabots.tools import diff_files

    (project / "a.tex").write_text("same\ntext\n", encoding="utf-8")
    (project / "b.tex").write_text("same\ntext\n", encoding="utf-8")
    out = _call(diff_files, "a.tex", "b.tex")
    assert out.splitlines()[-1] == "IDENTICAL: yes"


def test_diff_files_compares_ranges_with_file_line_numbers(project):
    from gilzabots.tools import diff_files

    lines = [f"line {i}" for i in range(1, 51)]
    (project / "a.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    changed = list(lines)
    changed[31] = "line 32 edited"
    (project / "b.tex").write_text("\n".join(changed) + "\n", encoding="utf-8")

    out = _call(diff_files, "a.tex", "b.tex", start_a=30, end_a=35, start_b=30, end_b=35)
    assert "a=a.tex lines 30-35 (6 lines)" in out
    assert "CHANGED: a 32 -> b 32" in out
    assert "- a32" in out and "+     b32  line 32 edited" in out


def test_diff_files_compares_two_ranges_of_one_file(project):
    from gilzabots.tools import diff_files

    (project / "notes.md").write_text("alpha\nbeta\n---\nalpha\ngamma\n", encoding="utf-8")
    out = _call(diff_files, "notes.md", "notes.md", start_a=1, end_a=2, start_b=4, end_b=5)
    assert "CHANGED: a 2 -> b 5" in out


def test_diff_files_pages_a_long_diff_without_losing_the_summary(project, monkeypatch):
    import gilzabots.tools as tools

    monkeypatch.setattr(tools, "_DIFF_PAGE_CHARS", 400)
    (project / "a.txt").write_text("".join(f"old {i}\n" for i in range(60)), encoding="utf-8")
    (project / "b.txt").write_text("".join(f"new {i}\n" for i in range(60)), encoding="utf-8")

    first = _call(tools.diff_files, "a.txt", "b.txt")
    assert "ADDED: 60 | REMOVED: 60" in first
    note = first.splitlines()[-1]
    assert note.startswith("[Showing diff lines 1-") and "offset=" in note
    offset = int(note.rsplit("offset=", 1)[1].split(")")[0])

    second = _call(tools.diff_files, "a.txt", "b.txt", offset=offset)
    assert "ADDED: 60 | REMOVED: 60" in second
    first_body = [l for l in first.splitlines() if l.startswith(("-", "+"))]
    second_body = [l for l in second.splitlines() if l.startswith(("-", "+"))]
    assert first_body and second_body and not set(first_body) & set(second_body)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"start_a": 10}, "beyond the end"),
        ({"start_a": 2, "end_a": 1}, "end must be >= start"),
        ({"start_a": -1}, r"0 \(whole file\)"),
        ({"context_lines": 11}, "context_lines"),
        ({"offset": 99}, "past the end of the diff"),
    ],
)
def test_diff_files_fails_fast_on_bad_arguments(project, kwargs, message):
    from gilzabots.tools import diff_files

    (project / "a.txt").write_text("1\n2\n3\n", encoding="utf-8")
    (project / "b.txt").write_text("1\nX\n3\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _call(diff_files, "a.txt", "b.txt", **kwargs)


def test_diff_files_refuses_missing_and_binary_files(project):
    from gilzabots.tools import diff_files

    (project / "a.txt").write_text("1\n", encoding="utf-8")
    (project / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ValueError, match="not found"):
        _call(diff_files, "a.txt", "missing.txt")
    with pytest.raises(ValueError, match="binary file"):
        _call(diff_files, "a.txt", "blob.bin")
