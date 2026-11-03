#!/usr/bin/env python3
"""End-to-end check of the mobile web layer in a real browser.

    python3 test/browser/run.py              (after `npm run build`)
    CHROME=/path/to/chrome python3 test/browser/run.py --headful

Drives the built app (`dist/`) at a phone-sized viewport with real input
through the DevTools Protocol, while Google Drive and the model provider are
answered by fakes behind request interception:

  sign in (development token) → project list → open project → configure a
  connection → chat turn that reads a file, proposes an edit, is approved →
  the answer renders with KaTeX → the journal and the edited file are in the
  fake Drive in the desktop's format → a failed turn is tried again in place,
  an answer is tried again in a new chat → the file viewer shows the new text
  → at tablet size, chats and files open beside the list.

What it cannot cover: the native plugin (Google authorization, Keystore,
native streaming). Those run only on Android. The browser is started with web
security off because CORS is not under test — on the device, model traffic goes
through native HTTP, and Google's APIs allow cross-origin calls.

Needs Chrome or Chromium and the `websockets` package (in the project venv).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import http.server
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
MOBILE = os.path.dirname(os.path.dirname(HERE))
DIST = os.path.join(MOBILE, "dist")
VIEWPORT = (412, 915)
FOLDER = "application/vnd.google-apps.folder"
CHROME_CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome-headless-shell"]

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{f'  ({detail})' if detail and not condition else ''}")
    if not condition:
        failures.append(label)


# ─── Fake Google Drive ────────────────────────────────────────────────────────

class FakeDrive:
    def __init__(self) -> None:
        self.files: dict[str, dict] = {"root": {"id": "root", "name": "My Drive", "mimeType": FOLDER, "parents": [], "content": None}}
        self.ids = itertools.count(1)
        self.clock = itertools.count(1)
        container = self.add("GilzaBots", "root", FOLDER)
        self.root = self.add("thesis", container, FOLDER)
        export = {
            "version": 1, "project": "thesis_db", "updated_at": "2026-10-19T10:00:00.000000+00:00",
            "settings": {"model": "openai/gpt-test", "description": "A thesis", "use_shared_memory": "0", "model_params": "{}"},
            "chats": [{"id": "main_thesis_db", "name": "Main Chat", "created_at": "2026-10-19T10:00:00.000000+00:00",
                       "agent_state": "{}", "core_memory": ""}],
            "messages": [
                {"chat_id": "main_thesis_db", "timestamp": "2026-10-19T10:00:01.000000+00:00", "role": "user",
                 "content": "desktop question", "client_message_id": "d1", "attachments": "[]", "deleted_at": "", "superseded_by": ""},
                {"chat_id": "main_thesis_db", "timestamp": "2026-10-19T10:00:02.000000+00:00", "role": "assistant",
                 "content": "desktop answer", "client_message_id": "", "attachments": "[]", "deleted_at": "", "superseded_by": ""},
            ],
            "activity": [],
        }
        self.write(".gilzabots/session/chats.json", json.dumps(export))
        self.write("main.tex", "\\section{Intro}\nHello world\n")
        self.write("notes.md", "# Notes\n\nNever opened on the phone.\n")

    def add(self, name, parent, mime="text/plain", content=None):
        file_id = f"f{next(self.ids)}"
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent],
                               "content": content, "modifiedTime": f"2026-10-19T12:00:{next(self.clock):02d}.000Z"}
        return file_id

    def child(self, parent, name, folder=None):
        for f in self.files.values():
            if f["name"] == name and parent in f["parents"] and (folder is None or (f["mimeType"] == FOLDER) == folder):
                return f
        return None

    def write(self, path, content):
        parent = self.root
        parts = path.split("/")
        for part in parts[:-1]:
            found = self.child(parent, part, True)
            parent = found["id"] if found else self.add(part, parent, FOLDER)
        existing = self.child(parent, parts[-1], False)
        if existing:
            existing["content"] = content
            return existing["id"]
        return self.add(parts[-1], parent, "text/plain", content)

    def read(self, path):
        parent = self.root
        node = None
        for part in path.split("/"):
            node = self.child(parent, part)
            if node is None:
                return None
            parent = node["id"]
        return node["content"]

    def meta(self, f):
        out = {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"], "trashed": False, "modifiedTime": f["modifiedTime"]}
        if f["content"] is not None:
            data = f["content"].encode()
            out["size"] = str(len(data))
            out["md5Checksum"] = hashlib.md5(data).hexdigest()
        return out

    def handle(self, method, url, body):
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path
        if method == "GET" and path == "/drive/v3/about":
            return 200, {"user": {"emailAddress": "tester@example.com"}}
        if method == "GET" and path == "/drive/v3/files":
            q = params["q"]
            parent = q.split("' in parents")[0].rsplit("'", 1)[-1]
            name = q.split("name = '", 1)[1].split("'", 1)[0] if q.startswith("name = '") else None
            folders = f"mimeType = '{FOLDER}'" in q
            found = [self.meta(f) for f in self.files.values() if parent in f["parents"]
                     and (name is None or f["name"] == name) and (not folders or f["mimeType"] == FOLDER)]
            return 200, {"files": found}
        if path.startswith("/drive/v3/files/") and method == "GET":
            f = self.files.get(urllib.parse.unquote(path.rsplit("/", 1)[1]))
            if f is None:
                return 404, {"error": {"message": "not found"}}
            if params.get("alt") == "media":
                return 200, f["content"]
            return 200, self.meta(f)
        if method == "POST" and path == "/drive/v3/files":
            meta = json.loads(body)
            return 200, self.meta(self.files[self.add(meta["name"], meta["parents"][0], meta["mimeType"])])
        if method == "POST" and path == "/upload/drive/v3/files":
            boundary_parts = body.split("\r\n--")
            meta = json.loads(boundary_parts[0].split("\r\n\r\n", 1)[1])
            content = boundary_parts[1].split("\r\n\r\n", 1)[1]
            return 200, self.meta(self.files[self.add(meta["name"], meta["parents"][0], "text/plain", content)])
        if method == "PATCH" and path.startswith("/upload/drive/v3/files/"):
            f = self.files[urllib.parse.unquote(path.rsplit("/", 1)[1])]
            f["content"] = body
            f["modifiedTime"] = f"2026-10-19T13:00:{next(self.clock):02d}.000Z"
            return 200, self.meta(f)
        return 400, {"error": {"message": f"unhandled {method} {path}"}}


# ─── Fake model provider (OpenAI-compatible) ──────────────────────────────────

def sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def tool_call(call_id, name, args):
    return sse([
        {"choices": [{"delta": {"reasoning_content": f"I should use {name}."}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ])


MODEL_SCRIPT = [
    tool_call("c1", "read_file", {"path": "main.tex"}),
    tool_call("c2", "edit_file", {"path": "main.tex", "old_string": "Hello world", "new_string": "Hello from the phone"}),
    sse([{"choices": [{"delta": {"content": piece}}]} for piece in ["Updated the intro. ", "Energy is $E=mc^2$."]]),
    sse([{"choices": [{"delta": {"content": "Answered by the other model."}}]}]),
    (500, json.dumps({"error": {"message": "The model is overloaded."}})),
    sse([{"choices": [{"delta": {"content": "Answered on the second try."}}]}]),
    sse([{"choices": [{"delta": {"content": "A fresh answer in the branch."}}]}]),
]
TABLET = (1024, 768)


# ─── Chrome over CDP ──────────────────────────────────────────────────────────

class Page:
    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers = {}

    async def reader(self):
        async for raw in self.ws:
            message = json.loads(raw)
            if "id" in message and message["id"] in self.pending:
                self.pending.pop(message["id"]).set_result(message)
            elif "method" in message and message["method"] in self.handlers:
                asyncio.ensure_future(self.handlers[message["method"]](message["params"]))

    async def send(self, method, **params):
        call_id = next(self.ids)
        future = asyncio.get_event_loop().create_future()
        self.pending[call_id] = future
        await self.ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        result = await asyncio.wait_for(future, 30)
        if "error" in result:
            raise RuntimeError(f"{method}: {result['error']}")
        return result.get("result", {})

    async def js(self, expression):
        result = await self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def wait(self, expression, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.js(f"Boolean({expression})"):
                return True
            await asyncio.sleep(0.1)
        return False

    # Screens stay mounted under the one on top (hidden), so every lookup
    # takes the first *visible* match.
    VISIBLE = "(e => e.getClientRects().length > 0)"

    async def click_text(self, text, selector="button"):
        # A control enabled by the keystroke just sent is enabled on React's
        # next render, not synchronously.
        await self.wait(f"""[...document.querySelectorAll({json.dumps(selector)})].some(e => {self.VISIBLE}(e) && e.textContent.includes({json.dumps(text)}) && !e.disabled)""", 5)
        rect = await self.js(f"""(() => {{
            const el = [...document.querySelectorAll({json.dumps(selector)})].find(e => {self.VISIBLE}(e) && e.textContent.includes({json.dumps(text)}) && !e.disabled);
            if (!el) return null;
            el.scrollIntoView({{block: 'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()""")
        if not rect:
            raise RuntimeError(f"no enabled {selector} with text {text!r}")
        await self.tap(rect["x"], rect["y"])

    def visible(self, selector):
        return f"[...document.querySelectorAll({json.dumps(selector)})].find({self.VISIBLE})"

    async def click_selector(self, selector):
        await self.wait(f"{self.visible(selector)} && !{self.visible(selector)}.disabled", 5)
        rect = await self.js(f"""(() => {{
            const el = {self.visible(selector)};
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()""")
        if not rect:
            raise RuntimeError(f"no element {selector}")
        await self.tap(rect["x"], rect["y"])

    async def tap(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    async def type(self, selector, text):
        await self.click_selector(selector)
        await self.send("Input.insertText", text=text)

    async def screenshot(self, path):
        data = await self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data["data"]))


def find_chrome():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    sys.exit("No Chrome found; set CHROME=/path/to/chrome.")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main(headful: bool, shots: str) -> int:
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        sys.exit("Build the app first: npm run build")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=DIST)
    handler.func.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app_url = f"http://127.0.0.1:{server.server_address[1]}/index.html"

    drive = FakeDrive()
    script = list(MODEL_SCRIPT)
    model_requests = []
    drive_calls = []
    drive_open = asyncio.Event()
    drive_open.set()
    debug_port = free_port()
    profile = tempfile.mkdtemp(prefix="gilzabots-mobile-chrome-")
    chrome = subprocess.Popen([
        find_chrome(), *([] if headful else ["--headless"]),
        f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile}",
        f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}", "--disable-web-security", "--no-first-run",
        "--lang=en-US", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # The checks read English strings; the UI follows the system language.
        env={**os.environ, "LANG": "en_US.UTF-8", "LANGUAGE": "en_US"})
    try:
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json"))
                page_target = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.1)
        async with websockets.connect(page_target["webSocketDebuggerUrl"], max_size=None) as ws:
            page = Page(ws)
            reader = asyncio.ensure_future(page.reader())

            async def on_paused(params):
                request = params["request"]
                body = request.get("postData", "")
                if "googleapis.com" in request["url"]:
                    drive_calls.append(request["url"])
                    await drive_open.wait()
                    status, payload = drive.handle(request["method"], request["url"], body)
                    text = payload if isinstance(payload, str) else json.dumps(payload)
                    content_type = "text/plain" if isinstance(payload, str) else "application/json"
                elif "api.openai.com" in request["url"]:
                    model_requests.append(json.loads(body))
                    entry = script.pop(0)
                    status, text = entry if isinstance(entry, tuple) else (200, entry)
                    content_type = "text/event-stream" if status == 200 else "application/json"
                else:
                    await page.send("Fetch.continueRequest", requestId=params["requestId"])
                    return
                await page.send(
                    "Fetch.fulfillRequest", requestId=params["requestId"], responseCode=status,
                    responseHeaders=[{"name": "Content-Type", "value": content_type},
                                     {"name": "Access-Control-Allow-Origin", "value": "*"}],
                    body=base64.b64encode(text.encode()).decode(),
                )

            page.handlers["Fetch.requestPaused"] = on_paused
            await page.send("Fetch.enable", patterns=[{"urlPattern": "*googleapis.com*"}, {"urlPattern": "*api.openai.com*"}])
            await page.send("Page.enable")
            await page.send("Runtime.enable")
            await page.send("Emulation.setLocaleOverride", locale="en-US")
            await page.send("Emulation.setDeviceMetricsOverride", width=VIEWPORT[0], height=VIEWPORT[1], deviceScaleFactor=2, mobile=True)
            await page.send("Page.navigate", url=app_url)

            check("the sign-in screen offers a development token in a browser",
                  await page.wait("document.querySelector('.dev-card input')"))
            await page.type(".dev-card input", "dev-token")
            await page.click_text("Use token")
            check("the project list shows the mirrored project", await page.wait("document.body.innerText.includes('thesis')"))
            await page.screenshot(os.path.join(shots, "1-projects.png"))
            await page.click_text("thesis", ".row")
            check("the desktop chat is listed", await page.wait("document.body.innerText.includes('Main Chat')"))
            no_overflow = await page.js("document.documentElement.scrollWidth <= window.innerWidth")
            check("no horizontal scroll at phone width", no_overflow)

            # Configure the connection the project's model names.
            await page.click_selector("button[aria-label='Settings']")
            await page.wait("document.body.innerText.includes('Model connections')")
            await page.click_text("Add connection")
            await page.wait("document.querySelector('.card input[type=password]')")
            await page.type(".card input[type=password]", "sk-test")
            await page.click_selector("h1")  # blur saves the key
            await page.type(".inline-add input", "gpt-other")
            await page.click_text("Add", ".inline-add button")
            await page.wait("document.querySelector('.model-tag')?.innerText.includes('gpt-other')")
            calls_before_back = len(drive_calls)
            await page.click_selector("button[aria-label='Back']")

            # Closing Settings returns to the project as it was: no reload.
            check("closing Settings shows the project at once",
                  await page.js("document.body.innerText.includes('Main Chat') && !document.body.innerText.includes('Loading')"))
            await asyncio.sleep(0.5)
            check("closing Settings does not re-sync the project", len(drive_calls) == calls_before_back,
                  f"{len(drive_calls) - calls_before_back} Drive requests")
            await page.click_text("Main Chat", ".row")
            check("the desktop conversation renders", await page.wait("document.body.innerText.includes('desktop answer')"))
            await page.type(".composer textarea", "Please update the intro")
            await page.click_selector("button[aria-label='Send']")

            check("the edit is offered for approval", await page.wait("document.querySelector('.sheet')", 15))
            sheet = await page.js("document.querySelector('.sheet')?.innerText || ''")
            check("the approval shows the exact change", "Hello world" in sheet and "Hello from the phone" in sheet, sheet)
            await page.screenshot(os.path.join(shots, "2-approval.png"))
            await page.click_text("Apply", ".sheet button")
            check("the answer arrives", await page.wait("document.body.innerText.includes('Updated the intro')", 15))
            check("mathematics renders with KaTeX", await page.wait("document.querySelector('.bubble.assistant .katex')"))
            await asyncio.sleep(1.0)
            await page.screenshot(os.path.join(shots, "3-chat.png"))

            check("the model got the read_file result", any(
                m.get("role") == "tool" and "Hello world" in m.get("content", "")
                for m in model_requests[1]["messages"]) if len(model_requests) > 1 else False)
            check("the edit reached Drive", drive.read("main.tex") == "\\section{Intro}\nHello from the phone\n", repr(drive.read("main.tex")))
            journal_files = [f for f in drive.files.values() if f["name"].endswith(".json") and f["parents"] and
                             drive.files[f["parents"][0]]["name"] == "devices"]
            check("one journal file was written", len(journal_files) == 1, str(len(journal_files)))
            if journal_files:
                journal = json.loads(journal_files[0]["content"])
                roles = [(m["role"], m["content"]) for m in journal["messages"]]
                check("the journal holds the turn in the desktop format",
                      journal["kind"] == "gilzabots.device-journal" and journal["project"] == "thesis_db"
                      and roles == [("user", "Please update the intro"), ("assistant", "Updated the intro. Energy is $E=mc^2$.")], str(roles))
                check("reasoning is stored as a thought row", any(a["event"] == "thought" and "read_file" in a["content"] for a in journal["activity"]))

            # Any configured model can run this chat.
            check("the chat runs on the desktop's model first", model_requests[-1]["model"] == "gpt-test", model_requests[-1]["model"])
            await page.click_selector("button[aria-label='Choose model']")
            await page.wait("document.querySelector('.picker')")
            picker = await page.js("document.querySelector('.picker').innerText")
            check("the picker offers the desktop's model and the added one", "Used on the desktop" in picker and "gpt-other" in picker, picker)
            await page.screenshot(os.path.join(shots, "5-picker.png"))
            await page.click_text("gpt-other", ".picker-row")
            check("the chosen model is shown in the chat", await page.wait("document.querySelector('.model-chip')?.innerText.includes('gpt-other')"))
            await page.type(".composer textarea", "Second question")
            await page.click_selector("button[aria-label='Send']")
            check("the next turn runs on the chosen model", await page.wait("document.body.innerText.includes('Answered by the other model')", 15))
            check("the request named the chosen model", model_requests[-1]["model"] == "gpt-other", model_requests[-1]["model"])

            # A failed turn keeps the question, and it is tried again in place.
            await page.type(".composer textarea", "Third question")
            await page.click_selector("button[aria-label='Send']")
            check("a failed turn offers to try again",
                  await page.wait("document.querySelector('.turn-end.failed')?.innerText.includes('Try again')", 15),
                  await page.js("document.querySelector('.messages')?.innerText.slice(-300)"))
            await page.screenshot(os.path.join(shots, "7-failed.png"))
            await page.click_text("Try again", ".turn-end button")
            check("trying again answers the stored question", await page.wait("document.body.innerText.includes('Answered on the second try')", 15))
            check("the retry sent the question once",
                  [m["content"] for m in model_requests[-1]["messages"] if m["role"] == "user"].count("Third question") == 1)
            # The journal is uploaded before the turn ends (the Stop button goes).
            await page.wait("!document.querySelector(\"button[aria-label='Stop']\")", 10)
            journal = json.loads(drive.read(".gilzabots/session/devices/" + journal_files[0]["name"]) or "{}")
            check("the journal holds the question once",
                  [m["content"] for m in journal.get("messages", [])].count("Third question") == 1)

            # An answer is tried again in a new chat; the original stays.
            last_more = await page.js("""(() => {
                const el = [...document.querySelectorAll('.bubble.assistant .bubble-more')].filter(e => e.getClientRects().length).pop();
                el.scrollIntoView({block: 'center'});
                const r = el.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            })()""")
            await page.tap(last_more["x"], last_more["y"])
            check("the message options offer copy and try again",
                  await page.wait("document.querySelector('.actions-sheet')?.innerText.includes('Try again in a new chat')"))
            await page.screenshot(os.path.join(shots, "8-actions.png"))
            await page.click_text("Try again in a new chat", ".actions-sheet button")
            check("the retry continues in a branch",
                  await page.wait(f"{page.visible('.chat-screen h1')}?.innerText.includes('Main Chat (branch)')", 10))
            check("the branch gets a new answer", await page.wait("document.body.innerText.includes('A fresh answer in the branch')", 15))
            check("the branch asked the copied question",
                  [m["content"] for m in model_requests[-1]["messages"] if m["role"] == "user"][-1] == "Third question")
            # The journal is uploaded before the turn ends (the Stop button goes).
            await page.wait("!document.querySelector(\"button[aria-label='Stop']\")", 10)
            journal = json.loads(drive.read(".gilzabots/session/devices/" + journal_files[0]["name"]) or "{}")
            branch_ids = [c["id"] for c in journal.get("chats", []) if c["name"] == "Main Chat (branch)"]
            check("the branch is a new chat in the journal", len(branch_ids) == 1, str(journal.get("chats")))
            check("the original answer is kept",
                  any(m["chat_id"] == "main_thesis_db" and m["content"] == "Answered on the second try." for m in journal.get("messages", [])))

            await page.click_selector("button[aria-label='Back']")
            await page.wait("document.querySelector('.tabs')")
            await page.click_text("Files", ".tabs button")
            await page.wait("document.body.innerText.includes('main.tex')")

            # Available offline: every viewable file is downloaded and counted.
            check("the offline switch starts off", await page.js(f"{page.visible('input[role=switch]')}?.checked === false"))
            media_before = sum(1 for url in drive_calls if "alt=media" in url)
            await page.click_selector("input[role=switch]")
            check("turning it on saves the project's files",
                  await page.wait("document.body.innerText.includes('2 of 2 files saved')", 10),
                  await page.js("document.querySelector('.offline-card')?.innerText"))
            check("the file never opened was downloaded for offline use",
                  sum(1 for url in drive_calls if "alt=media" in url) > media_before)
            await page.screenshot(os.path.join(shots, "6-offline.png"))
            check("internal GilzaBots files are not listed", not await page.js("document.body.innerText.includes('.gilzabots')"))
            await page.click_text("main.tex", ".row")
            check("the viewer shows the edited file", await page.wait("document.querySelector('.source')?.innerText.includes('Hello from the phone')"))
            await page.screenshot(os.path.join(shots, "4-file.png"))

            # Leave the project and come back while Drive does not answer: what
            # was already read is shown at once, not an empty first-run screen.
            await page.click_selector(".viewer-bar button[aria-label='Back']")  # close the file
            await page.click_text("Chats", ".tabs button")
            await page.click_selector("button[aria-label='Back']")  # project → project list
            await page.wait("document.body.innerText.includes('thesis')")
            drive_open.clear()
            await page.click_text("thesis", ".row")
            check("re-entering a project shows its chats while Drive is still answering",
                  await page.wait("document.body.innerText.includes('Main Chat')", 3))
            await page.click_text("Files", ".tabs button")
            check("the offline status is known before Drive answers",
                  await page.wait("document.body.innerText.includes('2 of 2 files saved')", 3))
            drive_open.set()
            await page.click_selector("button[aria-label='Back']")
            check("the project list marks the project as offline",
                  await page.wait("[...document.querySelectorAll('.row')].some(r => r.innerText.includes('thesis') && r.querySelector('.badge'))", 5))

            # At tablet size the list stays beside what is open.
            await page.send("Emulation.setDeviceMetricsOverride", width=TABLET[0], height=TABLET[1], deviceScaleFactor=2, mobile=True)
            await page.click_text("thesis", ".row")
            check("a tablet shows the list and an empty detail pane",
                  await page.wait("document.querySelector('.split .pane-list') && document.querySelector('.pane-empty')"))
            await page.click_text("Main Chat (branch)", ".row")
            check("a chat opens beside the list",
                  await page.wait("document.querySelector('.pane-detail .chat-screen')?.innerText.includes('A fresh answer') && "
                                  f"{page.visible('.pane-list .tabs')}"))
            check("the open chat is marked in the list", await page.js("document.querySelector('.row.selected')?.innerText.includes('(branch)')"))
            check("no horizontal scroll at tablet width", await page.js("document.documentElement.scrollWidth <= window.innerWidth"))
            await page.screenshot(os.path.join(shots, "9-tablet-chat.png"))
            await page.click_text("Files", ".tabs button")
            await page.click_text("main.tex", ".row")
            check("a file opens beside the list",
                  await page.wait("document.querySelector('.pane-detail .source')?.innerText.includes('Hello from the phone') && "
                                  f"{page.visible('.pane-list')}"))
            await page.screenshot(os.path.join(shots, "10-tablet-file.png"))
            reader.cancel()
    finally:
        chrome.terminate()
        chrome.wait(timeout=10)
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    shots_dir = os.environ.get("SCREENSHOTS", tempfile.gettempdir())
    os.makedirs(shots_dir, exist_ok=True)
    sys.exit(asyncio.run(main("--headful" in sys.argv, shots_dir)))
