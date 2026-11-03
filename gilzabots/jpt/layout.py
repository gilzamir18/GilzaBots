"""Turning an outline into a laid-out deck.

This is the part of the authoring API that exists because of what goes wrong
without it. An agent asked to "make a deck about X" and handed the raw format
writes plausible JSON and an unusable presentation: text past the edge of its
box, a title 40 units lower on slide 4 than on slide 3, two elements on top of
each other, 14-unit type nobody at the back of the room can read. None of that
is a failure of the format — it is what happens when the author has to invent
coordinates.

So the author does not get to invent them. It states intent — a title and some
bullets, a formula with a caption, an image beside a list — and everything
geometric is computed here from one grid, with the font size fitted to the text
rather than the text trusted to fit the font. Every layout draws its title in
exactly the same place, which is most of what makes a deck look deliberate.

The escape hatch is deliberate too: any slide may carry raw `elements`, because
a layout vocabulary that cannot be escaped becomes a cage the moment someone
needs a diagram. What comes through it is checked by `lint.py` like everything
else, so the escape hatch is not a hole in the guarantees — it just moves the
responsibility to where the linter can still see it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import metrics
from .model import (
    BULLET_STYLES, JptError, MAX_BULLET_LEVEL, add_element, add_slide,
    check_background, create_deck, create_element, create_slide, line_insets_em,
)

# ─── the grid ────────────────────────────────────────────────────────────────
# One grid for every slide. The numbers are deck units on a 1280x720 stage and
# scale with the deck: a 4:3 deck gets the same proportions, not the same
# pixels.


@dataclass(frozen=True)
class Region:
    x: float
    y: float
    w: float
    h: float

    def inset(self, dx: float, dy: float = None) -> "Region":
        dy = dx if dy is None else dy
        return Region(self.x + dx, self.y + dy, self.w - dx * 2, self.h - dy * 2)


@dataclass(frozen=True)
class Grid:
    width: float = 1280
    height: float = 720

    @property
    def margin_x(self) -> float:
        return round(self.width * 0.0625)      # 80 at 1280

    @property
    def margin_top(self) -> float:
        return round(self.height * 0.083)      # 60 at 720

    @property
    def margin_bottom(self) -> float:
        return round(self.height * 0.078)      # 56 at 720

    @property
    def gutter(self) -> float:
        return round(self.width * 0.031)       # 40 at 1280

    @property
    def title(self) -> Region:
        """Where a slide title goes — the same place on every slide, which is
        what stops a deck from appearing to jump as it advances."""
        return Region(
            self.margin_x, self.margin_top,
            self.width - self.margin_x * 2, round(self.height * 0.153),   # 110
        )

    @property
    def content(self) -> Region:
        """Everything below the title band."""
        top = self.title.y + self.title.h + round(self.height * 0.044)    # 32 gap
        return Region(
            self.margin_x, top,
            self.width - self.margin_x * 2, self.height - self.margin_bottom - top,
        )

    @property
    def full(self) -> Region:
        """The whole safe area, for slides with no title."""
        return Region(
            self.margin_x, self.margin_top,
            self.width - self.margin_x * 2,
            self.height - self.margin_top - self.margin_bottom,
        )

    def columns(self, count: int = 2, weights: list[float] | None = None) -> list[Region]:
        """The content area split into columns separated by one gutter."""
        area = self.content
        weights = weights or [1.0] * count
        if len(weights) != count:
            raise JptError("one weight per column, or none at all")
        total = sum(weights)
        free = area.w - self.gutter * (count - 1)
        out: list[Region] = []
        x = area.x
        for weight in weights:
            w = free * (weight / total)
            out.append(Region(round(x), area.y, round(w), area.h))
            x += w + self.gutter
        return out


# ─── type scale ──────────────────────────────────────────────────────────────
# Preferred size and the floor auto-fit will not go below. The floor is a
# readability decision, not a layout one: below it the answer is fewer words,
# and the linter says so rather than the layout shrinking text off a projector.

TYPE_SCALE = {
    "cover_title": (68, 40),
    "cover_subtitle": (30, 20),
    "section_title": (60, 36),
    "title": (48, 30),
    "body": (30, 20),
    "bullets": (30, 20),
    "quote": (40, 24),
    "caption": (20, 16),
    "equation": (48, 24),
    # A slide whose whole point is the formula gets a bigger one. The fit loop
    # still walks it down until the estimate fits the region, and the editor
    # re-measures the box on open, so the only cost of aiming high is one more
    # iteration of the loop.
    "hero_equation": (64, 28),
}

BULLET_LINE_HEIGHT = 1.5
BODY_LINE_HEIGHT = 1.4


def _mix(a: str, b: str, t: float) -> str:
    """`a` blended `t` of the way towards `b`, both `#rrggbb`."""
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return a
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def muted_color(theme: dict[str, Any]) -> str:
    """A caption colour that works on a light or a dark deck, because it is
    derived from the deck's own two colours rather than hardcoded to grey."""
    return _mix(theme.get("color", "#1a1a1a"), theme.get("background", "#ffffff"), 0.45)


