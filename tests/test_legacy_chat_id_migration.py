"""Pre-multi-chat rows must end up in the chat older builds created for them.

``chat_id`` was added to ``project_history`` and ``project_activity`` with
``DEFAULT 'main'`` and never backfilled. Since no chat is stored under that id,
every conversation written before multi-chat support became unreachable: the
read paths all scope by a real chat id. The migration moves those rows once,
into the "Main Chat" (`main_<project>`) those builds created with every project,
recreating it when it is gone -- new projects are created without any chat.
"""
import sqlite3

import pytest

from gilzabots.project import (
    LEGACY_MAIN_CHAT_ID,
    LEGACY_MAIN_CHAT_NAME,
    ProjectStore,
    legacy_main_chat_id,
)


def _legacy_rows(db_path, table, project):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE project = ? AND chat_id = ?",
            (project, LEGACY_MAIN_CHAT_ID),
        ).fetchone()[0]


def _write_legacy_rows(db_path, project, *, messages=(), activity=(), client_message_ids=None):
    """Insert rows exactly as the pre-multi-chat schema left them."""
    client_message_ids = client_message_ids or {}
    with sqlite3.connect(db_path) as conn:
        for content in messages:
            conn.execute(
                "INSERT INTO project_history (project, chat_id, timestamp, role, content, client_message_id)"
                " VALUES (?,?,?,?,?,?)",
                (project, LEGACY_MAIN_CHAT_ID, "2024-01-01T00:00:00Z", "user", content,
                 client_message_ids.get(content, "")),
            )
        for content in activity:
            conn.execute(
                "INSERT INTO project_activity (project, chat_id, timestamp, event, agent, content)"
                " VALUES (?,?,?,?,?,?)",
                (project, LEGACY_MAIN_CHAT_ID, "2024-01-01T00:00:00Z", "thought", "orchestrator", content),
            )


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "projects.db")


def test_legacy_history_is_reattached_to_the_legacy_main_chat(db_path):
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    _write_legacy_rows(db_path, "myproj", messages=["old question", "old answer"],
                       activity=["old thought"])

    # A fresh store re-runs the schema init, which carries the migration.
    store = ProjectStore(db_path=db_path)
    loaded = store.load("myproj")

    assert [m["content"] for m in loaded.history] == ["old question", "old answer"]
    assert [a["content"] for a in store.list_activity("myproj", legacy_main_chat_id("myproj"))] == ["old thought"]
    assert _legacy_rows(db_path, "project_history", "myproj") == 0
    assert _legacy_rows(db_path, "project_activity", "myproj") == 0


def test_migration_is_idempotent_and_does_not_duplicate(db_path):
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    _write_legacy_rows(db_path, "myproj", messages=["old question"])

    for _ in range(3):
        store = ProjectStore(db_path=db_path)

    assert [m["content"] for m in store.load("myproj").history] == ["old question"]


def test_legacy_rows_do_not_leak_into_other_chats(db_path):
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "second", "Second Chat")
    second = store.load("myproj", chat_id="second")
    store.append_message(second, "user", "message in second")
    _write_legacy_rows(db_path, "myproj", messages=["legacy message"])

    store = ProjectStore(db_path=db_path)

    legacy = store.load("myproj", chat_id=legacy_main_chat_id("myproj"))
    assert [m["content"] for m in legacy.history] == ["legacy message"]
    assert [m["content"] for m in store.load("myproj", chat_id="second").history] == ["message in second"]


def test_each_project_keeps_its_own_legacy_rows(db_path):
    store = ProjectStore(db_path=db_path)
    store.create("alpha", "chat", "gpt-4")
    store.create("beta", "chat", "gpt-4")
    _write_legacy_rows(db_path, "alpha", messages=["alpha history"])
    _write_legacy_rows(db_path, "beta", messages=["beta history"])

    store = ProjectStore(db_path=db_path)

    assert [m["content"] for m in store.load("alpha").history] == ["alpha history"]
    assert [m["content"] for m in store.load("beta").history] == ["beta history"]


def test_a_project_owning_a_chat_named_main_is_left_untouched(db_path):
    """Not a legacy project: those rows belong to a chat that really exists."""
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", LEGACY_MAIN_CHAT_ID, "Literally Main")
    real_main = store.load("myproj", chat_id=LEGACY_MAIN_CHAT_ID)
    store.append_message(real_main, "user", "belongs to the 'main' chat")

    store = ProjectStore(db_path=db_path)

    assert [m["content"] for m in store.load("myproj", chat_id=LEGACY_MAIN_CHAT_ID).history] == [
        "belongs to the 'main' chat"
    ]
    assert [c["id"] for c in store.load("myproj").chats] == [LEGACY_MAIN_CHAT_ID]


def test_legacy_main_chat_is_recreated_when_it_is_gone(db_path):
    """The chat may have been deleted, or the project never had one."""
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "second", "Second Chat")
    _write_legacy_rows(db_path, "myproj", messages=["orphaned history"])

    store = ProjectStore(db_path=db_path)
    loaded = store.load("myproj", chat_id=legacy_main_chat_id("myproj"))

    assert [m["content"] for m in loaded.history] == ["orphaned history"]
    # Restored with the project's own creation time: it holds the oldest
    # conversation, so it is listed last.
    assert [c["id"] for c in loaded.chats] == ["second", legacy_main_chat_id("myproj")]
    assert loaded.chats[1]["name"] == LEGACY_MAIN_CHAT_NAME


def test_a_colliding_client_message_id_does_not_abort_the_move(db_path):
    """The unique index is per (project, chat, client_message_id).

    A legacy row reusing an id already present in the main chat would abort the
    UPDATE; the message must survive, only its send-dedup marker is dropped.
    """
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    # The chat an older build created with the project, already in use.
    store.create_chat("myproj", legacy_main_chat_id("myproj"), LEGACY_MAIN_CHAT_NAME)
    project = store.load("myproj")
    store.append_message(project, "user", "current message", client_message_id="cid-1")
    _write_legacy_rows(db_path, "myproj", messages=["legacy message"],
                       client_message_ids={"legacy message": "cid-1"})

    store = ProjectStore(db_path=db_path)
    loaded = store.load("myproj")

    assert sorted(m["content"] for m in loaded.history) == ["current message", "legacy message"]
    assert _legacy_rows(db_path, "project_history", "myproj") == 0


def test_projects_without_legacy_rows_are_unchanged(db_path):
    store = ProjectStore(db_path=db_path)
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "modern", "Modern Chat")
    project = store.load("myproj")
    store.append_message(project, "user", "modern message", client_message_id="cid-1")

    store = ProjectStore(db_path=db_path)
    loaded = store.load("myproj")

    assert [m["content"] for m in loaded.history] == ["modern message"]
    assert [m["client_message_id"] for m in loaded.history] == ["cid-1"]
