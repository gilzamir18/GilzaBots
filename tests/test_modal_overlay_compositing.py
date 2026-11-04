"""Full-window overlays must not blur the application behind them.

Under software compositing (`--disable-gpu`: the Linux default and the "off"
window-graphics mode on Windows, see PROJECT_DESIGN.md §2.16.1) a
`backdrop-filter` over the whole window re-blurs everything beneath it on every
frame that anything there repaints. Measured in QtWebEngine with a scrolling
list over a repainting page: ~326 ms per frame (p95 650 ms) with `blur(3px)`
against ~26 ms without it. That is what made the directory picker stutter and
flicker. The dimmed `background` of the overlay is kept; only the blur is gone.
"""

import pathlib
import re

GUI_SRC = pathlib.Path(__file__).resolve().parents[1] / "gui_src" / "src"


def test_no_component_sets_an_inline_backdrop_filter():
    offenders = [
        f"{path.relative_to(GUI_SRC)}:{lineno}"
        for path in GUI_SRC.rglob("*.jsx")
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "backdropFilter" in line
    ]
    assert offenders == []


def test_the_shared_modal_overlay_has_no_backdrop_filter():
    css = (GUI_SRC / "index.css").read_text(encoding="utf-8")
    rules = re.findall(r"\.vscode-modal-overlay\s*\{([^}]*)\}", css)
    assert rules, "the .vscode-modal-overlay rule is missing"
    assert all("backdrop-filter" not in body for body in rules)
