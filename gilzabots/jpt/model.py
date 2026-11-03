"""The `.jpt` document model, in Python.

This is the second implementation of the format specified in
`docs/specs/jpt_format.md`; the first is `gui_src/src/slides/model.js`, which is
what the editor runs. Two implementations of one format is a drift hazard, so
the split of authority is explicit:

  * The **specification** is the contract both obey. The JS side is the
    reference implementation of the *editor*; this side is the reference
    implementation of the *author*.
  * `serialize()` here MUST produce byte-for-byte what `serializeDeck()` there
    produces for the same document (invariant I1). `tests/test_jpt_format.py`
    proves it by round-tripping a Python-built deck through the real JS
    serializer whenever Node is available, and validates every deck this module
    writes against `docs/specs/jpt.schema.json`.
  * Nothing here parses a deck leniently. The editor repairs a malformed file
    because a user must never be locked out of their own presentation; an agent
    writing one gets an exception instead, because a silently repaired deck is
    a defect the agent never learns about.

Only what an author needs lives here: construction, normalization, byte-exact
serialization, and the operation vocabulary of §9. Rendering, measuring and
direct manipulation stay on the JS side, where the DOM is.
"""

from __future__ import annotations

import json
import random
import re
import string
from typing import Any, Iterable

# ─── format constants (mirrors model.js) ─────────────────────────────────────

FORMAT_VERSION = 1
DECK_W = 1280
DECK_H = 720

ELEMENT_TYPES = ("text", "image", "shape", "equation", "video")
SHAPE_KINDS = ("rect", "ellipse", "triangle", "line", "arrow")
ALIGNMENTS = ("left", "center", "right")
BULLET_STYLES = ("disc", "dash", "number")

# Five nesting levels, the same depth PowerPoint offers and the same cap
# `MAX_BULLET_LEVEL` sets in model.js.
MAX_BULLET_LEVEL = 4
VALIGNMENTS = ("top", "middle", "bottom")
IMAGE_FITS = ("contain", "cover", "fill")
BACKGROUND_FITS = ("cover", "contain", "fill")

# A background is a colour with an optional picture over it, in separate fields
# rather than one polymorphic value: an older build ignores the keys it does not
# know (I2) and still draws the colour, where a `background` that had become an
# object would reach its CSS as "[object Object]".
DEFAULT_THEME: dict[str, Any] = {
    "background": "#ffffff",
    "backgroundImage": "",
    "backgroundFit": "cover",
    "backgroundOpacity": 1,
    "color": "#1a1a1a",
    "accent": "#2f6fb3",
    "fontFamily": "Inter, Segoe UI, system-ui, sans-serif",
    # Chrome. 0 means no band, which is the default: a deck that never asked for
    # a header must not grow one.
    "headerHeight": 0,
    "headerColor": None,
    "titleColor": None,
    "footerHeight": 0,
    "footerColor": None,
    "footerTextColor": "#ffffff",
    "footerText": "",
}

# Every field a theme may set. The single list both the store's sidecar reader
# and the agent tool validate against, so neither can write a field the editor
# does not understand into a user's deck.
THEME_FIELDS = tuple(DEFAULT_THEME)

# The band the grid reserves for a title, in deck units. A header taller than
# this would cover the title it is meant to sit behind.
TITLE_BAND_BOTTOM = 180

TEXT_DEFAULTS: dict[str, Any] = {
    "text": "",
    # What this text *is*, when it is more than text: a title follows the
    # theme's title colour and sits inside the header band.
    "role": None,
    "fontSize": 28,
    "fontFamily": None,
    "color": None,
    "bold": False,
    "italic": False,
    "underline": False,
    "align": "left",
    "valign": "top",
    "lineHeight": 1.3,
    # The list marker drawn in front of every non-empty line, or None. A
    # property of the box; what varies per line is the nesting level, and that
    # is written into the text as leading tabs.
    "bullet": None,
}

EQUATION_DEFAULTS: dict[str, Any] = {
    "latex": "",
    "displayMode": True,
    "fontSize": 40,
    "color": None,
}

IMAGE_DEFAULTS: dict[str, Any] = {
    "src": "",
    "alt": "",
    "fit": "contain",
}

