"""Checking a deck the way a reviewer would, before anyone projects it.

`layout.py` makes a good deck hard to get wrong; this makes a bad one hard to
ship. They are separate on purpose: the linter sees every deck, including the
ones that came through the raw-elements escape hatch, the ones an agent wrote
by hand, and the ones a user has been editing for a week.

Two rules shape what is in here:

  * **A finding names a defect the reader would notice.** Not a style opinion —
    nothing here says a slide should have three bullets rather than four. Text
    that does not fit its box, an element off the edge of the slide, type too
    small to read from the back of a room, grey-on-white nobody can make out:
    those are defects, and they are all this reports.
  * **Errors are things that are wrong, warnings are things that are probably
    wrong.** The distinction is load-bearing, because `create_presentation`
    refuses to write a deck with errors and happily writes one with warnings.
    Anything estimated rather than measured (see `metrics.py`) can only ever be
    a warning unless it is wrong by a wide margin.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import imagestats, metrics, model
from .model import background_of, text_color_of

# WCAG AA for large text. Slide type is large by definition, and the AAA
# threshold would flag colour schemes that project perfectly well.
MIN_CONTRAST = 3.0

# Type smaller than this is a defect on a projector, whatever the design says.
MIN_READABLE_SIZE = 16
COMFORTABLE_SIZE = 20

# How much of the area under a piece of text may fall below MIN_CONTRAST before
# the text is reported as hard to read. Not zero: a single stray pixel of a
# background picture grazing the corner of a box is not a legibility problem.
MAX_POOR_AREA = 0.06

# How much of the smaller element two elements may share before it reads as a
# collision rather than as a deliberate overlap.
OVERLAP_RATIO = 0.18

# An element covering this much of the slide is a background, and a background
# is meant to sit under things.
BACKGROUND_RATIO = 0.85

MAX_BULLETS = 7
MAX_WORDS = 70

LEVELS = ("error", "warning", "info")


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    slide: int | None = None          # 1-based, as a user counts slides
    element: str | None = None

    def __str__(self) -> str:
        where = ""
        if self.slide is not None:
            where = f"slide {self.slide}"
            if self.element:
                where += f", element {self.element}"
            where += ": "
        return f"[{self.level}] {self.code} — {where}{self.message}"


def has_errors(findings: list[Finding]) -> bool:
    return any(finding.level == "error" for finding in findings)


# ─── colour ──────────────────────────────────────────────────────────────────

def _rgb(color: str | None) -> tuple[float, float, float] | None:
    if not isinstance(color, str):
        return None
    value = color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str | None, b: str | None) -> float | None:
    """WCAG contrast between two colours, or None when either is not a plain
    hex colour this can reason about."""
    ca, cb = _rgb(a), _rgb(b)
    if ca is None or cb is None:
        return None
    la, lb = _luminance(ca), _luminance(cb)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ─── geometry ────────────────────────────────────────────────────────────────

def _intersection(a: dict[str, Any], b: dict[str, Any]) -> float:
    left = max(a["x"], b["x"])
    top = max(a["y"], b["y"])
    right = min(a["x"] + a["w"], b["x"] + b["w"])
    bottom = min(a["y"] + a["h"], b["y"] + b["h"])
    return max(0.0, right - left) * max(0.0, bottom - top)


def _area(element: dict[str, Any]) -> float:
    return max(1.0, element["w"] * element["h"])


# ─── LaTeX ───────────────────────────────────────────────────────────────────

def check_latex(latex: str) -> str | None:
    """A syntactic complaint about a formula, or None.

    Deliberately shallow. KaTeX is the authority on whether a formula renders
    and KaTeX does not run here, so this only catches the mistakes that are
    certain without it — and the one an agent actually makes, which is wrapping
    the source in the `$` delimiters the field does not want.
    """
    source = latex.strip()
    if not source:
        return None
    if "$" in source:
        return ("the latex field holds the formula itself, without $ delimiters "
                "— remove them")
    depth = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return "unbalanced braces: a '}' with no '{' before it"
        index += 1
    if depth:
        return f"unbalanced braces: {depth} unclosed '{{'"

    begins = source.count("\\begin{")
    ends = source.count("\\end{")
    if begins != ends:
        return f"{begins} \\begin and {ends} \\end in the same formula"
    return None


# ─── the checks ──────────────────────────────────────────────────────────────

def lint(deck: dict[str, Any], *, project_root: str | None = None) -> list[Finding]:
    """Every defect this can see in `deck`, worst first."""
    findings: list[Finding] = []
    width, height = deck["width"], deck["height"]

    for number, slide in enumerate(deck["slides"], start=1):
        elements = slide.get("elements", [])
        resolved = background_of(deck, slide)
        # Contrast against a photograph is not a number this can compute: the
        # text sits over whatever pixels happen to be behind it. Reporting the
        # colour-on-colour ratio anyway would be a confident wrong answer, so
        # the check is skipped and the reader is told to use their eyes.
        background = None if resolved["image"] else resolved["color"]
        findings += _check_slide_background(deck, slide, number, resolved, project_root)

        if not elements:
            findings.append(Finding(
                "warning", "empty-slide", "the slide has no elements", number,
            ))

        for element in elements:
            findings += _check_element(element, number, deck, background, project_root)

        findings += _check_overlaps(elements, number, width, height)
        findings += _check_density(elements, number)

    findings += _check_title_consistency(deck)
    order = {level: index for index, level in enumerate(LEVELS)}
    return sorted(findings, key=lambda f: (order.get(f.level, 9), f.slide or 0))


def ink_box(element: dict[str, Any]) -> dict[str, float]:
    """Where the glyphs of a text element actually are, not where its box is.

    A title box spans the whole slide width and its text usually does not, so
    measuring the picture under the *box* asks about a region the reader never
    looks at. The same wrapping and alignment the renderer applies decide this.
    """
    text = element.get("text", "")
    size = element["fontSize"]
    bold = bool(element.get("bold"))
    insets = model.line_insets_em(element)
    lines = (metrics.wrap_lines(text, element["w"], size, bold, insets)
             if text.strip() else [("", 0.0)])
    widest = max((inset + metrics.text_width(line, size, bold) for line, inset in lines),
                 default=0.0)
    widest = min(max(widest, 1.0), element["w"])
    height = min(max(len(lines) * size * element.get("lineHeight", 1.3), 1.0), element["h"])

    align = element.get("align", "left")
    x = element["x"]
    if align == "center":
        x += (element["w"] - widest) / 2
    elif align == "right":
        x += element["w"] - widest

    valign = element.get("valign", "top")
    y = element["y"]
    if valign == "middle":
        y += (element["h"] - height) / 2
    elif valign == "bottom":
        y += element["h"] - height
    return {"x": x, "y": y, "w": widest, "h": height}


def _check_text_over_picture(deck: dict[str, Any], slide: dict[str, Any], element: dict[str, Any],
                             number: int, resolved: dict[str, Any],
                             project_root: str | None) -> list[Finding]:
    """Whether this text can be read over the picture behind it.

    Measured, not guessed. The picture is decoded, the region under the glyphs
    is sampled, and the contrast against the ink is computed for each sample —
    so a pale background chosen for text to sit on raises nothing, and a
    photograph with a dark quarter raises exactly where it matters.
    """
    stats = imagestats.under(
        resolved["image"], ink_box(element),
        deck_w=deck["width"], deck_h=deck["height"],
        fit=resolved["fit"], project_root=project_root,
    )
    if stats is None:
        # Nothing could be decoded — an http: URL, or a picture this build
        # cannot read. Saying so beats both silence and a guess.
        return [Finding(
            "warning", "text-over-picture",
            "text sits over a background picture this check could not read; "
            "confirm by eye that it is legible",
            number, element["id"],
        )]

    ink = text_color_of(element, deck["theme"])
    legibility = imagestats.legibility(stats, ink, minimum=MIN_CONTRAST)
    if legibility is None or legibility["poor"] <= MAX_POOR_AREA:
        return []
    return [Finding(
        "warning", "low-contrast",
        f"{legibility['poor'] * 100:.0f}% of the picture under this text is within "
        f"{MIN_CONTRAST}:1 of {ink} — dim the background (backgroundOpacity around "
        "0.4) or move the text",
        number, element["id"],
    )]


def _check_slide_background(deck: dict[str, Any], slide: dict[str, Any], number: int,
                            resolved: dict[str, Any],
                            project_root: str | None) -> list[Finding]:
    findings: list[Finding] = []
    image = resolved["image"]
    if not image:
        return findings

    if not image.startswith(("data:", "jpt:", "http:", "https:", "blob:")) and project_root:
        path = image if os.path.isabs(image) else os.path.join(project_root, image)
        if not os.path.exists(path):
            findings.append(Finding(
                "error", "missing-background",
                f"the background picture {image} does not exist in the project",
                number,
            ))
            return findings

    for element in slide.get("elements", []):
        if element["type"] == "text" and element.get("text", "").strip():
            findings += _check_text_over_picture(
                deck, slide, element, number, resolved, project_root)
    return findings


def _check_element(element: dict[str, Any], number: int, deck: dict[str, Any],
                   background: str | None, project_root: str | None) -> list[Finding]:
    findings: list[Finding] = []
    eid = element["id"]
    width, height = deck["width"], deck["height"]

    # Off the slide. The unrotated box is what is checked, exactly as the
    # editor's own clamp does — a rotated element's corners may reach further,
    # and flagging that would make every deliberate tilt a finding.
    if (element["x"] + element["w"] <= 0 or element["y"] + element["h"] <= 0
            or element["x"] >= width or element["y"] >= height):
        findings.append(Finding(
            "error", "off-slide", "the element is entirely outside the slide",
            number, eid,
        ))
    elif (element["x"] < -1 or element["y"] < -1
          or element["x"] + element["w"] > width + 1
          or element["y"] + element["h"] > height + 1):
        findings.append(Finding(
            "warning", "crosses-edge",
            "the element runs past the edge of the slide and will be clipped",
            number, eid,
        ))

    if element["type"] == "text":
        findings += _check_text(element, number, deck, background)
    elif element["type"] == "equation":
        findings += _check_equation(element, number, deck, background)
    elif element["type"] == "image":
        findings += _check_image(element, number, project_root)
    elif element["type"] == "video":
        findings += _check_video(element, number, project_root)
    return findings


def _check_text(element: dict[str, Any], number: int, deck: dict[str, Any],
                background: str | None) -> list[Finding]:
    findings: list[Finding] = []
    eid = element["id"]
    text = element["text"]

    if not text.strip():
        findings.append(Finding(
            "warning", "empty-text", "the text box is empty", number, eid,
        ))
        return findings

    size = element["fontSize"]
    # A list starts after its marker and its indentation, so it has less width
    # to wrap into than the box suggests — which is exactly when a deck an agent
    # wrote overflows without anything looking wrong in the JSON.
    insets = model.line_insets_em(element)
    lines = metrics.wrap_lines(text, element["w"], size, element["bold"], insets)
    needed = len(lines) * size * element["lineHeight"]
    if needed > element["h"] * metrics.FIT_RATIO:
        # Estimated, so the level follows how badly it misses: a hair over is a
        # warning, half a box over is not a matter of opinion.
        level = "error" if needed > element["h"] * 1.15 else "warning"
        findings.append(Finding(
            level, "text-overflow",
            f"the text needs about {needed:.0f} units of height in a box "
            f"{element['h']:.0f} tall — shorten it, or give it more room",
            number, eid,
        ))
    widest = max(inset + metrics.text_width(line, size, element["bold"])
                 for line, inset in lines)
    if widest > element["w"] * 1.02:
        findings.append(Finding(
            "warning", "text-overflow",
            "a word is wider than its box and will overflow or break oddly",
            number, eid,
        ))

    if size < MIN_READABLE_SIZE:
        findings.append(Finding(
            "error", "tiny-type",
            f"{size:g} units is too small to read when projected "
            f"(minimum {MIN_READABLE_SIZE})",
            number, eid,
        ))
    elif size < COMFORTABLE_SIZE:
        findings.append(Finding(
            "warning", "small-type",
            f"{size:g} units is small for a slide; {COMFORTABLE_SIZE} or more reads better",
            number, eid,
        ))

    # Math left in a text box. A `.jpt` text element is plain text — there is no
    # inline math in it — so `$x^2$` copied out of a Beamer bullet renders as
    # those five characters. It is the single most likely mistake when
    # converting a LaTeX deck, and it is invisible until someone reads the slide.
    if _looks_like_math(text):
        findings.append(Finding(
            "warning", "math-in-text",
            "this text box contains math delimiters, and a text box renders them "
            "literally — move the formula into an equation element, or write it "
            "as plain characters",
            number, eid,
        ))

    findings += _check_contrast(element.get("color") or deck["theme"].get("color"),
                               background, number, eid)
    return findings


# Conservative on purpose: "costs $5" and "$PATH" are ordinary prose, so a bare
# pair of dollars is not enough — there has to be something mathematical
# between them, or one of the unambiguous LaTeX delimiters.
_MATH_DELIMITERS = re.compile(r"\\\(|\\\[|\$[^$\n]*[\\^_][^$\n]*\$")


def _looks_like_math(text: str) -> bool:
    return bool(_MATH_DELIMITERS.search(text))


def _check_equation(element: dict[str, Any], number: int, deck: dict[str, Any],
                    background: str | None) -> list[Finding]:
    findings: list[Finding] = []
    eid = element["id"]
    latex = element["latex"]

    if not latex.strip():
        findings.append(Finding(
            "warning", "empty-equation", "the equation has no formula", number, eid,
        ))
        return findings

    complaint = check_latex(latex)
    if complaint:
        findings.append(Finding("error", "latex-syntax", complaint, number, eid))

    if element["fontSize"] < MIN_READABLE_SIZE:
        findings.append(Finding(
            "error", "tiny-type",
            f"{element['fontSize']:g} units is too small to read when projected",
            number, eid,
        ))

    est_w, _ = metrics.estimate_equation_box(latex, element["fontSize"])
    if est_w > deck["width"] - 2 * 80:
        findings.append(Finding(
            "warning", "equation-too-wide",
            "the formula is estimated to be wider than the slide's safe area; "
            "split it over two lines with \\begin{aligned} or reduce its size",
            number, eid,
        ))

    findings += _check_contrast(element.get("color") or deck["theme"].get("color"),
                               background, number, eid)
    return findings


def _check_image(element: dict[str, Any], number: int,
                 project_root: str | None) -> list[Finding]:
    src = element["src"]
    if not src:
        return [Finding("error", "missing-image",
                        "the image element has no src", number, element["id"])]
    if src.startswith(("data:", "jpt:", "http:", "https:", "blob:")):
        return []
    if project_root:
        path = src if os.path.isabs(src) else os.path.join(project_root, src)
        if not os.path.exists(path):
            return [Finding(
                "error", "missing-image",
                f"{src} does not exist in the project — the slide will show a "
                "placeholder instead of the picture",
                number, element["id"],
            )]
    return []


# What a browser will actually play from a file. A deck that references a `.wmv`
# is not broken on disk, but nothing will show it, and the author would only
# discover that while presenting.
_PLAYABLE_VIDEO = (".mp4", ".webm", ".ogv", ".ogg", ".m4v", ".mov")


def _check_video(element: dict[str, Any], number: int,
                 project_root: str | None) -> list[Finding]:
    findings: list[Finding] = []
    eid = element["id"]
    source = model.video_source_of(element)
    if source is None:
        return [Finding("error", "missing-video",
                        "the video element has no src", number, eid)]

    if source["kind"] == "file":
        src = source["url"]
        if not src.startswith(("data:", "jpt:", "http:", "https:", "blob:")):
            if project_root:
                path = src if os.path.isabs(src) else os.path.join(project_root, src)
                if not os.path.exists(path):
                    findings.append(Finding(
                        "error", "missing-video",
                        f"{src} does not exist in the project — the slide will "
                        "show a placeholder instead of the video",
                        number, eid,
                    ))
            if not src.lower().endswith(_PLAYABLE_VIDEO):
                findings.append(Finding(
                    "warning", "video-format",
                    f"{src} is not a format browsers generally play; "
                    "MP4 (H.264) is the safe choice for a deck that has to run "
                    "on someone else's machine",
                    number, eid,
                ))

    # A video autoplays muted or it does not autoplay: every browser blocks a
    # video that starts with sound, so an unmuted autoplay is a slide that
    # silently stays on its first frame.
    if element.get("autoplay") and not element.get("muted"):
        findings.append(Finding(
            "warning", "video-autoplay",
            "autoplay without muted is blocked by browsers — the video will "
            "not start on its own unless it is muted",
            number, eid,
        ))

    if not element.get("poster"):
        findings.append(Finding(
            "info", "video-poster",
            "no poster: the editing canvas, the slide thumbnail and a PDF "
            "export will show a placeholder rather than a frame of the video",
            number, eid,
        ))
    return findings


def _check_contrast(color: str | None, background: str | None,
                    number: int, eid: str) -> list[Finding]:
    ratio = contrast_ratio(color, background)
    if ratio is not None and ratio < MIN_CONTRAST:
        return [Finding(
            "warning", "low-contrast",
            f"{color} on {background} has a contrast ratio of {ratio:.1f}; "
            f"{MIN_CONTRAST} is the floor for readable projected text",
            number, eid,
        )]
    return []


def _check_overlaps(elements: list[dict[str, Any]], number: int,
                    width: float, height: float) -> list[Finding]:
    findings: list[Finding] = []
    slide_area = width * height
    for i, a in enumerate(elements):
        if _area(a) >= slide_area * BACKGROUND_RATIO or a.get("opacity", 1) == 0:
            continue
        for b in elements[i + 1:]:
            if _area(b) >= slide_area * BACKGROUND_RATIO or b.get("opacity", 1) == 0:
                continue
            # A shape behind text is the usual deliberate overlap: a card, a
            # highlight, a band. Only two pieces of *content* colliding is a
            # defect worth reporting.
            if "shape" in (a["type"], b["type"]):
                continue
            shared = _intersection(a, b)
            if shared > min(_area(a), _area(b)) * OVERLAP_RATIO:
                findings.append(Finding(
                    "warning", "overlap",
                    f"{a['id']} and {b['id']} overlap by "
                    f"{shared / min(_area(a), _area(b)) * 100:.0f}% of the smaller one",
                    number, a["id"],
                ))
    return findings


def _check_density(elements: list[dict[str, Any]], number: int) -> list[Finding]:
    findings: list[Finding] = []
    words = 0
    for element in elements:
        if element["type"] != "text":
            continue
        text = element["text"]
        words += len(text.split())
        bullets = [line for line in text.split("\n") if line.strip()]
        # A list is one the box declares, or one whose markers were typed into
        # the text — every deck written before the `bullet` field existed looks
        # like the second, and a slide with twelve items is too long either way.
        is_list = (element.get("bullet") in model.BULLET_STYLES
                   or text.lstrip().startswith(("•", "-", "*", "–")))
        if len(bullets) > MAX_BULLETS and is_list:
            findings.append(Finding(
                "warning", "too-many-bullets",
                f"{len(bullets)} bullets on one slide; over {MAX_BULLETS} the "
                "audience reads instead of listening — split the slide",
                number, element["id"],
            ))
    if words > MAX_WORDS:
        findings.append(Finding(
            "warning", "wordy",
            f"about {words} words on one slide; consider splitting it",
            number,
        ))
    return findings


def _check_title_consistency(deck: dict[str, Any]) -> list[Finding]:
    """A title that moves between slides is the defect a reader notices without
    being able to name it: the deck appears to twitch as it advances."""
    band = deck["height"] * 0.28
    tops: list[tuple[int, float]] = []
    for number, slide in enumerate(deck["slides"], start=1):
        # Bold, and in the top band. Without the weight test this catches the
        # first line of a quote slide and reports the deck as drifting away
        # from a title it does not have; cover and section slides place their
        # title below the band on purpose and are excluded by it.
        titles = [
            el for el in slide.get("elements", [])
            if el["type"] == "text" and el["bold"] and el["y"] < band and el["text"].strip()
        ]
        if len(titles) == 1:
            tops.append((number, titles[0]["y"]))
    if len(tops) < 3:
        return []

    counts: dict[float, int] = {}
    for _, y in tops:
        counts[y] = counts.get(y, 0) + 1
    common = max(counts, key=lambda key: counts[key])
    if counts[common] < len(tops) / 2:
        return []
    return [
        Finding(
            "warning", "title-drift",
            f"the title sits at y={y:.0f} where most slides put it at y={common:.0f}",
            number,
        )
        for number, y in tops if abs(y - common) > 8
    ]


# ─── reporting ───────────────────────────────────────────────────────────────

def format_report(findings: list[Finding], *, limit: int = 40) -> str:
    """The report an agent reads. Terse, ordered, and it says what to do."""
    if not findings:
        return "No problems found."
    counts = {level: sum(1 for f in findings if f.level == level) for level in LEVELS}
    header = ", ".join(
        f"{counts[level]} {level}{'s' if counts[level] != 1 else ''}"
        for level in LEVELS if counts[level]
    )
    lines = [f"{header}:"]
    lines += [f"  {finding}" for finding in findings[:limit]]
    if len(findings) > limit:
        lines.append(f"  … and {len(findings) - limit} more")
    return "\n".join(lines)
