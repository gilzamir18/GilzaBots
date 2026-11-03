"""
Tests for the LaTeX graphic (TikZ / PGFPlots) preview backend.

The `render_graphic_to_svg` function in `gilzabots.latex_compiler` compiles a
small standalone LaTeX document with Tectonic and converts the first page to
SVG via PyMuPDF. These tests exercise the function without invoking Tectonic
or PyMuPDF, focusing on:

  - Input validation (empty source, missing Tectonic)
  - In-process cache hit on the second call
  - Preamble wrapping behaviour

The actual end-to-end compile path is exercised manually through the IDE
because it depends on external binaries (tectonic, fitz) being installed on
the developer machine.
"""

import os
import sys
import types

import pytest


def _ensure_compiler_importable():
    """Make sure the gilzabots package is importable when running directly."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_compiler_importable()


def _reload_module_with(monkeypatch, tectonic_path=None, fitz_module=None):
    """Re-import `latex_compiler` with patched external dependencies.

    Tectonic and PyMuPDF are not always installed on CI. Tests can pass
    ``tectonic_path`` and ``fitz_module`` to control whether the function
    thinks they are available. ``tectonic_path`` is propagated by replacing
    `get_tectonic_path` itself (rather than `shutil.which`) because the
    real implementation also probes a bundled `bin/tectonic` binary.
    """
    if "gilzabots.latex_compiler" in sys.modules:
        del sys.modules["gilzabots.latex_compiler"]

    if fitz_module is not None:
        sys.modules["fitz"] = fitz_module

    from gilzabots import latex_compiler  # noqa: WPS433 (intentional re-import)

    # Patch the function after import so the local-path probe is short-circuited.
    monkeypatch.setattr(
        latex_compiler, "get_tectonic_path", lambda: tectonic_path
    )
    return latex_compiler


def test_render_graphic_rejects_empty_source(monkeypatch):
    """An empty snippet must fail gracefully with a helpful log."""
    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    result = lc.render_graphic_to_svg(graphic_source="   ")

    assert result["success"] is False
    assert result["svg"] == ""
    assert "Empty graphic source" in result["log"]


def test_render_graphic_reports_missing_tectonic(monkeypatch):
    """When Tectonic is not on PATH, return a friendly error."""
    lc = _reload_module_with(monkeypatch, tectonic_path=None)

    result = lc.render_graphic_to_svg(
        graphic_source="\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}"
    )

    assert result["success"] is False
    assert "tectonic" in result["log"].lower()


def test_render_graphic_uses_inprocess_cache(monkeypatch):
    """Identical cache_key returns the previously rendered SVG without a rebuild."""
    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    # Pre-seed the module-level cache to simulate an earlier successful render.
    lc.render_graphic_to_svg._cache["k1"] = {
        "svg": "<svg>cached</svg>",
        "ts": 0.0,
    }

    result = lc.render_graphic_to_svg(
        graphic_source="anything",
        cache_key="k1",
    )

    assert result["success"] is True
    assert result["cached"] is True
    assert result["svg"] == "<svg>cached</svg>"


def test_render_graphic_falls_back_to_default_preamble(monkeypatch):
    """When no preamble and no project are given, the function should still
    produce a valid standalone wrapper. We verify the side-effect by
    intercepting subprocess.run to capture the generated .tex file path.
    """
    captured = {}

    class _FakeResult:
        returncode = 1  # bail out before PDF inspection
        stdout = ""
        stderr = "stop here"

    def _fake_run(cmd, cwd, capture_output, encoding, errors, **_kwargs):
        # First positional arg is the cmd list; last .tex file path is the target.
        tex_path = cmd[-1]
        captured["cwd"] = cwd
        try:
            with open(tex_path, "r", encoding="utf-8") as f:
                captured["body"] = f.read()
        except OSError:
            captured["body"] = ""
        return _FakeResult()

    monkeypatch.setattr("subprocess.run", _fake_run)

    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    result = lc.render_graphic_to_svg(
        graphic_source="\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}",
        project_path="",
        preamble="",
        cache_key="",
    )

    assert result["success"] is False
    assert "documentclass" in captured.get("body", "")
    assert "tikz" in captured.get("body", "")
    assert "tikzpicture" in captured.get("body", "")


def test_render_graphic_does_not_override_existing_documentclass(monkeypatch):
    """If the user supplies a preamble that already has \\documentclass, we
    keep it as-is and just append the body."""

    captured = {}

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = ""

    def _fake_run(cmd, cwd, capture_output, encoding, errors, **_kwargs):
        tex_path = cmd[-1]
        try:
            with open(tex_path, "r", encoding="utf-8") as f:
                captured["body"] = f.read()
        except OSError:
            captured["body"] = ""
        return _FakeResult()

    monkeypatch.setattr("subprocess.run", _fake_run)

    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    user_preamble = (
        "\\documentclass[border=2pt]{standalone}\n"
        "\\usepackage{tikz}\n"
    )
    lc.render_graphic_to_svg(
        graphic_source="\\draw (0,0)--(2,2);",
        project_path="",
        preamble=user_preamble,
        cache_key="",
    )

    body = captured.get("body", "")
    # The user-supplied documentclass should be preserved (only one occurrence).
    assert body.count("\\documentclass") == 1
    assert "[border=2pt]{standalone}" in body


def test_render_graphic_expands_input_relative_to_source_tex(monkeypatch, tmp_path):
    """A figure-level \\input{fig1} should render the target file contents."""
    project = tmp_path / "project"
    section = project / "chapters"
    section.mkdir(parents=True)
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{tikz}\n"
        "\\begin{document}\n"
        "\\input{chapters/body}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (section / "body.tex").write_text(
        "\\begin{figure}\\input{fig1}\\end{figure}",
        encoding="utf-8",
    )
    (section / "fig1.tex").write_text(
        "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}",
        encoding="utf-8",
    )

    captured = {}

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "stop here"

    def _fake_run(cmd, cwd, capture_output, encoding, errors, **_kwargs):
        tex_path = cmd[-1]
        with open(tex_path, "r", encoding="utf-8") as f:
            captured["body"] = f.read()
        return _FakeResult()

    monkeypatch.setattr("subprocess.run", _fake_run)

    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    lc.render_graphic_to_svg(
        graphic_source="\\input{fig1}",
        project_path=str(project),
        source_tex="chapters/body.tex",
        cache_key="",
    )

    body = captured.get("body", "")
    assert "\\input{fig1}" not in body
    assert "\\begin{tikzpicture}" in body
    assert "\\draw (0,0)--(1,1);" in body


def test_render_graphic_collects_project_color_definitions(monkeypatch, tmp_path):
    """External TikZ files can rely on colors defined elsewhere in the project."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{tikz}\n"
        "\\begin{document}\n"
        "\\definecolor{headerNonAgent}{HTML}{8B1E1E}\n"
        "\\begin{figure}\\input{fig1}\\end{figure}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "fig1.tex").write_text(
        "\\begin{tikzpicture}\\node[fill=headerNonAgent] {A};\\end{tikzpicture}",
        encoding="utf-8",
    )

    captured = {}

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "stop here"

    def _fake_run(cmd, cwd, capture_output, encoding, errors, **_kwargs):
        tex_path = cmd[-1]
        with open(tex_path, "r", encoding="utf-8") as f:
            captured["body"] = f.read()
        return _FakeResult()

    monkeypatch.setattr("subprocess.run", _fake_run)

    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    lc.render_graphic_to_svg(
        graphic_source="\\input{fig1}",
        project_path=str(project),
        source_tex="main.tex",
        cache_key="",
    )

    body = captured.get("body", "")
    color_pos = body.find("\\definecolor{headerNonAgent}{HTML}{8B1E1E}")
    tikz_pos = body.find("\\node[fill=headerNonAgent]")
    assert color_pos != -1
    assert tikz_pos != -1
    assert color_pos < tikz_pos