# A video is one `src` and a handful of playback switches. Whether that source
# is a provider's link or a file in the project is derived from the string, not
# stored — see `video_source_of` and, on the editor side, video.js.
VIDEO_DEFAULTS: dict[str, Any] = {
    "src": "",
    "poster": "",
    "alt": "",
    "fit": "contain",
    "autoplay": False,
    "loop": False,
    "muted": False,
    "controls": True,
    "start": 0,
}

SHAPE_DEFAULTS: dict[str, Any] = {
    "shape": "rect",
    "fill": "#2f6fb3",
    "stroke": None,
    "strokeWidth": 0,
    "radius": 0,
    "arrowStart": False,
    "arrowEnd": False,
}

_TYPE_DEFAULTS = {
    "text": TEXT_DEFAULTS,
    "equation": EQUATION_DEFAULTS,
    "image": IMAGE_DEFAULTS,
    "video": VIDEO_DEFAULTS,
    "shape": SHAPE_DEFAULTS,
}

# Key order used by `serialize`, byte-identical to ELEMENT_KEY_ORDER in
# model.js. Geometry first, then the type payload (invariant I3).
ELEMENT_KEY_ORDER = (
    "id", "type", "x", "y", "w", "h", "rotation", "opacity",
    "text", "role", "bullet", "latex", "displayMode",
    "fontSize", "fontFamily", "color", "bold", "italic", "underline",
    "align", "valign", "lineHeight",
    "src", "alt", "fit",
    "poster", "autoplay", "loop", "muted", "controls", "start",
    "shape", "fill", "stroke", "strokeWidth", "radius", "arrowStart", "arrowEnd",
)
SLIDE_KEY_ORDER = (
    "id", "background", "backgroundImage", "backgroundFit", "backgroundOpacity",
    "notes", "elements",
)
DECK_KEY_ORDER = ("version", "title", "width", "height", "theme", "slides")


class JptError(ValueError):
    """A deck an author asked for that the format cannot express."""


# ─── ids ─────────────────────────────────────────────────────────────────────

_ID_ALPHABET = string.ascii_lowercase + string.digits
_id_counter = 0


def new_id(prefix: str = "e") -> str:
    """A short, collision-resistant id, shaped like the ones model.js makes.

    Readable in a JSON diff on purpose: these end up in every element of a file
    a human may open.
    """
    global _id_counter
    _id_counter += 1
    rand = "".join(random.choice(_ID_ALPHABET) for _ in range(6))
    return f"{prefix}_{rand}{_base36(_id_counter)}"


def _base36(value: int) -> str:
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = _ID_ALPHABET[rem] + out
    return out


# ─── construction ────────────────────────────────────────────────────────────

def create_element(type_: str, **patch: Any) -> dict[str, Any]:
    """One element, with this type's defaults and the caller's overrides.

    Raises rather than guessing: an unknown type from an author is a mistake in
    the author, and degrading it to `text` the way the reader does would hide
    that (see the format spec §12.5).
    """
    if type_ not in ELEMENT_TYPES:
        raise JptError(
            f"unknown element type {type_!r}; expected one of {', '.join(ELEMENT_TYPES)}"
        )
    element = {
        "id": patch.pop("id", None) or new_id(type_[0]),
        "type": type_,
        "x": 100, "y": 100, "w": 400, "h": 120,
        "rotation": 0,
        "opacity": 1,
        **_TYPE_DEFAULTS[type_],
    }
    element.update(patch)
    return normalize_element(element)


def create_slide(**patch: Any) -> dict[str, Any]:
    slide = {
        "id": patch.pop("id", None) or new_id("s"),
        "background": None,
        # "" inherits the theme's picture, None is an explicit *no picture*
        # that overrides it, a string is this slide's own.
        "backgroundImage": "",
        "backgroundFit": "cover",
        "backgroundOpacity": 1,
        "notes": "",
        "elements": [],
    }
    slide.update(patch)
    return slide


