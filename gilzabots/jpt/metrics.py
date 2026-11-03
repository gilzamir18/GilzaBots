"""Estimating how much room text needs, without a browser.

The editor measures text by rendering it; this module cannot, so it estimates
from advance-width tables. That difference is the single most important thing
to know about this file:

  * The numbers below are Helvetica's advance widths, which sit within a few
    percent of Inter's and of every other system sans a deck is likely to use.
    An estimate is therefore *approximate*, and every consumer applies a safety
    margin (`FIT_RATIO`) rather than fitting text to the last pixel.
  * The estimate is only ever used to make text **smaller** or to raise a
    warning. Nothing that depends on it can silently clip content: the layout
    shrinks until the text fits with room to spare, and the linter reports what
    it could not make fit instead of trimming the words.
  * For equations there is no table that would help — the width of
    `\\sum_{i=0}^{n}` has nothing to do with the length of its source — so
    `estimate_equation_box` is a coarse guess, and the editor re-fits an
    equation's box the moment the deck is opened (format spec §4.2). Use it to
    place a formula, never to decide whether it fits.
"""

from __future__ import annotations

# Advance widths in 1/1000 em, Helvetica. Anything not listed falls back to
# DEFAULT_ADVANCE, and CJK falls back to a full em.
_ADVANCE = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556, "7": 556,
    "8": 556, "9": 556, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556,
    "@": 1015,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500,
    "{": 334, "|": 260, "}": 334, "~": 584,
}
DEFAULT_ADVANCE = 556
BOLD_FACTOR = 1.06

# How much of a box text is allowed to fill. The remainder absorbs the error in
# the table above and keeps a line from touching the edge of its box.
FIT_RATIO = 0.94

# The font-size ladder auto-fit walks down. Whole numbers, because a deck the
# user will edit by hand should not be full of sizes like 27.3.
SIZE_STEP = 2


def _char_advance(char: str) -> int:
    if char in _ADVANCE:
        return _ADVANCE[char]
    code = ord(char)
    # CJK, Hangul and full-width forms occupy a full em.
    if 0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7A3 \
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF60:
        return 1000
    return DEFAULT_ADVANCE


def text_width(text: str, font_size: float, bold: bool = False) -> float:
    """Estimated width of a single line, in the same units as `font_size`."""
    width = sum(_char_advance(char) for char in text) / 1000 * font_size
    return width * (BOLD_FACTOR if bold else 1.0)


def _inset_at(insets: list[float] | None, index: int, font_size: float) -> float:
    """The left inset of one paragraph, in the same units as the box.

    `insets` are multiples of the font size — a bullet's marker column and its
    nesting are both fractions of its type — so they are resolved here, against
    whichever size is being tried.
    """
    if not insets:
        return 0.0
    return insets[index if index < len(insets) else -1] * font_size


def wrap_lines(text: str, box_width: float, font_size: float, bold: bool = False,
               insets: list[float] | None = None) -> list[tuple[str, float]]:
    """The lines `text` breaks into inside `box_width`, each with its inset.

    Greedy word wrapping on spaces, the way a browser lays out a paragraph.
    Explicit newlines are hard breaks. A single word wider than the box is left
    on its own line rather than hyphenated — the caller sees the overflow and
    can shrink the font or the caller's own text.

    `insets` is one entry per hard-broken paragraph, in em: a bulleted line
    starts after its marker and its indentation, so it has that much less width
    to wrap into, and every wrapped continuation of it starts there too.
    """
    lines: list[tuple[str, float]] = []
    for index, paragraph in enumerate(text.split("\n")):
        inset = _inset_at(insets, index, font_size)
        usable = (box_width - inset) * FIT_RATIO
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if current and text_width(candidate, font_size, bold) > usable:
                lines.append((current, inset))
                current = word
            else:
                current = candidate
        lines.append((current, inset))
    return lines


def text_height(text: str, box_width: float, font_size: float,
                line_height: float = 1.3, bold: bool = False,
                insets: list[float] | None = None) -> float:
    """Estimated height of `text` wrapped into `box_width`."""
    return len(wrap_lines(text, box_width, font_size, bold, insets)) * font_size * line_height


def fits(text: str, box_width: float, box_height: float, font_size: float,
         line_height: float = 1.3, bold: bool = False,
         insets: list[float] | None = None) -> bool:
    if not text:
        return True
    lines = wrap_lines(text, box_width, font_size, bold, insets)
    if any(text_width(line, font_size, bold) > (box_width - inset) * FIT_RATIO
           for line, inset in lines):
        return False
    return len(lines) * font_size * line_height <= box_height * FIT_RATIO


def fit_font_size(text: str, box_width: float, box_height: float, *,
                  preferred: float, minimum: float, line_height: float = 1.3,
                  bold: bool = False, insets: list[float] | None = None) -> float:
    """The largest size at or below `preferred` at which `text` fits the box.

    Returns `minimum` when nothing in the ladder fits, which is the signal the
    caller needs: the text is too long for the space it was given, and the
    honest answers are a smaller font (already tried), a bigger box, or fewer
    words. The linter reports it rather than the layout silently clipping it.
    """
    size = float(preferred)
    while size > minimum:
        if fits(text, box_width, box_height, size, line_height, bold, insets):
            return size
        size -= SIZE_STEP
    return float(minimum)


# ─── equations ───────────────────────────────────────────────────────────────

# Commands that make a formula taller than one line, and roughly by how much.
_TALL_COMMANDS = {
    "\\frac": 1.1, "\\dfrac": 1.1, "\\sum": 0.7, "\\prod": 0.7, "\\int": 0.6,
    "\\begin{cases}": 1.2, "\\begin{matrix}": 1.2, "\\begin{pmatrix}": 1.2,
    "\\begin{bmatrix}": 1.2, "\\begin{aligned}": 1.0, "\\sqrt": 0.25,
    "^": 0.35, "_": 0.35,
}


def estimate_equation_box(latex: str, font_size: float) -> tuple[float, float]:
    """A rough box for a rendered formula, in deck units.

    Coarse by construction — see the module docstring. The editor replaces it
    with a real measurement as soon as the deck is opened, so this only has to
    be close enough to place the formula sensibly on the slide and to notice a
    formula that cannot possibly fit its column.
    """
    body = latex.strip()
    if not body:
        return (0.0, 0.0)

    # Control sequences render as one or two glyphs, not as their source, so
    # the visible length is closer to the token count than to the character
    # count. `\alpha` is one glyph; `x + y` is five.
    visible = 0
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            end = index + 1
            while end < len(body) and body[end].isalpha():
                end += 1
            visible += 1 if end > index + 1 else 1
            index = max(end, index + 2)
            continue
        if char in "{}$ ":
            index += 1
            continue
        visible += 1
        index += 1

    width = visible * 0.62 * font_size
    height = font_size * 1.35
    for command, extra in _TALL_COMMANDS.items():
        if command in body:
            height = max(height, font_size * (1.35 + extra))
    return (width, height)
