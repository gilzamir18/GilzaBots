"""A chat id either names a stored chat or it is an error.

The panel used to reopen showing one chat in the selector while the body held a
different conversation: the client persisted the literal ``"main"`` -- an id no
chat ever carries -- and the store answered it with whatever chat had been
created last. These tests pin both halves of that contract: the default chat is
resolved explicitly (the most recently created one, or none), and an unknown id
fails instead of substituting.

There is no "Main Chat": a project starts with no chat, any chat can be
deleted, and a project with no chat opens none.
"""
import pytest

from gilzabots.project import ChatNotFoundError, ProjectData, ProjectStore, _conn


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(db_path=str(tmp_path / "projects.db"))


def _set_created_at(store, chat_id, created_at):
    with _conn(store.db_path) as conn:
        conn.execute(
            "UPDATE project_chats SET created_at = ? WHERE project = ? AND id = ?",
            (created_at, "myproj", chat_id),
        )


def _project_with_two_chats(store):
    project = store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "first", "First Chat")
    _set_created_at(store, "first", "2026-10-20T10:00:00.000000+00:00")
    store.create_chat("myproj", "second", "Second Chat")
    _set_created_at(store, "second", "2026-10-20T11:00:00.000000+00:00")

    first = store.load("myproj", chat_id="first")
    store.append_message(first, "user", "message in first")
    second = store.load("myproj", chat_id="second")
    store.append_message(second, "user", "message in second")
    return project


def test_created_project_has_no_chat(store):
    project = store.create("myproj", "chat", "gpt-4")

    assert project.chats == []
    assert project.current_chat_id == ""

    loaded = store.load("myproj")
    assert loaded.chats == []
    assert loaded.current_chat_id == ""
    assert loaded.history == []


def test_load_without_chat_id_resolves_to_the_most_recent_chat(store):
    _project_with_two_chats(store)

    loaded = store.load("myproj")

    assert loaded.current_chat_id == "second"
    assert [m["content"] for m in loaded.history] == ["message in second"]


def test_load_with_unknown_chat_id_raises_instead_of_serving_another_chat(store):
    _project_with_two_chats(store)

    with pytest.raises(ChatNotFoundError) as excinfo:
        store.load("myproj", chat_id="main")

    assert "main" in str(excinfo.value)
    assert "myproj" in str(excinfo.value)


def test_load_with_deleted_chat_id_raises(store):
    _project_with_two_chats(store)
    store.delete_chat("myproj", "second")

    with pytest.raises(ChatNotFoundError):
        store.load("myproj", chat_id="second")


def test_load_with_explicit_chat_id_returns_that_chats_history(store):
    _project_with_two_chats(store)

    loaded = store.load("myproj", chat_id="first")

    assert loaded.current_chat_id == "first"
    assert [m["content"] for m in loaded.history] == ["message in first"]


def test_deleting_the_open_chat_falls_back_to_the_next_most_recent(store):
    _project_with_two_chats(store)

    store.delete_chat("myproj", "second")
    loaded = store.load("myproj")

    assert loaded.current_chat_id == "first"
    assert [m["content"] for m in loaded.history] == ["message in first"]


def test_the_last_chat_can_be_deleted(store):
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "only-one", "Only Chat")

    store.delete_chat("myproj", "only-one")

    loaded = store.load("myproj")
    assert loaded.chats == []
    assert loaded.current_chat_id == ""


def test_load_does_not_invent_a_chat_for_a_project_without_one(store):
    store.create("myproj", "chat", "gpt-4")

    store.load("myproj")

    with _conn(store.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM project_chats WHERE project = ?", ("myproj",)
        ).fetchone()[0]
    assert count == 0


def test_project_data_built_outside_the_store_has_no_chat():
    project = ProjectData(name="ephemeral")

    assert project.current_chat_id == ""


