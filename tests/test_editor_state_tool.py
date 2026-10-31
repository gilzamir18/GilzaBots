"""Tests for the native get_editor_state tool and the state the IDE stages for it.

The GUI writes <project>/.opalatex/_editor_state.json at the start of every agent
turn; the tool is the read side of that contract. It is a safe tool, so it must
answer in plan mode without a permission prompt -- exactly when the agent needs
to know which file the user is looking at.
"""

import asyncio
import json
from types import SimpleNamespace

from opalatex.tools import (
    editor_state_path,
    get_editor_state,
    set_project_context,
)


def _raw(tool):
    return getattr(tool, "_func", None) or tool


def _stage(tmp_path, **state):
    path = editor_state_path(str(tmp_path))
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


def _run(tmp_path, mode="auto", **kwargs):
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode=mode))
    return asyncio.run(_raw(get_editor_state)(**kwargs))


def test_reports_open_tabs_focused_file_and_selection(tmp_path):
    _stage(
        tmp_path,
        current_file="report.tex",
        open_files=["report.tex", "refs.bib"],
        selected_text="\\section{Results}",
        editor_content="line 1\nline 2\n",
    )

    out = _run(tmp_path)

    assert "`report.tex`" in out
    assert "`refs.bib`" in out
    assert "Open tabs (2)" in out
    assert "<- focused" in out
    assert "\\section{Results}" in out


def test_live_buffer_is_opt_in(tmp_path):
    _stage(tmp_path, current_file="a.tex", open_files=["a.tex"], editor_content="UNSAVED BUFFER")

    without = _run(tmp_path)
    assert "UNSAVED BUFFER" not in without
    assert "include_content=True" in without

    with_content = _run(tmp_path, include_content=True)
    assert "UNSAVED BUFFER" in with_content


def test_missing_state_explains_itself_instead_of_failing(tmp_path):
    out = _run(tmp_path)
    assert "No editor state has been staged" in out


def test_absent_tab_list_is_not_reported_as_no_tabs(tmp_path):
    # The inline editor posts current_file without a tab list. Saying "no tabs
    # open" there would be a fabricated fact, not a missing one.
    _stage(tmp_path, current_file="a.tex", selected_text="x")
    out = _run(tmp_path)
    assert "not reported by this turn's caller" in out


def test_empty_selection_is_stated_explicitly(tmp_path):
    _stage(tmp_path, current_file="a.tex", open_files=["a.tex"], selected_text="")
    out = _run(tmp_path)
    assert "(nothing selected)" in out


def test_focus_outside_the_reported_tabs_is_flagged(tmp_path):
    _stage(tmp_path, current_file="c.tex", open_files=["a.tex", "b.tex"])
    out = _run(tmp_path)
    assert "changed tabs after this turn started" in out


def test_runs_in_plan_mode_without_a_permission_prompt(tmp_path):
    # Unsafe tools return "Execution blocked" in plan mode; this one must not.
    _stage(tmp_path, current_file="a.tex", open_files=["a.tex"])
    out = _run(tmp_path, mode="plan")
    assert "Execution blocked" not in out
    assert "`a.tex`" in out


def test_tool_is_offered_to_orchestrator_and_helpers():
    from opalatex.tools import get_helper_tools
    from opalatex.agent_stdin import ALL_TOOLS_MAP

    assert any(getattr(t, "name", "") == "get_editor_state" for t in get_helper_tools())
    assert "get_editor_state" in ALL_TOOLS_MAP


# ─── Staging side (what the IDE writes before each turn) ──────────────────────


def test_stage_editor_state_records_the_tab_list(tmp_path):
    from opalatex.agent_stdin import stage_editor_state

    staged = stage_editor_state(str(tmp_path), {
        "current_file": "a.tex",
        "open_files": ["a.tex", "b.tex"],
        "editor_content": "x",
        "selected_text": "",
    })

    assert staged["open_files"] == ["a.tex", "b.tex"]
    with open(editor_state_path(str(tmp_path)), encoding="utf-8") as f:
        assert json.load(f)["open_files"] == ["a.tex", "b.tex"]


def test_absent_open_files_preserves_the_previous_list(tmp_path):
    # A caller that does not know the tab list omits the key. Overwriting the
    # staged one with [] would make the next get_editor_state report "no tabs",
    # so absent and empty have to stay distinct.
    from opalatex.agent_stdin import stage_editor_state

    stage_editor_state(str(tmp_path), {"current_file": "a.tex", "open_files": ["a.tex", "b.tex"]})
    staged = stage_editor_state(str(tmp_path), {"current_file": "a.tex"})

    assert staged["open_files"] == ["a.tex", "b.tex"]


def test_explicit_empty_open_files_clears_the_list(tmp_path):
    # Closing every tab is a fact the caller does know, and must be recorded.
    from opalatex.agent_stdin import stage_editor_state

    stage_editor_state(str(tmp_path), {"current_file": "a.tex", "open_files": ["a.tex"]})
    staged = stage_editor_state(str(tmp_path), {"current_file": "", "open_files": []})

    assert staged["open_files"] == []
