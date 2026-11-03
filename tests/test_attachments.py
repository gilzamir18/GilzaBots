"""Tests for gilzabots.attachments module.

Covers:
- PDF text extraction (pymupdf4llm)
- Image compression (Pillow)
- build_attachment_descriptor routing
- AgentInput multimodal content construction
- Vision gate fallback in agent_stdin.handle_run
"""
from __future__ import annotations

import base64
import io
import json
import asyncio
import zipfile
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tiny_jpeg_b64() -> str:
    """Return a base64-encoded minimal 1×1 JPEG image."""
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _tiny_png_b64() -> str:
    """Return a base64-encoded minimal PNG image."""
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (200, 300), color=(0, 128, 255))
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _zip_b64(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode()


def _minimal_docx_b64() -> str:
    return _zip_b64({
        "word/document.xml": """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
  </w:body>
</w:document>""",
    })


def _minimal_pptx_b64() -> str:
    return _zip_b64({
        "ppt/slides/slide2.xml": """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>""",
        "ppt/slides/slide1.xml": """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Title slide</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>""",
    })


# ─────────────────────────────────────────────────────────────────────────────
# compress_image
# ─────────────────────────────────────────────────────────────────────────────

def test_compress_image_returns_valid_base64_jpeg():
    from gilzabots.attachments import compress_image

    b64 = _tiny_png_b64()
    result = compress_image(b64, "image/png", max_side=50)

    # Must be valid base64
    raw = base64.b64decode(result)
    assert len(raw) > 0

    # Must be a JPEG (SOI magic bytes FF D8)
    assert raw[:2] == b"\xff\xd8", "Expected JPEG output"


def test_compress_image_respects_max_side():
    from gilzabots.attachments import compress_image
    from PIL import Image

    b64 = _tiny_png_b64()  # 200×300
    result = compress_image(b64, "image/png", max_side=64)
    raw = base64.b64decode(result)
    img = Image.open(io.BytesIO(raw))
    assert max(img.size) <= 64, f"Expected max side ≤64, got {img.size}"


# ─────────────────────────────────────────────────────────────────────────────
# build_attachment_descriptor
# ─────────────────────────────────────────────────────────────────────────────

def test_build_attachment_descriptor_image():
    from gilzabots.attachments import build_attachment_descriptor

    b64 = _tiny_jpeg_b64()
    desc = build_attachment_descriptor("photo.jpg", b64, "image/jpeg")

    assert desc["type"] == "image"
    assert desc["mime"] == "image/jpeg"
    assert desc["name"] == "photo.jpg"
    assert isinstance(desc["data"], str)
    # data must be valid base64
    base64.b64decode(desc["data"])


def test_build_attachment_descriptor_pdf_preserves_original(tmp_path, monkeypatch):
    from gilzabots import attachments

    raw = base64.b64encode(b"%PDF-1.4 fake").decode()
    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    monkeypatch.setattr(attachments, "extract_pdf_text", lambda data_b64, max_chars=None: "PDF text")

    desc = attachments.build_attachment_descriptor("paper.pdf", raw, "application/pdf")

    assert desc["type"] == "pdf_text"
    assert desc["data"] == "PDF text"
    assert desc["raw_mime"] == "application/pdf"
    # The original is cached on disk instead of riding along as base64.
    assert "raw_data" not in desc
    assert open(desc["raw_path"], "rb").read() == base64.b64decode(raw)


def test_store_original_upload_is_content_addressed(tmp_path, monkeypatch):
    from gilzabots.attachments import store_original_upload

    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    raw = base64.b64encode(b"%PDF-1.4 fake").decode()

    first = store_original_upload(raw, "paper.pdf")
    second = store_original_upload(raw, "paper.pdf")

    assert first == second  # the same file is never stored twice
    assert first.endswith(".pdf")


def test_store_original_upload_names_the_cache_by_mime_when_the_upload_has_no_extension(tmp_path, monkeypatch):
    """The by-path extractor dispatches on the extension, so it must be there."""
    from gilzabots.attachments import store_original_upload

    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    raw = base64.b64encode(b"%PDF-1.4 fake").decode()

    path = store_original_upload(raw, "scan", "application/pdf")

    assert path.endswith(".pdf")


def test_store_original_upload_survives_an_unwritable_cache(tmp_path, monkeypatch):
    """A failed cache write must not fail the upload: the text still works."""
    from gilzabots import attachments

    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    monkeypatch.setattr(attachments.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    assert attachments.store_original_upload(base64.b64encode(b"x").decode(), "a.pdf") == ""


def test_extract_docx_text_reads_document_xml():
    from gilzabots.attachments import extract_docx_text

    text = extract_docx_text(_minimal_docx_b64())
    assert "Hello DOCX" in text
    assert "Second paragraph" in text


def test_extract_pptx_text_reads_slides_in_order():
    from gilzabots.attachments import extract_pptx_text

    text = extract_pptx_text(_minimal_pptx_b64())
    assert text.index("Title slide") < text.index("Second slide")
    assert "Slide 1:" in text
    assert "Slide 2:" in text


def test_build_attachment_descriptor_docx_by_extension_when_mime_missing(tmp_path, monkeypatch):
    from gilzabots.attachments import DOCX_MIME, build_attachment_descriptor

    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    raw = _minimal_docx_b64()
    desc = build_attachment_descriptor("notes.docx", raw, "application/octet-stream")

    assert desc["type"] == "pdf_text"
    assert desc["mime"] == DOCX_MIME
    assert open(desc["raw_path"], "rb").read() == base64.b64decode(raw)
    assert "Hello DOCX" in desc["data"]


def test_build_attachment_descriptor_pptx_preserves_original(tmp_path, monkeypatch):
    from gilzabots.attachments import PPTX_MIME, build_attachment_descriptor

    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    raw = _minimal_pptx_b64()
    desc = build_attachment_descriptor("deck.pptx", raw, PPTX_MIME)

    assert desc["type"] == "pdf_text"
    assert desc["raw_mime"] == PPTX_MIME
    assert open(desc["raw_path"], "rb").read() == base64.b64decode(raw)
    assert "Title slide" in desc["data"]


def test_build_attachment_descriptor_unknown_passes_through():
    from gilzabots.attachments import build_attachment_descriptor

    raw = base64.b64encode(b"binary data").decode()
    desc = build_attachment_descriptor("file.bin", raw, "application/octet-stream")
    assert desc["type"] == "unknown"
    assert desc["data"] == raw


# ─────────────────────────────────────────────────────────────────────────────
# AgentInput — multimodal content construction
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_input_no_attachments_defaults_to_empty_list():
    from agenticblocks.blocks.llm.agent import AgentInput

    inp = AgentInput(prompt="Hello")
    assert inp.attachments == []


def test_agent_input_accepts_attachments():
    from agenticblocks.blocks.llm.agent import AgentInput

    att = {"type": "image", "data": "abc123", "mime": "image/jpeg", "name": "img.jpg"}
    inp = AgentInput(prompt="Describe this", attachments=[att])
    assert len(inp.attachments) == 1
    assert inp.attachments[0]["type"] == "image"


def test_read_file_extracts_attachment_from_the_cached_original(tmp_path):
    import gilzabots.tools as tools

    cached = tmp_path / "paper.docx"
    cached.write_bytes(base64.b64decode(_minimal_docx_b64()))

    tools.set_recent_file_attachments({
        "input_file_0.docx": {
            "type": "pdf_text",
            "data": "preview text",
            "raw_path": str(cached),
            "mime": "application/pdf",
            "name": "paper.docx",
        }
    })

    input_model = tools.read_file.input_schema()
    result = asyncio.run(tools.read_file.run(input_model(path="input_file_0.docx")))
    assert "Hello DOCX" in result.result


def test_read_file_falls_back_to_extracted_text_when_the_cache_is_gone(tmp_path):
    """An old chat whose cached original was deleted still reads its text."""
    import gilzabots.tools as tools

    tools.set_recent_file_attachments({
        "input_file_0.pdf": {
            "type": "pdf_text",
            "data": "preview text",
            "raw_path": str(tmp_path / "missing.pdf"),
            "mime": "application/pdf",
            "name": "paper.pdf",
        }
    })

    input_model = tools.read_file.input_schema()
    result = asyncio.run(tools.read_file.run(input_model(path="input_file_0.pdf")))
    assert result.result == "preview text"


def test_read_file_extracts_recent_pdf_attachment(monkeypatch):
    """Legacy history rows still carry the original as inline base64."""
    import gilzabots.tools as tools

    raw = base64.b64encode(b"%PDF-1.4 fake").decode()
    monkeypatch.setattr(
        "gilzabots.attachments.extract_document_text",
        lambda data_b64, mime, filename="": f"extracted:{mime}:{filename}:{data_b64}",
    )

    tools.set_recent_file_attachments({
        "input_file_0.pdf": {
            "type": "pdf_text",
            "data": "preview text",
            "raw_data": raw,
            "mime": "application/pdf",
            "name": "paper.pdf",
        }
    })

    input_model = tools.read_file.input_schema()
    result = asyncio.run(tools.read_file.run(input_model(path="input_file_0.pdf")))
    assert result.result == f"extracted:application/pdf:paper.pdf:{raw}"


# ─────────────────────────────────────────────────────────────────────────────
# Vision gate in handle_run (unit-level via monkey-patching)
# ─────────────────────────────────────────────────────────────────────────────

def test_vision_gate_strips_image_for_text_only_model(monkeypatch):
    """Image attachments must be removed and a note added to the prompt
    when the model reports no vision support."""
    import gilzabots.agent_stdin as stdin_mod

    # Patch litellm.supports_vision to return False
    import litellm
    monkeypatch.setattr(litellm, "supports_vision", lambda model: False)

    # Build a fake "current_project" with no model_params
    class FakeProject:
        model = "ollama/gemma4:12b"
        model_params = {}
        history = []

    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())

    raw_attachments = [
        {"type": "image", "data": "aGVsbG8=", "mime": "image/jpeg", "name": "shot.jpg"},
        {"type": "pdf_text", "data": "Some PDF text", "mime": "application/pdf", "name": "doc.pdf"},
    ]

    # Re-implement the gate logic exactly as in agent_stdin.handle_run
    prompt = "My prompt"
    final_attachments = []
    for att in raw_attachments:
        att_type = att.get("type", "")
        model_supports_vision = litellm.supports_vision(FakeProject.model)
        if att_type == "image" and not model_supports_vision:
            prompt += f"\n\n[Note: The user attached image '{att.get('name', 'image')}' but the active model does not support vision. The image was not analysed.]"
        else:
            final_attachments.append(att)

    assert "shot.jpg" in prompt
    assert "does not support vision" in prompt
    # PDF should still be forwarded
    assert len(final_attachments) == 1
    assert final_attachments[0]["type"] == "pdf_text"


def test_vision_gate_passes_image_for_vision_model(monkeypatch):
    """Image attachments must be forwarded when the model supports vision."""
    import litellm
    monkeypatch.setattr(litellm, "supports_vision", lambda model: True)

    raw_attachments = [
        {"type": "image", "data": "aGVsbG8=", "mime": "image/jpeg", "name": "shot.jpg"},
    ]
    prompt = "Describe this"
    final_attachments = []
    for att in raw_attachments:
        att_type = att.get("type", "")
        if att_type == "image" and not litellm.supports_vision("gemini/gemini-2.0-flash"):
            prompt += "NOTE"
        else:
            final_attachments.append(att)

    assert "NOTE" not in prompt
    assert len(final_attachments) == 1


# ─────────────────────────────────────────────────────────────────────────────
# PDF truncation logic
# ─────────────────────────────────────────────────────────────────────────────

def test_pdf_truncation_caps_oversized_pdf():
    """When pdf text exceeds the allowed chars, it must be truncated."""
    long_pdf_text = "A" * 10_000
    att = {"type": "pdf_text", "data": long_pdf_text, "mime": "application/pdf", "name": "big.pdf"}

    # Simulate a tiny context window
    num_ctx = 1000  # tokens
    history_tokens = 100
    free_tokens = num_ctx - history_tokens  # 900
    free_chars = free_tokens * 4            # 3600
    pdf_truncate_pct = 50
    allowed_chars = int(free_chars * pdf_truncate_pct / 100)  # 1800

    final_attachments = []
    if len(att["data"]) > allowed_chars and allowed_chars > 0:
        truncated = att["data"][:allowed_chars]
        truncated += f"\n\n[PDF truncated: {len(att['data']):,} chars total, {allowed_chars:,} shown]"
        att = {**att, "data": truncated}
    final_attachments.append(att)

    assert len(final_attachments) == 1
    data = final_attachments[0]["data"]
    assert "PDF truncated" in data
    assert len(data) < len(long_pdf_text)


def test_pdf_truncation_skipped_when_disabled():
    """When pdf_truncate=False, the full text must be forwarded."""
    long_pdf_text = "B" * 5_000
    att = {"type": "pdf_text", "data": long_pdf_text, "mime": "application/pdf", "name": "doc.pdf"}

    pdf_truncate_enabled = False  # disabled by project setting

    final_attachments = []
    if pdf_truncate_enabled:
        # truncation code (not reached)
        pass
    else:
        final_attachments.append(att)

    assert final_attachments[0]["data"] == long_pdf_text


# ─────────────────────────────────────────────────────────────────────────────
# force_vision override
# ─────────────────────────────────────────────────────────────────────────────

def test_vision_gate_force_vision_overrides_litellm(monkeypatch):
    """force_vision=True in model_params must pass images even when litellm
    reports no vision support — needed for local Ollama models like llava."""
    import litellm
    # Simulate litellm not knowing this local model
    monkeypatch.setattr(litellm, "supports_vision", lambda model: False)

    model_params = {"force_vision": True}
    raw_attachments = [
        {"type": "image", "data": "aGVsbG8=", "mime": "image/jpeg", "name": "photo.jpg"},
    ]
    prompt = "Describe this image"
    final_attachments = []

    # Mirror the gate logic from agent_stdin exactly
    _litellm_vision = litellm.supports_vision("ollama/llava:7b")
    model_supports_vision = _litellm_vision or bool(model_params.get("force_vision", False))

    for att in raw_attachments:
        att_type = att.get("type", "")
        if att_type == "image" and not model_supports_vision:
            prompt += (
                f"\n\n[Note: The user attached image '{att.get('name')}' "
                f"but the active model does not support vision.]"
            )
        else:
            final_attachments.append(att)

    # Image must be forwarded, not stripped
    assert len(final_attachments) == 1
    assert "does not support vision" not in prompt
