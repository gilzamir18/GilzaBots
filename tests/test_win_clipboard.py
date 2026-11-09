"""The Win32 clipboard behind ``/api/clipboard/*`` on Windows (no Qt there)."""

import base64
import io
import sys

import pytest
from PIL import Image

from gilzabots import ide_server, win_clipboard


def test_line_ends_are_converted_both_ways_like_qt_did():
    assert win_clipboard.to_clipboard_text("a\nb\r\nc") == "a\r\nb\r\nc"
    assert win_clipboard.from_clipboard_text("a\r\nb\r\n") == "a\nb\n"
    assert win_clipboard.from_clipboard_text(win_clipboard.to_clipboard_text("x\ny")) == "x\ny"


def test_an_image_on_the_clipboard_comes_back_as_png():
    png = win_clipboard.read_image_png_b64(grab=lambda: Image.new("RGB", (3, 2), "red"))
    image = Image.open(io.BytesIO(base64.b64decode(png)))
    assert (image.format, image.size) == ("PNG", (3, 2))


@pytest.mark.parametrize("content", [None, ["C:\\Users\\u\\shot.png"]])
def test_no_image_and_copied_files_are_no_image(content):
    assert win_clipboard.read_image_png_b64(grab=lambda: content) == ""


def test_a_locked_clipboard_is_no_image():
    def grab():
        raise OSError("clipboard busy")

    assert win_clipboard.read_image_png_b64(grab=grab) == ""


def test_the_endpoints_use_win32_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    written = []
    monkeypatch.setattr(win_clipboard, "read_text", lambda: "from win32")
    monkeypatch.setattr(win_clipboard, "write_text", written.append)
    monkeypatch.setattr(win_clipboard, "read_image_png_b64", lambda: "cG5n")
    monkeypatch.setattr(ide_server, "_qt_invoke", lambda fn: pytest.fail("Qt must not be used on Windows"))

    assert ide_server._read_clipboard() == "from win32"
    assert ide_server._write_clipboard("hello") == (True, "")
    assert written == ["hello"]
    assert ide_server._read_clipboard_image() == "cG5n"


def test_a_write_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def fail(_text):
        raise win_clipboard.ClipboardError("the clipboard is held by another program")

    monkeypatch.setattr(win_clipboard, "write_text", fail)
    assert ide_server._write_clipboard("x") == (False, "the clipboard is held by another program")


@pytest.mark.skipif(sys.platform != "win32", reason="the Win32 clipboard exists only on Windows")
def test_text_round_trips_through_the_real_clipboard():
    """Writes the real clipboard; whatever text it held is put back afterwards."""
    saved = win_clipboard.read_text()
    try:
        win_clipboard.write_text("linha 1\nação ✓")
        assert win_clipboard.read_text() == "linha 1\nação ✓"
    finally:
        win_clipboard.write_text(saved)