def create_deck(title: str = "Untitled presentation", **patch: Any) -> dict[str, Any]:
    """An empty deck. Unlike the editor's, it has no slides: an author adds the
    slides it means to, and a title slide it did not ask for would have to be
    found and deleted."""
    deck = {
        "version": FORMAT_VERSION,
        "title": title,
        "width": DECK_W,
        "height": DECK_H,
        "theme": {**DEFAULT_THEME, **(patch.pop("theme", None) or {})},
        "slides": [],
    }
    deck.update(patch)
    return deck


# ─── normalization ───────────────────────────────────────────────────────────

def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JptError(f"{field} must be a number, got {value!r}")
    return value


def normalize_element(element: dict[str, Any]) -> dict[str, Any]:
    """Validate an element and coerce what is safely coercible.

    Strict where the editor is lenient. The editor clamps a zero width to 1
    because a file on disk may be anything; an author that asks for a zero-wide
    box has made an arithmetic error and needs to hear about it.
    """
    type_ = element.get("type")
    if type_ not in ELEMENT_TYPES:
        raise JptError(f"unknown element type {type_!r}")
    if not isinstance(element.get("id"), str) or not element["id"]:
        raise JptError("every element needs a non-empty string id")

    for field in ("x", "y", "w", "h", "rotation", "opacity"):
        element[field] = _number(element.get(field), f"element.{field}")
    if element["w"] < 1 or element["h"] < 1:
        raise JptError(
            f"element {element['id']} is {element['w']}x{element['h']}; "
            "width and height must be at least 1 deck unit"
        )
    if not 0 <= element["opacity"] <= 1:
        raise JptError(f"element {element['id']}: opacity must be between 0 and 1")

    if type_ == "text":
        if not isinstance(element.get("text"), str):
            raise JptError(f"element {element['id']}: text must be a string")
        if element.get("role") not in (None, "title"):
            raise JptError(
                f"element {element['id']}: role={element['role']!r} is not a role "
                "(null or 'title')"
            )
        _require_enum(element, "align", ALIGNMENTS)
        _require_enum(element, "valign", VALIGNMENTS)
        if element.get("bullet") not in (None, *BULLET_STYLES):
            raise JptError(
                f"element {element['id']}: bullet={element['bullet']!r} is not a "
                f"list style ({', '.join(BULLET_STYLES)}, or null for none)"
            )
        element["fontSize"] = _positive(element, "fontSize")
        element["lineHeight"] = _positive(element, "lineHeight")
    elif type_ == "equation":
        if not isinstance(element.get("latex"), str):
            raise JptError(f"element {element['id']}: latex must be a string")
        element["displayMode"] = bool(element.get("displayMode", True))
        element["fontSize"] = _positive(element, "fontSize")
    elif type_ == "image":
        if not isinstance(element.get("src"), str):
            raise JptError(f"element {element['id']}: src must be a string")
        _require_enum(element, "fit", IMAGE_FITS)
    elif type_ == "video":
        if not isinstance(element.get("src"), str) or not element["src"].strip():
            raise JptError(
                f"element {element['id']}: a video needs a src — a link to a "
                "video page or a path to a video file in the project"
            )
        if not isinstance(element.get("poster"), str):
            raise JptError(f"element {element['id']}: poster must be a string")
        if not isinstance(element.get("alt"), str):
            raise JptError(f"element {element['id']}: alt must be a string")
        _require_enum(element, "fit", IMAGE_FITS)
        for flag in ("autoplay", "loop", "muted", "controls"):
            if not isinstance(element.get(flag), bool):
                raise JptError(f"element {element['id']}: {flag} must be true or false")
        if _number(element.get("start"), "start") < 0:
            raise JptError(f"element {element['id']}: start cannot be negative")
    elif type_ == "shape":
        _require_enum(element, "shape", SHAPE_KINDS)
        if _number(element.get("strokeWidth"), "strokeWidth") < 0:
            raise JptError(f"element {element['id']}: strokeWidth cannot be negative")
    return element


def _require_enum(element: dict[str, Any], field: str, allowed: Iterable[str]) -> None:
    value = element.get(field)
    if value not in allowed:
        raise JptError(
            f"element {element['id']}: {field}={value!r} is not one of {', '.join(allowed)}"
        )


def _positive(element: dict[str, Any], field: str) -> float:
    value = _number(element.get(field), field)
    if value <= 0:
        raise JptError(f"element {element['id']}: {field} must be greater than zero")
    return value


