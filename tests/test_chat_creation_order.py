"""A project's chats are listed newest first.

The front-ends open a project on the first chat of this list, so the order is a
contract, not a presentation detail. Chats synced from another device keep the
``created_at`` they were made with and can arrive after newer local ones; they
must still sort by when they were created. Chats created at the same instant
are listed with the later insert first.
"""
import pytest

from gilzabots.project import ProjectStore, _conn


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(db_path=str(tmp_path / "projects.db"))


def _set_created_at(store, chat_id, created_at):
    with _conn(store.db_path) as conn:
        conn.execute(
            "UPDATE project_chats SET created_at = ? WHERE project = ? AND id = ?",
            (created_at, "myproj", chat_id),
        )


def _chat_ids(store):
    return [c["id"] for c in store.load("myproj").chats]


def test_chats_are_listed_newest_first(store):
    store.create("myproj", "chat", "gpt-4")
    for chat_id, created_at in (
        ("first", "2026-10-20T10:00:00.000000+00:00"),
        ("second", "2026-10-20T11:00:00.000000+00:00"),
        ("third", "2026-10-20T12:00:00.000000+00:00"),
    ):
        store.create_chat("myproj", chat_id, chat_id.title())
        _set_created_at(store, chat_id, created_at)

    assert _chat_ids(store) == ["third", "second", "first"]


def test_a_chat_that_arrives_later_sorts_by_when_it_was_created(store):
    store.create("myproj", "chat", "gpt-4")
    store.create_chat("myproj", "old", "Old")
    _set_created_at(store, "old", "2026-10-20T10:00:00.000000+00:00")
    store.create_chat("myproj", "local", "Local")
    _set_created_at(store, "local", "2026-10-20T19:00:00.000000+00:00")
    # Inserted last, but made earlier on another device.
    store.create_chat("myproj", "synced", "Synced")
    _set_created_at(store, "synced", "2026-10-20T18:00:00.000000+00:00")

    assert _chat_ids(store) == ["local", "synced", "old"]


def test_chats_created_at_the_same_instant_list_the_later_insert_first(store):
    store.create("myproj", "chat", "gpt-4")
    for chat_id in ("b", "a", "c"):
        store.create_chat("myproj", chat_id, chat_id.upper())
        _set_created_at(store, chat_id, "2026-10-21T12:00:00.000000+00:00")

    assert _chat_ids(store) == ["c", "a", "b"]
