"""Authoring `.jpt` presentations from Python.

The format is specified in `docs/specs/jpt_format.md`; this package is the
authoring half of it, and exists so an agent can produce a deck that is *good*
rather than merely valid.

    from gilzabots import jpt

    deck = jpt.compile_outline({
        "title": "Gauss",
        "slides": [
            {"layout": "title", "title": "The Gaussian integral"},
            {"layout": "bullets", "title": "Why it matters",
             "bullets": ["It has no elementary antiderivative",
                         {"text": "and yet it is exactly √π", "level": 1}]},
            {"layout": "equation", "title": "The result",
             "equation": r"\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}"},
        ],
    })
    findings = jpt.lint(deck)
    text = jpt.serialize(deck)

Three layers, in the order they matter:

  * `layout` — intent in, geometry out. The agent never picks a coordinate.
  * `lint` — the check that makes "perfect" a property something verifies
    rather than a hope. `create_presentation` refuses to write a deck with
    errors in it.
  * `model` — the format itself: construction, validation, and serialization
    that is byte-identical to the editor's own.

`metrics` sits under `layout` and is the one part with a caveat worth knowing:
text is *estimated* from advance-width tables, because measuring it properly
needs the browser the editor runs in. Everything downstream treats an estimate
as an estimate.
"""

from .assets import (
    MAX_EMBED_BYTES, describe, embed_images, is_portable, to_data_uri, used_sources,
)
from .layout import Grid, Region, compile_outline, build_slide, LAYOUTS
from .lint import Finding, format_report, has_errors, lint
from .model import (
    BACKGROUND_FITS, BULLET_STYLES, DECK_H, DECK_W, DEFAULT_THEME, ELEMENT_TYPES,
    FORMAT_VERSION, JptError, MAX_BULLET_LEVEL,
    SHAPE_KINDS, THEME_FIELDS, TITLE_BAND_BOTTOM, apply_theme, background_of, check_background,
    bullet_metrics, chrome_of, line_insets_em, text_color_of, text_lines,
    title_element_of, video_source_of,
    add_element, add_slide, create_deck, create_element, create_slide,
    delete_element, delete_slide, find_element, find_slide, move_slide, parse,
    reorder_element, serialize, update_element,
)
from .package import (
    ASSET_URI_PREFIX, DECK_MEMBER, MIMETYPE_MEMBER, PACKAGE_MIMETYPE,
    AssetInfo, JptDocument, JptPackageError, PackageWriteResult, asset_member,
    get_asset_info, is_packaged_jpt, iter_asset, read_asset, read_jpt,
    read_jpt_bytes, write_packaged_jpt,
)

__all__ = [
    "BACKGROUND_FITS", "BULLET_STYLES", "DECK_H", "DECK_W", "DEFAULT_THEME",
    "ELEMENT_TYPES", "FORMAT_VERSION", "MAX_BULLET_LEVEL",
    "bullet_metrics", "line_insets_em", "text_lines",
    "MAX_EMBED_BYTES", "THEME_FIELDS", "describe", "embed_images", "is_portable",
    "to_data_uri", "used_sources",
    "Finding", "Grid", "TITLE_BAND_BOTTOM", "apply_theme", "background_of",
    "check_background", "chrome_of", "text_color_of", "title_element_of",
    "JptError", "LAYOUTS", "Region", "SHAPE_KINDS", "add_element", "add_slide",
    "build_slide", "compile_outline", "create_deck", "create_element",
    "create_slide", "delete_element", "delete_slide", "find_element",
    "find_slide", "format_report", "has_errors", "lint", "move_slide", "parse",
    "reorder_element", "serialize", "update_element", "video_source_of",
    "ASSET_URI_PREFIX", "DECK_MEMBER", "MIMETYPE_MEMBER", "PACKAGE_MIMETYPE",
    "AssetInfo", "JptDocument", "JptPackageError", "PackageWriteResult",
    "asset_member", "get_asset_info", "is_packaged_jpt", "iter_asset",
    "read_asset", "read_jpt", "read_jpt_bytes", "write_packaged_jpt",
]
