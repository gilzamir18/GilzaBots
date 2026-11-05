"""Moving monitors and connectors between machines (`gilzabots/transfer.py`).

Each test exports from one data folder and imports into another, the way the
bundle travels between two machines.
"""
import asyncio
import json
import os

import pytest

from gilzabots import transfer, transfer_api, transfer_cli
from gilzabots.connectors import store as cstore
from gilzabots.monitors import store as mst
from gilzabots.project import ProjectStore


@pytest.fixture
def machines(tmp_path, monkeypatch):
    """Two data folders; ``use(name)`` makes one the current machine."""
    homes = {name: tmp_path / name for name in ("source", "target")}
    for home in homes.values():
        home.mkdir()

    def use(name):
        monkeypatch.setenv("GILZABOTS_HOME", str(homes[name]))
        # The conftest pins one monitors.db for the whole test; each machine has its own.
        monkeypatch.setattr(mst, "default_db_path", lambda: str(homes[name] / "monitors.db"))
        return homes[name]

    use("source")
    return use


def _sessions():
    return ProjectStore(db_path=os.path.join(os.environ["GILZABOTS_HOME"], "sessions.db"))


def _project(name, path, chats=()):
    sessions = _sessions()
    os.makedirs(path, exist_ok=True)
    sessions.create(name, "auto", "", project_path=str(path))
    for chat_id in chats:
        sessions.create_chat(name, chat_id, chat_id)
    return sessions


def _monitor(**overrides):
    from gilzabots.monitors.spec import normalize_spec

    raw = {"name": "Site up", "check": {"type": "http", "url": "https://example.com/health"},
           "condition": {"type": "contains", "value": "ok"}, "action": {"type": "notify", "message": "up"},
           "interval_seconds": 60}
    raw.update(overrides)
    return mst.MonitorStore().create_monitor(normalize_spec(raw))


def _export():
    bundle, _notes = transfer.build_bundle()
    return json.loads(json.dumps(bundle))  # what the file carries


class TestExport:
    def test_connector_secrets_and_catalog_stay_behind(self, machines):
        cstore.create_connector({"id": "gh", "name": "GitHub", "url": "https://api.example.com/mcp",
                                 "auth": "bearer", "bearer_token": "tok-123",
                                 "headers": [{"name": "X-Team", "value": "core"}]})
        cstore.save_catalog("gh", [{"name": "search"}])
        item = _export()["connectors"][0]
        text = json.dumps(item)
        assert "tok-123" not in text and "core" not in text
        assert item["needs"] == ["bearer_token", "header:X-Team"]
        assert item["headers"] == ["X-Team"]
        assert "tools" not in item and "bearer_token" not in item

    def test_monitor_state_stays_behind_and_header_values_are_secrets(self, machines):
        monitor = _monitor(check={"type": "http", "url": "https://example.com/api",
                                  "headers": {"Authorization": "Bearer s3cret"}}, max_checks=10)
        store = mst.MonitorStore()
        store.record_check(monitor["id"], value="ok", met=True, reference=None, next_check_at=None,
                           fired=False)
        item = _export()["monitors"][0]
        assert "s3cret" not in json.dumps(item)
        assert item["needs"] == ["header:Authorization"]
        assert item["max_checks"] == 9  # one check used
        assert item["source_id"] == monitor["id"]
        assert "last_value" not in item and "check_count" not in item

    def test_finished_monitors_are_not_exported_by_default(self, machines):
        done = _monitor(name="Done")
        mst.MonitorStore().set_status(done["id"], mst.STATUS_CANCELLED)
        _monitor(name="Live")
        assert [m["name"] for m in _export()["monitors"]] == ["Live"]

    def test_an_unknown_id_is_an_error(self, machines):
        with pytest.raises(transfer.TransferError, match="No connector with id 'nope'"):
            transfer.build_bundle(connector_ids=["nope"])


