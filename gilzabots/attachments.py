"""Attachment utilities: document extraction and image compression for chat uploads."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET


ATTACHMENT_CACHE_DIRNAME = "attachment_cache"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    DOCX_MIME: ".docx",
    PPTX_MIME: ".pptx",
    XLSX_MIME: ".xlsx",
}


def _truncate(text: str, max_chars: int | None) -> str:
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def _guess_mime(filename: str, mime: str) -> str:
    """Return a useful MIME type even when the browser sends an empty value."""
    normalized = (mime or "").lower()
    if normalized and normalized != "application/octet-stream":
        return normalized
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".docx":
        return DOCX_MIME
    if ext == ".pptx":
        return PPTX_MIME
    if ext == ".xlsx":
        return XLSX_MIME
    return normalized or "application/octet-stream"


def extract_pdf_text(data_b64: str, max_chars: int | None = None) -> str:
    """Decode a base64-encoded PDF and extract its content as Markdown.

    Uses pymupdf4llm, which preserves headings, tables, and code blocks —
    producing much richer output for LLM consumption than plain-text extractors.

    Args:
        data_b64: Base64-encoded PDF bytes.
        max_chars: If set, truncate the output to this many characters.

    Returns:
        Markdown string representation of the PDF content.
    """
    import pymupdf4llm  # type: ignore

    raw = base64.b64decode(data_b64)
    # pymupdf4llm.to_markdown() requires a file path, not a buffer.
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        md_text: str = pymupdf4llm.to_markdown(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return _truncate(md_text, max_chars)


def _read_zip_xml(data_b64: str, member_names: list[str]) -> list[ET.Element]:
    raw = base64.b64decode(data_b64)
    roots: list[ET.Element] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in member_names:
            try:
                roots.append(ET.fromstring(archive.read(name)))
            except KeyError:
                continue
    return roots


def _xml_text(root: ET.Element) -> str:
    pieces: list[str] = []
    paragraph_break_tags = {"p", "tr"}
    line_break_tags = {"br", "cr"}
    tab_tags = {"tab"}

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "t" and element.text:
            pieces.append(element.text)
        elif tag in tab_tags:
            pieces.append("\t")
        elif tag in line_break_tags:
            pieces.append("\n")
        elif tag in paragraph_break_tags:
            if pieces and not pieces[-1].endswith("\n"):
                pieces.append("\n")

    text = "".join(pieces)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_docx_text(data_b64: str, max_chars: int | None = None) -> str:
    """Decode a base64-encoded DOCX and extract its visible document text."""
    roots = _read_zip_xml(data_b64, ["word/document.xml"])
    text = "\n\n".join(_xml_text(root) for root in roots).strip()
    return _truncate(text, max_chars)


def extract_pptx_text(data_b64: str, max_chars: int | None = None) -> str:
    """Decode a base64-encoded PPTX and extract slide text in slide order."""
    raw = base64.b64decode(data_b64)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        slide_names = [
            name for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ]
        slide_names.sort(key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)))
        roots = [ET.fromstring(archive.read(name)) for name in slide_names]

    slides = []
    for idx, root in enumerate(roots, start=1):
        text = _xml_text(root).strip()
        if text:
            slides.append(f"Slide {idx}:\n{text}")
    return _truncate("\n\n".join(slides), max_chars)


def _cell_text(value) -> str:
    """Render one spreadsheet cell as text.

    Dates get an explicit ISO rendering: openpyxl hands back datetime objects,
    whose str() is "2026-08-26 00:00:00", and a whole column of midnight
    timestamps is noise in a document whose point is usually the dates.
    """
    import datetime as _dt

    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def extract_xlsx_text(data_b64: str, max_chars: int | None = None) -> str:
    """Decode a base64-encoded XLSX and render every sheet as tab-separated rows.

    Read-only mode streams the rows instead of building the whole workbook in
    memory, and `data_only` returns the cached result of a formula rather than
    the formula source, which is what a reader actually wants to see.
    """
    import openpyxl  # type: ignore

    raw = base64.b64decode(data_b64)
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell_text(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                rows.append("\t".join(cells))
            while rows and not rows[-1]:
                rows.pop()
            body = "\n".join(rows)
            blocks.append(f"# Sheet: {sheet.title}\n{body}" if body else f"# Sheet: {sheet.title}\n(empty)")
    finally:
        workbook.close()
    return _truncate("\n\n".join(blocks), max_chars)


def extract_document_text(
    data_b64: str,
    mime: str,
    filename: str = "",
    max_chars: int | None = None,
) -> str:
    """Extract text from supported document uploads."""
    normalized_mime = _guess_mime(filename, mime)
    if normalized_mime == "application/pdf":
        return extract_pdf_text(data_b64, max_chars=max_chars)
    if normalized_mime == DOCX_MIME:
        return extract_docx_text(data_b64, max_chars=max_chars)
    if normalized_mime == PPTX_MIME:
        return extract_pptx_text(data_b64, max_chars=max_chars)
    if normalized_mime == XLSX_MIME:
        return extract_xlsx_text(data_b64, max_chars=max_chars)
    raise ValueError(f"Unsupported document type: {mime or filename or 'unknown'}")


EXTRACTABLE_DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx"}


def extract_document_text_from_path(path: str, max_chars: int | None = None) -> str:
    """Extract a document's text from a file on disk.

    The extractors were written for chat uploads and take base64, so this is the
    bridge for a file that merely sits in the workspace: same tested extraction
    code, reached by path instead of by upload. Without it, `read_file` on a
    workspace PDF had no route at all -- extraction existed but only for files
    that arrived through the attachment pipeline.
    """
    with open(path, "rb") as f:
        raw = f.read()
    return extract_document_text(
        base64.b64encode(raw).decode(),
        "",
        os.path.basename(path),
        max_chars=max_chars,
    )


def compress_image(data_b64: str, mime: str, max_side: int = 1024) -> str:
    """Resize an image to fit within max_side × max_side and re-encode as JPEG.

    Args:
        data_b64: Base64-encoded image bytes.
        mime: Original MIME type (e.g. "image/png").
        max_side: Maximum pixel length for the longest dimension.

    Returns:
        Base64-encoded JPEG string (without data-URL prefix).
    """
    from PIL import Image  # type: ignore

    raw = base64.b64decode(data_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def store_original_upload(data_b64: str, filename: str, mime: str = "") -> str:
    """Write an uploaded document to the local cache and return its path.

    The original bytes have to survive the upload: `read_file(alias)` re-extracts
    the attached document on demand. Carrying them as base64 inside the
    descriptor meant the whole file was sent back to the server on every turn of
    the chat and stored again in each history row, so a handful of PDFs made both
    the request and the database grow by tens of megabytes. On disk it is written
    once, addressed by content so repeated uploads of the same file share it, and
    it travels as a path.

    Returns an empty string when the cache cannot be written; the caller then
    keeps working from the extracted text alone.
    """
    from gilzabots.config import get_gilzabots_home

    try:
        raw = base64.b64decode(data_b64)
        digest = hashlib.sha256(raw).hexdigest()
        # The extension is what tells the by-path extractor which format this
        # is, so an upload named without one takes it from the MIME type.
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in EXTRACTABLE_DOC_EXTS:
            ext = _MIME_EXTENSIONS.get(_guess_mime(filename, mime), "")
        cache_dir = os.path.join(get_gilzabots_home(), ATTACHMENT_CACHE_DIRNAME)
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{digest}{ext}")
        if not os.path.exists(path):
            # Write to a sibling first so a crash cannot leave a half file that
            # the content hash would later vouch for.
            tmp_path = f"{path}.part"
            with open(tmp_path, "wb") as handle:
                handle.write(raw)
            os.replace(tmp_path, path)
        return path
    except Exception:
        return ""


def build_attachment_descriptor(
    filename: str,
    data_b64: str,
    mime: str,
    *,
    max_chars: int | None = None,
) -> dict:
    """Convert a raw upload into a normalised attachment descriptor.

    For images: compresses and returns type="image".
    For documents: extracts text, caches the original on disk and returns
    type="pdf_text" with a "raw_path" pointing at it.
    Unknown types: returned as-is with type="unknown".

    Returns:
        {"type": ..., "data": ..., "mime": ..., "name": filename}
    """
    normalized_mime = _guess_mime(filename, mime)
    if normalized_mime in {"application/pdf", DOCX_MIME, PPTX_MIME}:
        text = extract_document_text(data_b64, normalized_mime, filename, max_chars=max_chars)
        return {
            "type": "pdf_text",
            "data": text,
            "raw_path": store_original_upload(data_b64, filename, normalized_mime),
            "raw_mime": normalized_mime,
            "mime": normalized_mime,
            "name": filename,
        }

    if normalized_mime.startswith("image/"):
        compressed = compress_image(data_b64, normalized_mime)
        return {"type": "image", "data": compressed, "mime": "image/jpeg", "name": filename}

    # Fallback: pass raw data through unchanged
    return {"type": "unknown", "data": data_b64, "mime": normalized_mime, "name": filename}