# ─── building blocks ─────────────────────────────────────────────────────────

def title_element(grid: Grid, theme: dict[str, Any], text: str, *,
                  scale: str = "title", align: str = "left",
                  region: Region | None = None, color: str | None = None) -> dict[str, Any]:
    area = region or grid.title
    preferred, minimum = TYPE_SCALE[scale]
    size = metrics.fit_font_size(
        text, area.w, area.h, preferred=preferred, minimum=minimum,
        line_height=1.2, bold=True,
    )
    return create_element(
        "text", x=area.x, y=area.y, w=area.w, h=area.h, text=text,
        fontSize=size, bold=True, align=align, valign="middle",
        lineHeight=1.2, color=color,
        # Only a title in the band the grid reserves is a *slide* title: a cover
        # or a section divider places its title lower on purpose, and marking
        # those would put a header band behind them.
        role="title" if region is None else None,
    )


def bullet_text(bullets: Any) -> str:
    """A list of items as the one string a text box holds.

    An item is a string, a string with leading tabs, or ``{"text": …, "level":
    n}`` — three spellings of the same thing, because an author writing an
    outline should not have to learn a representation. What comes out is what
    the format stores: one line per item, its nesting written as leading tabs.
    """
    if isinstance(bullets, str):
        bullets = [line for line in bullets.splitlines() if line.strip()]
    if not isinstance(bullets, (list, tuple)):
        raise JptError("bullets must be a list of items, or one string per line")

    lines: list[str] = []
    for item in bullets:
        if isinstance(item, dict):
            text = str(item.get("text") or "")
            level = item.get("level", 0)
            if not isinstance(level, int) or isinstance(level, bool) or level < 0:
                raise JptError(f"bullet level must be a whole number ≥ 0, not {level!r}")
        else:
            text = str(item)
            level = 0
        tabs = len(text) - len(text.lstrip("\t"))
        text = text[tabs:].strip()
        level = min(level + tabs, MAX_BULLET_LEVEL)
        lines.append("\t" * level + text)
    return "\n".join(lines)


def bullets_element(theme: dict[str, Any], region: Region, bullets: Any, *,
                    scale: str = "bullets", color: str | None = None,
                    style: str = "disc") -> dict[str, Any]:
    """A bullet list as one text box.

    One box rather than one box per bullet: a browser wraps a paragraph better
    than an author can place one, and a list split across five elements is five
    things to keep aligned every time the text changes.

    The markers are a field, never characters in the text. Writing "•  " in
    front of every line — which is what this did before the format had
    `bullet` — produced a list that could not be restyled, could not nest, and
    arrived in PowerPoint as text somebody had to delete by hand.
    """
    if style not in BULLET_STYLES:
        raise JptError(
            f"bullet style {style!r} is not one of {', '.join(BULLET_STYLES)}"
        )
    body = bullet_text(bullets)
    preferred, minimum = TYPE_SCALE[scale]
    # The marker column and the nesting take width away from every line, so the
    # fit is computed against what the words actually get, not the whole box.
    insets = line_insets_em({"text": body, "bullet": style})
    size = metrics.fit_font_size(
        body, region.w, region.h, preferred=preferred, minimum=minimum,
        line_height=BULLET_LINE_HEIGHT, insets=insets,
    )
    return create_element(
        "text", x=region.x, y=region.y, w=region.w, h=region.h, text=body,
        fontSize=size, align="left", valign="top", lineHeight=BULLET_LINE_HEIGHT,
        color=color, bullet=style,
    )


