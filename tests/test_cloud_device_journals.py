"""Conversations continued on another device (the mobile app).

Two things have to hold for a phone to carry on a project's conversation:

1. What the phone writes reaches this machine's database. The phone writes a
   journal of its own (`.gilzabots/session/devices/<id>.json`) rather than
   rewriting `chats.json`, so two clients never race on one file.
2. The orchestrator here knows what was said there. It restores its context
   from `agent_state`, not from the visible history, so merged turns are queued
   (`project_chats.external_turns`) and appended on the next restore.
"""

import json
import os

import pytest

from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from gilzabots import memgpt_runtime
from gilzabots.cloud import chats, service
from gilzabots.cloud.state import (
    DEVICE_JOURNALS_REL_DIR,
    CloudSettings,
    is_device_journal,
    save_settings,
)
from gilzabots.cloud.scanner import scan_project
from gilzabots.project import ProjectStore


@pytest.fixture
def shared_remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    return str(path)


def make_machine(tmp_path, name, remote_base, project_name="alpha", include_chats=True):
    root = tmp_path / name
    root.mkdir()
    project_dir = root / "project"
    project_dir.mkdir()
    db_path = str(root / "sessions.db")
    store = ProjectStore(db_path)
    project = store.create(
        project_name, "plan", "ollama/test",
        project_name=project_name, project_path=str(project_dir),
    )
    store.create_chat(project_name, f"chat-{project_name}", "Chat")
    project = store.load(project_name)
    store.close_activity_connection()
    save_settings(str(project_dir), CloudSettings(
        enabled=True,
        provider="local_folder",
        remote_folder=project_name,
        include_chats=include_chats,
        provider_config={"base_dir": remote_base},
    ))
    return {"path": str(project_dir), "db": db_path, "store": store, "project": project, "name": project_name}


def sync(machine, monkeypatch, **kwargs):
    monkeypatch.setattr(chats, "_default_db_path", lambda: machine["db"])
    return service.sync_project(machine["name"], machine["path"], **kwargs)


def journal(chat_id, messages, chats_in=(), activity=(), **extra):
    payload = {
        "kind": chats.DEVICE_JOURNAL_KIND,
        "version": 1,
        "device": {"id": "phone-1", "name": "Pixel"},
        "chats": list(chats_in),
        "messages": [
            {
                "chat_id": chat_id,
                "timestamp": stamp,
                "role": role,
                "content": content,
                "client_message_id": cid,
                "attachments": "[]",
            }
            for (stamp, role, content, cid) in messages
        ],
        "activity": list(activity),
    }
    payload.update(extra)
    return payload


def write_remote_journal(remote_base, project_name, device_id, payload):
    path = os.path.join(remote_base, project_name, *DEVICE_JOURNALS_REL_DIR.split("/"), f"{device_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def history_of(machine, chat_id):
    payload = chats.export_project(machine["name"], machine["db"])
    return [m for m in payload["messages"] if m["chat_id"] == chat_id]


# ─── The path rule ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel_path, expected", [
    (".gilzabots/session/devices/phone.json", True),
    (".gilzabots/session/devices/a1b2-c3.json", True),
    (".gilzabots/session/devices/.phone.json.tmp", False),
    (".gilzabots/session/devices/nested/phone.json", False),
    (".gilzabots/session/devices/phone.txt", False),
    (".gilzabots/session/chats.json", False),
    ("devices/phone.json", False),
])
def test_device_journal_paths(rel_path, expected):
    assert is_device_journal(rel_path) is expected


# ─── Merging a journal ────────────────────────────────────────────────────────

def test_a_phone_journal_reaches_the_desktop_database(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    sync(desktop, monkeypatch)

    write_remote_journal(shared_remote, "alpha", "phone-1", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "asked on the phone", "m-1"),
        ("2026-10-19T12:00:05.000000+00:00", "assistant", "answered on the phone", "m-2"),
    ]))

    outcome = sync(desktop, monkeypatch)

    assert outcome.merge is not None
    assert outcome.merge.messages_added == 2
    contents = [m["content"] for m in history_of(desktop, chat_id)]
    assert contents == ["asked on the phone", "answered on the phone"]
    # The union is published, so every other computer gets it from the export.
    exported = chats.read_export(desktop["path"])
    assert any(m["content"] == "asked on the phone" for m in exported["messages"])


