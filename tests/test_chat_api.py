import json
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer, _ollama_tags_url_for_model_info
from gilzabots.project import ProjectStore


@pytest.mark.asyncio
async def test_chat_truncate_endpoint_returns_superseded_ids(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    project = store.create(
        name="myproj",
        mode="plan",
        model="fake/model",
        project_name="My Project",
        project_path=str(tmp_path / "project"),
    )
    store.create_chat("myproj", "chat-myproj", "Chat")
    project = store.load("myproj")
    first_id = store.append_message(project, "user", "first")
    reply_id = store.append_message(project, "assistant", "first reply")
    second_id = store.append_message(project, "user", "second")
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "POST",
        "/api/chat/truncate",
        {},
        {},
        json.dumps({
            "project_name": "myproj",
            "chat_id": project.current_chat_id,
            "message_id": reply_id,
        }).encode("utf-8"),
        writer,
    )

    assert responses == [
        (
            200,
            {"status": "ok", "superseded_ids": [reply_id, second_id]},
            "application/json",
        )
    ]
    loaded = store.load("myproj", chat_id=project.current_chat_id)
    assert [m["id"] for m in loaded.history] == [first_id]


@pytest.mark.asyncio
async def test_chat_truncate_endpoint_rejects_request_without_stable_anchor(tmp_path, monkeypatch):
    """A positional index is not accepted: it cannot be mapped to stored rows safely."""
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    project = store.create(
        name="myproj",
        mode="plan",
        model="fake/model",
        project_name="My Project",
        project_path=str(tmp_path / "project"),
    )
    store.create_chat("myproj", "chat-myproj", "Chat")
    project = store.load("myproj")
    store.append_message(project, "user", "first")
    store.append_message(project, "assistant", "first reply")
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    server.send_response = lambda _w, status, body, ctype="text/plain": responses.append(
        (status, json.loads(body.decode("utf-8")), ctype)
    )

    await server.route_api(
        "POST",
        "/api/chat/truncate",
        {},
        {},
        json.dumps({
            "project_name": "myproj",
            "chat_id": project.current_chat_id,
            "from_index": 1,
        }).encode("utf-8"),
        writer,
    )

    assert responses[0][0] == 400
    loaded = store.load("myproj", chat_id=project.current_chat_id)
    assert [m["content"] for m in loaded.history] == ["first", "first reply"]

@pytest.mark.asyncio
async def test_clear_all_chats_endpoint_resets_project_chats(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    project = store.create(
        name="myproj",
        mode="plan",
        model="fake/model",
        project_name="My Project",
        project_path=str(tmp_path / "project"),
    )
    store.create_chat("myproj", "chat-myproj", "Chat")
    project = store.load("myproj")
    store.append_message(project, "user", "main message")
    store.create_chat("myproj", "branch-1", "Branch")
    branch = store.load("myproj", chat_id="branch-1")
    store.append_message(branch, "user", "branch message")
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "POST",
        "/api/chat/clear-all",
        {},
        {},
        json.dumps({"project_name": "myproj"}).encode("utf-8"),
        writer,
    )

    assert responses == [(200, {"status": "ok"}, "application/json")]
    # No chat is recreated: the project is left with none.
    loaded = store.load("myproj")
    assert loaded.chats == []
    assert loaded.current_chat_id == ""
    assert loaded.history == []


@pytest.mark.asyncio
async def test_load_local_ollama_models_endpoint_returns_added_models(monkeypatch):
    added_models = [{
        "id": "ollama/new-model:latest",
        "provider": "ollama",
        "name": "new-model:latest",
        "api_key": "",
        "api_base": "http://localhost:11434/v1",
        "supports_thinking": False,
    }]
    monkeypatch.setattr(
        "gilzabots.models_store.load_local_ollama_models",
        lambda: added_models,
    )
    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    await server.route_api(
        "POST",
        "/api/settings/models/load-local-ollama",
        {},
        {},
        b"{}",
        writer,
    )

    assert responses == [
        (200, {"models": added_models, "added_count": 1}, "application/json")
    ]


