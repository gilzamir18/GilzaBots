"""End-to-end sync between two machines sharing one remote folder.

`sync_project` is the whole feature from the server's point of view, so these
tests drive it the way the endpoints do — including the conversation export,
which is the part that turns "mirror the files" into "mirror the project".
"""

import json
import os

import pytest

from gilzabots.cloud import chats, service
from gilzabots.cloud.providers.local_folder import LocalFolderProvider
from gilzabots.cloud.registry import get_cloud_provider, list_providers, register_provider
from gilzabots.cloud.state import CloudSettings, load_state, save_settings
from gilzabots.project import ProjectStore


@pytest.fixture
def shared_remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    return str(path)


def make_machine(tmp_path, name, remote_base, project_name="alpha"):
    """A project directory plus its own sessions.db, as a second computer has."""
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
        provider_config={"base_dir": remote_base},
    ))
    return {
        "path": str(project_dir),
        "db": db_path,
        "store": store,
        "project": project,
        "name": project_name,
    }


def write(root, rel_path, content):
    absolute = os.path.join(root, rel_path.replace("/", os.sep))
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8") as handle:
        handle.write(content)


def read(root, rel_path):
    with open(os.path.join(root, rel_path.replace("/", os.sep)), encoding="utf-8") as handle:
        return handle.read()


def sync(machine, monkeypatch, **kwargs):
    # Each machine has its own sessions.db; the chats module resolves the path
    # through config, so it is pointed at this machine's copy.
    monkeypatch.setattr(chats, "_default_db_path", lambda: machine["db"])
    return service.sync_project(machine["name"], machine["path"], **kwargs)


# ─── Files ────────────────────────────────────────────────────────────────────

