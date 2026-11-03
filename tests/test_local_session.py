"""Real HTTP boundary tests: authentication applies to files and media alike."""
import asyncio
import json
from urllib.parse import urlencode

import pytest
import pytest_asyncio

from gilzabots.ide_server import AsyncHTTPServer


@pytest_asyncio.fixture
async def http_server(tmp_path):
    app = AsyncHTTPServer(port=0, static_dir=str(tmp_path))
    (tmp_path / "index.html").write_text('<html>GilzaBots</html>')
    (tmp_path / "document.txt").write_text('original')
    await app.start()
    async def request(method, path, headers=None, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        values = {"Host": f"127.0.0.1:{app.port}", "Content-Length": str(len(body)), **(headers or {})}
        reader, writer = await asyncio.open_connection('127.0.0.1', app.port)
        head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in values.items()) + '\r\n'
        writer.write(head.encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        await writer.wait_closed()
        head, content = response.split(b'\r\n\r\n', 1)
        lines = head.decode().split('\r\n')
        return int(lines[0].split()[1]), dict(line.split(': ', 1) for line in lines[1:]), content
    yield app, request, tmp_path
    app.server.close()
    await app.server.wait_closed()
    app.stop()


@pytest.mark.asyncio
async def test_session_bootstrap_and_file_write_need_no_user_interaction(http_server):
    app, request, root = http_server
    payload = {"projectPath": str(root), "filePath": "document.txt", "content": "saved"}
    assert (await request('POST', '/api/file/write', payload=payload))[0] == 401
    status, headers, _ = await request('POST', '/api/session', {'X-GilzaBots-Bootstrap': '1', 'Sec-Fetch-Site': 'same-origin'})
    assert status == 200
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    cookie = headers['Set-Cookie'].split(';', 1)[0]
    assert (await request('GET', '/api/session', {'Cookie': cookie}))[0] == 200
    status, headers, _ = await request('POST', '/api/file/write', {'Cookie': cookie}, payload)
    assert status == 200 and (root / 'document.txt').read_text() == 'saved'
    assert 'Access-Control-Allow-Origin' not in headers
    path = '/api/file/raw?' + urlencode({'projectPath': root, 'filePath': 'document.txt'})
    status, _, body = await request('GET', path, {'Cookie': cookie, 'Sec-Fetch-Site': 'same-origin'})
    assert status == 200 and body == b'saved'


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [
    {'Host': 'untrusted.invalid:3000'},
    {'Origin': 'http://untrusted.invalid'},
    {'Origin': 'null'},
    {'Sec-Fetch-Site': 'cross-site'},
    {'Sec-Fetch-Site': 'same-site'},
])
async def test_foreign_requests_rejected_even_with_valid_cookie(http_server, headers):
    app, request, root = http_server
    auth = {'Cookie': app.local_session.cookie().split(';',1)[0], **headers}
    status, _, _ = await request('POST', '/api/file/write', auth,
                                {'projectPath': str(root), 'filePath': 'document.txt', 'content': 'bad'})
    assert status == 403
    assert (root / 'document.txt').read_text() == 'original'
    status, response_headers, _ = await request('POST', '/api/session', {**headers, 'X-GilzaBots-Bootstrap': '1'})
    assert status == 403 and 'Set-Cookie' not in response_headers


@pytest.mark.asyncio
async def test_bootstrap_requires_custom_header_and_preflight_is_not_open(http_server):
    _, request, _ = http_server
    assert (await request('POST', '/api/session'))[0] == 403
    status, headers, _ = await request('OPTIONS', '/api/session', {'Origin': 'https://untrusted.invalid'})
    assert status == 403 and 'Access-Control-Allow-Origin' not in headers
    status, headers, _ = await request('GET', '/')
    assert status == 200 and headers['X-Frame-Options'] == 'SAMEORIGIN'


@pytest.mark.asyncio
async def test_background_tool_uses_authenticated_nonblocking_local_api(http_server, monkeypatch):
    from types import SimpleNamespace
    from gilzabots import tools
    app, _, root = http_server
    sent = []
    app.active_terminal = SimpleNamespace(project_path=str(root), is_running=True, write=sent.append, close=lambda: None)
    monkeypatch.setattr(tools, '_PROJECT_SESSION', SimpleNamespace(mode='auto'))
    monkeypatch.setattr(tools, '_PROJECT_PATH', str(root))
    result = await tools.run_background_command._func('echo authenticated')
    assert result.startswith('SUCCESS:')
    assert sent == ['echo authenticated\r']