def normalize_deck(deck: dict[str, Any]) -> dict[str, Any]:
    """Validate a whole deck. Raises on anything an author should not emit."""
    if not isinstance(deck, dict):
        raise JptError("a deck must be a JSON object")
    for field in ("width", "height"):
        if _number(deck.get(field), f"deck.{field}") <= 0:
            raise JptError(f"deck.{field} must be greater than zero")
    if not isinstance(deck.get("title"), str):
        raise JptError("deck.title must be a string")
    if not isinstance(deck.get("slides"), list) or not deck["slides"]:
        raise JptError("a deck needs at least one slide")
    check_background(deck.get("theme") or {}, "theme")

    seen_slides: set[str] = set()
    for slide in deck["slides"]:
        if not isinstance(slide.get("id"), str) or not slide["id"]:
            raise JptError("every slide needs a non-empty string id")
        if slide["id"] in seen_slides:
            raise JptError(f"duplicate slide id {slide['id']!r}")
        seen_slides.add(slide["id"])
        if not isinstance(slide.get("notes", ""), str):
            raise JptError(f"slide {slide['id']}: notes must be a string")
        check_background(slide, f"slide {slide['id']}")
        seen_elements: set[str] = set()
        for element in slide.get("elements", []):
            normalize_element(element)
            if element["id"] in seen_elements:
                raise JptError(
                    f"slide {slide['id']}: duplicate element id {element['id']!r}"
                )
            seen_elements.add(element["id"])
    return deck


# ─── serialization ───────────────────────────────────────────────────────────

def check_background(holder: dict[str, Any], where: str) -> None:
    """Validate the background fields of a slide or a theme."""
    image = holder.get("backgroundImage", "")
    if image is not None and not isinstance(image, str):
        raise JptError(f"{where}: backgroundImage must be a string, or null for none")
    fit = holder.get("backgroundFit", "cover")
    if fit not in BACKGROUND_FITS:
        raise JptError(
            f"{where}: backgroundFit={fit!r} is not one of {', '.join(BACKGROUND_FITS)}"
        )
    opacity = holder.get("backgroundOpacity", 1)
    if not isinstance(opacity, (int, float)) or isinstance(opacity, bool) \
            or not 0 <= opacity <= 1:
        raise JptError(f"{where}: backgroundOpacity must be a number between 0 and 1")


def title_element_of(slide: dict[str, Any]) -> dict[str, Any] | None:
    """The element that is this slide's title, or None. Mirrors the editor's
    `titleElementOf`, including its fallback for decks written before roles."""
    for element in slide.get("elements", []):
        if (element.get("type") == "text" and element.get("role") == "title"
                and element["y"] + element["h"] <= TITLE_BAND_BOTTOM):
            return element
    candidates = [
        el for el in slide.get("elements", [])
        if el.get("type") == "text" and el.get("bold") and str(el.get("text", "")).strip()
        and el["y"] + el["h"] <= TITLE_BAND_BOTTOM
    ]
    return candidates[0] if len(candidates) == 1 else None


def chrome_of(deck: dict[str, Any], slide: dict[str, Any]) -> dict[str, Any] | None:
    """The bands a theme draws behind `slide`, or None. Mirrors `chromeOf`: the
    header is the title's background, so it appears only where a title does."""
    theme = deck.get("theme") or DEFAULT_THEME
    header = max(0.0, float(theme.get("headerHeight") or 0)) if title_element_of(slide) else 0.0
    footer = max(0.0, float(theme.get("footerHeight") or 0))
    if not header and not footer:
        return None
    accent = theme.get("accent") or DEFAULT_THEME["accent"]
    return {
        "header": header,
        "headerColor": theme.get("headerColor") or accent,
        "footer": footer,
        "footerColor": theme.get("footerColor") or accent,
        "footerTextColor": theme.get("footerTextColor") or "#ffffff",
        "footerText": "title" if theme.get("footerText") == "title" else "",
    }


# Mirrors YOUTUBE / VIMEO in gui_src/src/slides/video.js. The two must agree
# about what counts as a provider link, because the editor decides from it
# whether to draw a player and the linter decides from it whether a `src` is a
# path that has to exist on disk.
_YOUTUBE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/|v/)"
    r"|youtu\.be/)([\w-]{6,})",
    re.I,
)
_VIMEO = re.compile(r"vimeo\.com/(?:video/)?(\d+)", re.I)