@pytest.mark.asyncio
async def test_load_local_ollama_models_endpoint_reports_missing_installation(monkeypatch):
    from gilzabots.models_store import LocalOllamaNotInstalledError

    def raise_not_installed():
        raise LocalOllamaNotInstalledError()

    monkeypatch.setattr(
        "gilzabots.models_store.load_local_ollama_models",
        raise_not_installed,
    )
    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    await server.route_api(
        "POST",
        "/api/settings/models/load-local-ollama",
        {},
        {},
        b"{}",
        writer,
    )

    assert responses == [
        (409, {"error": "ollama_not_installed"}, "application/json")
    ]
def test_ollama_model_info_uses_configured_remote_api_base():
    assert _ollama_tags_url_for_model_info(
        "ollama/gpt-oss:20b",
        "http://100.85.255.111:11434/v1",
    ) == "http://100.85.255.111:11434/api/tags"


def test_ollama_model_info_without_api_base_uses_localhost():
    assert _ollama_tags_url_for_model_info(
        "ollama/gemma4:26b",
        "",
    ) == "http://127.0.0.1:11434/api/tags"


@pytest.mark.asyncio
async def test_update_project_endpoint_updates_model(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    project = store.create(
        name="myproj",
        mode="plan",
        model="ollama/old-model",
        project_name="My Project",
        project_path=str(tmp_path / "project"),
    )
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)
    # Selecting a local Ollama model pre-fetches it; the test must not shell out
    # to `ollama pull`. See tests/test_ollama_model_prefetch.py for that path.
    pulled = []
    monkeypatch.setattr(
        "gilzabots.ollama_manager.pull_model_in_background",
        lambda name, report=None: pulled.append(name) or True,
    )

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    await server.route_api(
        "POST",
        "/api/gilzabots/update-project",
        {},
        {},
        json.dumps({
            "project_name": "myproj",
            "model": "ollama/new-model:latest",
        }).encode("utf-8"),
        writer,
    )

    assert len(responses) == 1
    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["model"] == "ollama/new-model:latest"
    loaded = store.load("myproj")
    assert loaded.model == "ollama/new-model:latest"
    assert pulled == ["new-model:latest"]



def _api_harness(tmp_path, monkeypatch):
    """A server whose responses are collected instead of written to a socket."""
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    monkeypatch.setattr("gilzabots.config.DEFAULT_DB_PATH", db_path)

    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return store, server, responses