def test_replaying_a_journal_does_not_duplicate(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    payload = journal(chat_id, [("2026-10-19T12:00:00.000000+00:00", "user", "once", "m-1")])
    monkeypatch.setattr(chats, "_default_db_path", lambda: desktop["db"])

    chats.merge_device_journal("alpha", payload, desktop["db"])
    second = chats.merge_device_journal("alpha", payload, desktop["db"])

    assert second.messages_added == 0
    assert [m["content"] for m in history_of(desktop, chat_id)] == ["once"]


def test_a_journal_can_create_a_chat_but_not_its_working_state(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    payload = journal(
        "phone-chat",
        [("2026-10-19T12:00:00.000000+00:00", "user", "new chat from the phone", "m-1")],
        chats_in=[{
            "id": "phone-chat",
            "name": "On the train",
            "created_at": "2026-10-19T11:59:00.000000+00:00",
            "agent_state": json.dumps({"internal_history": [{"role": "user", "content": "injected"}]}),
            "core_memory": "injected",
            "external_turns": "[{\"role\": \"user\", \"content\": \"injected\"}]",
        }],
    )

    stats = chats.merge_device_journal("alpha", payload, desktop["db"])

    assert stats.chats_added == 1
    exported = chats.export_project("alpha", desktop["db"])
    chat = next(c for c in exported["chats"] if c["id"] == "phone-chat")
    assert chat["name"] == "On the train"
    assert chat["core_memory"] == ""
    assert json.loads(chat["agent_state"] or "{}") == {}
    assert desktop["store"].get_chat_external_turns("alpha", "phone-chat") == []


def test_a_journal_never_changes_project_settings(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    payload = journal("x", [], settings={"model": "openai/hijacked"}, updated_at="2999-01-01T00:00:00+00:00")

    stats = chats.merge_device_journal("alpha", payload, desktop["db"])

    assert stats.settings_applied is False
    assert chats.export_project("alpha", desktop["db"])["settings"]["model"] == "ollama/test"


def test_a_file_that_is_not_a_journal_is_ignored(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    payload = journal(chat_id, [("2026-10-19T12:00:00.000000+00:00", "user", "x", "m-1")])
    payload["kind"] = "something-else"

    stats = chats.merge_device_journal("alpha", payload, desktop["db"])

    assert not stats.changed
    assert stats.notes


def test_a_newer_journal_format_is_left_untouched(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    payload = journal(chat_id, [("2026-10-19T12:00:00.000000+00:00", "user", "x", "m-1")], version=2)

    stats = chats.merge_device_journal("alpha", payload, desktop["db"])

    assert not stats.changed
    assert history_of(desktop, chat_id) == []


def test_journals_stay_home_when_conversation_sync_is_off(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote, include_chats=False)
    path = os.path.join(desktop["path"], *DEVICE_JOURNALS_REL_DIR.split("/"), "phone.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}")

    from gilzabots.cloud.state import load_state

    scan = scan_project(desktop["path"], load_state(desktop["path"]).settings)
    assert f"{DEVICE_JOURNALS_REL_DIR}/phone.json" not in scan.entries
    assert scan.skipped[f"{DEVICE_JOURNALS_REL_DIR}/phone.json"] == "chats"


def test_a_clone_brings_journal_only_conversations(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    path = os.path.join(desktop["path"], *DEVICE_JOURNALS_REL_DIR.split("/"), "phone-1.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(journal(chat_id, [("2026-10-19T12:00:00.000000+00:00", "user", "only in the journal", "m-1")]), handle)

    stats = chats.apply_device_journals("alpha", desktop["path"], desktop["db"])

    assert stats.messages_added == 1
    assert [m["content"] for m in history_of(desktop, chat_id)] == ["only in the journal"]


# ─── The orchestrator learns what was said elsewhere ──────────────────────────

def _saved_state(store, name, chat_id, history):
    store.save_chat_agent_state(name, chat_id, {"internal_history": history, "recursive_summary": "s"})


def test_merged_turns_are_queued_for_a_chat_with_saved_state(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    _saved_state(desktop["store"], "alpha", chat_id, [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
    ])

    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:05.000000+00:00", "assistant", "phone answer", "m-2"),
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
    ]), desktop["db"])

    queued = desktop["store"].get_chat_external_turns("alpha", chat_id)
    assert [(t["role"], t["content"]) for t in queued] == [
        ("user", "phone question"), ("assistant", "phone answer"),
    ]


def test_nothing_is_queued_for_a_chat_that_will_be_seeded(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    chat_id = desktop["project"].current_chat_id

    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
    ]), desktop["db"])

    assert desktop["store"].get_chat_external_turns("alpha", chat_id) == []


def test_the_same_holds_for_another_computer_s_export(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    laptop = make_machine(tmp_path, "laptop", shared_remote)
    chat_id = desktop["project"].current_chat_id
    sync(desktop, monkeypatch)
    sync(laptop, monkeypatch)
    _saved_state(desktop["store"], "alpha", chat_id, [{"role": "user", "content": "hello"}])

    laptop["store"].append_message(laptop["project"], "user", "typed on the laptop")
    laptop["store"].close_activity_connection()
    sync(laptop, monkeypatch)
    sync(desktop, monkeypatch)

    queued = desktop["store"].get_chat_external_turns("alpha", chat_id)
    assert [t["content"] for t in queued] == ["typed on the laptop"]


def _orchestrator():
    return MemGPTAgentBlock(name="chat_orchestrator", system_prompt="system", model="ollama/test")


def test_restore_appends_queued_turns_and_save_consumes_them(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    store, project = desktop["store"], desktop["project"]
    chat_id = project.current_chat_id
    _saved_state(store, "alpha", chat_id, [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
    ])
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
        ("2026-10-19T12:00:05.000000+00:00", "assistant", "phone answer", "m-2"),
    ]), desktop["db"])

    memgpt = _orchestrator()
    assert memgpt_runtime.restore_chat_orchestrator_state(memgpt, project, store) is True
    assert [m["content"] for m in memgpt.internal_history] == [
        "hello", "hi", "phone question", "phone answer",
    ]
    # Not consumed until the state that holds them is saved.
    assert len(store.get_chat_external_turns("alpha", chat_id)) == 2

    memgpt_runtime.save_chat_orchestrator_state(memgpt, project, store)

    assert store.get_chat_external_turns("alpha", chat_id) == []
    saved = store.get_chat_agent_state("alpha", chat_id)
    assert [m["content"] for m in saved["internal_history"]][-2:] == ["phone question", "phone answer"]


def test_turns_arriving_during_a_turn_survive_its_save(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    store, project = desktop["store"], desktop["project"]
    chat_id = project.current_chat_id
    _saved_state(store, "alpha", chat_id, [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "first", "m-1"),
    ]), desktop["db"])

    memgpt = _orchestrator()
    memgpt_runtime.restore_chat_orchestrator_state(memgpt, project, store)
    # A sync pass merges more while the turn runs.
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "first", "m-1"),
        ("2026-10-19T12:01:00.000000+00:00", "user", "second", "m-3"),
    ]), desktop["db"])
    memgpt_runtime.save_chat_orchestrator_state(memgpt, project, store)

    assert [t["content"] for t in store.get_chat_external_turns("alpha", chat_id)] == ["second"]