def test_a_file_written_on_one_machine_reaches_the_other(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    laptop = make_machine(tmp_path, "laptop", shared_remote)

    write(desktop["path"], "main.tex", "\\section{Intro}")
    first = sync(desktop, monkeypatch)
    second = sync(laptop, monkeypatch)

    assert first.report.ok and second.report.ok
    assert read(laptop["path"], "main.tex") == "\\section{Intro}"


def test_settings_without_a_provider_report_a_clear_error(tmp_path, shared_remote, monkeypatch):
    machine = make_machine(tmp_path, "solo", shared_remote)
    save_settings(machine["path"], CloudSettings(enabled=True, provider=""))

    outcome = sync(machine, monkeypatch)

    assert outcome.report is None
    assert "No cloud provider" in outcome.error


def test_an_unknown_provider_fails_instead_of_falling_back(tmp_path, shared_remote, monkeypatch):
    # Silently mirroring to a different backend than the one recorded would put
    # the user's files somewhere they never chose.
    machine = make_machine(tmp_path, "solo", shared_remote)
    save_settings(machine["path"], CloudSettings(enabled=True, provider="dropbox_someday"))

    outcome = sync(machine, monkeypatch)

    assert "Unknown cloud provider" in outcome.error


def test_the_baseline_is_persisted_after_a_pass(tmp_path, shared_remote, monkeypatch):
    machine = make_machine(tmp_path, "desktop", shared_remote)
    write(machine["path"], "main.tex", "body")

    sync(machine, monkeypatch)

    state = load_state(machine["path"])
    assert "main.tex" in state.entries
    assert state.last_sync_at


# ─── Conversations ────────────────────────────────────────────────────────────

def test_conversations_travel_with_the_project(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    laptop = make_machine(tmp_path, "laptop", shared_remote)

    desktop["store"].append_message(desktop["project"], "user", "written on the desktop")
    desktop["store"].close_activity_connection()

    sync(desktop, monkeypatch)
    outcome = sync(laptop, monkeypatch)

    # The export travelled as a file...
    payload = chats.read_export(laptop["path"])
    assert payload is not None
    assert any(m["content"] == "written on the desktop" for m in payload["messages"])

    # ...and the pass merged it into the laptop's own database, which is what
    # makes the conversation actually appear in the UI there.
    assert outcome.merge is not None
    assert outcome.merge.messages_added == 1
    stored = chats.export_project(laptop["name"], laptop["db"])
    assert any(m["content"] == "written on the desktop" for m in stored["messages"])


def test_messages_from_both_machines_survive_the_merge(tmp_path, shared_remote, monkeypatch):
    desktop = make_machine(tmp_path, "desktop", shared_remote)
    laptop = make_machine(tmp_path, "laptop", shared_remote)

    desktop["store"].append_message(desktop["project"], "user", "from the desktop")
    desktop["store"].close_activity_connection()
    laptop["store"].append_message(laptop["project"], "user", "from the laptop")
    laptop["store"].close_activity_connection()

    # Desktop publishes, laptop pulls and merges, laptop republishes the union.
    sync(desktop, monkeypatch)
    sync(laptop, monkeypatch)
    # Desktop picks the union back up.
    sync(desktop, monkeypatch)

    monkeypatch.setattr(chats, "_default_db_path", lambda: desktop["db"])
    chats.apply_export(desktop["name"], desktop["path"], desktop["db"])

    payload = chats.export_project(desktop["name"], desktop["db"])
    contents = [m["content"] for m in payload["messages"]]
    assert "from the desktop" in contents
    assert "from the laptop" in contents


def test_a_quiet_project_stops_re_uploading_its_conversation(tmp_path, shared_remote, monkeypatch):
    # The export is regenerated on every pass; if it were rewritten byte-for-byte
    # each time, its mtime would change and it would upload forever.
    machine = make_machine(tmp_path, "desktop", shared_remote)
    machine["store"].append_message(machine["project"], "user", "hello")
    machine["store"].close_activity_connection()

    sync(machine, monkeypatch)
    second = sync(machine, monkeypatch)

    assert second.report.changed == 0


def test_conversations_stay_home_when_the_option_is_off(tmp_path, shared_remote, monkeypatch):
    machine = make_machine(tmp_path, "desktop", shared_remote)
    save_settings(machine["path"], CloudSettings(
        enabled=True, provider="local_folder", remote_folder="alpha",
        include_chats=False, provider_config={"base_dir": shared_remote},
    ))
    machine["store"].append_message(machine["project"], "user", "private")
    machine["store"].close_activity_connection()
    write(machine["path"], "main.tex", "body")

    outcome = sync(machine, monkeypatch)

    assert "main.tex" in outcome.report.uploaded
    assert not os.path.exists(chats.export_path(machine["path"]))
    assert not any(p.startswith(".gilzabots/session/") for p in outcome.report.uploaded)


# ─── Status and settings plumbing ─────────────────────────────────────────────

def test_status_reports_providers_and_connection(tmp_path, shared_remote):
    machine = make_machine(tmp_path, "desktop", shared_remote)

    status = service.status_for(machine["path"], machine["name"])

    assert status["settings"]["provider"] == "local_folder"
    assert status["connected"] is True  # the destination folder exists and is writable
    assert any(p["id"] == "google_drive" for p in status["providers"])


def test_status_surfaces_a_missing_destination(tmp_path, shared_remote):
    machine = make_machine(tmp_path, "desktop", shared_remote)
    service.update_settings(machine["path"], {"provider_config": {"base_dir": str(tmp_path / "gone")}})

    status = service.status_for(machine["path"], machine["name"])

    assert status["connected"] is False
    assert "not found" in status["auth_error"].lower()


def test_update_settings_only_touches_known_keys(tmp_path, shared_remote):
    machine = make_machine(tmp_path, "desktop", shared_remote)

    settings = service.update_settings(machine["path"], {
        "include_build_artifacts": False,
        "not_a_real_setting": "ignored",
    })

    assert settings.include_build_artifacts is False
    assert settings.provider == "local_folder"  # untouched keys survive
    assert not hasattr(settings, "not_a_real_setting")


def test_switching_provider_drops_the_stale_baseline(tmp_path, shared_remote):
    # Remote revisions and the root handle belong to the old backend; comparing
    # against them after a switch could never match.
    machine = make_machine(tmp_path, "desktop", shared_remote)
    state = load_state(machine["path"])
    state.record("main.tex", hash_="h", size=1, mtime=1.0, remote_rev="r")
    state.root = "old-root"
    from gilzabots.cloud.state import save_state
    save_state(machine["path"], state)

    service.update_settings(machine["path"], {"provider": "google_drive"})

    after = load_state(machine["path"])
    assert after.entries == {}
    assert after.root == ""


# ─── Registry ─────────────────────────────────────────────────────────────────

def test_a_new_backend_needs_only_a_registry_entry(tmp_path, shared_remote, monkeypatch):
    # The point of the facade: a second provider plugs in without the engine or
    # the service knowing anything about it.
    calls = []

    class RecordingProvider(LocalFolderProvider):
        id = "recording"
        display_name = "Recording"

        def upload(self, root, rel_path, local_path, expected_rev=None):
            calls.append(rel_path)
            return super().upload(root, rel_path, local_path, expected_rev)

    register_provider(
        "recording", "Recording",
        lambda config: RecordingProvider(config.get("base_dir", "")),
        requires_authorization=False,
    )

    machine = make_machine(tmp_path, "desktop", shared_remote)
    save_settings(machine["path"], CloudSettings(
        enabled=True, provider="recording", remote_folder="alpha",
        include_chats=False, provider_config={"base_dir": shared_remote},
    ))
    write(machine["path"], "main.tex", "body")

    outcome = sync(machine, monkeypatch)

    assert outcome.report.ok
    assert "main.tex" in calls
    assert any(p.id == "recording" for p in list_providers())


def test_google_drive_is_listed_as_available_without_extra_dependencies():
    # The backend is stdlib-only on purpose; a packaged build must not need an
    # optional install for it to appear.
    info = next(p for p in list_providers() if p.id == "google_drive")

    assert info.available
    assert info.requires_authorization


def test_google_drive_reports_it_needs_a_client_when_none_is_set(monkeypatch):
    from gilzabots.cloud.providers import google_drive

    monkeypatch.setattr(google_drive, "load_client_config", lambda: {"client_id": "", "client_secret": ""})
    monkeypatch.setattr(google_drive, "_load_token", lambda: google_drive._Token())

    status = get_cloud_provider("google_drive").auth_status()

    assert status.connected is False
    assert "OAuth client" in status.error
