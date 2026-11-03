"""A running turn stays bound to the chat it started in.

Traced in a real database: "Continue" was pressed in a secondary chat, and the
turn's reply, all 745 of its activity rows and its working memory were written
into the project's main chat instead -- overwriting the main chat's own saved
memory. `/api/gilzabots/update-project` loads the project's *default* chat and
used to swap it in for the live project, rebinding a turn in flight; a slash
command reloads the project the same way.

The same trace showed the second half of the cost: the resumed turn started from
the visible chat bubbles instead of its restored working memory, because the
front-end sent them and the backend replaced the orchestrator's history with
them, so the turn re-ran the compilations and re-read the files the stopped turn
had already handled.
"""
import asyncio
import json
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gilzabots.agent_stdin as stdin_mod
from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.project import PROJECT_SETTINGS_FIELDS, ProjectData, ProjectStore


@pytest.fixture()
def api(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store.create(
        name="myproj",
        mode="auto",
        model="ollama/gemma4:latest",
        project_name="My Project",
        project_path=str(project_dir),
        description="before",
    )
    store.create_chat("myproj", "chat-myproj", "Chat")
    store.create_chat("myproj", "second-chat", "Aula03")
    live = store.load("myproj", chat_id="second-chat")

    responses = []
    server = AsyncHTTPServer()
    server.send_response = lambda _w, status, body, ctype="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")))
    )

    monkeypatch.setattr(stdin_mod, "current_project", live)
    monkeypatch.setattr(stdin_mod, "current_store", store)
    monkeypatch.setattr(stdin_mod, "current_memgpt", "the running turn's orchestrator")
    yield types.SimpleNamespace(store=store, server=server, responses=responses, live=live)

    import gilzabots.tools as _tools
    _tools._PROJECT_SESSION = None


async def _update(api, payload):
    await api.server.route_api(
        "POST", "/api/gilzabots/update-project", {}, {},
        json.dumps(payload).encode("utf-8"), AsyncMock(),
    )
    return api.responses[-1]


@pytest.mark.asyncio
async def test_saving_settings_during_a_turn_does_not_move_the_turn_to_another_chat(api, monkeypatch):
    def must_not_rebuild(*_a, **_k):
        raise AssertionError("a running turn keeps the orchestrator it was built with")

    monkeypatch.setattr("gilzabots.memgpt_runtime.build_chat_orchestrator", must_not_rebuild)
    alive = asyncio.create_task(asyncio.Event().wait())
    api.server.active_agent_task = alive
    try:
        status, _body = await _update(api, {"project_name": "myproj", "description": "after"})
    finally:
        alive.cancel()

    assert status == 200
    assert stdin_mod.current_project is api.live, "the live project is never swapped"
    assert api.live.current_chat_id == "second-chat"
    assert api.live.description == "after", "and it still receives the edited settings"
    assert stdin_mod.current_memgpt == "the running turn's orchestrator"


@pytest.mark.asyncio
async def test_the_turn_end_save_does_not_revert_settings_edited_mid_turn(api):
    """The turn saves its project when it ends; that copy must carry the edit."""
    alive = asyncio.create_task(asyncio.Event().wait())
    api.server.active_agent_task = alive
    try:
        await _update(api, {"project_name": "myproj", "model_params": {"max_heartbeats": 80}})
    finally:
        alive.cancel()

    api.store.save(stdin_mod.current_project)

    assert api.store.load("myproj").model_params.get("max_heartbeats") == 80


@pytest.mark.asyncio
async def test_saving_settings_between_turns_rebuilds_for_the_live_chat(api, monkeypatch):
    built = []
    monkeypatch.setattr(
        "gilzabots.memgpt_runtime.build_chat_orchestrator",
        lambda project, store: built.append(project) or "rebuilt",
    )

    await _update(api, {"project_name": "myproj", "description": "after"})

    assert built == [api.live]
    assert built[0].current_chat_id == "second-chat", "not the default chat the endpoint loaded"
    assert stdin_mod.current_memgpt == "rebuilt"