def paragraph_element(theme: dict[str, Any], region: Region, text: str, *,
                      scale: str = "body", align: str = "left",
                      valign: str = "top", color: str | None = None) -> dict[str, Any]:
    preferred, minimum = TYPE_SCALE[scale]
    size = metrics.fit_font_size(
        text, region.w, region.h, preferred=preferred, minimum=minimum,
        line_height=BODY_LINE_HEIGHT,
    )
    return create_element(
        "text", x=region.x, y=region.y, w=region.w, h=region.h, text=text,
        fontSize=size, align=align, valign=valign, lineHeight=BODY_LINE_HEIGHT,
        color=color,
    )


def equation_element(region: Region, latex: str, *, display_mode: bool = True,
                     color: str | None = None, scale: str = "equation",
                     vertical: str = "center") -> dict[str, Any]:
    """A formula centred in `region`, at the largest size that is estimated to
    fit it.

    The box written here is provisional on purpose: the editor measures the
    rendered MathML and re-fits it the moment the deck is opened (format spec
    §4.2), so this only has to be close enough to sit in the right place and
    not to overlap its neighbours.
    """
    preferred, minimum = TYPE_SCALE[scale]
    size = float(preferred)
    while size > minimum:
        w, h = metrics.estimate_equation_box(latex, size)
        if w <= region.w * metrics.FIT_RATIO and h <= region.h * metrics.FIT_RATIO:
            break
        size -= metrics.SIZE_STEP
    w, h = metrics.estimate_equation_box(latex, size)
    w = max(1.0, min(w, region.w))
    h = max(1.0, min(h, region.h))
    # Centred in its region, except in a column beside a list: a formula
    # floating at the vertical middle of a tall column while the bullets next
    # to it start at the top reads as two slides side by side.
    y = region.y if vertical == "top" else region.y + (region.h - h) / 2
    return create_element(
        "equation",
        x=round(region.x + (region.w - w) / 2),
        y=round(y),
        w=round(w), h=round(h),
        latex=latex, displayMode=display_mode, fontSize=size, color=color,
    )


def image_element(region: Region, src: str, *, alt: str = "",
                  fit: str = "contain") -> dict[str, Any]:
    """An image filling its region.

    `contain` is the default because the region's aspect is the grid's, not the
    picture's: a photograph told to `cover` a column would be cropped by the
    layout rather than by whoever chose the picture.
    """
    return create_element(
        "image", x=region.x, y=region.y, w=region.w, h=region.h,
        src=src, alt=alt, fit=fit,
    )


def accent_rule(grid: Grid, theme: dict[str, Any], y: float, *,
                width_ratio: float = 0.14) -> dict[str, Any]:
    """The short rule under a section title. Decoration, but the one piece of
    decoration that makes a divider read as a divider."""
    return create_element(
        "shape", shape="rect",
        x=grid.margin_x, y=round(y), w=round(grid.width * width_ratio), h=6,
        fill=theme.get("accent", "#2f6fb3"), radius=3,
    )


# ─── blocks ──────────────────────────────────────────────────────────────────
# A block is what fills one region: bullets, a paragraph, a formula or a
# picture. Layouts are then compositions of regions and blocks, which is why
# there are nine of them and not nine hundred lines.

BLOCK_KEYS = ("bullets", "text", "equation", "image")


def fill_region(theme: dict[str, Any], region: Region, block: dict[str, Any],
                *, muted: str, vertical: str = "center",
                hero: bool = False) -> list[dict[str, Any]]:
    """The elements that fill `region` with `block`, caption included."""
    if not isinstance(block, dict):
        raise JptError(f"a block must be an object with one of {BLOCK_KEYS}")

    caption = str(block.get("caption") or "")
    body = region
    if caption:
        caption_h = 52
        body = Region(region.x, region.y, region.w, max(1, region.h - caption_h))

    elements: list[dict[str, Any]] = []
    present = [key for key in BLOCK_KEYS if block.get(key)]
    if len(present) > 1:
        raise JptError(
            f"a block holds one of {BLOCK_KEYS}, not {len(present)}: {', '.join(present)}"
        )
    if not present:
        raise JptError(f"a block needs one of {BLOCK_KEYS}")
    kind = present[0]

    if kind == "bullets":
        elements.append(bullets_element(
            theme, body, block["bullets"],
            style=str(block.get("bulletStyle") or "disc"),
        ))
    elif kind == "text":
        elements.append(paragraph_element(theme, body, str(block["text"])))
    elif kind == "equation":
        elements.append(equation_element(
            body, str(block["equation"]),
            scale="hero_equation" if hero else "equation", vertical=vertical,
        ))
    elif kind == "image":
        elements.append(image_element(
            body, str(block["image"]), alt=str(block.get("alt") or ""),
            fit=str(block.get("fit") or "contain"),
        ))

    if caption:
        elements.append(paragraph_element(
            theme,
            Region(region.x, region.y + region.h - 52, region.w, 52),
            caption, scale="caption", align="center", valign="top", color=muted,
        ))
    return elements