def test_render_graphic_adds_common_tikz_libraries_with_project_preamble(monkeypatch, tmp_path):
    """The project preamble path must still support common diagram keys."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\usepackage{tikz}\n"
        "\\begin{document}\n"
        "\\begin{figure}\\input{fig1}\\end{figure}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project / "fig1.tex").write_text(
        "\\begin{tikzpicture}\\node[drop shadow] {A};\\end{tikzpicture}",
        encoding="utf-8",
    )

    captured = {}

    class _FakeResult:
        returncode = 1
        stdout = ""
        stderr = "stop here"

    def _fake_run(cmd, cwd, capture_output, encoding, errors, **_kwargs):
        tex_path = cmd[-1]
        with open(tex_path, "r", encoding="utf-8") as f:
            captured["body"] = f.read()
        return _FakeResult()

    monkeypatch.setattr("subprocess.run", _fake_run)

    lc = _reload_module_with(monkeypatch, tectonic_path="/bin/true")

    lc.render_graphic_to_svg(
        graphic_source="\\input{fig1}",
        project_path=str(project),
        source_tex="main.tex",
        cache_key="",
    )

    body = captured.get("body", "")
    assert "\\usetikzlibrary{arrows.meta,backgrounds,calc,fit,positioning,shadows}" in body
    assert "\\node[drop shadow]" in body
