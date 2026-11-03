"""Tests for the shared context budget applied to document attachments.

Several documents in one turn used to receive the full truncation percentage
each, so N documents occupied N times the intended slice of the free context.
"""

from gilzabots.agent_stdin import _apply_document_budget


def _doc(name: str, chars: int) -> dict:
    return {
        "type": "pdf_text",
        "data": "A" * chars,
        "mime": "application/pdf",
        "name": name,
    }


def _shown_chars(att: dict) -> int:
    """Length of the payload without the appended truncation note."""
    data = att["data"]
    marker = "\n\n[PDF truncated:"
    return len(data.split(marker)[0])


def test_single_document_keeps_previous_budget():
    free_chars = 3600
    result = _apply_document_budget([_doc("big.pdf", 10_000)], free_chars, 50)

    assert _shown_chars(result[0]) == 1800
    assert "PDF truncated" in result[0]["data"]
    assert "shared across" not in result[0]["data"]


def test_budget_is_shared_between_documents():
    free_chars = 4000  # 50% => 2000 chars for the whole turn
    attachments = [_doc("a.pdf", 10_000), _doc("b.pdf", 10_000), _doc("c.pdf", 10_000)]

    result = _apply_document_budget(attachments, free_chars, 50)

    assert sum(_shown_chars(att) for att in result) == 2000
    for att in result:
        assert "shared across 3 documents" in att["data"]


def test_unused_share_rolls_over_to_the_next_document():
    free_chars = 4000  # 50% => 2000 chars
    attachments = [_doc("small.pdf", 100), _doc("big.pdf", 10_000)]

    result = _apply_document_budget(attachments, free_chars, 50)

    assert result[0]["data"] == "A" * 100  # short document untouched
    assert _shown_chars(result[1]) == 1900  # inherits what the first left over
    assert sum(_shown_chars(att) for att in result) == 2000


def test_documents_below_the_budget_are_untouched():
    attachments = [_doc("a.pdf", 100), _doc("b.pdf", 200)]

    result = _apply_document_budget(attachments, 4000, 50)

    assert result == attachments


def test_exhausted_context_leaves_attachments_untouched():
    """A full window is a broken request either way; do not hide it."""
    attachments = [_doc("a.pdf", 10_000)]

    assert _apply_document_budget(attachments, 0, 50) == attachments


def test_images_and_order_are_preserved():
    image = {"type": "image", "data": "b64", "mime": "image/jpeg", "name": "shot.jpg"}
    attachments = [image, _doc("a.pdf", 10_000), image]

    result = _apply_document_budget(attachments, 4000, 50)

    assert result[0] == image
    assert result[2] == image
    assert _shown_chars(result[1]) == 2000


def test_no_documents_returns_a_copy():
    image = {"type": "image", "data": "b64", "mime": "image/jpeg", "name": "shot.jpg"}
    attachments = [image]

    result = _apply_document_budget(attachments, 4000, 50)

    assert result == attachments
    assert result is not attachments
