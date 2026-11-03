"""Tests for the clipboard image endpoint used by chat image paste."""

import base64
import json
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


@pytest.mark.asyncio
async def test_read_image_returns_clipboard_png(monkeypatch):
    payload = base64.b64encode(b"fake-png-bytes").decode()
    monkeypatch.setattr("gilzabots.ide_server._read_clipboard_image", lambda: payload)

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/clipboard/read-image", {}, {}, b"", AsyncMock())

    status, body, _ = responses[0]
    assert status == 200
    assert body["data_b64"] == payload
    assert body["mime"] == "image/png"


@pytest.mark.asyncio
async def test_read_image_reports_an_empty_clipboard(monkeypatch):
    """No image on the clipboard is a normal outcome, not an error."""
    monkeypatch.setattr("gilzabots.ide_server._read_clipboard_image", lambda: "")

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/clipboard/read-image", {}, {}, b"", AsyncMock())

    status, body, _ = responses[0]
    assert status == 200
    assert body["data_b64"] is None
    assert body["mime"] is None


@pytest.mark.asyncio
async def test_upload_of_a_pasted_image_returns_an_image_descriptor(monkeypatch):
    """The paste path uploads through the same endpoint as picker and drag-drop."""
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    data_b64 = base64.b64encode(buf.getvalue()).decode()

    server, responses = _server_with_capture()
    body = json.dumps({
        "filename": "pasted-image-1700000000.png",
        "data_b64": data_b64,
        "mime": "image/png",
    }).encode()
    await server.route_api("POST", "/api/chat/upload", {}, {}, body, AsyncMock())

    status, descriptor, _ = responses[0]
    assert status == 200
    assert descriptor["type"] == "image"
    assert descriptor["name"] == "pasted-image-1700000000.png"
    assert descriptor["mime"] == "image/jpeg"  # compressed for the model
    assert descriptor["data"]