def video_source_of(element: dict[str, Any]) -> dict[str, str] | None:
    """What a video element's `src` is, as ``{kind, id, url}``, or None.

    ``kind`` is ``'youtube'``, ``'vimeo'`` or ``'file'``. Anything that is not a
    recognised provider is a file, including an ``https://`` URL to an ``.mp4``.
    """
    src = str(element.get("src") or "").strip()
    if not src:
        return None
    match = _YOUTUBE.search(src)
    if match:
        return {"kind": "youtube", "id": match.group(1), "url": src}
    match = _VIMEO.search(src)
    if match:
        return {"kind": "vimeo", "id": match.group(1), "url": src}
    return {"kind": "file", "id": "", "url": src}


def text_color_of(element: dict[str, Any], theme: dict[str, Any]) -> str:
    """A text element's colour: its own, then its role's, then the theme's."""
    if element.get("color"):
        return element["color"]
    if element.get("role") == "title" and theme.get("titleColor"):
        return theme["titleColor"]
    return theme.get("color") or DEFAULT_THEME["color"]


# ─── bullets ─────────────────────────────────────────────────────────────────
# The reader's half of this lives in `textLinesOf` in model.js, and the two must
# agree line for line: this side estimates whether a list fits its box and the
# editor is what actually draws it. Markers, the level cap and the two lengths
# below are therefore mirrored rather than reinvented.

DISC_MARKERS = ("\u2022", "\u25e6", "\u25aa")
DASH_MARKERS = ("\u2013",)
NUMBER_FORMATS = ("arabic", "alpha", "roman")

BULLET_INDENT_EM = 1.5
BULLET_GUTTER_EM = {"disc": 1.1, "dash": 1.1, "number": 1.7}

_ROMAN = ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def _roman(value: int) -> str:
    out, n = "", value
    for amount, glyph in _ROMAN:
        while n >= amount:
            out += glyph
            n -= amount
    return out or str(value)


def _alpha(value: int) -> str:
    out, n = "", value
    while n > 0:
        out = chr(97 + (n - 1) % 26) + out
        n = (n - 1) // 26
    return out


def _number_marker(level: int, ordinal: int) -> str:
    fmt = NUMBER_FORMATS[level % len(NUMBER_FORMATS)]
    if fmt == "alpha":
        return f"{_alpha(ordinal)}."
    if fmt == "roman":
        return f"{_roman(ordinal)}."
    return f"{ordinal}."


def text_lines(element: dict[str, Any]) -> list[dict[str, Any]]:
    """The lines of a text element: ``{level, text, marker}``, one per break.

    ``level`` is the leading tabs, clamped; ``marker`` is what the box's style
    puts in front of the line, and is empty on a blank one — an empty paragraph
    in a list is a gap the author left, not an item they forgot to write.
    """
    style = element.get("bullet")
    if style not in BULLET_STYLES:
        style = None
    raw = element.get("text")
    raw = raw if isinstance(raw, str) else str(raw or "")
    counters: list[int] = []
    out: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        tabs = len(line) - len(line.lstrip("\t"))
        level = min(tabs, MAX_BULLET_LEVEL)
        text = line[tabs:]
        if not style or not text.strip():
            out.append({"level": level, "text": text, "marker": ""})
            continue
        del counters[level + 1:]                    # a deeper list restarts
        while len(counters) <= level:
            counters.append(0)
        counters[level] += 1
        if style == "number":
            marker = _number_marker(level, counters[level])
        else:
            markers = DASH_MARKERS if style == "dash" else DISC_MARKERS
            marker = markers[level % len(markers)]
        out.append({"level": level, "text": text, "marker": marker})
    return out


def bullet_metrics(element: dict[str, Any]) -> dict[str, float]:
    """The marker column and one step of nesting, in deck units."""
    style = element.get("bullet")
    if style not in BULLET_STYLES:
        style = None
    font_size = float(element.get("fontSize") or TEXT_DEFAULTS["fontSize"])
    return {
        "indent": font_size * BULLET_INDENT_EM,
        "gutter": font_size * BULLET_GUTTER_EM[style] if style else 0.0,
    }