def test_settings_are_copied_without_the_chat_view():
    live = ProjectData(name="p", current_chat_id="second-chat", history=[{"role": "user", "content": "hi"}],
                       core_memory="this chat's memory")
    edited = ProjectData(name="p", current_chat_id="main_p", description="after", model="m2",
                         model_params={"max_heartbeats": 80}, core_memory="the main chat's memory")

    live.apply_settings_from(edited)

    assert (live.description, live.model, live.model_params) == ("after", "m2", {"max_heartbeats": 80})
    assert live.current_chat_id == "second-chat"
    assert live.history == [{"role": "user", "content": "hi"}]
    assert live.core_memory == "this chat's memory"
    assert not {"current_chat_id", "history", "chats", "core_memory"} & set(PROJECT_SETTINGS_FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/gilzabots/slash-command", "/api/gilzabots/slash-command/continue"])
async def test_slash_commands_are_refused_while_a_turn_is_alive(api, monkeypatch, path):
    async def must_not_run(_data):
        raise AssertionError("a slash command reloads the project a running turn persists through")

    monkeypatch.setattr(stdin_mod, "handle_slash_command", must_not_run)
    monkeypatch.setattr(stdin_mod, "handle_slash_command_continue", must_not_run)
    alive = asyncio.create_task(asyncio.Event().wait())
    api.server.active_agent_task = alive
    try:
        await api.server.route_api(
            "POST", path, {}, {},
            json.dumps({"prompt": "/clear", "project_name": "myproj", "chat_id": "main_myproj"}).encode("utf-8"),
            AsyncMock(),
        )
    finally:
        alive.cancel()

    status, body = api.responses[-1]
    assert status == 409
    assert body["reason"] == "turn_active"
    assert stdin_mod.current_project is api.live


@pytest.mark.asyncio
async def test_client_messages_do_not_replace_the_orchestrators_restored_memory(tmp_path, monkeypatch):
    store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    store.create(name="proj", mode="auto", model="fake/model", project_name="Proj", project_path=str(tmp_path))
    store.create_chat("proj", "chat-proj", "Chat")
    project = store.load("proj")

    restored = [
        {"role": "user", "content": "build the slides"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "run_command", "content": "compiled"},
    ]
    seen = {}

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = list(restored)
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            seen["history"] = list(self.internal_history)
            return SimpleNamespace(response="done")

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "Continue",
        "chat_id": project.current_chat_id,
        "messages": [
            {"role": "assistant", "content": "Hello! I'm ready to assist you."},
            {"role": "assistant", "content": "truncated bubble"},
        ],
    })

    assert seen["history"] == restored, "the tool calls and results the resume needs survive"


def test_continue_does_not_send_the_visible_bubbles():
    app = open("gui_src/src/App.jsx", encoding="utf-8").read()

    assert "serializeChatHistoryForAgent" not in app
    assert "messages: messagesForRequest" not in app


def _function_body(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n  };\n", start)]


def test_a_second_start_from_this_window_cannot_idle_the_running_turn():
    """A double click on "save edit" created two branches of one message.

    Both clicks read `isAgentRunning === false` (React state, stale until the next
    render), so both branched and both called /run. The second was refused with
    409, and its cleanup reset `isAgentRunning` and the run's chat binding while
    the first turn kept streaming into the window: the stop button went idle and
    the reasoning kept moving. Starts are now guarded by a ref set synchronously.
    """
    app = open("gui_src/src/App.jsx", encoding="utf-8").read()
    send = _function_body(app, "const handleSendMessage = async (")
    inline = _function_body(app, "const handleSendMessageWithPrompt = async (")
    edit = _function_body(app, "const handleEditUserMessage = async (")

    for body in (send, inline):
        assert "|| isAgentRunning)" not in body, "a start must not rely on render-time state alone"
        assert "localTurnRef.current = true;" in body
        # The lock is taken before the first await, so a second call sees it.
        lock = body.index("localTurnRef.current = true;")
        # (The composer's queue call is the one await there: it returns without
        # starting a turn.)
        guarded = body[body.index("isTurnBusy()"):lock].replace(
            "await queueMessageForRunningAgent(userText, attachmentsSnapshot, targetChatId);\n        return;", ""
        )
        assert "await " not in guarded
        assert body.count("localTurnRef.current = false;") >= 1

    # Every exit after the lock in handleSendMessage releases it: a failed
    # creation of the chat a first message starts, the failed truncate, the
    # slash-command path and the run itself.
    assert send.count("localTurnRef.current = false;") == 4

    assert "isTurnBusy()" in edit
    branch = edit.index("editBranchInFlightRef.current = true;")
    assert branch < edit.index("fetch('/api/chat/branch-edit'")
    assert "editBranchInFlightRef.current = false;" in edit[edit.index("finally {", branch):]
