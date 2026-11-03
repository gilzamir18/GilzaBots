import pytest
import os
import json
from unittest.mock import AsyncMock
from gilzabots.ide_server import AsyncHTTPServer

@pytest.mark.asyncio
async def test_file_delete_api(tmp_path):
    # Setup test file
    test_dir = tmp_path / "project"
    test_dir.mkdir()
    test_file = test_dir / "test.txt"
    test_file.write_text("hello world")
    
    server = AsyncHTTPServer()
    writer = AsyncMock()
    
    # 1. Test success deletion
    body_data = {
        "projectPath": str(test_dir),
        "filePath": "test.txt"
    }
    body_bytes = json.dumps(body_data).encode('utf-8')
    
    # We mock send_response
    responses = []
    def mock_send_response(w, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode('utf-8'))))
        
    server.send_response = mock_send_response
    
    await server.route_api("POST", "/api/file/delete", {}, {}, body_bytes, writer)
    
    assert len(responses) == 1
    assert responses[0][0] == 200
    assert responses[0][1] == {"success": True}
    assert not test_file.exists()
    
    # 2. Test delete nonexistent file
    responses.clear()
    await server.route_api("POST", "/api/file/delete", {}, {}, body_bytes, writer)
    assert len(responses) == 1
    assert responses[0][0] == 404
    assert "error" in responses[0][1]

    # 3. Test path traversal attempt
    test_file2 = tmp_path / "outside.txt"
    test_file2.write_text("secret")
    
    body_data_traversal = {
        "projectPath": str(test_dir),
        "filePath": "../outside.txt"
    }
    body_bytes_traversal = json.dumps(body_data_traversal).encode('utf-8')
    responses.clear()
    
    await server.route_api("POST", "/api/file/delete", {}, {}, body_bytes_traversal, writer)
    assert len(responses) == 1
    assert responses[0][0] == 403
    assert "error" in responses[0][1]
    assert test_file2.exists()


async def _post(server, path, payload):
    responses = []
    server.send_response = lambda w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )
    await server.route_api("POST", path, {}, {}, json.dumps(payload).encode("utf-8"), AsyncMock())
    return responses


@pytest.mark.asyncio
async def test_file_delete_refuses_the_project_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.tex").write_text("x")

    for root_alias in (".", "./", "sub/.."):
        responses = await _post(AsyncHTTPServer(), "/api/file/delete", {"projectPath": str(project), "filePath": root_alias})
        assert responses[0][0] == 403
    assert (project / "main.tex").exists()


@pytest.mark.asyncio
async def test_file_endpoints_refuse_a_sibling_sharing_the_project_prefix(tmp_path):
    # "../projectX" resolves to a folder whose path starts with the project's,
    # which a bare startswith() check accepted.
    project = tmp_path / "project"
    project.mkdir()
    sibling = tmp_path / "projectX"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    server = AsyncHTTPServer()

    responses = await _post(server, "/api/file/delete", {"projectPath": str(project), "filePath": "../projectX"})
    assert responses[0][0] == 403
    responses = await _post(server, "/api/file/mkdir", {"projectPath": str(project), "dirPath": "../projectX/new"})
    assert responses[0][0] == 403
    responses = await _post(server, "/api/file/rename", {
        "projectPath": str(project), "oldPath": "../projectX/keep.txt", "newPath": "moved.txt",
    })
    assert responses[0][0] == 403
    responses = await _post(server, "/api/file/copy", {
        "projectPath": str(project), "sourcePath": "../projectX/keep.txt", "targetPath": "copy.txt",
    })
    assert responses[0][0] == 403
    assert (sibling / "keep.txt").read_text() == "keep"
    assert not (sibling / "new").exists()
