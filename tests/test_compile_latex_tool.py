"""The `compile_latex` agent tool and the PATH its shells inherit.

Agents used to verify LaTeX with `run_command("pdflatex ...")` — the prompts
suggested exactly that — and, finding no such engine, closed their turns with
"could not compile". GilzaBots compiles with Tectonic, resolved outside PATH, so
the agent gets the IDE's own compile as a tool, and the shells the app starts
reach the tools the app manages.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _call(tool, *args, **kwargs):
    raw = getattr(tool, "_func", None) or tool
    result = raw(*args, **kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


@pytest.fixture
def project(tmp_path):
    from gilzabots.tools import set_project_context

    (tmp_path / "chapters").mkdir()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nSee \\ref{nope}.\n"
        "\\input{chapters/one}\n\\end{document}\n",
        encoding="utf-8",
    )
    (tmp_path / "chapters" / "one.tex").write_text("Chapter text.\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), main_file=""))
    return tmp_path


@pytest.fixture
def fake_compiler(monkeypatch):
    """Record compile requests and answer with a canned result."""
    from gilzabots import latex_compiler

    calls = []
    reply = {"result": {"success": True, "pdf_path": "", "log": "", "timing": {"compile_seconds": 1.25}}}

    def compile_latex(content, file_path=None, main_file="", project_dir="", **kwargs):
        calls.append({"file_path": file_path, "main_file": main_file, "project_dir": project_dir, **kwargs})
        return dict(reply["result"])

    monkeypatch.setattr(latex_compiler, "get_tectonic_path", lambda: "/opt/tectonic")
    monkeypatch.setattr(latex_compiler, "compile_latex", compile_latex)
    return SimpleNamespace(calls=calls, reply=reply)


# ---------------------------------------------------------------------------
# registration and authority
# ---------------------------------------------------------------------------

def test_compile_latex_is_a_workspace_action_for_the_main_agent_and_custom_agents():
    from gilzabots.agent_stdin import ALL_TOOLS_MAP
    from gilzabots.tools import get_helper_tools, get_workspace_action_tools

    assert "compile_latex" in {t.name for t in get_workspace_action_tools()}
    # Compiling checks a change, and changes are the main agent's.
    assert "compile_latex" not in {t.name for t in get_helper_tools()}
    assert "compile_latex" in ALL_TOOLS_MAP


def test_compile_latex_is_refused_in_plan_mode(tmp_path, fake_compiler):
    from gilzabots.tools import compile_latex, set_project_context

    (tmp_path / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan", main_file=""))
    # It writes a PDF, so it is an action: the mode gate stops it before it runs.
    assert _call(compile_latex).startswith("Execution blocked")
    assert fake_compiler.calls == []


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------

def test_no_path_compiles_the_projects_root_document(project, fake_compiler):
    from gilzabots.tools import compile_latex

    report = _call(compile_latex)
    assert report.startswith("SUCCESS: main.tex compiled (full compile in 1.2s, Tectonic)")
    call = fake_compiler.calls[0]
    assert call["main_file"] == "main.tex"
    assert call["project_dir"] == str(project)
    # Nothing is written back into the source: the file on disk is compiled.
    assert call["file_path"] is None
    assert call["draft"] is False


def test_a_chapter_compiles_through_the_document_that_includes_it(project, fake_compiler):
    from gilzabots.tools import compile_latex

    _call(compile_latex, "chapters/one.tex", draft=True)
    assert fake_compiler.calls[0]["main_file"] == "main.tex"
    assert fake_compiler.calls[0]["draft"] is True


def test_the_projects_configured_main_file_wins(project, fake_compiler):
    from gilzabots.tools import compile_latex, set_project_context

    (project / "other.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(project), main_file="other.tex"))
    _call(compile_latex)
    assert fake_compiler.calls[0]["main_file"] == "other.tex"


@pytest.mark.parametrize(
    "path, message",
    [
        ("missing.tex", "File not found"),
        ("notes.txt", r"compiles \.tex files"),
    ],
)
def test_bad_targets_fail_fast(project, fake_compiler, path, message):
    from gilzabots.tools import compile_latex

    (project / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _call(compile_latex, path)
    assert fake_compiler.calls == []


def test_a_file_outside_the_project_is_refused(project, fake_compiler, tmp_path_factory):
    from gilzabots.tools import compile_latex

    outside = tmp_path_factory.mktemp("elsewhere") / "x.tex"
    outside.write_text("\\documentclass{article}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the project"):
        _call(compile_latex, str(outside))


def test_a_project_without_a_root_document_asks_for_a_path(tmp_path, fake_compiler):
    from gilzabots.tools import compile_latex, set_project_context

    (tmp_path / "part.tex").write_text("Only a fragment.\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), main_file=""))
    with pytest.raises(ValueError, match="No LaTeX root document"):
        _call(compile_latex)


def test_missing_tectonic_says_so_and_points_to_the_installer(project, monkeypatch):
    from gilzabots import latex_compiler
    from gilzabots.tools import compile_latex

    monkeypatch.setattr(latex_compiler, "get_tectonic_path", lambda: None)
    with pytest.raises(ValueError, match="Tectonic is not installed.*Settings"):
        _call(compile_latex)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_success_reports_the_pdf_and_every_warning_once(project, fake_compiler):
    from gilzabots.tools import compile_latex

    (project / "main.log").write_text(
        "LaTeX Warning: Reference `nope' on page 1 undefined on input line 3.\n"
        "LaTeX Warning: There were undefined references.\n",
        encoding="utf-8",
    )
    fake_compiler.reply["result"] = {
        "success": True,
        "pdf_path": str(project / "main.pdf"),
        "log": "warning: main.tex:3: Overfull \\hbox\nwarning: main.tex:3: Overfull \\hbox\n"
               "warning: warnings were issued by the TeX engine; use --print and/or --keep-logs for details.\n",
        "timing": {"compile_seconds": 0.5},
    }
    report = _call(compile_latex)
    assert "PDF: main.pdf" in report
    assert "Warnings (2):" in report
    assert report.count("Overfull") == 1
    assert "Reference `nope' on page 1 undefined" in report
    assert "warnings were issued" not in report


def test_a_draft_pass_explains_its_undefined_references(project, fake_compiler):
    from gilzabots.tools import compile_latex

    (project / "main.log").write_text("LaTeX Warning: Citation `k' on page 1 undefined on input line 3.\n", encoding="utf-8")
    report = _call(compile_latex, draft=True)
    assert "draft pass" in report
    assert "a full compile resolves them" in report


def test_failure_returns_the_compiler_output(project, fake_compiler):
    from gilzabots.tools import compile_latex

    fake_compiler.reply["result"] = {
        "success": False,
        "log": "error: chapters/one:2: Undefined control sequence\n" + "x" * 10000,
        "timing": {"compile_seconds": 0.3},
    }
    report = _call(compile_latex)
    assert report.startswith("FAILED: main.tex did not compile")
    assert "error: chapters/one:2: Undefined control sequence" in report
    assert "[TRUNCATED]" in report and len(report) < 4000


@pytest.mark.parametrize("success", [True, False])
def test_the_ide_is_told_about_the_compile(project, fake_compiler, monkeypatch, success):
    # The PDF pane and Problems show the agent's compile as if Compile had been
    # pressed; without the event the pane kept the old PDF.
    from gilzabots import agent_stdin
    from gilzabots.tools import compile_latex

    events = []
    monkeypatch.setattr(agent_stdin, "event_hook", events.append)
    fake_compiler.reply["result"] = {"success": success, "pdf_path": "", "log": "LOG", "timing": {}}
    _call(compile_latex, draft=True)

    compiled = [e for e in events if e.get("event") == "latex_compiled"]
    assert len(compiled) == 1
    assert compiled[0]["project_path"] == str(project)
    assert compiled[0]["main_file"] == "main.tex"
    assert compiled[0]["success"] is success
    assert compiled[0]["draft"] is True and compiled[0]["log"] == "LOG"


@pytest.mark.skipif(
    not __import__("gilzabots.latex_compiler", fromlist=["get_tectonic_path"]).get_tectonic_path(),
    reason="Tectonic is not available",
)
def test_real_compile_reports_errors_with_their_location(project):
    from gilzabots.tools import compile_latex

    ok = _call(compile_latex, "chapters/one.tex")
    assert ok.startswith("SUCCESS: main.tex compiled")
    assert (project / "main.pdf").is_file()

    (project / "chapters" / "one.tex").write_text("Broken \\foo here.\n", encoding="utf-8")
    failed = _call(compile_latex, "chapters/one.tex")
    assert failed.startswith("FAILED")
    assert "Undefined control sequence" in failed


# ---------------------------------------------------------------------------
# PATH for the shells the app starts
# ---------------------------------------------------------------------------

def test_managed_tool_directories_are_prepended_once(monkeypatch, tmp_path):
    from gilzabots import external_tools

    tool = tmp_path / "bin" / "tectonic"
    monkeypatch.setattr(external_tools, "managed_tool_paths", lambda: [str(tool), str(tool)])
    env = external_tools.environment_with_managed_tools({"PATH": os.pathsep.join(["/usr/bin", "/bin"]), "X": "1"})
    assert env["PATH"].split(os.pathsep) == [str(tmp_path / "bin"), "/usr/bin", "/bin"]
    assert env["X"] == "1"

    # Already reachable: PATH is left exactly as it was.
    again = external_tools.environment_with_managed_tools(env)
    assert again["PATH"] == env["PATH"]


def test_no_managed_tools_leaves_path_untouched(monkeypatch):
    from gilzabots import external_tools

    monkeypatch.setattr(external_tools, "managed_tool_paths", lambda: [])
    assert external_tools.environment_with_managed_tools({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_run_command_reaches_a_managed_tool_outside_path(monkeypatch, tmp_path):
    import json

    from gilzabots import external_tools
    from gilzabots.tools import run_command, set_project_context

    bin_dir = tmp_path / "managed"
    bin_dir.mkdir()
    tool = bin_dir / "gilzabots-fake-engine"
    tool.write_text("#!/bin/sh\necho engine-ran\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(external_tools, "managed_tool_paths", lambda: [str(tool)])
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    result = json.loads(json.loads(_call(run_command, "gilzabots-fake-engine"))["result"])
    assert result["exit_code"] == 0
    assert result["stdout"] == "engine-ran"


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def _read(*parts):
    with open(os.path.join(_REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize(
    "manifest",
    [
        ("skills", "chat-orchestrator", "SKILL.md"),
        ("skills", "chat-orchestrator", "SKILL.light.md"),
        ("skills", "command-line", "SKILL.md"),
        ("skills", "command-line", "SKILL.light.md"),
        ("skills", "latex-assistant", "SKILL.md"),
    ],
)
def test_prompts_send_agents_to_compile_latex_not_pdflatex(manifest):
    body = _read(*manifest)
    assert "compile_latex" in body
    # pdflatex may only be named as what not to use, never offered as a command.
    assert 'run_command("pdflatex' not in body
    assert "`pdflatex main.tex`" not in body


def test_worker_tool_guidance_names_compile_latex():
    from gilzabots.prompt_profiles import _FULL_WORKER_TOOL_GUIDANCE

    assert "Tectonic" in _FULL_WORKER_TOOL_GUIDANCE["compile_latex"]
