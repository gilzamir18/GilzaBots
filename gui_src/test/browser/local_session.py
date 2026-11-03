#!/usr/bin/env python3
"""Verify automatic authentication in Chromium, for packaged and Vite frontends.

Run from the repository root with .venv/bin/python gui_src/test/browser/local_session.py.
Uses a temporary application home and browser profile; no user data is accessed.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlencode

from run import Page, find_chrome, free_port, wait_for
import websockets

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


async def checks(work):
    os.environ['GILZABOTS_HOME'] = str(work / 'home')
    from gilzabots.ide_server import AsyncHTTPServer

    class Server(AsyncHTTPServer):
        async def route_api(self, method, path, query, headers, body, writer):
            if path == '/api/review-events':
                self.send_response(writer, 200, b'data: authenticated\n\n', 'text/event-stream')
            else:
                await super().route_api(method, path, query, headers, body, writer)

    server = Server(port=0, static_dir=str(ROOT / 'gilzabots/gui'))
    await server.start()
    backend_url = f'http://127.0.0.1:{server.port}'
    vite_port, cdp_port = free_port(), free_port()
    log = (work / 'process.log').open('w')
    vite = subprocess.Popen(['npx', 'vite', '--port', str(vite_port), '--strictPort'],
                            cwd=ROOT / 'gui_src', env={**os.environ, 'GILZABOTS_BACKEND_URL': backend_url},
                            stdout=log, stderr=log)
    chrome = subprocess.Popen([find_chrome(), '--headless', '--no-sandbox', '--disable-gpu', '--no-first-run',
                               f'--user-data-dir={work / "browser"}', f'--remote-debugging-port={cdp_port}',
                               'about:blank'], stdout=log, stderr=log)
    try:
        await asyncio.to_thread(wait_for, f'http://localhost:{vite_port}')
        await asyncio.to_thread(wait_for, f'http://127.0.0.1:{cdp_port}/json')
        targets = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(f'http://127.0.0.1:{cdp_port}/json')))
        url = next(target['webSocketDebuggerUrl'] for target in targets if target['type'] == 'page')
        image = work / 'pixel.png'
        image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        image_url = '/api/file/raw?' + urlencode({'projectPath': str(work), 'filePath': image.name})
        async with websockets.connect(url) as ws:
            page = Page(ws, '')
            for origin in (backend_url, f'http://localhost:{vite_port}'):
                await page.cmd('Page.navigate', {'url': origin})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if await page.js("!!document.querySelector('.vscode-app')"):
                        break
                    await asyncio.sleep(.2)
                else:
                    raise AssertionError(f'App failed to mount at {origin}: ' + str(await page.js('document.body.innerText.slice(0,800)')))
                assert await page.js("fetch('/api/session').then(r=>r.status)") == 200
                assert not await page.js("document.cookie.includes('gilzabots_session')")
                assert await page.js(f"new Promise(resolve=>{{const img=new Image(); img.onload=()=>resolve(img.naturalWidth); img.onerror=()=>resolve(0); img.src={json.dumps(image_url)};}})") == 1
                assert await page.js("new Promise(resolve=>{const events=new EventSource('/api/review-events');events.onmessage=e=>{events.close();resolve(e.data)};events.onerror=()=>{events.close();resolve('error')}})") == 'authenticated'
                print(f'PASS: {origin} mounts, authenticates fetch, loads native image and EventSource; token is HttpOnly')
            # The page on the current sources (Vite) loaded its preferences from
            # the backend before mounting, importing the old localStorage once,
            # and a preference written there reaches ui_settings.json.
            prefs = await page.js("fetch('/api/settings/client-prefs').then(r=>r.json())")
            assert prefs['imported'] is True, prefs
            await page.js("fetch('/api/settings/client-prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({set:{theme:'light'},remove:[]})}).then(r=>r.status)")
            settings = json.loads((work / 'home' / 'ui_settings.json').read_text(encoding='utf-8'))
            assert settings['client_prefs'].get('theme') == 'light', settings
            print('PASS: preferences are hydrated from and saved to the backend')
            # A different local origin must not be able to use the backend session.
            assert await page.js(f"fetch({json.dumps(backend_url + '/api/session')},{{credentials:'include'}}).then(()=>false).catch(()=>true)")
            print('PASS: foreign-origin browser request is rejected')
    finally:
        server.server.close()
        await server.server.wait_closed()
        for process in (chrome, vite):
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='gilzabots-session-browser-') as temp:
        asyncio.run(checks(Path(temp)))