def test_a_restore_after_an_interrupted_tool_call_still_lands_the_turns(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    store, project = desktop["store"], desktop["project"]
    chat_id = project.current_chat_id
    _saved_state(store, "alpha", chat_id, [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
    ])
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
    ]), desktop["db"])

    memgpt = _orchestrator()
    memgpt_runtime.restore_chat_orchestrator_state(memgpt, project, store)

    assert memgpt.internal_history[-1] == {"role": "user", "content": "phone question"}


def test_a_seeded_restore_discards_the_queue(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    store, project = desktop["store"], desktop["project"]
    chat_id = project.current_chat_id
    _saved_state(store, "alpha", chat_id, [{"role": "user", "content": "hello"}])
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
    ]), desktop["db"])
    # Clearing the chat's working state means the next turn seeds from history.
    store.save_chat_agent_state("alpha", chat_id, {})

    assert memgpt_runtime.restore_chat_orchestrator_state(_orchestrator(), project, store) is False
    assert store.get_chat_external_turns("alpha", chat_id) == []


def test_clearing_a_chat_also_clears_its_queue(tmp_path, shared_remote):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    store = desktop["store"]
    chat_id = desktop["project"].current_chat_id
    _saved_state(store, "alpha", chat_id, [{"role": "user", "content": "hello"}])
    chats.merge_device_journal("alpha", journal(chat_id, [
        ("2026-10-19T12:00:00.000000+00:00", "user", "phone question", "m-1"),
    ]), desktop["db"])

    store.clear_project_history("alpha", chat_id)

    assert store.get_chat_external_turns("alpha", chat_id) == []
