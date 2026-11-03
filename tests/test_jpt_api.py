"""HTTP boundaries keep the slide editor unaware of the ZIP container."""

import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from gilzabots import jpt
from gilzabots.ide_server import AsyncHTTPServer


def _deck_with_image(src):
    deck = jpt.create_deck("API")
    slide = jpt.create_slide(id="slide-1")
    slide["elements"].append(jpt.create_element(
        "image", id="image-1", src=src, x=10, y=10, w=100, h=100,
    ))
    deck["slides"].append(slide)
    return deck


@pytest.mark.asyncio
async def test_file_write_packages_jpt_and_returns_canonical_json(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "picture.png").write_bytes(b"picture")
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _writer, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body), content_type)
    )

    body = json.dumps({
        "projectPath": str(project),
        "filePath": "deck.jpt",
        "content": jpt.serialize(_deck_with_image("picture.png")),
    }).encode()
    await server.route_api("POST", "/api/file/write", {}, {}, body, AsyncMock())

    assert responses[0][0] == 200
    payload = responses[0][1]
    assert payload["jptPackaged"] is True
    assert payload["assets"] == 1
    assert "jpt:assets/" in payload["content"]
    assert jpt.is_packaged_jpt(project / "deck.jpt")


@pytest.mark.asyncio
async def test_file_read_returns_the_logical_json_and_package_flag(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "deck.jpt"
    result = jpt.write_packaged_jpt(
        target, _deck_with_image("data:image/png;base64,cGljdHVyZQ=="),
        project_root=project,
    )
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _writer, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body), content_type)
    )

    await server.route_api(
        "GET", "/api/file/read",
        {"projectPath": [str(project)], "filePath": ["deck.jpt"]},
        {}, b"", AsyncMock(),
    )

    assert responses == [(200, {
        "content": result.text,
        "jptPackaged": True,
    }, "application/json")]


@pytest.mark.asyncio
async def test_jpt_asset_endpoint_serves_video_byte_ranges(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "film.mp4").write_bytes(bytes(range(100)))
    deck = jpt.create_deck("Video")
    slide = jpt.create_slide(id="slide-1")
    slide["elements"].append(jpt.create_element(
        "video", id="video-1", src="film.mp4", x=10, y=10, w=320, h=180,
    ))
    deck["slides"].append(slide)
    result = jpt.write_packaged_jpt(
        project / "deck.jpt", deck, project_root=project
    )
    ref = json.loads(result.text)["slides"][0]["elements"][0]["src"]

    server = AsyncHTTPServer()
    responses = []
    async def capture_stream(
        _writer, status, chunks, content_length,
        content_type="application/octet-stream", extra_headers=None,
    ):
        body = b"".join(chunks)
        assert len(body) == content_length
        responses.append((status, body, content_type, extra_headers))

    server.send_streaming_response = capture_stream
    await server.route_api(
        "GET", "/api/jpt/asset",
        {
            "projectPath": [str(project)],
            "filePath": ["deck.jpt"],
            "src": [ref],
        },
        {"range": "bytes=20-29"}, b"", AsyncMock(),
    )

    status, body, content_type, headers = responses[0]
    assert status == 206
    assert body == bytes(range(20, 30))
    assert content_type == "video/mp4"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Range"] == "bytes 20-29/100"


@pytest.mark.asyncio
async def test_file_read_keeps_legacy_json_compatible(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    text = jpt.serialize(_deck_with_image("https://example.com/image.png"))
    (project / "legacy.jpt").write_text(text, encoding="utf-8")
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _writer, status, body, content_type="text/plain": responses.append(
        json.loads(body)
    )

    await server.route_api(
        "GET", "/api/file/read",
        {"projectPath": [str(project)], "filePath": ["legacy.jpt"]},
        {}, b"", AsyncMock(),
    )
    assert responses == [{"content": text, "jptPackaged": False}]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_git_diff_shows_packaged_jpt_as_logical_json(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "deck.jpt"
    before_deck = _deck_with_image("https://example.com/image.png")
    jpt.write_packaged_jpt(target, before_deck, project_root=project)
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "deck.jpt"],
        ["git", "commit", "-m", "before"],
    ):
        subprocess.run(command, cwd=project, check=True, capture_output=True)

    after_deck = _deck_with_image("https://example.com/image.png")
    after_deck["title"] = "After"
    jpt.write_packaged_jpt(target, after_deck, project_root=project)
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _writer, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body))
    )

    await server.route_api(
        "GET", "/api/git/diff",
        {
            "projectPath": [str(project)],
            "filePath": ["deck.jpt"],
            "shadow": ["false"],
        },
        {}, b"", AsyncMock(),
    )

    assert responses[0][0] == 200
    diff = responses[0][1]["diff"]
    assert "::deck.json" in diff
    assert '-  "title": "API"' in diff
    assert '+  "title": "After"' in diff
    assert "Binary files" not in diff
