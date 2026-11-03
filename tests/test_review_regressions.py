"""Regression tests for plan authority and cloud file preservation."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gilzabots.cloud.engine import SyncEngine
from gilzabots.cloud.providers.local_folder import LocalFolderProvider
from gilzabots.cloud.state import CloudSettings, CloudState


@pytest.mark.parametrize("command", ["pwd", "echo ok ; touch marker", "find . -delete", "echo $(touch marker)"])
def test_plan_blocks_all_shell_even_with_remembered_permission(monkeypatch, command):
    import gilzabots.tools as tools
    monkeypatch.setattr(tools, "_PROJECT_SESSION", SimpleNamespace(mode="plan", results={"allowed_tools": ["run_command"]}))
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **kw: pytest.fail("Shell must not execute in plan mode"))
    assert "Execution blocked" in asyncio.run(tools.run_command._func(command))


def test_plan_blocks_remembered_file_write_but_allows_persistent_memory(tmp_path, monkeypatch):
    import gilzabots.tools as tools
    saved = []
    project = SimpleNamespace(mode="plan", results={"allowed_tools": ["write_file"]}, use_shared_memory=True, core_memory="")
    monkeypatch.setattr(tools, "_PROJECT_SESSION", project)
    monkeypatch.setattr(tools, "_PROJECT_STORE", SimpleNamespace(save=lambda p: saved.append(p.core_memory)))
    result = asyncio.run(tools.write_file._func(str(tmp_path / "blocked"), "content"))
    assert "Execution blocked" in result
    assert not (tmp_path / "blocked").exists()
    asyncio.run(tools.append_core_memory._func("Remember this finding"))
    assert saved == ["- Remember this finding"]


def test_plan_catalog_changes_only_after_explicit_approval(tmp_path, monkeypatch):
    from gilzabots.memgpt_runtime import build_chat_orchestrator
    from gilzabots.project import ProjectData
    import gilzabots.tools as tools
    import gilzabots.agent_stdin as bridge
    project = ProjectData(name="review", project_path=str(tmp_path), mode="plan", model="ollama/test")
    agent = build_chat_orchestrator(project)
    names = {tool.name for tool in agent.tools}
    assert not names.intersection({tool.name for tool in tools.get_workspace_action_tools()} | {"run_skill"})
    assert {"append_core_memory", "inspect_git", "inspect_project", "read_document", "create_plan"} <= names
    def approve(event, data):
        if event == "input_request":
            bridge._gui_input_pending[data["id"]].set_result('yes')
    monkeypatch.setattr(bridge, "print_event", approve)
    monkeypatch.setattr(tools, "_persist_approved_plan", lambda text: None)
    monkeypatch.setattr(tools, "_record_mode_event", lambda text: None)
    plan_tool = next(tool for tool in agent.tools if tool.name == "create_plan")
    plan_input = plan_tool.input_schema()(plan_content="Approved work")
    asyncio.run(plan_tool.run(plan_input))
    assert project.mode == "auto"
    names = {tool.name for tool in agent.tools_provider()}
    assert {"write_file", "run_python_script", "run_command", "run_skill"} <= names
    assert "create_plan" not in names
    assert "Mode**: auto" in agent.system_prompt


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "no"])
def test_unapproved_plan_never_enables_execution(monkeypatch, outcome):
    import gilzabots.tools as tools
    import gilzabots.agent_stdin as bridge
    project = SimpleNamespace(mode="plan", results={}, plan_text="")
    monkeypatch.setattr(tools, "_PROJECT_SESSION", project)
    records = []
    monkeypatch.setattr(tools, "_record_mode_event", records.append)
    monkeypatch.setattr(tools, "_persist_approved_plan", lambda _: pytest.fail("Unapproved plan was persisted"))
    futures = []
    def event(name, data):
        if name == "input_request":
            futures.append(bridge._gui_input_pending[data["id"]])
    monkeypatch.setattr(bridge, "print_event", event)
    async def wait(future, **kw):
        if outcome == "timeout":
            raise asyncio.TimeoutError()
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return "no"
    monkeypatch.setattr(asyncio, "wait_for", wait)
    with pytest.raises((ValueError, asyncio.CancelledError)):
        asyncio.run(tools.create_plan._func("Never approved"))
    assert project.mode == "plan"
    assert not any("[PLAN APPROVED]" in record for record in records)
    assert futures and all(future.done() for future in futures)
    assert all(future not in bridge._gui_input_pending.values() for future in futures)


@pytest.fixture
def mirror(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    provider = LocalFolderProvider(str(remote))
    state = CloudState(settings=CloudSettings(enabled=True))
    root = Path(provider.ensure_root("local"))
    return local, root, provider, state


@pytest.mark.parametrize("name", [".env", ".git/config", "nested/node_modules/a.js", ".gilzabots/cloud/state.json"])
def test_remote_excluded_file_never_overwrites_local(mirror, name):
    local, root, provider, state = mirror
    for directory, text in [(local, "local"), (root, "remote")]:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    report = SyncEngine(str(local), provider, state).run()
    assert report.ok
    assert not report.downloaded
    assert (local / name).read_text() == "local"
    assert (root / name).read_text() == "remote"


def test_excluding_tracked_file_preserves_both_copies_and_baseline(mirror):
    local, root, provider, state = mirror
    (local / "main.tex").write_text("initial")
    assert SyncEngine(str(local), provider, state).run().ok
    original = state.entries["main.tex"]
    state.settings.extra_excludes = ["main.tex"]
    (local / "main.tex").write_text("local edit while ignored")
    report = SyncEngine(str(local), provider, state).run()
    assert report.ok and report.changed == 0
    assert (root / "main.tex").read_text() == "initial"
    assert state.entries["main.tex"] == original
    state.settings.extra_excludes = []
    assert SyncEngine(str(local), provider, state).run().uploaded == ["main.tex"]
    assert (root / "main.tex").read_text() == "local edit while ignored"


@pytest.mark.parametrize("conflict", [False, True])
def test_edit_during_upload_is_detected_next_pass(mirror, monkeypatch, conflict):
    local, root, provider, state = mirror
    source = local / "main.tex"
    source.write_text("AAAA")
    if conflict:
        assert SyncEngine(str(local), provider, state).run().ok
        source.write_text("LLLL")
        (root / "main.tex").write_text("RRRR")
    expected = source.read_text()
    original_upload = provider.upload
    uploaded_paths = []
    def upload(*args, **kwargs):
        uploaded_paths.append(Path(args[2]))
        assert Path(args[2]) != source
        result = original_upload(*args, **kwargs)
        source.write_text("BBBB")
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        return result
    monkeypatch.setattr(provider, "upload", upload)
    assert SyncEngine(str(local), provider, state).run().ok
    assert (root / "main.tex").read_text() == expected
    assert all(not path.exists() for path in uploaded_paths)
    monkeypatch.setattr(provider, "upload", original_upload)
    report = SyncEngine(str(local), provider, state).run()
    assert "main.tex" in report.uploaded
    assert (root / "main.tex").read_text() == "BBBB"


def test_chat_export_is_ignored_when_chat_sync_disabled(mirror):
    local, root, provider, state = mirror
    state.settings.include_chats = False
    name = ".gilzabots/session/chats.json"
    target = local / name
    target.parent.mkdir(parents=True)
    target.write_text('{"private":"conversation"}')
    assert SyncEngine(str(local), provider, state).run().changed == 0
    assert not (root / name).exists()


def test_file_changed_while_preparing_snapshot_is_not_uploaded(mirror, monkeypatch):
    import gilzabots.cloud.engine as engine
    local, root, provider, state = mirror
    source = local / 'main.tex'
    source.write_text('initial')
    original = engine.shutil.copyfile
    snapshots = []
    def copy(src, dest):
        original(src, dest)
        snapshots.append(Path(dest))
        source.write_text('changed while copying')
    monkeypatch.setattr(engine.shutil, 'copyfile', copy)
    report = SyncEngine(str(local), provider, state).run()
    assert not report.ok and 'changed while preparing upload' in report.errors[0][1]
    assert not (root / 'main.tex').exists()
    assert not state.entries
    assert all(not snapshot.exists() for snapshot in snapshots)


def test_failed_upload_cleans_snapshot_and_preserves_baseline(mirror, monkeypatch):
    from gilzabots.cloud.base import CloudError
    local, root, provider, state = mirror
    source = local / 'main.tex'
    source.write_text('initial')
    assert SyncEngine(str(local), provider, state).run().ok
    baseline = state.entries['main.tex']
    source.write_text('new content')
    snapshots = []
    def fail(root, rel, path, expected):
        snapshots.append(Path(path))
        raise CloudError('Upload interrupted')
    monkeypatch.setattr(provider, 'upload', fail)
    report = SyncEngine(str(local), provider, state).run()
    assert not report.ok
    assert state.entries['main.tex'] == baseline
    assert (root / 'main.tex').read_text() == 'initial'
    assert all(not snapshot.exists() for snapshot in snapshots)