class TestImport:
    def test_round_trip_arrives_switched_off_with_the_secrets_supplied(self, machines, tmp_path):
        cstore.create_connector({"id": "gh", "name": "GitHub", "url": "https://api.example.com/mcp",
                                 "auth": "bearer", "bearer_token": "tok-123"})
        _project("paper", tmp_path / "src-paper")
        _monitor(name="Build", project="paper", project_path=str(tmp_path / "src-paper"),
                 check={"type": "file", "path": "out/report.pdf"}, condition={"type": "exists"})
        bundle = _export()

        machines("target")
        _project("paper", tmp_path / "dst-paper")
        report = transfer.import_bundle(bundle, secrets={"gh": {"bearer_token": "tok-new"}})
        assert report["imported"] == 2 and report["failed"] == 0

        connector = cstore.get_connector("gh")
        assert connector.enabled is False and connector.bearer_token == "tok-new"
        [monitor] = mst.MonitorStore().list_monitors()
        assert monitor["status"] == mst.STATUS_PAUSED
        assert monitor["status_reason"] == transfer.IMPORTED_MONITOR_REASON
        assert monitor["project_path"] == str(tmp_path / "dst-paper")  # this machine's folder
        assert monitor["has_value"] is False  # the baseline is taken here, not carried over

    def test_importing_twice_imports_nothing_new(self, machines):
        cstore.create_connector({"id": "docs", "name": "Docs", "url": "https://docs.example.com/mcp"})
        _monitor()
        bundle = _export()
        machines("target")
        assert transfer.import_bundle(bundle)["imported"] == 2
        again = transfer.import_bundle(bundle)
        assert again["imported"] == 0 and again["existing"] == 2 and again["failed"] == 0
        assert len(mst.MonitorStore().list_monitors()) == 1

    def test_missing_secrets_are_named_and_nothing_is_imported(self, machines):
        cstore.create_connector({"id": "gh", "name": "GitHub", "url": "https://api.example.com/mcp",
                                 "auth": "bearer", "bearer_token": "tok"})
        bundle = _export()
        machines("target")
        [result] = transfer.import_bundle(bundle)["results"]
        assert result["status"] == transfer.RESULT_NEEDS_SECRETS
        assert result["missing"] == ["bearer_token"]
        assert cstore.list_connectors() == []

    def test_dry_run_changes_nothing(self, machines):
        cstore.create_connector({"id": "docs", "name": "Docs", "url": "https://docs.example.com/mcp"})
        _monitor()
        bundle = _export()
        machines("target")
        report = transfer.import_bundle(bundle, dry_run=True)
        assert [r["status"] for r in report["results"]] == [transfer.RESULT_READY] * 2
        assert cstore.list_connectors() == [] and mst.MonitorStore().list_monitors() == []

    def test_a_project_missing_here_is_reported_and_can_be_mapped(self, machines, tmp_path):
        _project("paper", tmp_path / "src")
        _monitor(project="paper", project_path=str(tmp_path / "src"))
        bundle = _export()
        machines("target")
        [result] = transfer.import_bundle(bundle)["results"]
        assert result["status"] == transfer.RESULT_NO_PROJECT
        assert "--project paper=<name>" in result["message"]

        _project("thesis", tmp_path / "dst")
        report = transfer.import_bundle(bundle, project_map={"paper": "thesis"})
        assert report["imported"] == 1
        assert mst.MonitorStore().list_monitors()[0]["project"] == "thesis"

    def test_a_wake_up_of_a_chat_missing_here_is_not_redirected(self, machines, tmp_path):
        _project("paper", tmp_path / "src", chats=["chat-1"])
        _monitor(project="paper", chat_id="chat-1",
                 action={"type": "resume_chat", "prompt": "continue", "chat": "origin"})
        bundle = _export()
        machines("target")
        _project("paper", tmp_path / "dst")
        [result] = transfer.import_bundle(bundle)["results"]
        assert result["status"] == transfer.RESULT_NO_CHAT
        assert mst.MonitorStore().list_monitors() == []

        _sessions().create_chat("paper", "chat-1", "mirrored")  # the conversation arrived
        assert transfer.import_bundle(bundle)["imported"] == 1

    def test_a_connector_invalid_here_is_reported_by_the_registry(self, machines, tmp_path):
        folder = tmp_path / "notes"
        folder.mkdir()
        cstore.create_connector({"id": "fs", "name": "Files", "transport": "stdio",
                                 "command": "npx", "args": ["server"], "cwd": str(folder)})
        bundle = _export()
        folder.rmdir()
        machines("target")
        [result] = transfer.import_bundle(bundle)["results"]
        assert result["status"] == transfer.RESULT_INVALID
        assert "does not exist" in result["message"]

    def test_header_secrets_of_a_monitor_are_supplied_at_import(self, machines):
        monitor = _monitor(check={"type": "http", "url": "https://example.com/api",
                                  "headers": {"Authorization": "Bearer old"}})
        bundle = _export()
        machines("target")
        report = transfer.import_bundle(
            bundle, secrets={monitor["id"]: {"header:Authorization": "Bearer new"}})
        assert report["imported"] == 1
        assert mst.MonitorStore().list_monitors()[0]["check"]["headers"] == {"Authorization": "Bearer new"}

    @pytest.mark.parametrize("bundle, message", [
        ({"connectors": []}, "not a GilzaBots transfer bundle"),
        ({"format": transfer.BUNDLE_FORMAT, "version": 99}, "newer GilzaBots"),
        ({"format": transfer.BUNDLE_FORMAT, "version": 1, "monitors": "x"}, "'monitors' must be a list"),
    ])
    def test_an_unreadable_bundle_is_refused(self, machines, bundle, message):
        with pytest.raises(transfer.TransferError, match=message):
            transfer.import_bundle(bundle)


class TestInterfaces:
    def test_cli_round_trip(self, machines, tmp_path, capsys):
        cstore.create_connector({"id": "gh", "name": "GitHub", "url": "https://api.example.com/mcp",
                                 "auth": "bearer", "bearer_token": "ghp-secret-value"})
        path = tmp_path / "bundle.json"
        assert transfer_cli.main(["export", "-o", str(path)]) == 0
        assert "ghp-secret-value" not in path.read_text(encoding="utf-8")

        machines("target")
        assert transfer_cli.main(["import", str(path)]) == 1
        assert "--secret gh.bearer_token=<value>" in capsys.readouterr().out
        assert transfer_cli.main(["import", str(path), "--secret", "gh.bearer_token=tok2"]) == 0
        assert cstore.get_connector("gh").bearer_token == "tok2"

    def test_cli_secret_syntax(self):
        assert transfer_cli.parse_secrets(["gh.env:TOKEN=a=b"]) == {"gh": {"env:TOKEN": "a=b"}}
        with pytest.raises(transfer.TransferError, match="KEY.NAME=VALUE"):
            transfer_cli.parse_secrets(["bearer_token=x"])

    def test_api_round_trip(self, machines):
        _monitor()
        status, body = asyncio.run(transfer_api.handle("/api/transfer/export", "POST", {}))
        assert status == 200 and len(body["bundle"]["monitors"]) == 1
        machines("target")
        status, report = asyncio.run(transfer_api.handle(
            "/api/transfer/import", "POST", {"bundle": body["bundle"]}))
        assert status == 200 and report["imported"] == 1
        status, error = asyncio.run(transfer_api.handle("/api/transfer/import", "POST", {"bundle": {}}))
        assert status == 400 and "not a GilzaBots transfer bundle" in error["error"]
