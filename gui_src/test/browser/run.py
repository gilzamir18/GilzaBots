#!/usr/bin/env python3
"""Browser-level checks for the terminal, the PDF viewer and the chat.

    npm run test:browser              both interface scales
    npm run test:browser -- 1         one scale
    npm run test:browser -- --headful watch it run

Why this exists, when there are already unit tests
--------------------------------------------------
The unit suite covers everything that can be made pure. It cannot see whether
a click reaches an element, whether a panel is covered, whether focus survives
the press that opened an editor, or whether a presented page fits the screen.
Those defects live in the layout and event semantics of a real browser, and most
of them only show under the app's CSS zoom (the interface scale).

So the checks below drive a real browser with *real* input: mouse events are
dispatched through the DevTools Protocol, which delivers them the way the
operating system does. That distinction is not academic — synthetic
`dispatchEvent` can pass against a broken double click, because the test author
sets fields (`detail`) that real Chrome reports differently. A test that invents
its own inputs proves nothing.

Requires Google Chrome (or Chromium) and the `websockets` package, both of which
the project already depends on for other reasons.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import websockets
except ImportError:  # pragma: no cover - environment guard
    sys.exit("browser tests need the 'websockets' package: pip install websockets")

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_SRC = os.path.dirname(os.path.dirname(HERE))
HARNESS_PATH = "/test/browser/harness.html"
CHAT_PATH = "/test/browser/chat.html"
PDF_PATH = "/test/browser/pdf.html"
EDITOR_PATH = "/test/browser/editor.html"

# Wide enough that the IDE surfaces under test lay out as they do on a desktop.
WINDOW = (1440, 900)

CHROME_CANDIDATES = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if found:
            return found
    sys.exit(
        "no Chrome or Chromium found. These checks drive a real browser; install one, "
        "or run only the pure suites with `npm run test:wysiwyg`."
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {url}")


class Checks:
    """Collects results so one failure does not hide the rest."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def report(self, label: str) -> None:
        passed = len(self.rows) - self.failed
        print(f"--- {label}: {passed}/{len(self.rows)} pass ---")
        for name, ok, detail in self.rows:
            print(("  PASS " if ok else "  FAIL "), name.ljust(34), detail)


class Page:
    """A very small DevTools Protocol client: evaluate, and dispatch input."""

    def __init__(self, ws, url: str) -> None:
        self._ws = ws
        self._id = 0
        self.url = url          # so a step can reload a pristine fixture

    async def reload(self) -> None:
        """Back to the fixture as authored."""
        await self.cmd("Page.navigate", {"url": self.url})
        deadline = time.time() + 30
        while time.time() < deadline:
            if await self.js("!!window.__ready && !!document.querySelector('#terminal-fixture')"):
                await asyncio.sleep(0.4)
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("harness did not reload")

    async def cmd(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def js(self, expression: str):
        result = await self.cmd("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(f"page error: {result['exceptionDetails'].get('text')}")
        return result.get("result", {}).get("value")

    # ── real input ───────────────────────────────────────────────────────────
    async def _mouse(self, kind: str, x: float, y: float, clicks: int) -> None:
        await self.cmd("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": "left",
            "buttons": 0 if kind == "mouseReleased" else 1, "clickCount": clicks,
        })

    async def press(self, x, y, clicks=1): await self._mouse("mousePressed", x, y, clicks)
    async def release(self, x, y, clicks=1): await self._mouse("mouseReleased", x, y, clicks)
    async def move(self, x, y): await self._mouse("mouseMoved", x, y, 0)

    async def right_click(self, x: float, y: float) -> None:
        """A real secondary click. Which of press/release carries `contextmenu`
        is a platform detail, so both are dispatched and the page decides."""
        for kind, buttons in (("mousePressed", 2), ("mouseReleased", 0)):
            await self.cmd("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "right",
                "buttons": buttons, "clickCount": 1,
            })

    async def chord(self, key: str, code: str, key_code: int) -> None:
        """Ctrl + a letter, as the operating system delivers it."""
        for kind in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code,
                "modifiers": 2,     # Ctrl
            })

    async def key(self, key: str, key_code: int, modifiers: int = 0,
                  text: str | None = None, code: str | None = None) -> None:
        """A key, as the operating system delivers it.

        `text` is what the key would type, and Enter is the reason it exists:
        without it Chrome delivers the keystroke but inserts nothing, so a test
        for what Enter does to a line would pass against an editor that does
        nothing at all.
        """
        for kind in ("keyDown", "keyUp"):
            event = {
                "type": kind, "key": key, "code": code or key,
                "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code,
                "modifiers": modifiers,
            }
            if text and kind == "keyDown":
                event["text"] = text
            await self.cmd("Input.dispatchKeyEvent", event)

    async def click(self, x, y, clicks=1) -> None:
        await self.press(x, y, clicks)
        await self.release(x, y, clicks)

    async def double_click(self, x, y) -> None:
        """The sequence a real double click produces, at real double-click speed."""
        await self.click(x, y, 1)
        await asyncio.sleep(0.06)
        await self.click(x, y, 2)

    async def type_text(self, text: str) -> None:
        await self.cmd("Input.insertText", {"text": text})

    async def rect(self, selector: str, index: int = 0):
        return await self.js(
            "(() => {"
            f"  const el = document.querySelectorAll({json.dumps(selector)})[{index}];"
            "   if (!el) return null;"
            "   const r = el.getBoundingClientRect();"
            "   return {x: r.left, y: r.top, w: r.width, h: r.height,"
            "           cx: r.left + r.width / 2, cy: r.top + r.height / 2,"
            "           right: r.right, bottom: r.bottom};"
            "})()"
        )


