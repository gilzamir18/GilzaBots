"""Presentation themes in the Asset Store.

A theme is a `.yaml` describing a deck's whole look — colours, type, the header
and footer bands, and *optionally* a picture beside it. The optionality is the
point: it replaced a background-only asset because a background is not a theme,
and most themes (Madrid among them) carry no picture at all.

It is also the one asset that is not *installed*: it is applied to a
presentation open in the editor, which changes that document, not the project's
files.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.assetstore import (
    VALID_TYPES,
    install_asset,
    list_assets,
    resolve_asset_icon_path,
    theme_image_path,
)
from gilzabots.jpt.model import THEME_FIELDS

BUNDLED = Path(__file__).resolve().parent.parent / "gilzabots" / "assetstore" / "theme"


def _api_harness():
    """A server whose responses are collected instead of written to a socket."""
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, body, content_type))

    server.send_response = mock_send_response
    return server, responses


async def _get(server, path, query):
    await server.route_api("GET", path, query, {}, b"", AsyncMock())


def _theme(asset_id):
    return next(a for a in list_assets("theme") if a["id"] == asset_id)


# ─── the catalog ─────────────────────────────────────────────────────────────

def test_theme_is_an_asset_type_and_background_is_no_longer_one():
    assert "theme" in VALID_TYPES
    assert "background" not in VALID_TYPES


def test_every_sidecar_in_the_store_directory_is_listed():
    on_disk = {p.stem for p in BUNDLED.iterdir() if p.suffix in (".yaml", ".yml")}
    assert {a["id"] for a in list_assets("theme")} == on_disk


def test_a_theme_needs_no_picture():
    """Madrid is colours and bands and nothing else, which is what the whole
    asset kind exists to express."""
    madrid = _theme("madrid")
    assert theme_image_path(madrid) is None
    assert resolve_asset_icon_path(madrid) is None
    assert madrid["theme"]["headerHeight"] == 132
    assert madrid["theme"]["footerHeight"] == 24
    assert madrid["theme"]["fontFamily"].startswith("Latin Modern Sans")
    assert madrid["theme"]["titleColor"] == "#ffffff"
    assert madrid["theme"]["footerText"] == "title"


def test_a_theme_with_a_picture_previews_it():
    arcs = _theme("blue-arcs")
    image = theme_image_path(arcs)
    assert image is not None and image.is_file()
    assert resolve_asset_icon_path(arcs) == image


def test_a_sidecar_can_only_set_fields_the_format_defines():
    """A theme is written straight into a user's document, so the store must not
    be able to introduce a key the editor does not understand."""
    for asset in list_assets("theme"):
        assert set(asset["theme"]) <= set(THEME_FIELDS), asset["id"]


def test_a_sidecar_field_that_is_not_a_theme_field_is_dropped(tmp_path):
    from gilzabots import assetstore
    store = tmp_path / "theme"
    store.mkdir()
    (store / "odd.yaml").write_text(
        "name: Odd\ntheme:\n  color: '#123456'\n  rm -rf: yes\n  headerHeight: not a number\n",
        encoding="utf-8",
    )
    original = assetstore._store_dirs
    assetstore._store_dirs = lambda t: [store] if t == "theme" else original(t)
    try:
        odd = _theme("odd")
        assert odd["theme"] == {"color": "#123456"}
    finally:
        assetstore._store_dirs = original


def test_a_picture_named_outside_the_store_is_ignored(tmp_path):
    from gilzabots import assetstore
    store = tmp_path / "theme"
    store.mkdir()
    (store / "escape.yaml").write_text("name: Escape\nimage: ../../../etc/hostname\n", encoding="utf-8")
    original = assetstore._store_dirs
    assetstore._store_dirs = lambda t: [store] if t == "theme" else original(t)
    try:
        assert theme_image_path(_theme("escape")) is None
    finally:
        assetstore._store_dirs = original


def test_listing_every_type_at_once_still_works():
    assert {"skill", "template", "theme"} <= {a["type"] for a in list_assets()}


# ─── not installable ─────────────────────────────────────────────────────────

def test_installing_a_theme_is_refused_with_an_explanation(tmp_path):
    with pytest.raises(ValueError, match="applied to an open presentation"):
        install_asset(_theme("madrid"), str(tmp_path))


# ─── the endpoints the front-end uses ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_assets_endpoint_serves_the_theme_catalog():
    server, responses = _api_harness()
    await _get(server, "/api/assets", {"type": ["theme"]})
    status, body, _ = responses[-1]
    assert status == 200
    assets = json.loads(body.decode("utf-8"))["assets"]

    madrid = next(a for a in assets if a["id"] == "madrid")
    assert madrid["hasImage"] is False
    # The store draws a preview from these when there is no picture to show.
    assert madrid["theme"]["headerColor"] == "#3333b3"

    arcs = next(a for a in assets if a["id"] == "blue-arcs")
    assert arcs["hasImage"] is True and arcs["hasIcon"] is True


@pytest.mark.asyncio
async def test_the_icon_endpoint_returns_the_theme_picture():
    server, responses = _api_harness()
    await _get(server, "/api/assets/icon", {"id": ["blue-arcs"]})
    status, body, content_type = responses[-1]
    assert status == 200
    assert content_type.startswith("image/")
    assert body[:3] == b"\xff\xd8\xff", "not a JPEG"


@pytest.mark.asyncio
async def test_a_theme_with_no_picture_has_no_icon():
    server, responses = _api_harness()
    await _get(server, "/api/assets/icon", {"id": ["madrid"]})
    assert responses[-1][0] == 404
