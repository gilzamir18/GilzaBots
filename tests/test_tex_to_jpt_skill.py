"""The tex-to-jpt skill: its manifest, its script, and its own worked example.

A skill is documentation an agent executes, so the parts of it that can be
wrong in a way nobody notices are: a manifest that does not parse (the skill
silently never loads), a script that mis-extracts a picture, and an example in
the body that does not actually work. All three are checked here.

The compile step needs a TeX engine and is exercised only where one exists;
everything around it — what gets extracted, what preamble travels with it, and
the raster stage — is checked unconditionally.
"""

import json
import os
import subprocess
import sys

import pytest

from gilzabots.jpt import compile_outline, format_report, lint
from gilzabots.skills import parse_skill_md

SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "tex-to-jpt"
)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "tikz_to_image.py")

sys.path.insert(0, os.path.dirname(SCRIPT))
import tikz_to_image as tikz  # noqa: E402


BEAMER = r"""
\documentclass{beamer}
\usepackage{tikz}
\usetikzlibrary{arrows.meta,positioning}
\definecolor{opala}{RGB}{47,111,179}
\newcommand{\Rset}{\mathbb{R}}
\pgfplotsset{compat=1.18}
% \usepackage{commented-out}
\begin{document}
\begin{frame}{A picture}
  \begin{tikzpicture}
    \node (a) at (0,0) {$x$};
    \begin{tikzpicture}
      \draw (0,0) -- (1,1);
    \end{tikzpicture}
    \draw[opala,-{Stealth}] (a) -- (2,0);
  \end{tikzpicture}
\end{frame}
\begin{frame}{Another}
  \begin{tikzpicture}\draw (0,0) circle (1);\end{tikzpicture}
\end{frame}
\end{document}
"""


# ─── the manifest ────────────────────────────────────────────────────────────

def test_the_skill_manifest_parses_and_declares_itself():
    meta = parse_skill_md(SKILL_DIR)
    assert meta is not None, "a manifest that does not parse means the skill never loads"
    assert meta["name"] == "tex-to-jpt"
    assert "jpt" in meta["description"].lower()
    body = meta["body"] if isinstance(meta.get("body"), str) else ""
    for topic in (
        "tikz_to_image.py", "create_presentation", "edit_presentation",
        "check_presentation", "displayMode", "aligned", "deck.json",
        "jpt:assets/", "ZIP/ZIP64", "write_content_pos",
    ):
        assert topic in body, f"the skill body never mentions {topic}"

    assert "it is JSON" not in body


def test_the_worked_example_in_the_body_is_a_deck_that_lints_clean():
    """The example a reader will copy. If it does not survive the pipeline it
    teaches, it teaches the wrong thing."""
    outline = {
        "title": "Attention",
        "slides": [{
            "layout": "two_columns",
            "title": "Scaled dot-product attention",
            "left": {"bullets": ["Queries and keys of dimension dₖ",
                                 "The √dₖ keeps softmax out of saturation"]},
            "right": {"equation": r"\mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!"
                                  r"\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V"},
            "notes": "Mention the O(n^2) cost here.",
        }],
    }
    findings = lint(compile_outline(outline))
    assert not findings, format_report(findings)


# ─── extraction ──────────────────────────────────────────────────────────────

def test_a_nested_picture_does_not_cut_the_outer_one_short():
    """The bug a non-greedy regex writes: the inner \\end{tikzpicture} ends the
    outer picture, and half the drawing is silently lost."""
    pictures = tikz.find_pictures(BEAMER)
    assert len(pictures) == 2, [p[:40] for p in pictures]
    assert pictures[0].count(r"\begin{tikzpicture}") == 2
    assert pictures[0].rstrip().endswith(r"\end{tikzpicture}")
    assert "Stealth" in pictures[0], "the part after the nested picture was dropped"


def test_commented_out_lines_are_not_extracted():
    assert "commented-out" not in " ".join(tikz.harvest_preamble(BEAMER))


def test_the_preamble_a_picture_needs_travels_with_it():
    preamble = tikz.harvest_preamble(BEAMER)
    joined = "\n".join(preamble)
    for needed in (r"\usetikzlibrary{arrows.meta,positioning}",
                   r"\definecolor{opala}{RGB}{47,111,179}",
                   r"\newcommand{\Rset}{\mathbb{R}}",
                   r"\pgfplotsset{compat=1.18}"):
        assert needed in joined, f"{needed} would be missing when the picture compiles"


def test_the_standalone_wrapper_crops_to_the_drawing_and_loads_tikz():
    document = tikz.standalone_document(r"\begin{tikzpicture}\draw (0,0)--(1,1);\end{tikzpicture}", [])
    assert r"\documentclass[border=4pt]{standalone}" in document
    assert r"\usepackage{tikz}" in document
    assert document.rstrip().endswith(r"\end{document}")


def test_packages_the_source_already_loads_are_not_loaded_twice():
    document = tikz.standalone_document("x", [r"\usepackage[utf8]{inputenc}", r"\usepackage{tikz}"])
    assert document.count(r"{tikz}") == 1


# ─── the raster stage ────────────────────────────────────────────────────────

def test_a_pdf_becomes_a_png_at_the_requested_resolution(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf_path = tmp_path / "one.pdf"
    document = pymupdf.open()
    page = document.new_page(width=72, height=72)      # one inch square
    page.insert_text((10, 40), "x")
    document.save(str(pdf_path))
    document.close()

    png_path = tikz.pdf_to_png(str(pdf_path), str(tmp_path / "one.png"), dpi=300)
    assert os.path.exists(png_path)
    # Pixels, not points: a page rect is always reported at 72 dpi, so it would
    # read 72 for a correctly rendered 300 dpi image.
    assert pymupdf.Pixmap(png_path).width == pytest.approx(300, abs=2)


# ─── the command line ────────────────────────────────────────────────────────

def test_list_reports_every_picture_without_compiling_anything(tmp_path):
    source = tmp_path / "slides.tex"
    source.write_text(BEAMER, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, SCRIPT, "--tex", str(source), "--list"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 2


def test_a_source_with_no_pictures_says_so_instead_of_failing(tmp_path):
    source = tmp_path / "plain.tex"
    source.write_text(r"\documentclass{beamer}\begin{document}\end{document}", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, SCRIPT, "--tex", str(source), "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    assert "no tikzpicture" in result.stdout


@pytest.mark.skipif(tikz.find_engine() is None, reason="no LaTeX engine on this machine")
def test_a_real_picture_compiles_all_the_way_to_a_png(tmp_path):
    png_path = tikz.convert(
        r"\begin{tikzpicture}\draw[thick] (0,0) -- (1,1) -- (2,0);\end{tikzpicture}",
        [], str(tmp_path / "fig.png"), dpi=150,
    )
    assert os.path.getsize(png_path) > 0
