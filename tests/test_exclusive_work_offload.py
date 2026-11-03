"""Slow request work runs off the event loop without reordering requests.

A Tectonic compile, `git status` and the project-tree walk used to run on the
server's event loop, freezing every streaming response (agent turns, terminal
output) for their whole duration. They now run in a worker thread while holding
the server's exclusive-work lock, and every other request waits on that lock
before it is dispatched — so requests are still handled in arrival order and
never interleave with that work, exactly as when it blocked the loop.
"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

import gilzabots.latex_compiler as latex_compiler
from gilzabots.ide_server import AsyncHTTPServer


async def _wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_a_request_arriving_during_exclusive_work_runs_after_it():
    server = AsyncHTTPServer()
    order = []
    release = asyncio.Event()

    async def slow_compile():
        order.append("compile:start")
        await release.wait()
        order.append("compile:end")

    async def save():
        order.append("save")

    compile_task = asyncio.create_task(server._dispatch("/api/latex/compile", slow_compile))
    await _wait_until(lambda: order == ["compile:start"])
    save_task = asyncio.create_task(server._dispatch("/api/file/write", save))
    await asyncio.sleep(0.05)
    assert order == ["compile:start"], "the save must not run while the compile is in flight"

    release.set()
    await asyncio.gather(compile_task, save_task)
    assert order == ["compile:start", "compile:end", "save"]


@pytest.mark.asyncio
async def test_requests_keep_their_arrival_order_around_exclusive_work():
    server = AsyncHTTPServer()
    order = []
    gates = {name: asyncio.Event() for name in ("first", "second")}

    def exclusive(name):
        async def handler():
            order.append(f"{name}:start")
            await gates[name].wait()
            order.append(f"{name}:end")
        return handler

    def plain(name):
        async def handler():
            order.append(name)
        return handler

    tasks = [asyncio.create_task(server._dispatch("/api/latex/compile", exclusive("first")))]
    await _wait_until(lambda: "first:start" in order)
    tasks.append(asyncio.create_task(server._dispatch("/api/file/read", plain("read"))))
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(server._dispatch("/api/git/status", exclusive("second"))))
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(server._dispatch("/api/settings", plain("settings"))))
    await asyncio.sleep(0.05)

    gates["first"].set()
    await _wait_until(lambda: "second:start" in order)
    gates["second"].set()
    await asyncio.gather(*tasks)

    assert order == [
        "first:start", "first:end",
        "read",
        "second:start", "second:end",
        "settings",
    ]


@pytest.mark.asyncio
async def test_a_request_with_nothing_in_flight_is_dispatched_immediately():
    server = AsyncHTTPServer()
    ran = []

    async def handler():
        ran.append(True)

    await server._dispatch("/api/file/read", handler)
    assert ran == [True]


@pytest.mark.asyncio
async def test_compile_runs_in_a_worker_thread_and_leaves_the_loop_free(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    main = project / "main.tex"
    main.write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    compile_threads = []

    def fake_compile_latex(content, full_path, main_file, project_path, **kwargs):
        compile_threads.append(threading.current_thread())
        started.set()
        assert release.wait(5), "test never released the compile"
        return {"success": False, "log": "fake", "timing": {}}

    monkeypatch.setattr(latex_compiler, "compile_latex", fake_compile_latex)

    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )
    body = json.dumps({
        "content": main.read_text(encoding="utf-8"),
        "filePath": "main.tex",
        "projectPath": str(project),
        "mainFile": "main.tex",
    }).encode("utf-8")

    compile_task = asyncio.create_task(server._dispatch(
        "/api/latex/compile",
        lambda: server.route_api("POST", "/api/latex/compile", {}, {}, body, AsyncMock()),
    ))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)

    # The loop is not blocked: other coroutines (a streaming agent turn, say)
    # keep running while Tectonic would be working.
    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 5
    assert not compile_task.done()
    assert compile_threads[0] is not threading.main_thread()

    release.set()
    await compile_task
    assert responses and responses[0][0] == 200
    assert responses[0][1]["success"] is False


@pytest.mark.asyncio
async def test_file_tree_and_git_status_answer_as_before(tmp_path):
    project = tmp_path / "project"
    (project / "chapters").mkdir(parents=True)
    (project / "main.tex").write_text("x", encoding="utf-8")
    (project / "chapters" / "intro.tex").write_text("y", encoding="utf-8")

    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, body, content_type="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    await server.route_api("GET", "/api/files", {"projectPath": [str(project)]}, {}, b"", AsyncMock())
    status, payload = responses.pop()
    assert status == 200
    assert [entry["name"] for entry in payload["files"]] == ["chapters", "main.tex"]
    assert payload["files"][0]["children"][0]["path"].replace("\\", "/") == "chapters/intro.tex"

    await server.route_api(
        "GET", "/api/git/status",
        {"projectPath": [str(project)], "shadow": ["false"]}, {}, b"", AsyncMock(),
    )
    status, payload = responses.pop()
    assert status == 200
    assert payload["files"] == []


GIT_ENDPOINTS = [
    ("GET", "/api/git/file-at-head", {"projectPath": ["P"], "filePath": ["a.tex"]}, {}),
    ("POST", "/api/git/commit", {}, {"projectPath": "P"}),
    ("GET", "/api/git/diff", {"projectPath": ["P"]}, {}),
    ("GET", "/api/git/archive", {"projectPath": ["P"], "commit": ["abc"]}, {}),
    ("POST", "/api/git/restore", {}, {"projectPath": "P", "commit": "abc"}),
    ("GET", "/api/git/log", {"projectPath": ["P"]}, {}),
    ("POST", "/api/git/stage", {}, {"projectPath": "P", "filePath": "a.tex"}),
    ("POST", "/api/git/discard", {}, {"projectPath": "P", "filePath": "a.tex"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,query,body", GIT_ENDPOINTS, ids=[e[1] for e in GIT_ENDPOINTS])
async def test_git_endpoints_run_git_in_a_worker_thread(tmp_path, monkeypatch, method, path, query, body):
    """A git call no longer holds the event loop: a diff makes up to a dozen
    of them and a commit runs the repository's hooks."""
    import gilzabots.ide_server as ide_server

    assert path in AsyncHTTPServer.EXCLUSIVE_WORK_PATHS
    started = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    threads = []

    def blocking_git_context(*_args, **_kwargs):
        threads.append(threading.get_ident())
        started.set()
        release.wait(5)
        raise ide_server.GitContextError("no repository")

    monkeypatch.setattr(ide_server, "_resolve_git_context", blocking_git_context)
    query = {k: [str(tmp_path) if v == ["P"] else v[0]] for k, v in query.items()}
    body = {k: (str(tmp_path) if v == "P" else v) for k, v in body.items()}
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda _w, status, payload, content_type="text/plain": responses.append(status)

    request = asyncio.create_task(server.route_api(method, path, query, {}, json.dumps(body).encode(), AsyncMock()))
    await _wait_until(started.is_set)
    ticks = 0
    for _ in range(3):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 3 and not request.done()
    release.set()
    await request
    assert threads and threads[0] != loop_thread
    assert len(responses) == 1
