"""Creating entries from the workspace tree must never overwrite existing ones."""

import json
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer


def _server():
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _writer, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body))
    )
    return server, responses


async def _post(server, path, payload):
    await server.route_api("POST", path, {}, {}, json.dumps(payload).encode(), AsyncMock())


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.mark.asyncio
async def test_exclusive_write_creates_a_missing_file(project):
    server, responses = _server()
    await _post(server, "/api/file/write", {
        "projectPath": str(project), "filePath": "src/new.tex", "content": "", "exclusive": True,
    })
    assert responses[0][0] == 200
    assert (project / "src" / "new.tex").read_text() == ""


@pytest.mark.asyncio
async def test_exclusive_write_refuses_an_existing_file_and_keeps_its_content(project):
    (project / "main.tex").write_text("precious")
    server, responses = _server()
    await _post(server, "/api/file/write", {
        "projectPath": str(project), "filePath": "main.tex", "content": "", "exclusive": True,
    })
    assert responses[0][0] == 409
    assert responses[0][1]["code"] == "exists"
    assert (project / "main.tex").read_text() == "precious"


@pytest.mark.asyncio
async def test_exclusive_write_refuses_a_path_taken_by_a_directory(project):
    (project / "figures").mkdir()
    server, responses = _server()
    await _post(server, "/api/file/write", {
        "projectPath": str(project), "filePath": "figures", "content": "", "exclusive": True,
    })
    assert responses[0][0] == 409
    assert (project / "figures").is_dir()


@pytest.mark.asyncio
async def test_plain_write_still_overwrites(project):
    (project / "main.tex").write_text("old")
    server, responses = _server()
    await _post(server, "/api/file/write", {
        "projectPath": str(project), "filePath": "main.tex", "content": "new",
    })
    assert responses[0][0] == 200
    assert (project / "main.tex").read_text() == "new"


@pytest.mark.asyncio
async def test_exclusive_mkdir_creates_a_missing_nested_directory(project):
    server, responses = _server()
    await _post(server, "/api/file/mkdir", {
        "projectPath": str(project), "dirPath": "src/components", "exclusive": True,
    })
    assert responses[0][0] == 200
    assert (project / "src" / "components").is_dir()


@pytest.mark.asyncio
async def test_exclusive_mkdir_refuses_existing_directory_or_file(project):
    (project / "src").mkdir()
    (project / "notes.txt").write_text("keep")
    server, responses = _server()
    await _post(server, "/api/file/mkdir", {"projectPath": str(project), "dirPath": "src", "exclusive": True})
    await _post(server, "/api/file/mkdir", {"projectPath": str(project), "dirPath": "notes.txt", "exclusive": True})
    assert [status for status, _ in responses] == [409, 409]
    assert (project / "notes.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_plain_mkdir_on_existing_directory_still_succeeds(project):
    (project / "src").mkdir()
    server, responses = _server()
    await _post(server, "/api/file/mkdir", {"projectPath": str(project), "dirPath": "src"})
    assert responses[0][0] == 200