def line_insets_em(element: dict[str, Any]) -> list[float]:
    """Where each line's text starts, as multiples of the font size.

    In em rather than deck units because the estimator walks the font size
    down until the words fit, and a list indents by a fraction of its type: an
    inset frozen at the size the author asked for would leave the estimate
    wrong at every size but that one.
    """
    style = element.get("bullet")
    if style not in BULLET_STYLES:
        style = None
    gutter = BULLET_GUTTER_EM[style] if style else 0.0
    return [
        BULLET_INDENT_EM * line["level"] + gutter
        for line in text_lines(element)
    ]


def apply_theme(deck: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    """Write a theme into a deck, marking each slide's title as one.

    The second half is what makes this an operation rather than a dict update: a
    deck written before roles existed has none, and without the marking pass a
    theme with a white title colour would leave every title in dark ink on its
    own header band.
    """
    deck["theme"] = {**(deck.get("theme") or {}), **theme}
    marks = float(deck["theme"].get("headerHeight") or 0) > 0 or bool(deck["theme"].get("titleColor"))
    if marks:
        for slide in deck.get("slides", []):
            title = title_element_of(slide)
            if title is not None:
                title["role"] = "title"
    return deck


def background_of(deck: dict[str, Any], slide: dict[str, Any]) -> dict[str, Any]:
    """What a slide actually draws behind its elements.

    The same three-state rule the editor's `backgroundOf` applies, and the same
    reason for existing: one function decides, so the authoring side and the
    rendering side cannot disagree about what is behind the content.
    """
    theme = deck.get("theme") or DEFAULT_THEME
    inherits = slide.get("backgroundImage", "") == ""
    source = theme if inherits else slide
    return {
        "color": slide.get("background") or theme.get("background"),
        "image": (theme.get("backgroundImage") if inherits else slide.get("backgroundImage")) or "",
        "fit": source.get("backgroundFit") or "cover",
        "opacity": source.get("backgroundOpacity", 1),
    }


def _ordered(obj: dict[str, Any], key_order: Iterable[str]) -> dict[str, Any]:
    out = {key: obj[key] for key in key_order if key in obj}
    for key, value in obj.items():
        out.setdefault(key, value)
    return out


def _js_numbers(value: Any) -> Any:
    """Make numbers print the way `JSON.stringify` prints them.

    JavaScript has one number type and writes 1 for 1.0; Python writes `1.0`,
    which would break the byte-exact round-trip (I1) on the very first save the
    editor makes.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _js_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_js_numbers(item) for item in value]
    return value


def serialize(deck: dict[str, Any]) -> str:
    """The deck as the bytes that go on disk, identical to serializeDeck()."""
    normalize_deck(deck)
    out = _ordered(
        {
            **deck,
            "slides": [
                _ordered(
                    {
                        **slide,
                        "elements": [
                            _ordered(el, ELEMENT_KEY_ORDER) for el in slide.get("elements", [])
                        ],
                    },
                    SLIDE_KEY_ORDER,
                )
                for slide in deck["slides"]
            ],
        },
        DECK_KEY_ORDER,
    )
    text = json.dumps(_js_numbers(out), indent=2, ensure_ascii=False)
    return f"{text}\n"


def parse(text: str) -> dict[str, Any]:
    """Read a deck an author is about to edit.

    Strict, like everything else here: an agent that opens a deck it did not
    write needs to know the file is damaged, not to inherit a silent repair and
    write the damage back.
    """
    try:
        deck = json.loads(text)
    except json.JSONDecodeError as error:
        raise JptError(f"not valid JSON: {error}") from error
    if not isinstance(deck, dict):
        raise JptError("deck file must contain a JSON object")
    deck.setdefault("version", FORMAT_VERSION)
    deck.setdefault("title", "Untitled presentation")
    deck.setdefault("width", DECK_W)
    deck.setdefault("height", DECK_H)
    deck["theme"] = {**DEFAULT_THEME, **(deck.get("theme") or {})}
    for slide in deck.get("slides") or []:
        slide.setdefault("background", None)
        slide.setdefault("backgroundImage", "")
        slide.setdefault("backgroundFit", "cover")
        slide.setdefault("backgroundOpacity", 1)
        slide.setdefault("notes", "")
        slide.setdefault("elements", [])
        for element in slide["elements"]:
            if isinstance(element, dict) and element.get("type") in ELEMENT_TYPES:
                _apply_read_defaults(element)
    return normalize_deck(deck)


# What an absent key means when reading. The rule the strictness draws its line
# on: **absent is a default, present but wrong is wrong.** A file that omits
# `rotation` is one the editor opens without complaint, so refusing it here
# would make an agent unable to read a deck a user is happily editing; a file
# that says `"rotation": "left"` is damaged, and saying so is the whole point of
# reading strictly.
_READ_DEFAULTS = {"x": 0, "y": 0, "w": 100, "h": 40, "rotation": 0, "opacity": 1}


def _apply_read_defaults(element: dict[str, Any]) -> None:
    if not element.get("id"):
        element["id"] = new_id(element["type"][0])
    for key, value in _READ_DEFAULTS.items():
        element.setdefault(key, value)
    for key, value in _TYPE_DEFAULTS[element["type"]].items():
        element.setdefault(key, value)


# ─── operations (the §9 vocabulary) ──────────────────────────────────────────
# Mutating rather than pure, unlike model.js: the editor needs structural
# sharing for undo and React identity, and an author needs a deck it is
# building. The names and the meanings are the same, so a change made here and
# a change made there are the same change.

def find_slide(deck: dict[str, Any], slide_id: str) -> dict[str, Any]:
    for slide in deck["slides"]:
        if slide["id"] == slide_id:
            return slide
    raise JptError(f"no slide with id {slide_id!r}")


def find_element(deck: dict[str, Any], element_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for slide in deck["slides"]:
        for element in slide["elements"]:
            if element["id"] == element_id:
                return slide, element
    raise JptError(f"no element with id {element_id!r}")


def add_slide(deck: dict[str, Any], slide: dict[str, Any] | None = None, at: int | None = None) -> dict[str, Any]:
    slide = slide if slide is not None else create_slide()
    index = len(deck["slides"]) if at is None else max(0, min(at, len(deck["slides"])))
    deck["slides"].insert(index, slide)
    return slide


def delete_slide(deck: dict[str, Any], slide_id: str) -> None:
    slide = find_slide(deck, slide_id)
    if len(deck["slides"]) <= 1:
        # The editor's rule: a deck with no slides has no canvas to draw.
        slide["elements"] = []
        slide["notes"] = ""
        return
    deck["slides"].remove(slide)


def move_slide(deck: dict[str, Any], from_index: int, to_index: int) -> None:
    slides = deck["slides"]
    if not 0 <= from_index < len(slides):
        raise JptError(f"no slide at index {from_index}")
    slide = slides.pop(from_index)
    slides.insert(max(0, min(to_index, len(slides))), slide)


def add_element(deck: dict[str, Any], slide_id: str, element: dict[str, Any]) -> dict[str, Any]:
    find_slide(deck, slide_id)["elements"].append(normalize_element(element))
    return element


def update_element(deck: dict[str, Any], element_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    _, element = find_element(deck, element_id)
    if "type" in patch and patch["type"] != element["type"]:
        raise JptError(
            "an element cannot change type; delete it and add the new one instead"
        )
    element.update(patch)
    return normalize_element(element)


def delete_element(deck: dict[str, Any], element_id: str) -> None:
    slide, element = find_element(deck, element_id)
    slide["elements"].remove(element)


def reorder_element(deck: dict[str, Any], element_id: str, direction: str) -> None:
    """Z-order is array order and nothing else (spec §3.3)."""
    slide, element = find_element(deck, element_id)
    elements = slide["elements"]
    index = elements.index(element)
    last = len(elements) - 1
    target = {
        "front": last,
        "back": 0,
        "forward": min(last, index + 1),
        "backward": max(0, index - 1),
    }.get(direction)
    if target is None:
        raise JptError(
            f"unknown direction {direction!r}; expected front, forward, backward or back"
        )
    elements.insert(target, elements.pop(index))
