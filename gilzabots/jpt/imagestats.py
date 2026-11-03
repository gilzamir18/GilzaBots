"""Measuring the picture under a piece of text.

The linter cannot judge contrast against a photograph by arithmetic on two
colour values, and for a while it said so and stopped there: any text over any
background picture raised a warning. That is a confident answer to the wrong
question — most slide backgrounds are pale, near-uniform artwork chosen exactly
so text can sit on them, and warning about every one of them teaches the reader
to ignore the linter.

PyMuPDF is a hard dependency of GilzaBots, and the picture is right there, so the
question can simply be measured: what is actually behind *this* text box? Only
where the answer is "something busy" — where the luminance under the box varies
too much for any single ink to be legible over it — is there nothing left to
compute.

The mapping from slide coordinates to picture coordinates follows the same
`fit` the renderer uses, so the region sampled is the region the reader sees.
"""

from __future__ import annotations

import base64
import binascii
import os
import statistics
from typing import Any

# Long side of the grid the picture is sampled on. Enough to see a gradient or a
# hard edge, small enough that measuring costs nothing.
SAMPLE_SIDE = 64


def _pixmap(src: str, project_root: str | None):
    """A PyMuPDF pixmap for a data URI or a project-relative path, or None."""
    try:
        import pymupdf                                       # noqa: PLC0415
    except ImportError:
        try:
            import fitz as pymupdf                           # noqa: PLC0415
        except ImportError:
            return None

    data: bytes | None = None
    if src.startswith("data:"):
        try:
            header, _, payload = src.partition(",")
            data = base64.b64decode(payload) if ";base64" in header else payload.encode()
        except (binascii.Error, ValueError):
            return None
    elif src.startswith(("http:", "https:", "blob:")):
        return None                                          # not ours to fetch
    else:
        path = src if os.path.isabs(src) else os.path.join(project_root or "", src)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None

    try:
        return pymupdf.Pixmap(data)
    except Exception:
        return None


def _luminance(r: float, g: float, b: float) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _source_rect(box: dict[str, float], deck_w: float, deck_h: float,
                 img_w: int, img_h: int, fit: str) -> tuple[float, float, float, float]:
    """The part of the picture that ends up under `box` on the slide.

    The same three fits the renderer honours: `fill` stretches the picture onto
    the slide, `cover` scales it up until it covers and crops the overflow, and
    `contain` scales it down until it fits.
    """
    if fit == "fill" or not img_w or not img_h:
        sx, sy, ox, oy = img_w / deck_w, img_h / deck_h, 0.0, 0.0
    else:
        pick = max if fit == "cover" else min
        scale = pick(deck_w / img_w, deck_h / img_h)
        drawn_w, drawn_h = img_w * scale, img_h * scale
        ox, oy = (deck_w - drawn_w) / 2, (deck_h - drawn_h) / 2
        sx = sy = 1 / scale
    left = (box["x"] - ox) * sx
    top = (box["y"] - oy) * sy
    return (left, top, left + box["w"] * sx, top + box["h"] * sy)


def under(src: str, box: dict[str, float], *, deck_w: float, deck_h: float,
          fit: str = "cover", project_root: str | None = None) -> dict[str, Any] | None:
    """What the picture looks like beneath `box`, or None when it cannot be read.

    Returns the sampled luminances and their summary. The question the caller
    asks of them is not "is this picture uniform" but "is any part of what is
    under this text too close to the ink" — a pale background with one dark
    stripe across it is uniform nowhere and perfectly legible almost
    everywhere, and only the second question distinguishes those.
    """
    pixmap = _pixmap(src, project_root)
    if pixmap is None or not pixmap.width or not pixmap.height:
        return None

    left, top, right, bottom = _source_rect(
        box, deck_w, deck_h, pixmap.width, pixmap.height, fit)
    left = max(0, min(pixmap.width - 1, int(left)))
    top = max(0, min(pixmap.height - 1, int(top)))
    right = max(left + 1, min(pixmap.width, int(right)))
    bottom = max(top + 1, min(pixmap.height, int(bottom)))

    step_x = max(1, (right - left) // SAMPLE_SIDE)
    step_y = max(1, (bottom - top) // SAMPLE_SIDE)
    samples = pixmap.samples
    stride, comps = pixmap.stride, pixmap.n
    values: list[float] = []
    for y in range(top, bottom, step_y):
        row = y * stride
        for x in range(left, right, step_x):
            index = row + x * comps
            if comps >= 3:
                r, g, b = samples[index] / 255, samples[index + 1] / 255, samples[index + 2] / 255
            else:
                r = g = b = samples[index] / 255
            values.append(_luminance(r, g, b))
    if not values:
        return None

    return {
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "luminances": values,
    }


def _ratio(a: float, b: float) -> float:
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def legibility(stats: dict[str, Any], color: str, *, minimum: float) -> dict[str, Any] | None:
    """How readable `color` is over the measured region.

    `poor` is the share of the area whose contrast against the ink falls under
    `minimum` — which is the number a reader would notice, where an average
    would hide a dark stripe in a pale picture.
    """
    from .lint import _rgb                                    # noqa: PLC0415

    rgb = _rgb(color)
    if rgb is None:
        return None
    ink = _luminance(*rgb)
    ratios = [_ratio(ink, value) for value in stats["luminances"]]
    poor = sum(1 for ratio in ratios if ratio < minimum) / len(ratios)
    return {"worst": min(ratios), "median": statistics.median(ratios), "poor": poor}
