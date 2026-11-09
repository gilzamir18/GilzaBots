"""Tests for the directory listing endpoint that backs the directory picker."""

import json
import os
import sys
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8"))))

    server.send_response = mock_send_response
    return server, responses


async def _list_dirs(path):
    server, responses = _server_with_capture()
    body = json.dumps({"path": str(path)}).encode()
    await server.route_api("POST", "/api/fs/dirs", {}, {}, body, AsyncMock())
    return responses[0]


@pytest.mark.asyncio
async def test_lists_parent_entry_and_subdirectories(tmp_path):
    (tmp_path / "beta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")

    status, body = await _list_dirs(tmp_path)

    assert status == 200
    assert body["current"] == str(tmp_path)
    assert [d["name"] for d in body["dirs"]] == ["..", "alpha", "beta"]
    assert body["dirs"][0]["path"] == str(tmp_path.parent)


@pytest.mark.asyncio
async def test_unreadable_folder_still_offers_the_way_back(tmp_path, monkeypatch):
    """A folder whose listing is denied must keep the ".." entry.

    Regression: the Windows "My Documents" junction denies listing, and the
    picker used to receive an empty list with no way to leave the folder.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    real_listdir = os.listdir

    def denying_listdir(path):
        if os.path.abspath(path) == str(locked):
            raise PermissionError(5, "Access is denied", str(locked))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", denying_listdir)

    status, body = await _list_dirs(locked)

    assert status == 403
    assert body["error"] == "Permission denied"
    assert body["current"] == str(locked)
    assert body["dirs"] == [{"name": "..", "path": str(tmp_path)}]


@pytest.mark.asyncio
async def test_unexpected_listing_error_still_offers_the_way_back(tmp_path, monkeypatch):
    target = tmp_path / "broken"
    target.mkdir()

    def failing_listdir(path):
        raise OSError("device not ready")

    monkeypatch.setattr(os, "listdir", failing_listdir)

    status, body = await _list_dirs(target)

    assert status == 500
    assert "device not ready" in body["error"]
    assert body["dirs"] == [{"name": "..", "path": str(tmp_path)}]


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only junction")
async def test_real_my_documents_junction_keeps_parent_entry():
    junction = os.path.join(os.path.expanduser("~"), "Meus Documentos")
    if not os.path.isdir(junction):
        junction = os.path.join(os.path.expanduser("~"), "My Documents")
    if not os.path.isdir(junction):
        pytest.skip("No legacy My Documents junction on this machine")

    status, body = await _list_dirs(junction)

    assert {"name": "..", "path": os.path.dirname(junction)} in body["dirs"]
    if status != 200:
        assert body["error"]
