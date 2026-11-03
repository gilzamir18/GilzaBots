"""Portable export/merge of a project's conversations.

The global `sessions.db` holds every project's chats, so it can never be
mirrored as a file. These tests cover the document that stands in for it.
"""

import json
import os
import sqlite3

import pytest

from gilzabots.cloud import chats
from gilzabots.project import ProjectStore


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "sessions.db")


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return str(path)


def make_project(db_path, name, project_path, messages=(), activity=()):
    store = ProjectStore(db_path)
    project = store.create(name, "plan", "ollama/test", project_name=name, project_path=project_path)
    store.create_chat(name, f"chat-{name}", "Chat")
    project = store.load(name)
    for role, content in messages:
        store.append_message(project, role, content)
    for event, content in activity:
        store.append_activity(name, project.current_chat_id, event, content=content)
    store.close_activity_connection()
    return store, project


def test_export_carries_only_the_named_project(db, project_dir, tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    make_project(db, "alpha", project_dir, messages=[("user", "alpha message")])
    make_project(db, "beta", str(other_dir), messages=[("user", "beta message")])

    payload = chats.export_project("alpha", db)

    contents = [m["content"] for m in payload["messages"]]
    assert "alpha message" in contents
    assert "beta message" not in contents


def test_export_is_deterministic(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one"), ("assistant", "two")])

    first = json.dumps(chats.export_project("alpha", db), sort_keys=True)
    second = json.dumps(chats.export_project("alpha", db), sort_keys=True)

    assert first == second


def test_write_export_reports_no_change_when_content_is_identical(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one")])
    payload = chats.export_project("alpha", db)

    assert chats.write_export(project_dir, payload) is True
    # Rewriting identical bytes would bump the mtime and make the scanner
    # re-upload the file on every pass.
    assert chats.write_export(project_dir, payload) is False


def test_export_never_carries_machine_local_paths(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one")])

    payload = chats.export_project("alpha", db)

    assert "project_path" not in payload.get("settings", {})
    assert "git_root_path" not in payload.get("settings", {})
    assert project_dir not in json.dumps(payload)


def test_merge_adds_messages_from_another_machine(db, project_dir, tmp_path):
    make_project(db, "alpha", project_dir, messages=[("user", "shared")])
    payload = chats.export_project("alpha", db)
    payload["messages"].append(
        {
            "chat_id": payload["messages"][0]["chat_id"],
            "timestamp": "2030-01-01T00:00:00+00:00",
            "role": "user",
            "content": "from the laptop",
            "client_message_id": "laptop-1",
            "attachments": "[]",
        }
    )

    stats = chats.merge_into_database("alpha", payload, db)

    assert stats.messages_added == 1
    with sqlite3.connect(db) as conn:
        rows = [r[0] for r in conn.execute("SELECT content FROM project_history WHERE project='alpha'")]
    assert "from the laptop" in rows
    assert "shared" in rows


def test_merge_is_idempotent(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one"), ("assistant", "two")])
    payload = chats.export_project("alpha", db)

    first = chats.merge_into_database("alpha", payload, db)
    second = chats.merge_into_database("alpha", payload, db)

    assert first.messages_added == 0
    assert second.messages_added == 0
    with sqlite3.connect(db) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM project_history WHERE project='alpha'"
        ).fetchone()
    assert count == 2


def test_merge_deduplicates_by_content_when_no_client_id(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "hello")])
    payload = chats.export_project("alpha", db)
    # Same message arriving from the other machine without a client id: it must
    # not be inserted a second time.
    duplicate = dict(payload["messages"][0])
    duplicate["client_message_id"] = ""
    payload["messages"].append(duplicate)

    stats = chats.merge_into_database("alpha", payload, db)

    with sqlite3.connect(db) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM project_history WHERE project='alpha'"
        ).fetchone()
    assert count == 1
    assert stats.messages_added == 0


def test_merge_adds_a_chat_created_elsewhere(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one")])
    payload = chats.export_project("alpha", db)
    payload["chats"].append(
        {"id": "chat_from_laptop", "name": "Laptop chat", "created_at": "2030-01-01T00:00:00+00:00"}
    )
    payload["messages"].append(
        {
            "chat_id": "chat_from_laptop",
            "timestamp": "2030-01-01T00:00:01+00:00",
            "role": "user",
            "content": "written on the laptop",
            "client_message_id": "laptop-2",
            "attachments": "[]",
        }
    )

    stats = chats.merge_into_database("alpha", payload, db)

    assert stats.chats_added == 1
    assert stats.messages_added == 1
    with sqlite3.connect(db) as conn:
        rows = [
            r[0]
            for r in conn.execute(
                "SELECT content FROM project_history WHERE chat_id='chat_from_laptop'"
            )
        ]
    assert rows == ["written on the laptop"]


def test_merge_skips_a_message_whose_chat_is_unknown(db, project_dir):
    # A message referring to a chat that neither exists locally nor arrives in
    # the export would be invisible in every chat; dropping it is better than
    # hiding it.
    make_project(db, "alpha", project_dir, messages=[("user", "one")])
    payload = chats.export_project("alpha", db)
    payload["messages"].append(
        {
            "chat_id": "chat_that_does_not_exist",
            "timestamp": "2030-01-01T00:00:00+00:00",
            "role": "user",
            "content": "orphan",
            "client_message_id": "orphan-1",
            "attachments": "[]",
        }
    )

    stats = chats.merge_into_database("alpha", payload, db)

    assert stats.messages_added == 0


def test_merge_refuses_a_newer_format(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one")])

    stats = chats.merge_into_database("alpha", {"version": 999, "messages": []}, db)

    assert stats.messages_added == 0
    assert stats.notes


def test_merge_does_nothing_for_an_unknown_project(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "one")])
    payload = chats.export_project("alpha", db)

    stats = chats.merge_into_database("does-not-exist", payload, db)

    assert not stats.changed
    assert stats.notes


def test_newer_settings_win_older_ones(db, project_dir):
    store, _ = make_project(db, "alpha", project_dir)
    payload = chats.export_project("alpha", db)
    payload["updated_at"] = "2999-01-01T00:00:00+00:00"
    payload["settings"]["description"] = "written on the other machine"

    stats = chats.merge_into_database("alpha", payload, db)

    assert stats.settings_applied
    with sqlite3.connect(db) as conn:
        (description,) = conn.execute(
            "SELECT description FROM projects WHERE name='alpha'"
        ).fetchone()
    assert description == "written on the other machine"


def test_older_settings_do_not_overwrite_newer_ones(db, project_dir):
    make_project(db, "alpha", project_dir)
    payload = chats.export_project("alpha", db)
    payload["updated_at"] = "1999-01-01T00:00:00+00:00"
    payload["settings"]["description"] = "stale"

    stats = chats.merge_into_database("alpha", payload, db)

    assert not stats.settings_applied
    with sqlite3.connect(db) as conn:
        (description,) = conn.execute(
            "SELECT description FROM projects WHERE name='alpha'"
        ).fetchone()
    assert description != "stale"


def test_round_trip_through_disk(db, project_dir):
    make_project(db, "alpha", project_dir, messages=[("user", "on disk")])

    assert chats.refresh_export("alpha", project_dir, db) is True
    assert os.path.isfile(chats.export_path(project_dir))

    stats = chats.apply_export("alpha", project_dir, db)

    assert stats.messages_added == 0  # already present locally