# ─── the checks ──────────────────────────────────────────────────────────────
# Each group states what only a browser can answer. Gestures are separated by
# more than the double-click window (450ms) wherever two clicks would otherwise
# be paired into one the test never meant to perform.

SETTLE = 0.25
UNPAIR = 0.6


async def open_pdf_harness(page: Page, pages: int = 5) -> bool:
    """Navigate to the PDF harness at the suite's scale and wait for its pages."""
    base = page.url.split(HARNESS_PATH)[0]
    scale = page.url.split("uiScale=")[-1]
    await page.cmd("Page.navigate", {"url": f"{base}{PDF_PATH}?uiScale={scale}"})
    return await wait_for_pdf_pages(page, pages)


async def wait_for_pdf_pages(page: Page, count: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await page.js("!!window.__ready && "
                         f"document.querySelectorAll('.pdf-page-wrapper canvas').length === {count}"):
            return True
        await asyncio.sleep(0.2)
    return False


PDF_VIEW_POSITION = (
    "(() => { const c = document.querySelector('.pdf-preview-scroll'); if (!c) return null;"
    "  const zoom = [...document.querySelectorAll('.pdf-preview-controls span')]"
    "    .find(s => /^\\d+%$/.test(s.textContent.trim()));"
    "  const nav = document.querySelector('.pdf-preview-controls input[placeholder]');"
    "  return {top: c.scrollTop, max: c.scrollHeight - c.clientHeight,"
    "          zoom: zoom && zoom.textContent.trim(), page: nav && nav.getAttribute('placeholder')}; })()"
)


async def check_pdf_tab_switch(page: Page, check: Checks) -> None:
    """Leaving a PDF for another tab and coming back returns to where it was left.

    A tab of another kind replaces the viewer, which unmounts it, so its scroll
    position and zoom used to be lost and every return opened on page one. Only
    a browser can answer whether the position really survives: a remounted
    viewer lays its pages out asynchronously, and a position applied before that
    finishes lands on whatever page happens to be there.
    """
    if not check("pdf viewer renders the tab fixture", await open_pdf_harness(page)):
        return
    await asyncio.sleep(0.5)

    zoom_in = await page.js(
        "(() => { const i = document.querySelector('.pdf-preview-controls .lucide-zoom-in');"
        "  const b = i && i.closest('button'); if (!b) return null; const r = b.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
    )
    if not check("pdf zoom in button found", zoom_in is not None):
        return
    await page.click(zoom_in["x"], zoom_in["y"])
    await asyncio.sleep(0.8)

    # Part-way into a page rather than at its top, so a restore that only found
    # the right page would still be caught.
    async def park(page_number: int, fraction: float) -> float:
        return await page.js(
            "(() => { const c = document.querySelector('.pdf-preview-scroll');"
            f"  const p = c.querySelector('[data-page-number=\"{page_number}\"]');"
            f"  c.scrollTop = Math.round(p.offsetTop + p.offsetHeight * {fraction}); return c.scrollTop; }})()"
        )

    async def settle_at(predicate, timeout: float = 8.0):
        deadline = time.time() + timeout
        state = None
        while time.time() < deadline:
            state = await page.js(PDF_VIEW_POSITION)
            if state and predicate(state):
                return state, True
            await asyncio.sleep(0.15)
        return state, False

    target = await park(3, 0.5)
    await asyncio.sleep(0.6)
    before = await page.js(PDF_VIEW_POSITION)
    check("the fixture scrolls far enough to park mid-page",
          before["top"] < before["max"] and abs(before["top"] - target) <= 1,
          f"top={before['top']} max={before['max']}")

    await page.js("window.__openTab('source')")
    await asyncio.sleep(0.4)
    check("a tab of another kind unmounts the viewer",
          await page.js("!document.querySelector('.pdf-preview-scroll') && !!document.getElementById('not-a-pdf')"))

    await page.js("window.__openTab('main')")
    await wait_for_pdf_pages(page, 5)
    back, ok = await settle_at(lambda s: abs(s["top"] - before["top"]) <= 2)
    check("returning to the pdf restores its scroll position", ok,
          f"before={before['top']} after={back and back['top']}")
    check("and its zoom", back is not None and back["zoom"] == before["zoom"],
          f"before={before['zoom']} after={back and back['zoom']}")
    check("and the page in view", back is not None and back["page"] == before["page"],
          f"before={before['page']} after={back and back['page']}")

    await page.js("window.__openTab('other')")
    await wait_for_pdf_pages(page, 2)
    await asyncio.sleep(0.6)
    other = await page.js(PDF_VIEW_POSITION)
    check("another pdf opens at its own position, not the last one's",
          other is not None and other["top"] < 40 and other["zoom"] == "120%",
          f"top={other and other['top']} zoom={other and other['zoom']}")
    other_target = await park(2, 0.3)
    await asyncio.sleep(0.6)

    await page.js("window.__openTab('main')")
    await wait_for_pdf_pages(page, 5)
    _, ok = await settle_at(lambda s: abs(s["top"] - before["top"]) <= 2)
    check("each pdf keeps a position of its own", ok)
    await page.js("window.__openTab('other')")
    await wait_for_pdf_pages(page, 2)
    _, ok = await settle_at(lambda s: abs(s["top"] - other_target) <= 2)
    check("including the second one", ok)

    # Recompiled while the user was elsewhere, down to two pages: the remembered
    # page 3 no longer exists.
    await page.js("window.__recompile(2)")
    await asyncio.sleep(0.3)
    await page.js("window.__openTab('main')")
    await wait_for_pdf_pages(page, 2)
    page_two = await page.js(
        "(() => { const p = document.querySelector('[data-page-number=\"2\"]'); return p && p.offsetTop; })()"
    )
    shrunk, ok = await settle_at(lambda s: page_two is not None and abs(s["top"] - page_two) <= 2)
    check("a pdf that lost the remembered page opens at its new last page", ok,
          f"top={shrunk and shrunk['top']} page 2 at={page_two}")


PDF_TEXT_SPAN = (
    "(() => { const s = [...document.querySelectorAll('.textLayer span')]"
    "    .find(e => e.textContent.includes('Amazon'));"
    "  if (!s) return null; const r = s.getBoundingClientRect();"
    "  return {left: r.left, right: r.right, cy: r.top + r.height / 2, cx: r.left + r.width / 2}; })()"
)


def element_with_text(selector: str, text: str) -> str:
    """JS returning the centre of the first `selector` whose text contains `text`."""
    return (
        f"(() => {{ const el = [...document.querySelectorAll({json.dumps(selector)})]"
        f"    .find(e => e.textContent.includes({json.dumps(text)}));"
        "  if (!el) return null; const r = el.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2,"
        "          disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true')}; })()"
    )


async def wait_js(page: Page, expression: str, timeout: float = 8.0):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = await page.js(expression)
        if value:
            return value
        await asyncio.sleep(0.15)
    return value


async def select_pdf_excerpt(page: Page) -> dict | None:
    """Drag across the fixture's line of text, as a mouse would."""
    span = await wait_js(page, PDF_TEXT_SPAN)
    if not span:
        return None
    await page.press(span["left"] + 1, span["cy"])
    await page.move(span["cx"], span["cy"])
    await page.move(span["right"] - 1, span["cy"])
    await page.release(span["right"] - 1, span["cy"])
    await asyncio.sleep(SETTLE)
    return span


async def check_pdf_lookup(page: Page, check: Checks) -> None:
    """Look up an excerpt: a question and its answer in a card, never in the chat.

    What only a browser can answer: that the selection survives the right-click
    and the menu press (it collapses the moment a button takes focus), that the
    question box takes the keyboard, that Ctrl+Enter sends, that a click on the
    document does not throw the answer away, and that "Save as PDF note"
    writes a highlight carrying the answer on exactly the passage selected.
    """
    if not check("pdf viewer renders for the look-up", await open_pdf_harness(page)):
        return
    await page.js("window.__openTab('text'); window.__requests = []")
    if not check("the text fixture renders", await wait_for_pdf_pages(page, 1)):
        return

    span = await select_pdf_excerpt(page)
    selected = await page.js("String(window.getSelection())")
    if not check("a mouse drag selects the excerpt", span is not None and "Amazon" in (selected or ""),
                 f"selection={selected!r}"):
        return

    await page.right_click(span["cx"], span["cy"])
    item = await wait_js(page, element_with_text(".vscode-context-menu-item", "Look up the selection"))
    menu_items = await page.js("[...document.querySelectorAll('.vscode-context-menu-item')].map(e => e.textContent)")
    check("the menu offers Look up for a selection", item is not None and not item["disabled"],
          f"items={menu_items}")
    if not item:
        return
    await page.click(item["x"], item["y"])

    card = await wait_js(page, "!!document.querySelector('.snippet-query-popup')")
    if not check("Look up opens the card", card):
        return
    excerpt = await page.js("document.querySelector('.snippet-query-popup .snippet-translate-popup-source').textContent")
    check("the card quotes the excerpt captured at right-click", "Amazon" in (excerpt or ""), repr(excerpt))
    check("the question box has the keyboard",
          await page.js("document.activeElement && document.activeElement.id === 'snippet-query-question'"))
    send = await page.js(element_with_text(".snippet-query-footer button", "Send"))
    check("Send is disabled until a question is written", send is not None and send["disabled"])

    await page.type_text("What kind of forest is it?")
    await page.key("Enter", 13, modifiers=2)        # Ctrl+Enter
    answered = await wait_js(page, "(() => { const s = document.querySelector('.snippet-query-answer strong');"
                                   " return s && s.textContent; })()")
    check("Ctrl+Enter sends and the answer renders as Markdown", answered == "tropical", repr(answered))

    sent = await page.js("window.__requests.filter(r => r.path === '/api/snippet-query')")
    body = (sent or [{}])[0].get("body") or {}
    source = body.get("source") or {}
    check("one look-up request was sent", len(sent or []) == 1, f"{len(sent or [])} requests")
    check("it carries the excerpt and the question",
          "Amazon" in body.get("excerpt", "") and body.get("question") == "What kind of forest is it?")
    check("it names the document, the page and the project",
          source.get("surface") == "pdf" and source.get("documentPath") == "doc.pdf"
          and source.get("page") == 1 and source.get("totalPages") == 1
          and body.get("projectPath") == "/harness-project" and body.get("model") == "harness-model",
          json.dumps({k: body.get(k) for k in ("source", "projectPath", "model")}))

    # A click on the document is not a dismissal: that would lose the answer.
    scroller = await page.rect(".pdf-preview-scroll")
    await page.click(scroller["x"] + 12, scroller["bottom"] - 12)
    await asyncio.sleep(SETTLE)
    check("a click on the document keeps the card open",
          await page.js("!!document.querySelector('.snippet-query-popup')"))

    save_note = await page.js(element_with_text(".snippet-query-footer button", "Save as PDF note"))
    check("a standalone PDF offers to save the answer as a note",
          save_note is not None and not save_note["disabled"])
    if save_note:
        await page.click(save_note["x"], save_note["y"])
        posted = await wait_js(page, "window.__requests.filter(r => r.path === '/api/pdf/annotations')"
                                     ".map(r => r.body)[0] || null")
        posted = posted or {}
        check("the note is a highlight on the selected passage",
              posted.get("kind") == "highlight" and posted.get("page") == 1 and len(posted.get("rects") or []) > 0,
              json.dumps({k: posted.get(k) for k in ("kind", "page")}))
        check("carrying the question and the answer",
              str(posted.get("content", "")).startswith("Question: What kind of forest is it?")
              and "tropical" in str(posted.get("content", "")))
        saved = await wait_js(page, element_with_text(".snippet-query-footer button", "Saved as PDF note"))
        check("and the button says it is saved", saved is not None and saved["disabled"])

    # Save as Markdown asks for a path in the app's own dialog. Escape there
    # belongs to the dialog: it must cancel the save, not close the card.
    save_md = await page.js(element_with_text(".snippet-query-footer button", "Save as Markdown"))
    if check("the card offers to save the answer as Markdown", save_md is not None and not save_md["disabled"]):
        dialog_input = "document.querySelector('.vscode-modal-content input[type=text]')"
        await page.click(save_md["x"], save_md["y"])
        suggested = await wait_js(page, f"(() => {{ const i = {dialog_input}; return i && i.value; }})()")
        check("the dialog suggests a dated file under lookups/",
              bool(re.fullmatch(r"lookups/\d{4}-\d{2}-\d{2}-\d{4}-what-kind-of-forest-is-it\.md", suggested or "")),
              repr(suggested))
        await page.key("Escape", 27)
        await asyncio.sleep(SETTLE)
        check("Escape in the dialog cancels the save and keeps the card",
              await page.js(f"!{dialog_input} && !!document.querySelector('.snippet-query-popup')"))
        check("and writes nothing",
              await page.js("window.__requests.filter(r => r.path === '/api/file/write').length") == 0)

        await page.click(save_md["x"], save_md["y"])
        # The dialog focuses its input on a timer; typing before that would go
        # to the button that opened it, which no person manages to do.
        await wait_js(page, f"(() => {{ const i = {dialog_input}; return !!i && document.activeElement === i; }})()")
        await page.key("Enter", 13, text="\r")
        written = await wait_js(page, "window.__requests.filter(r => r.path === '/api/file/write').map(r => r.body)[0] || null")
        written = written or {}
        check("Enter writes the file without overwriting anything",
              written.get("exclusive") is True and written.get("filePath") == suggested
              and written.get("projectPath") == "/harness-project",
              json.dumps({k: written.get(k) for k in ("filePath", "exclusive")}))
        check("the file holds the excerpt, the question and the answer",
              "> The Amazon" in written.get("content", "") and "## Answer" in written.get("content", "")
              and "tropical" in written.get("content", ""))
        saved_line = await wait_js(page, "(() => { const s = document.querySelector('.snippet-query-saved');"
                                         " return s && s.textContent; })()")
        check("the card says where it was saved", suggested and suggested in (saved_line or ""), repr(saved_line))

    await page.key("Escape", 27)
    await asyncio.sleep(SETTLE)
    check("Escape closes the card", await page.js("!document.querySelector('.snippet-query-popup')"))


async def check_pdf_translation_popup(page: Page, check: Checks) -> None:
    """The translation popup, after its drag and clamp moved to a shared hook.

    It must still open at the click, follow a drag of its header, and close on a
    click elsewhere, which the look-up card deliberately does not do.
    """
    if not check("pdf viewer renders for translation", await open_pdf_harness(page)):
        return
    await page.js("window.__openTab('text'); window.__requests = []")
    if not await wait_for_pdf_pages(page, 1):
        return
    span = await select_pdf_excerpt(page)
    if not span:
        check("the text fixture has a selectable line", False)
        return
    await page.right_click(span["cx"], span["cy"])
    item = await wait_js(page, element_with_text(".vscode-context-menu-item", "Translate selection"))
    if not check("the menu offers Translate", item is not None):
        return
    await page.click(item["x"], item["y"])
    result = await wait_js(page, "(() => { const r = document.querySelector('.snippet-translate-popup-result');"
                                 " return r && r.textContent; })()")
    check("the translation renders", result == "A Amazônia é a maior floresta.", repr(result))

    header = await page.rect(".snippet-translate-popup-header")
    start = await page.rect(".snippet-translate-popup")
    grab_x, grab_y = header["x"] + 20, header["cy"]
    await page.press(grab_x, grab_y)
    await page.move(grab_x - 60, grab_y + 40)
    await page.move(grab_x - 120, grab_y + 80)
    await page.release(grab_x - 120, grab_y + 80)
    await asyncio.sleep(SETTLE)
    moved = await page.rect(".snippet-translate-popup")
    check("dragging the header moves the popup",
          abs((start["x"] - moved["x"]) - 120) <= 3 and abs((moved["y"] - start["y"]) - 80) <= 3,
          f"dx={start['x'] - moved['x']:.1f} dy={moved['y'] - start['y']:.1f}")

    await page.press(grab_x - 120, grab_y + 80)
    await page.move(-400, -400)
    await page.release(-400, -400)
    await asyncio.sleep(SETTLE)
    clamped = await page.rect(".snippet-translate-popup")
    check("and a drag off-screen keeps the header reachable",
          clamped["x"] >= 0 and clamped["y"] >= 0, f"x={clamped['x']:.1f} y={clamped['y']:.1f}")

    scroller = await page.rect(".pdf-preview-scroll")
    await page.click(scroller["right"] - 12, scroller["bottom"] - 12)
    await asyncio.sleep(SETTLE)
    check("a click elsewhere closes the translation",
          await page.js("!document.querySelector('.snippet-translate-popup')"))


async def check_pdf_presentation(page: Page, check: Checks) -> None:
    """A PDF presented page by page must fill the screen, sharply, on the right page.

    The overlay crosses the app's zoom boundary, with one more way to get it
    wrong: the page is a canvas, so a backing store sized for the
    unzoomed page is stretched and blurred rather than clipped. Which page is on
    screen is read from the canvas pixels — every fixture page is its own flat
    colour — because the counter could advance over a canvas that never did.
    """
    if not check("pdf viewer renders the fixture", await open_pdf_harness(page)):
        return
    await asyncio.sleep(0.5)

    async def key(name: str, code: int) -> None:
        for kind in ("keyDown", "keyUp"):
            await page.cmd("Input.dispatchKeyEvent", {
                "type": kind, "key": name, "code": name, "windowsVirtualKeyCode": code,
            })

    async def shown(expected_hud: str, timeout: float = 8.0):
        """The visible page once it has actually painted, or None.

        Painted is react-pdf's own signal: the canvas stays `visibility: hidden`
        until its render task resolves. The pixels are no signal on their own —
        the canvas is opaque black before anything is drawn on it.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = await page.js(
                "(() => { const hud = document.querySelector('.pdf-present-hud');"
                "  const p = [...document.querySelectorAll('.pdf-present-page')]"
                "    .find(e => e.style.visibility === 'visible');"
                "  const c = p && p.querySelector('canvas');"
                "  if (!hud || !c || !c.width || c.style.visibility === 'hidden') return null;"
                "  const px = c.getContext('2d').getImageData(c.width >> 1, c.height >> 1, 1, 1).data;"
                "  const r = c.getBoundingClientRect();"
                "  return {hud: hud.textContent, rgba: [...px], l: r.left, t: r.top, r: r.right, b: r.bottom,"
                "          w: r.width, h: r.height, bw: c.width, dpr: devicePixelRatio,"
                "          vw: innerWidth, vh: innerHeight,"
                "          hudBox: hud.getBoundingClientRect().toJSON()}; })()"
            )
            if state and state["hud"] == expected_hud:
                return state
            await asyncio.sleep(0.15)
        return None

    def is_page(state, number: int) -> bool:
        rgb = [round(v * 255) for v in pages[number - 1]["rgb"]]
        return all(abs(a - b) <= 3 for a, b in zip(state["rgba"][:3], rgb))

    pages = await page.js("window.__pages")

    button = await page.js(
        "(() => { const b = [...document.querySelectorAll('.pdf-preview-controls button')]"
        "   .find(x => /Present|Apresentar/.test(x.getAttribute('aria-label') || ''));"
        "  if (!b || b.disabled) return null; const r = b.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
    )
    if not check("pdf present button found", button is not None):
        return
    # The page in view is the one at the centre of the viewer, as its toolbar
    # reports it — which depends on the window height, so it is read, not assumed.
    in_view = await page.js(
        "document.querySelector('.pdf-preview-controls input[placeholder]')?.getAttribute('placeholder')"
    )
    await page.click(button["x"], button["y"])

    opened = await shown(f"{in_view} / 5")
    if not check("pdf presentation opens on the page in view", opened is not None,
                 f"page in view={in_view}"):
        return
    await key("Home", 36)
    first = await shown("1 / 5")
    if not check("home goes to the first page", first is not None):
        return
    vw, vh = first["vw"], first["vh"]
    overlay = await page.rect(".pdf-present")
    check("pdf overlay covers the viewport exactly",
          abs(overlay["w"] - vw) < 2 and abs(overlay["h"] - vh) < 2,
          f"overlay={overlay['w']:.0f}x{overlay['h']:.0f} viewport={vw}x{vh}")
    check("the first page is the one drawn", is_page(first, 1), f"rgba={first['rgba']}")

    def fits(state, number: int, label: str) -> None:
        spec = pages[number - 1]
        check(f"{label} fits inside the viewport",
              state["l"] >= -1 and state["t"] >= -1 and state["r"] <= vw + 1 and state["b"] <= vh + 1,
              f"l={state['l']:.0f} t={state['t']:.0f} r={state['r']:.0f} b={state['b']:.0f}")
        check(f"{label} keeps its aspect ratio",
              abs(state["w"] / state["h"] - spec["w"] / spec["h"]) < 0.01,
              f"ratio={state['w'] / state['h']:.4f} expected={spec['w'] / spec['h']:.4f}")
        # Canvas CSS sizes are floored in app pixels, so up to one app pixel —
        # uiScale viewport pixels — can be lost to rounding.
        check(f"{label} is as large as it can be",
              abs(state["w"] - vw) < 3 or abs(state["h"] - vh) < 3,
              f"page={state['w']:.0f}x{state['h']:.0f} viewport={vw}x{vh}")
        check(f"{label} is rendered at screen resolution",
              abs(state["bw"] - state["w"] * state["dpr"]) <= 3,
              f"backing={state['bw']} displayed={state['w'] * state['dpr']:.0f}")

    fits(first, 1, "a 4:3 page")
    hud = first["hudBox"]
    check("pdf page counter stays on screen", hud["right"] <= vw + 1 and hud["bottom"] <= vh + 1,
          f"hud right={hud['right']:.0f} bottom={hud['bottom']:.0f}")

    await key("ArrowRight", 39)
    portrait = await shown("2 / 5")
    if check("arrow right advances to a page that is drawn", portrait is not None and is_page(portrait, 2)):
        fits(portrait, 2, "a portrait page")
    # The neighbours start rendering when this page becomes current, so they are
    # given a moment — but they must finish while it is still on screen, which is
    # what makes the next advance instant.
    neighbours, deadline = 0, time.time() + 3
    while time.time() < deadline:
        neighbours = await page.js(
            "[...document.querySelectorAll('.pdf-present-page canvas')]"
            "  .filter(c => c.width > 0 && c.style.visibility !== 'hidden').length"
        )
        if neighbours == 3:
            break
        await asyncio.sleep(0.1)
    check("the pages either side are rendered in the background", neighbours == 3,
          f"painted canvases={neighbours}")

    await key("ArrowRight", 39)
    wide = await shown("3 / 5")
    if check("a wide page is reached", wide is not None and is_page(wide, 3)):
        fits(wide, 3, "a wide page")

    await key("End", 35)
    last = await shown("5 / 5")
    check("end jumps to the last page", last is not None and is_page(last, 5))
    await key("ArrowRight", 39)
    await asyncio.sleep(0.3)
    check("advancing past the last page stays there", (await shown("5 / 5")) is not None)
    await key("Home", 36)
    check("home returns from the last page", (await shown("1 / 5")) is not None)

    await page.click(vw * 0.8, vh / 2)
    check("a click on the right advances", (await shown("2 / 5")) is not None)
    await asyncio.sleep(UNPAIR)
    await page.click(vw * 0.1, vh / 2)
    check("a click on the left goes back", (await shown("1 / 5")) is not None)
    for _ in range(2):
        await asyncio.sleep(UNPAIR)
        await page.click(vw * 0.8, vh / 2)
    check("clicks reach the third page", (await shown("3 / 5")) is not None)

    await key("Escape", 27)
    await asyncio.sleep(1.2)
    check("escape leaves the pdf presentation",
          await page.js("!document.querySelector('.pdf-present')"))
    in_view = await page.js(
        "document.querySelector('.pdf-preview-controls input[placeholder]')?.getAttribute('placeholder')"
    )
    check("the viewer is left on the last page presented", in_view == "3", f"page in view={in_view}")

    await page.click(button["x"], button["y"])
    reopened = await shown("3 / 5")
    check("presenting again starts where the viewer is", reopened is not None and is_page(reopened, 3))

    await key("End", 35)
    await shown("5 / 5")
    await page.js("window.__recompile(3)")
    rebuilt = await shown("3 / 3")
    check("a recompile that drops pages keeps presenting its new last page",
          rebuilt is not None and is_page(rebuilt, 3))
    await key("Escape", 27)
    await asyncio.sleep(0.4)



async def check_terminal_selection(page: Page, check: Checks) -> None:
    """xterm selection must use the cells actually under the pointer.

    xterm measures cell dimensions without ancestor CSS zoom but normally reads
    pointer offsets after that zoom. This real drag catches the resulting drift:
    at 140%, aiming at row six selects row eight without the coordinate-space
    boundary used by TerminalInstance.
    """
    await page.js("document.querySelector('#terminal-fixture').style.display = 'block'")
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            ready = await page.js(
                "!!window.__terminal && window.__terminal.rows >= 10 && "
                "document.querySelector('.xterm-screen')?.getBoundingClientRect().height > 0"
            )
            if ready:
                break
            await asyncio.sleep(0.1)
        if not check("terminal mounted", bool(ready)):
            return

        await page.js(
            "new Promise(resolve => {"
            " const term = window.__terminal;"
            " const lines = Array.from({length: term.rows}, (_, i) => "
            "   String(i + 1).padStart(2, '0') + ' terminal row');"
            " term.write('\\x1b[2J\\x1b[H' + lines.join('\\r\\n'), resolve);"
            "})"
        )
        await asyncio.sleep(0.15)

        screen = await page.rect("#terminal-fixture .xterm-screen")
        geometry = await page.js(
            "(() => {"
            " const screen = document.querySelector('#terminal-fixture .xterm-screen');"
            " const rect = screen.getBoundingClientRect();"
            " return { scaleX: rect.width / screen.offsetWidth,"
            "          scaleY: rect.height / screen.offsetHeight,"
            "          cols: window.__terminal.cols, rows: window.__terminal.rows };"
            "})()"
        )
        check("terminal uses a 1:1 mouse coordinate space",
              abs(geometry["scaleX"] - 1) < 0.01 and abs(geometry["scaleY"] - 1) < 0.01,
              f"scale=({geometry['scaleX']:.3f}, {geometry['scaleY']:.3f})")

        cell_width = screen["w"] / geometry["cols"]
        cell_height = screen["h"] / geometry["rows"]
        row = 5
        y = screen["y"] + (row + 0.5) * cell_height
        await page.press(screen["x"] + 0.1 * cell_width, y)
        await page.move(screen["x"] + 2.1 * cell_width, y)
        await page.release(screen["x"] + 2.1 * cell_width, y)
        selection = await page.js(
            "({text: window.__terminal.getSelection(), "
            "  position: window.__terminal.getSelectionPosition()})"
        )
        check("terminal selects the row under the pointer",
              selection["text"] == "06" and selection["position"]["start"]["y"] == row,
              f"selection={selection}")
    finally:
        await page.js("document.querySelector('#terminal-fixture').style.display = 'none'")


MESSAGE_EDITOR_JS = (
    "[...document.querySelectorAll('textarea.vscode-chat-textarea')].find(t => !t.closest('form'))"
)


async def check_chat_message_edit(page: Page, check: Checks) -> None:
    """The pencil's editor keeps an edit until it is accepted.

    An edit that the app could not apply used to close the editor anyway and
    drop the text without a word; the natural next move — sending the text from
    the composer — appends it after the very turns the edit was meant to
    replace, and the agent answers with them in view. What only a browser can
    answer is what the keys actually do in that editor: Enter in a textarea
    inserts a line unless something stops it, and focus decides which textarea
    receives the key at all.
    """
    base = page.url.split(HARNESS_PATH)[0]
    await page.cmd("Page.navigate", {"url": f"{base}{CHAT_PATH}"})
    pencils = "[...document.querySelectorAll('button')].filter(b => b.querySelector('svg.lucide-pencil'))"
    deadline = time.time() + 30
    while time.time() < deadline:
        if await page.js(f"!!window.__ready && {pencils}.length === 2"):
            break
        await asyncio.sleep(0.2)
    else:
        check("chat harness loads", False, "the pencils never rendered")
        return
    await asyncio.sleep(0.3)

    async def open_editor(index: int) -> bool:
        button = await page.js(
            f"(() => {{ const b = {pencils}[{index}]; if (!b) return null;"
            "  const r = b.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
        )
        if not button:
            return False
        await page.click(button["x"], button["y"])
        await asyncio.sleep(SETTLE)
        return bool(await page.js(f"!!({MESSAGE_EDITOR_JS})"))

    # An earlier message, not the last one: the case that branches the chat.
    if not check("the pencil opens an editor", await open_editor(0)):
        return
    check("the editor has the focus, not the composer",
          await page.js(f"document.activeElement === ({MESSAGE_EDITOR_JS})"))

    await page.chord("a", "KeyA", 65)
    await page.type_text("Edited question")
    await page.key("Enter", 13, modifiers=8, text="\r")          # Shift+Enter
    await page.type_text("second line")
    await asyncio.sleep(SETTLE)
    draft = await page.js(f"({MESSAGE_EDITOR_JS}).value")
    check("Shift+Enter starts a new line", draft == "Edited question\nsecond line", f"draft={draft!r}")
    check("and does not save", await page.js("window.__editCalls.length") == 0)

    await page.key("Enter", 13, text="\r")
    await asyncio.sleep(0.4)
    calls = await page.js("window.__editCalls")
    check("Enter saves the edit", len(calls) == 1 and calls[0]["content"] == "Edited question\nsecond line"
          and calls[0]["id"] == 1, f"calls={calls}")
    check("a refused edit keeps the editor open", await page.js(f"!!({MESSAGE_EDITOR_JS})"))
    check("with the edited text in it",
          await page.js(f"({MESSAGE_EDITOR_JS})?.value") == "Edited question\nsecond line")
    alert = await page.js("document.querySelector('.chat-edit-error')?.textContent || ''")
    check("and says why", "agent is still working" in alert, f"alert={alert!r}")
    check("nothing went to the composer",
          await page.js("window.__composerText() === '' && window.__sentFromComposer.length === 0"))

    await page.js("window.__editMode = 'accept'")
    await page.js(f"({MESSAGE_EDITOR_JS}).focus()")
    await page.key("Enter", 13, text="\r")
    await asyncio.sleep(0.4)
    check("an accepted edit closes the editor",
          await page.js(f"!({MESSAGE_EDITOR_JS}) && window.__editCalls.length === 2"))

    if not check("the pencil reopens an editor", await open_editor(1)):
        return
    await page.type_text(" more")
    await page.key("Escape", 27, code="Escape")
    await asyncio.sleep(SETTLE)
    check("Escape cancels without saving",
          await page.js(f"!({MESSAGE_EDITOR_JS}) && window.__editCalls.length === 2"))


async def check_editor_file_switch(page: Page, check: Checks) -> None:
    """Monaco shows the file the tab names, even when the switch keeps focus.

    `@monaco-editor/react` creates a file's model from `defaultValue` in the
    commit that first names its `path`. The panel refreshed that value in an
    effect, which runs after the commit, so the new file's model was created
    holding the *previous* file's text. A later sync repaired it — unless the
    editor had keyboard focus, which that sync deliberately leaves alone so it
    never fights the user's typing. A click on the file tree takes the focus
    away; an agent's `open_file` does not. Observed: the agent wrote an HTML
    file and opened it while the cursor sat in a Python file, and the HTML tab
    showed the Python source; the next tab switch cached that text as the HTML
    file's buffer, one Ctrl+S away from overwriting the file on disk. Only a
    browser runs Monaco's model lifecycle and real focus, so this lives here.
    """
    base = page.url.split(HARNESS_PATH)[0]
    await page.cmd("Page.navigate", {"url": f"{base}{EDITOR_PATH}"})
    py, html = "src/regressao.py", "src/anim/gradient.html"
    py_text = "import random\n\nXd = [[1, 60], [1, 80]]\n"
    html_text = "<!DOCTYPE html>\n<html><body>gradient</body></html>\n"
    state = "window.__editor()"

    deadline = time.time() + 30
    while time.time() < deadline:
        if await page.rect(".editor-fixture-file"):
            break
        await asyncio.sleep(0.2)
    else:
        check("editor harness loads", False, "the file list never rendered")
        return
    row = await page.rect(f'.editor-fixture-file[data-path="{py}"]')
    await page.click(row["cx"], row["cy"])
    deadline = time.time() + 60          # Monaco itself loads on first use
    while time.time() < deadline:
        if await page.js("!!window.__ready && !!document.querySelector('.monaco-editor .view-lines')"):
            break
        await asyncio.sleep(0.2)
    else:
        check("editor harness loads", False, "Monaco never mounted")
        return
    await asyncio.sleep(0.6)

    lines = await page.rect(".monaco-editor .view-lines")
    await page.click(lines["x"] + 40, lines["y"] + 8)
    await asyncio.sleep(SETTLE)
    if not check("the editor holds the keyboard focus", await page.js(f"{state}.focused")):
        return

    # The agent's open_file: no click, the focus stays in Monaco.
    await page.js(f"window.__open({json.dumps(html)})")
    await asyncio.sleep(0.8)
    now = await page.js(state)
    check("a new file opened under focus shows its own text",
          now["model"] == html and now["text"] == html_text,
          f"model {now['model']!r} holds {now['text'][:30]!r}")

    await page.js(f"window.__open({json.dumps(py)})")
    await asyncio.sleep(0.6)
    await page.js(f"window.__open({json.dumps(html)})")
    await asyncio.sleep(0.6)
    now = await page.js(state)
    check("switching back keeps each file's buffer",
          now["cached"].get(py) == py_text and now["cached"].get(html) == html_text
          and now["text"] == html_text,
          f"cached html {now['cached'].get(html, '')[:30]!r}")

    # A hidden tab whose buffer moved on (the agent rewrote the file) reuses
    # the model Monaco kept from the earlier visit, which is now stale.
    await page.js(f"window.__open({json.dumps(py)})")
    await asyncio.sleep(0.6)
    rewritten = "<!DOCTYPE html>\n<html><body>rewritten</body></html>\n"
    await page.js(f"window.__setCached({json.dumps(html)}, {json.dumps(rewritten)})")
    await asyncio.sleep(SETTLE)
    lines = await page.rect(".monaco-editor .view-lines")
    await page.click(lines["x"] + 40, lines["y"] + 8)
    await asyncio.sleep(SETTLE)
    await page.js(f"window.__open({json.dumps(html)})")
    await asyncio.sleep(0.6)
    now = await page.js(state)
    check("a revisited tab shows its current buffer",
          now["focused"] and now["text"] == rewritten,
          f"focused={now['focused']} text {now['text'][:40]!r}")

    # The sync still leaves the user's own typing alone.
    await page.key("End", 35, code="End")
    await page.type_text("<!-- typed -->")
    await asyncio.sleep(0.05)
    typed = await page.js(f"{state}.text")
    await asyncio.sleep(0.6)
    now = await page.js(state)
    check("typing in the focused editor is kept",
          "<!-- typed -->" in typed and now["text"] == typed and now["buffer"] == typed,
          f"text {now['text'][-30:]!r}")


SUITE = (
    check_terminal_selection,
    check_pdf_tab_switch,
    check_pdf_presentation,
    check_pdf_lookup,
    check_pdf_translation_popup,
    check_chat_message_edit,
    check_editor_file_switch,
)


async def run_scale(devtools_url: str, base_url: str, scale: str) -> Checks:
    check = Checks()
    url = f"{base_url}{HARNESS_PATH}?uiScale={scale}"
    async with websockets.connect(devtools_url, max_size=20_000_000) as ws:
        page = Page(ws, url)
        await page.cmd("Page.enable")
        await page.cmd("Runtime.enable")
        try:
            await page.reload()
        except TimeoutError:
            check("harness loads", False, "the harness never mounted")
            return check

        for step in SUITE:
            try:
                await step(page, check)
            except Exception as error:  # one broken step must not hide the rest
                check(step.__name__, False, f"raised {type(error).__name__}: {error}")
    return check


def main(argv: list[str]) -> int:
    headful = "--headful" in argv
    scales = [a for a in argv if not a.startswith("-")] or ["1", "1.4"]

    chrome = find_chrome()
    vite_port, cdp_port = free_port(), free_port()
    base_url = f"http://localhost:{vite_port}"

    vite = subprocess.Popen(
        # Resolved through PATH/PATHEXT: on Windows the executable is `npx.cmd`,
        # which CreateProcess does not find under the bare name.
        [shutil.which("npx") or "npx", "vite", "--port", str(vite_port), "--strictPort"],
        cwd=GUI_SRC, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    browser = None
    try:
        wait_for(f"{base_url}{HARNESS_PATH}")
        browser = subprocess.Popen(
            [chrome, *([] if headful else ["--headless"]),
             "--disable-gpu", "--no-sandbox", "--no-first-run", "--hide-scrollbars",
             f"--remote-debugging-port={cdp_port}",
             f"--window-size={WINDOW[0]},{WINDOW[1]}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        wait_for(f"http://127.0.0.1:{cdp_port}/json")
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json"))
        devtools_url = next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"]

        failures = 0
        for scale in scales:
            check = asyncio.run(run_scale(devtools_url, base_url, scale))
            check.report(f"interface scale {scale}")
            failures += check.failed
        if failures:
            print(f"\n{failures} check(s) failed")
            return 1
        print("\nall browser checks passed")
        return 0
    finally:
        for process in (browser, vite):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