def _block_of(spec: dict[str, Any]) -> dict[str, Any]:
    """The block a slide spec carries at its top level, so `{"layout":
    "bullets", "bullets": [...]}` needs no nesting."""
    block = {key: spec[key] for key in BLOCK_KEYS if spec.get(key)}
    for extra in ("caption", "alt", "fit", "bulletStyle"):
        if spec.get(extra):
            block[extra] = spec[extra]
    return block


# ─── layouts ─────────────────────────────────────────────────────────────────

def _slide_title(grid: Grid, theme: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    title = str(spec.get("title") or "")
    return [title_element(grid, theme, title)] if title else []


def layout_title(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                 muted: str) -> list[dict[str, Any]]:
    """The cover: the one slide whose title is not in the title band, because a
    cover with the same top-left title as every other slide reads as a slide
    someone forgot to fill in."""
    area = grid.full
    band = Region(area.x, round(area.y + area.h * 0.28), area.w, round(area.h * 0.30))
    elements = [title_element(
        grid, theme, str(spec.get("title") or ""), scale="cover_title",
        align="center", region=band,
    )]
    subtitle = str(spec.get("subtitle") or "")
    if subtitle:
        elements.append(paragraph_element(
            theme,
            Region(area.x, band.y + band.h + 16, area.w, 90),
            subtitle, scale="cover_subtitle", align="center", valign="top", color=muted,
        ))
    return elements


def layout_section(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                   muted: str) -> list[dict[str, Any]]:
    area = grid.full
    band = Region(area.x, round(area.y + area.h * 0.34), area.w, round(area.h * 0.26))
    elements = [accent_rule(grid, theme, band.y - 34)]
    elements.append(title_element(
        grid, theme, str(spec.get("title") or ""), scale="section_title", region=band,
    ))
    subtitle = str(spec.get("subtitle") or "")
    if subtitle:
        elements.append(paragraph_element(
            theme, Region(area.x, band.y + band.h + 8, area.w, 70),
            subtitle, scale="caption", color=muted,
        ))
    return elements


def layout_content(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                   muted: str) -> list[dict[str, Any]]:
    """Title plus one block. Covers `bullets`, `text`, `equation` and `image`,
    which are the same slide with a different thing in the middle."""
    elements = _slide_title(grid, theme, spec)
    block = _block_of(spec)
    if block:
        # The block has the whole content area to itself, so a formula here is
        # the point of the slide rather than one column of it.
        elements += fill_region(theme, grid.content, block, muted=muted, hero=True)
    return elements


def layout_two_columns(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                       muted: str) -> list[dict[str, Any]]:
    left, right = grid.columns(2, spec.get("weights"))
    elements = _slide_title(grid, theme, spec)
    for region, key in ((left, "left"), (right, "right")):
        block = spec.get(key)
        if not block:
            continue
        elements += fill_region(theme, region, block, muted=muted, vertical="top")
    return elements


def layout_image_text(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                      muted: str) -> list[dict[str, Any]]:
    """A picture on one side, words on the other. The picture gets the smaller
    share by default: a slide is read, not looked at."""
    weights = spec.get("weights") or [1.0, 1.2]
    first, second = grid.columns(2, weights)
    side = str(spec.get("side") or "left")
    if side not in ("left", "right"):
        raise JptError("side must be 'left' or 'right'")
    image_region, text_region = (first, second) if side == "left" else (second, first)

    elements = _slide_title(grid, theme, spec)
    elements += fill_region(theme, image_region, {
        "image": spec.get("image"),
        "alt": spec.get("alt"),
        "caption": spec.get("caption"),
    }, muted=muted)
    words = {key: spec[key] for key in ("bullets", "text", "equation", "bulletStyle")
             if spec.get(key)}
    if words:
        elements += fill_region(theme, text_region, words, muted=muted, vertical="top")
    return elements


def layout_quote(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                 muted: str) -> list[dict[str, Any]]:
    area = grid.full.inset(round(grid.width * 0.055), 0)
    band = Region(area.x, round(area.y + area.h * 0.22), area.w, round(area.h * 0.42))
    text = str(spec.get("quote") or spec.get("text") or "")
    elements = [paragraph_element(
        theme, band, f"“{text}”", scale="quote", align="center", valign="middle",
    )]
    attribution = str(spec.get("attribution") or "")
    if attribution:
        elements.append(paragraph_element(
            theme, Region(area.x, band.y + band.h + 12, area.w, 60),
            f"— {attribution}", scale="caption", align="center", color=muted,
        ))
    return elements


def layout_blank(grid: Grid, theme: dict[str, Any], spec: dict[str, Any],
                 muted: str) -> list[dict[str, Any]]:
    return _slide_title(grid, theme, spec)


LAYOUTS = {
    "title": layout_title,
    "section": layout_section,
    "bullets": layout_content,
    "text": layout_content,
    "equation": layout_content,
    "image": layout_content,
    "content": layout_content,
    "two_columns": layout_two_columns,
    "image_text": layout_image_text,
    "quote": layout_quote,
    "blank": layout_blank,
}


# ─── the outline compiler ────────────────────────────────────────────────────

def build_slide(grid: Grid, theme: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise JptError("each slide must be an object")
    layout = str(spec.get("layout") or "bullets")
    builder = LAYOUTS.get(layout)
    if builder is None:
        raise JptError(
            f"unknown layout {layout!r}; expected one of {', '.join(sorted(LAYOUTS))}"
        )

    slide = create_slide(id=spec.get("id"), notes=str(spec.get("notes") or ""))
    if spec.get("background"):
        slide["background"] = str(spec["background"])
    # A picture behind this slide. "" (the default) inherits whatever the deck's
    # theme sets; an explicit null turns the theme's picture off for this slide.
    if "backgroundImage" in spec:
        image = spec["backgroundImage"]
        slide["backgroundImage"] = None if image is None else str(image)
    if spec.get("backgroundFit"):
        slide["backgroundFit"] = str(spec["backgroundFit"])
    if spec.get("backgroundOpacity") is not None:
        slide["backgroundOpacity"] = spec["backgroundOpacity"]
    # Checked here rather than at serialization, so the diagnostic names the
    # slide the author got wrong instead of surfacing three steps later.
    check_background(slide, "background")

    for element in builder(grid, theme, spec, muted_color(theme)):
        slide["elements"].append(element)

    # The escape hatch: raw elements land on top of the layout, in the order
    # given, and are validated exactly like everything else.
    for raw in spec.get("elements") or []:
        if not isinstance(raw, dict) or "type" not in raw:
            raise JptError("a raw element needs at least a 'type'")
        payload = {k: v for k, v in raw.items() if k != "type"}
        slide["elements"].append(create_element(raw["type"], **payload))
    return slide


def compile_outline(outline: dict[str, Any]) -> dict[str, Any]:
    """An outline as described in the agent API section of the format spec,
    compiled into a laid-out deck."""
    if not isinstance(outline, dict):
        raise JptError("an outline must be a JSON object")
    slides = outline.get("slides")
    if not isinstance(slides, list) or not slides:
        raise JptError("an outline needs a non-empty 'slides' array")

    deck = create_deck(
        str(outline.get("title") or "Untitled presentation"),
        theme=outline.get("theme") or {},
    )
    if outline.get("width"):
        deck["width"] = outline["width"]
    if outline.get("height"):
        deck["height"] = outline["height"]
    grid = Grid(deck["width"], deck["height"])

    for index, spec in enumerate(slides):
        try:
            add_slide(deck, build_slide(grid, deck["theme"], spec))
        except JptError as error:
            raise JptError(f"slide {index + 1}: {error}") from error

    if outline.get("slide_numbers"):
        _number_slides(deck, grid)
    return deck


def _number_slides(deck: dict[str, Any], grid: Grid) -> None:
    """A page number on every slide but the cover and the dividers, where one
    would be clutter."""
    muted = muted_color(deck["theme"])
    for index, slide in enumerate(deck["slides"]):
        if index == 0:
            continue
        add_element(deck, slide["id"], create_element(
            "text",
            x=round(grid.width - grid.margin_x - 120),
            y=round(grid.height - grid.margin_bottom + 8),
            w=120, h=34,
            # At the readability floor, not below it: a page number is small
            # by design, but a deck must not emit type its own linter would
            # refuse, or the linter becomes noise the reader learns to skip.
            text=str(index + 1), fontSize=20, align="right", valign="middle",
            color=muted,
        ))
