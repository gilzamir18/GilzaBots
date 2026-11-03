"""The standalone PDF viewer's recompile button: /api/latex/pdf-source tells
whether a PDF has a .tex to rebuild it from, and /api/latex/compile-pdf-source
rebuilds it from disk without the .tex being open in the editor."""

import json
import os
from unittest.mock import AsyncMock

import pytest

import gilzabots.latex_compiler as latex_compiler
from gilzabots.ide_server import AsyncHTTPServer


ROOT_DOCUMENT = "\\documentclass{article}\n\\begin{document}\nRebuilt\n\\end{document}\n"


def _server():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8"))))

    server.send_response = mock_send_response
    return server, responses


@pytest.mark.asyncio
async def test_pdf_source_reports_the_sibling_root_document(tmp_path):
    (tmp_path / "main.tex").write_text(ROOT_DOCUMENT, encoding="utf-8")
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4")
    server, responses = _server()

    for pdf in ("main.pdf", "paper.pdf"):
        await server.route_api(
            "GET", "/api/latex/pdf-source",
            {"projectPath": [str(tmp_path)], "pdfPath": [pdf]}, {}, b"", AsyncMock(),
        )

    assert responses == [(200, {"texFile": "main.tex"}), (200, {"texFile": ""})]


@pytest.mark.asyncio
async def test_compile_without_source_fails_with_a_diagnostic(tmp_path):
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4")
    server, responses = _server()

    await server.route_api(
        "POST", "/api/latex/compile-pdf-source", {}, {},
        json.dumps({"projectPath": str(tmp_path), "pdfPath": "paper.pdf"}).encode("utf-8"),
        AsyncMock(),
    )

    status, body = responses[0]
    assert status == 200
    assert body["success"] is False
    assert "paper.pdf" in body["log"]
    assert (tmp_path / "paper.pdf").read_bytes() == b"%PDF-1.4"


@pytest.mark.skipif(not latex_compiler.get_tectonic_path(), reason="Tectonic is not installed")
@pytest.mark.asyncio
async def test_compile_rebuilds_the_viewed_pdf_from_disk(tmp_path):
    (tmp_path / "doc.tex").write_text(ROOT_DOCUMENT, encoding="utf-8")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"stale")
    server, responses = _server()

    await server.route_api(
        "POST", "/api/latex/compile-pdf-source", {}, {},
        json.dumps({"projectPath": str(tmp_path), "pdfPath": "doc.pdf"}).encode("utf-8"),
        AsyncMock(),
    )

    status, body = responses[0]
    assert status == 200
    assert body["success"] is True, body.get("log")
    assert body["compiled_main_file"] == "doc.tex"
    assert pdf.read_bytes().startswith(b"%PDF")
    assert os.path.samefile(server.last_pdf_path, pdf)


@pytest.mark.skipif(not latex_compiler.get_tectonic_path(), reason="Tectonic is not installed")
@pytest.mark.asyncio
async def test_failed_compile_returns_the_source_for_fix_with_ai(tmp_path):
    # The .tex is not open in the editor, so "Fix with AI" can only show the
    # agent the source if the endpoint returns it.
    broken = "\\documentclass{article}\n\\begin{document}\n\\undefinedmacro\n\\end{document}\n"
    (tmp_path / "doc.tex").write_text(broken, encoding="utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    server, responses = _server()

    await server.route_api(
        "POST", "/api/latex/compile-pdf-source", {}, {},
        json.dumps({"projectPath": str(tmp_path), "pdfPath": "doc.pdf"}).encode("utf-8"),
        AsyncMock(),
    )

    status, body = responses[0]
    assert status == 200
    assert body["success"] is False
    assert body["compiled_main_file"] == "doc.tex"
    assert body["source_content"] == broken