def test_writes_without_an_open_chat_fail(store):
    project = store.create("myproj", "chat", "gpt-4")

    with pytest.raises(ChatNotFoundError, match="no open chat"):
        store.append_message(project, "user", "lost")
    with pytest.raises(ChatNotFoundError, match="no open chat"):
        store.append_activity(project, "thought", "lost")

    with _conn(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_history").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_turn_without_an_open_chat_fails_before_running(store, monkeypatch):
    """The front-ends create a chat before the first message; a turn without one is refused."""
    import gilzabots.agent_stdin as stdin_mod
    from gilzabots.i18n import _

    project = store.create("myproj", "auto", "ollama/qwen3.5:cloud")
    events = []
    ran = []

    class FakeMemGPT:
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        model = "ollama/qwen3.5:cloud"

        async def run(self, agent_input):
            ran.append(agent_input.prompt)

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())

    await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "hello"})

    assert ran == []
    assert [data["message"] for event, data in events if event == "error"] == [_("no_chat_open")]
    assert [event for event, _data in events if event == "agent_finished"]
    assert store.load("myproj").chats == []


def _capture_stdin_run(monkeypatch, store):
    import gilzabots.agent_stdin as stdin_mod

    events, runs = [], []

    async def fake_run(data):
        runs.append((data, stdin_mod.current_project))

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "handle_run", fake_run)
    monkeypatch.setattr(stdin_mod, "build_chat_orchestrator", lambda project, store: "rebuilt")
    return stdin_mod, events, runs


@pytest.mark.asyncio
async def test_a_first_stdin_run_naming_the_project_starts_a_chat(store, monkeypatch):
    """The stdin front-end starts a chat with the first message, as the window and the REPL do."""
    from gilzabots.i18n import _

    store.create("myproj", "auto", "ollama/qwen3.5:cloud")
    stdin_mod, events, runs = _capture_stdin_run(monkeypatch, store)

    await stdin_mod.handle_stdin_run({"command": "run", "prompt": "hello", "project_name": "myproj", "db": store.db_path})

    chats = store.load("myproj").chats
    assert [c["name"] for c in chats] == [_("default_chat_name")]
    assert runs[0][0]["chat_id"] == chats[0]["id"]
    assert ("chat_created", {"project_name": "myproj", "chat_id": chats[0]["id"], "name": chats[0]["name"]}) in events


@pytest.mark.asyncio
async def test_a_first_stdin_run_after_load_project_moves_the_loaded_project_to_the_new_chat(store, monkeypatch):
    store.create("myproj", "auto", "ollama/qwen3.5:cloud")
    loaded = store.load("myproj")
    loaded.model = "override/kept-in-memory"
    stdin_mod, _events, runs = _capture_stdin_run(monkeypatch, store)
    monkeypatch.setattr(stdin_mod, "current_project", loaded)
    monkeypatch.setattr(stdin_mod, "current_store", store)

    await stdin_mod.handle_stdin_run({"command": "run", "prompt": "hello"})

    chat_id = store.load("myproj").chats[0]["id"]
    _data, project = runs[0]
    assert project is loaded
    assert loaded.current_chat_id == chat_id
    assert loaded.model == "override/kept-in-memory"
    assert stdin_mod.current_memgpt == "rebuilt"


@pytest.mark.asyncio
async def test_a_stdin_run_on_a_project_with_a_chat_creates_none(store, monkeypatch):
    store.create("myproj", "auto", "ollama/qwen3.5:cloud")
    store.create_chat("myproj", "existing", "Existing")
    stdin_mod, events, runs = _capture_stdin_run(monkeypatch, store)

    await stdin_mod.handle_stdin_run({"command": "run", "prompt": "hello", "project_name": "myproj", "db": store.db_path})
    await stdin_mod.handle_stdin_run({
        "command": "run", "prompt": "", "resume_interrupted": True,
        "project_name": "myproj", "db": store.db_path,
    })

    assert [c["id"] for c in store.load("myproj").chats] == ["existing"]
    assert "chat_id" not in runs[0][0]
    assert not [e for e, _d in events if e == "chat_created"]


@pytest.mark.asyncio
async def test_a_stdin_run_on_an_unregistered_project_names_the_cause(store, monkeypatch):
    """The history tables reference `projects`: such a turn would die on its first write."""
    from gilzabots.i18n import _

    stdin_mod, events, runs = _capture_stdin_run(monkeypatch, store)

    await stdin_mod.handle_stdin_run({"command": "run", "prompt": "hello", "project_name": "ghost", "db": store.db_path})

    assert runs == []
    assert [d["message"] for e, d in events if e == "error"] == [_("project_not_registered", name="ghost")]
    assert [e for e, _d in events if e == "agent_finished"]
    assert store.load("ghost") is None