@pytest.mark.asyncio
async def test_chat_history_rejects_an_unknown_chat_id(tmp_path, monkeypatch):
    """The stale-id case that made the selector and the transcript disagree.

    A client holding the sentinel ``"main"`` used to be answered with the most
    recently created chat, so the panel showed one chat in the selector over
    another chat's messages. It must fail instead, letting the client fall back
    to a real id.
    """
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "first", "First Chat")
    first = store.load("myproj", chat_id="first")
    store.append_message(first, "user", "message in first")
    store.create_chat("myproj", "second", "Second Chat")
    second = store.load("myproj", chat_id="second")
    store.append_message(second, "user", "message in second")

    await server.route_api(
        "GET",
        "/api/chat/history",
        {"project_name": ["myproj"], "chat_id": ["main"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 404
    assert "main" in data["error"]


@pytest.mark.asyncio
async def test_chat_history_without_chat_id_returns_the_most_recent_chat(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "first", "First Chat")
    first = store.load("myproj", chat_id="first")
    store.append_message(first, "user", "message in first")
    store.create_chat("myproj", "second", "Second Chat")
    second = store.load("myproj", chat_id="second")
    store.append_message(second, "user", "message in second")

    await server.route_api(
        "GET",
        "/api/chat/history",
        {"project_name": ["myproj"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["chat_id"] == "second"
    assert [m["content"] for m in data["history"]] == ["message in second"]


@pytest.mark.asyncio
async def test_chat_history_of_a_project_without_chats_is_empty(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))

    await server.route_api(
        "GET", "/api/chat/history", {"project_name": ["myproj"]}, {}, b"", AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["chat_id"] == ""
    assert data["history"] == []
    # Reading does not invent a chat.
    assert store.load("myproj").chats == []


@pytest.mark.asyncio
async def test_clearing_without_a_chat_is_refused(tmp_path, monkeypatch):
    """An empty chat id must never reach the store: it would erase every chat."""
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))

    await server.route_api(
        "POST", "/api/chat/clear", {}, {},
        json.dumps({"project_name": "myproj"}).encode("utf-8"), AsyncMock(),
    )

    assert responses[0][0] == 404


@pytest.mark.asyncio
async def test_chat_history_names_the_chat_it_answered_for(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "second", "Second Chat")
    second = store.load("myproj", chat_id="second")
    store.append_message(second, "user", "message in second")

    await server.route_api(
        "GET",
        "/api/chat/history",
        {"project_name": ["myproj"], "chat_id": ["second"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["chat_id"] == "second"
    assert [m["content"] for m in data["history"]] == ["message in second"]


@pytest.mark.asyncio
async def test_chat_history_serves_the_occupancy_stored_for_that_chat(tmp_path, monkeypatch):
    """Reopening a chat must report its measured window, not an empty one.

    The in-process measurement holds a single scope, so running any other
    conversation (or restarting the server) leaves nothing for this chat. The
    endpoint then answered ``context_usage: null`` and the panel fell back to the
    character estimate, showing a full battery for a nearly full window.
    """
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "second", "Second Chat")
    store.save_chat_context_usage("myproj", "second", {
        "prompt_tokens": 34_500,
        "completion_tokens": 800,
        "total_tokens": 35_300,
        "context_window": 1_000_000,
    })

    # Another conversation owns the in-process slot, exactly as it would after
    # sending a message in a different chat.
    from gilzabots import token_usage
    token_usage.set_context_scope(token_usage.context_scope_key("/elsewhere", "other"))
    token_usage.record_context_tokens(999)

    try:
        await server.route_api(
            "GET",
            "/api/chat/history",
            {"project_name": ["myproj"], "chat_id": ["second"]},
            {},
            b"",
            AsyncMock(),
        )
    finally:
        token_usage.set_context_scope("reset")

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["context_usage"]["prompt_tokens"] == 34_500
    assert data["context_usage"]["context_window"] == 1_000_000


@pytest.mark.asyncio
async def test_chat_history_reports_no_occupancy_for_an_unmeasured_chat(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "second", "Second Chat")

    await server.route_api(
        "GET",
        "/api/chat/history",
        {"project_name": ["myproj"], "chat_id": ["second"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["context_usage"] is None


@pytest.mark.asyncio
async def test_chat_list_is_newest_first_and_opens_the_most_recent(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "first", "First Chat")
    store.create_chat("myproj", "second", "Second Chat")

    await server.route_api(
        "GET",
        "/api/chat/list",
        {"project_name": ["myproj"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert "main_chat_id" not in data
    assert [c["id"] for c in data["chats"]] == ["second", "first"]
    assert data["current_chat_id"] == "second"


@pytest.mark.asyncio
async def test_chat_list_of_a_project_without_chats_opens_none(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))

    await server.route_api(
        "GET", "/api/chat/list", {"project_name": ["myproj"]}, {}, b"", AsyncMock(),
    )

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["chats"] == []
    assert data["current_chat_id"] == ""


@pytest.mark.asyncio
async def test_chat_delete_accepts_the_last_chat(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "only", "Only Chat")

    await server.route_api(
        "POST",
        "/api/chat/delete",
        {},
        {},
        json.dumps({"project_name": "myproj", "chat_id": "only"}).encode("utf-8"),
        AsyncMock(),
    )

    status_code, _data, _ = responses[0]
    assert status_code == 200
    assert store.load("myproj").chats == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["first", "second"])
async def test_chat_history_reports_the_reasoning_of_each_answer(tmp_path, monkeypatch, chat_id):
    """Answer tokens must never displace persisted reasoning on chat reload.

    The transcript no longer carries the reasoning itself -- it is one row per
    streamed token, and a long chat shipped tens of megabytes of it on every
    open for text that starts collapsed. What it carries is where each answer's
    reasoning is, and /api/chat/thoughts reads it when the panel is expanded.
    Nothing is dropped and nothing is capped: that is what this guards.
    """
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "first", "First Chat")
    store.create_chat("myproj", "second", "Second Chat")
    project = store.load("myproj", chat_id=chat_id)
    store.append_message(project, "user", "Inspect the document.")
    store.append_activity(project, "thought", "Inspecting the document.")
    for _ in range(1001):
        store.append_activity(project, "stream_chunk", "answer token ")
    store.append_message(project, "assistant", "Document inspected.")
    other_id = "second" if chat_id == "first" else "first"
    other = store.load("myproj", chat_id=other_id)
    store.append_activity(other, "thought", "Another chat's thought.")
    store.close_activity_connection()

    # Bounded diagnostic callers keep their existing contract.
    assert len(store.list_activity("myproj", chat_id)) == 1000
    for _ in range(2):
        await server.route_api(
            "GET", "/api/chat/history",
            {"project_name": ["myproj"], "chat_id": [chat_id]},
            {}, b"", AsyncMock(),
        )
        status, data, _ = responses[-1]
        assert status == 200
        assert data["chat_id"] == chat_id
        # The reasoning is announced, not shipped.
        assert not [a for a in data["activity"] if a["event"] == "thought"]
        assert len(data["activity"]) == 1001
        windows = data["thought_windows"]
        assert [w["count"] for w in windows] == [1]
        assert data["history"][windows[0]["index"]]["role"] == "assistant"
        assert data["history"][-1]["content"] == "Document inspected."

        await server.route_api(
            "GET", "/api/chat/thoughts",
            {
                "project_name": ["myproj"], "chat_id": [chat_id],
                "from_id": [str(windows[0]["from_id"])], "to_id": [str(windows[0]["to_id"])],
            },
            {}, b"", AsyncMock(),
        )
        status, tail, _ = responses[-1]
        assert status == 200
        # This chat's reasoning, and only this chat's.
        assert tail["content"] == "Inspecting the document."


@pytest.mark.asyncio
async def test_the_token_stream_cap_can_never_reach_the_reasoning(tmp_path, monkeypatch):
    """The cap that protects memory must never be able to reach a thought.

    `stream_chunk` is written once per token, so a long-lived chat accumulates
    tens of thousands of rows that an unbounded read would load and serialize on
    every open. Capping them is safe; losing reasoning is the bug this guards --
    now against a different mechanism, since the reasoning is fetched per
    message instead of being capped alongside the stream.
    """
    from gilzabots.ide_server import CHAT_HISTORY_STREAM_CHUNK_LIMIT

    store, server, responses = _api_harness(tmp_path, monkeypatch)
    project = store.create("myproj", "plan", "fake/model", project_path=str(tmp_path / "project"))
    store.create_chat("myproj", "chat-myproj", "Chat")
    project = store.load("myproj")
    chat_id = project.current_chat_id

    store.append_message(project, "user", "Do it.")
    store.append_activity(project, "thought", "First thought.")
    for _ in range(CHAT_HISTORY_STREAM_CHUNK_LIMIT + 250):
        store.append_activity(project, "stream_chunk", "token ")
    store.append_activity(project, "thought", "Last thought.")
    store.append_activity(project, "error", "Something failed.")
    store.append_message(project, "assistant", "Done.")
    store.close_activity_connection()

    await server.route_api(
        "GET", "/api/chat/history",
        {"project_name": ["myproj"], "chat_id": [chat_id]},
        {}, b"", AsyncMock(),
    )
    status, data, _ = responses[-1]
    assert status == 200

    activity = data["activity"]
    # The error still reaches the transcript; the token stream is capped.
    assert [a["content"] for a in activity if a["event"] == "error"] == ["Something failed."]
    assert len([a for a in activity if a["event"] == "stream_chunk"]) == CHAT_HISTORY_STREAM_CHUNK_LIMIT
    assert [a["id"] for a in activity] == sorted(a["id"] for a in activity)

    # Both thoughts survive in full, despite being on opposite sides of the
    # 5250 stream chunks that the cap cut through.
    window = data["thought_windows"][0]
    assert window["count"] == 2
    await server.route_api(
        "GET", "/api/chat/thoughts",
        {
            "project_name": ["myproj"], "chat_id": [chat_id],
            "from_id": [str(window["from_id"])], "to_id": [str(window["to_id"])],
        },
        {}, b"", AsyncMock(),
    )
    _status, tail, _ = responses[-1]
    assert tail["content"] == "First thought.Last thought."
