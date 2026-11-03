"""Diagnostic tools inspect files as data, including hostile project code."""
import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from gilzabots import tools
from gilzabots.diagnostics import inspect_git, inspect_python


@pytest.fixture(autouse=True)
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, '_PROJECT_SESSION', SimpleNamespace(mode='plan'))
    monkeypatch.setattr(tools, '_PROJECT_PATH', str(tmp_path))
    monkeypatch.setattr(tools, 'free_context_chars', lambda: 4000)


def test_static_python_inspection_never_executes_imports_or_top_level_code(tmp_path):
    target = tmp_path / 'danger.py'
    target.write_text("raise RuntimeError('must not execute')\nimport nonexistent\nclass Example:\n    async def read(self):\n        pass\n")
    result = inspect_python(str(target))
    assert result['valid_syntax']
    assert [s['name'] for s in result['symbols']] == ['Example', 'read']
    assert result['imports'][0]['module'] == 'nonexistent'
    target.write_text('def invalid(:\n')
    assert inspect_python(str(target))['valid_syntax'] is False


def test_document_paging_has_no_gaps_and_creates_no_files(tmp_path, monkeypatch):
    target = tmp_path / 'fake.pdf'
    target.write_bytes(b'%PDF')
    content = 'alpha\nbeta\ngamma' * 50
    monkeypatch.setattr('gilzabots.attachments.extract_document_text_from_path', lambda path: content)
    offset, parts = 0, []
    while offset is not None:
        page = json.loads(asyncio.run(tools.read_document._func(str(target), offset=offset, limit=37)))
        parts.append(page['content'])
        offset = page['next_offset']
    assert ''.join(parts) == content
    assert list(tmp_path.iterdir()) == [target]


def test_search_pages_logs_with_context(tmp_path):
    (tmp_path/'build.log').write_text('before\nerror one\nafter\nerror two\nend\n')
    def search(**kwargs):
        return asyncio.run(tools.search_code._func('error', max_results=1, include_logs=True, context_lines=1, **kwargs))
    first, second = search(), search(offset=1)
    assert 'build.log:1: before' in first and 'offset=1' in first
    assert 'error two' not in first
    assert 'build.log:4: error two' in second and 'offset=2' in second


def test_git_diff_does_not_execute_external_diff_fsmonitor_or_filters(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    target = tmp_path / 'file.txt'
    target.write_text('old\n')
    git('add', 'file.txt')
    target.write_text('new\n')
    marker = tmp_path / 'executed'
    hook = tmp_path / 'hook.sh'
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    git('config', 'diff.external', str(hook))
    git('config', 'core.fsmonitor', str(hook))
    git('config', 'filter.danger.clean', str(hook))
    git('config', 'filter.danger.process', str(hook))
    git('config', 'filter.danger.required', 'true')
    (tmp_path / '.gitattributes').write_text('*.txt filter=danger\n')
    index = tmp_path / '.git/index'
    before = index.read_bytes(), index.stat().st_mtime_ns
    diff = inspect_git(str(tmp_path), 'diff')
    assert '-old' in diff['content'] and '+new' in diff['content']
    assert 'file.txt' in inspect_git(str(tmp_path), 'status')['content']
    assert not marker.exists()
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    with pytest.raises(ValueError):
        inspect_git(str(tmp_path), 'reset')
