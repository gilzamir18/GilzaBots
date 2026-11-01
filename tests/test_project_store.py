"""Tests for ProjectStore and ProjectData.

Verifies:
1. ProjectStore.create always includes 'opalatex' in skills
2. ProjectStore.load round-trips all ProjectData fields correctly
3. ProjectStore.rename fails if target name already exists
4. ProjectData.context_header produces the expected format
5. ProjectStore.save persists changes and reload reflects them
6. ProjectStore.delete removes project and history
"""

import os
import tempfile
import pytest
import asyncio
import uuid

from opalatex.project import ProjectData, ProjectStore


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "test.db")
    return ProjectStore(db_path=db)


def _base_args(**overrides):
    defaults = dict(
        name="myproj",
        mode="plan",
        model="fake/model",
        project_name="My Project",
        project_path="/home/user/myproject",
        skills=["python_subprocess"],
        description="A test project",
    )
    defaults.update(overrides)
    return defaults


def _tmp_store():
    db_path = os.path.join(tempfile.gettempdir(), f"opalatex-test-{uuid.uuid4().hex}.db")
    return ProjectStore(db_path=db_path)


# ---------------------------------------------------------------------------
# 1. create always adds 'opalatex'
# ---------------------------------------------------------------------------


def test_project_data_enables_streaming_by_default():
    """New in-memory projects should emit live model output by default."""
    project = ProjectData(name="stream-default")
    assert project.model_params["stream"] is True
    assert project.worker_model_params["stream"] is True


def test_load_enables_streaming_for_legacy_project_without_stream_setting(store):
    """A project saved before the streaming default still receives live output."""
    project = store.create(**_base_args())
    project.model_params.pop("stream")
    project.worker_model_params.pop("stream", None)
    store.save(project)

    reloaded = store.load(project.name)
    assert reloaded.model_params["stream"] is True
    assert reloaded.worker_model_params["stream"] is True

def test_create_always_includes_opalatex(store):
    """Skills list must always contain 'opalatex', even if not passed."""
    p = store.create(**_base_args(skills=["react_vite"]))
    assert "opalatex" in p.skills


def test_create_without_skills_defaults_to_opalatex(store):
    """When skills is None, result must still have 'opalatex'."""
    p = store.create(**_base_args(skills=None))
    assert p.skills == ["opalatex"]


def test_create_does_not_duplicate_opalatex(store):
    """If opalatex is explicitly in the list, it must not appear twice."""
    p = store.create(**_base_args(skills=["opalatex", "html_css_js"]))
    assert p.skills.count("opalatex") == 1


def test_stale_cloud_project_model_loads_as_default_in_community(store, tmp_path):
    """Community mode must not surface stale Opala Cloud aliases from old projects."""
    from opalatex.config import DEFAULT_MODEL

    project_dir = tmp_path / "stale_cloud"
    store.create(
        **_base_args(
            name="stale_cloud",
            model="OpalaTexCloud",
            worker_model="OpalaTexCloudGemini35Flash",
            project_path=str(project_dir),
        )
    )

    loaded = store.load("stale_cloud")
    listed = store.list_projects()[0]

    assert loaded.model == DEFAULT_MODEL
    assert loaded.worker_model == DEFAULT_MODEL
    assert listed["model"] == DEFAULT_MODEL
    assert listed["worker_model"] == DEFAULT_MODEL


def test_create_does_not_bake_num_ctx_into_model_params(store, tmp_path):
    """num_ctx is no longer defaulted at project-creation time: a project created
    without an explicit override resolves its context window at runtime from the
    model's catalog entry, then the local/cloud heuristic
    (config.resolve_effective_num_ctx). Baking a fixed value here would shadow a
    catalog entry registered or edited after the project was created."""
    project_dir = tmp_path / "local_ctx"
    p = store.create(
        **_base_args(
            name="local_ctx",
            model="ollama/gemma4:12b",
            project_path=str(project_dir),
        )
    )

    assert "num_ctx" not in p.model_params
    assert p.worker_model_params == {}
    assert "max_tokens" not in p.model_params
    assert "max_context_tokens" not in p.model_params


def test_create_preserves_explicit_num_ctx(store, tmp_path):
    """An explicit num_ctx passed by the caller (the project's Advanced override)
    is preserved as-is and always takes priority over the catalog/heuristic."""
    project_dir = tmp_path / "explicit_ctx"
    p = store.create(
        **_base_args(
            name="explicit_ctx",
            model="gemini/gemini-3.5-flash-lite",
            project_path=str(project_dir),
            model_params={"num_ctx": 32768},
        )
    )

    assert p.model_params["num_ctx"] == 32768
    assert p.worker_model_params == {}


def test_create_explicit_worker_model_does_not_bake_num_ctx(store, tmp_path):
    project_dir = tmp_path / "worker_ctx"
    p = store.create(
        **_base_args(
            name="worker_ctx",
            model="ollama/gemma4:12b",
            worker_model="gemini/gemini-3.5-flash-lite",
            project_path=str(project_dir),
        )
    )

    assert "num_ctx" not in p.model_params
    assert "num_ctx" not in p.worker_model_params


# ---------------------------------------------------------------------------
# 2. load round-trips all fields
# ---------------------------------------------------------------------------

def test_load_roundtrips_all_fields(store):
    args = _base_args()
    store.create(**args)
    loaded = store.load("myproj")

    assert loaded is not None
    assert loaded.name == "myproj"
    assert loaded.mode == "plan"
    assert loaded.model == "fake/model"
    assert loaded.project_name == "My Project"
    assert loaded.project_path == os.path.abspath("/home/user/myproject")
    assert "opalatex" in loaded.skills
    assert "python_subprocess" in loaded.skills
    assert loaded.description == "A test project"
    assert loaded.compile_on_save_partial is True
    assert loaded.compile_on_save_full is False


def test_load_nonexistent_returns_none(store):
    assert store.load("ghost") is None


def test_compile_on_save_settings_persist(store):
    project = store.create(**_base_args())
    project.compile_on_save_partial = False
    project.compile_on_save_full = True
    store.save(project)

    loaded = store.load("myproj")
    listed = store.list_projects()[0]

    assert loaded.compile_on_save_partial is False
    assert loaded.compile_on_save_full is True
    assert listed["compile_on_save_partial"] is False
    assert listed["compile_on_save_full"] is True


def test_compile_on_save_settings_are_mutually_exclusive(store):
    project = store.create(**_base_args())
    project.compile_on_save_partial = True
    project.compile_on_save_full = True
    store.save(project)

    loaded = store.load("myproj")
    listed = store.list_projects()[0]

    assert loaded.compile_on_save_partial is False
    assert loaded.compile_on_save_full is True
    assert listed["compile_on_save_partial"] is False
    assert listed["compile_on_save_full"] is True


def test_compile_on_save_settings_can_be_disabled(store):
    project = store.create(**_base_args())
    project.compile_on_save_partial = False
    project.compile_on_save_full = False
    store.save(project)

    loaded = store.load("myproj")
    listed = store.list_projects()[0]

    assert loaded.compile_on_save_partial is False
    assert loaded.compile_on_save_full is False
    assert listed["compile_on_save_partial"] is False
    assert listed["compile_on_save_full"] is False


# ---------------------------------------------------------------------------
# 3. rename fails if target exists
# ---------------------------------------------------------------------------

def test_rename_fails_if_target_exists(store):
    store.create(**_base_args(name="proj_a"))
    store.create(**_base_args(name="proj_b"))
    result = store.rename("proj_a", "proj_b")
    assert result is False
    # Both should still exist under original names
    assert store.exists("proj_a")
    assert store.exists("proj_b")


def test_rename_succeeds_when_target_is_free(store):
    store.create(**_base_args(name="proj_a"))
    result = store.rename("proj_a", "proj_renamed")
    assert result is True
    assert not store.exists("proj_a")
    assert store.exists("proj_renamed")


# ---------------------------------------------------------------------------
# 4. context_header format
# ---------------------------------------------------------------------------

def test_context_header_format():
    p = ProjectData(
        name="myproj",
        project_name="My Project",
        project_path="/home/user/myproject",
    )
    header = p.context_header()
    assert header.startswith("[PROJECT:")
    assert "My Project" in header
    assert "/home/user/myproject" in header
    # Must match the format the orchestrator parses
    assert "PATH:" in header


def test_context_header_uses_name_when_project_name_empty():
    p = ProjectData(name="fallback_name", project_name="", project_path="/some/path")
    header = p.context_header()
    assert "fallback_name" in header


# ---------------------------------------------------------------------------
# 5. save persists changes
# ---------------------------------------------------------------------------

def test_save_persists_description_change(store):
    store.create(**_base_args())
    p = store.load("myproj")
    p.description = "Updated description"
    store.save(p)

    reloaded = store.load("myproj")
    assert reloaded.description == "Updated description"


def test_save_persists_git_root_path(store, tmp_path):
    proj_dir = tmp_path / "project"
    git_root = proj_dir / "paper"
    git_root.mkdir(parents=True)
    store.create(**_base_args(project_path=str(proj_dir)))

    p = store.load("myproj")
    p.git_root_path = str(git_root)
    store.save(p)

    reloaded = store.load("myproj")
    assert reloaded.git_root_path == str(git_root)

    listed = store.list_projects()
    assert listed[0]["git_root_path"] == str(git_root)


def test_save_always_keeps_opalatex_in_skills(store):
    """Even if someone accidentally removes opalatex before save, it must be restored."""
    store.create(**_base_args())
    p = store.load("myproj")
    p.skills = ["html_css_js"]  # opalatex removed
    store.save(p)

    reloaded = store.load("myproj")
    assert "opalatex" in reloaded.skills


# ---------------------------------------------------------------------------
# 6. delete removes project and history
# ---------------------------------------------------------------------------

def test_delete_removes_project(store):
    store.create(**_base_args())
    assert store.exists("myproj")
    store.delete("myproj")
    assert not store.exists("myproj")
    assert store.load("myproj") is None


def test_delete_removes_history(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "hello")
    store.append_message(p, "assistant", "hi")

    store.delete("myproj")
    # Re-creating with same name should start with empty history
    store.create(**_base_args())
    p2 = store.load("myproj")
    assert p2.history == []


def test_clear_all_chats_leaves_the_project_without_a_chat(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-1", "First")
    project = store.load("myproj", chat_id="chat-1")
    store.append_message(project, "user", "First chat message")
    store.append_activity(project, "thought", "First chat thought", agent="chat_orchestrator")
    store.create_chat("myproj", "branch-1", "Branch")
    branch = store.load("myproj", chat_id="branch-1")
    store.append_message(branch, "user", "Branch message")

    store.clear_all_chats("myproj")

    loaded = store.load("myproj")
    assert loaded.chats == []
    assert loaded.current_chat_id == ""
    assert loaded.history == []
    assert store.list_activity("myproj", "chat-1") == []
    assert store.list_activity("myproj", "branch-1") == []


def test_append_message_persists_attachments(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    att = {"type": "image", "data": "abc123", "mime": "image/jpeg", "name": "shot.jpg"}

    message_id = store.append_message(p, "user", "describe this", attachments=[att])

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    assert isinstance(message_id, int)
    assert loaded.history[0]["id"] == message_id
    assert loaded.history[0]["content"] == "describe this"
    assert loaded.history[0]["_attachments"] == [att]


def test_append_message_is_idempotent_by_client_message_id(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")

    first_id = store.append_message(
        p,
        "user",
        "review main.tex",
        client_message_id="client-turn-1",
    )
    retry_id = store.append_message(
        p,
        "user",
        "review main.tex",
        client_message_id="client-turn-1",
    )

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    assert retry_id == first_id
    assert [m["content"] for m in loaded.history] == ["review main.tex"]
    assert loaded.history[0]["client_message_id"] == "client-turn-1"


def test_append_activity_persists_diagnostics_outside_chat_history(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "review main.tex")

    activity_id = store.append_activity(
        p,
        "thought",
        "I should inspect main.tex.",
        agent="chat_orchestrator",
        payload={"agent": "chat_orchestrator"},
    )
    store.append_activity(p, "stream_chunk", "Visible token", agent="chat_orchestrator")

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    activity = store.list_activity("myproj", p.current_chat_id)

    assert isinstance(activity_id, int)
    assert [m["content"] for m in loaded.history] == ["review main.tex"]
    assert [(item["event"], item["content"]) for item in activity] == [
        ("thought", "I should inspect main.tex."),
        ("stream_chunk", "Visible token"),
    ]


def test_branch_chat_copies_attachments(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    att = {"type": "image", "data": "abc123", "mime": "image/jpeg", "name": "shot.jpg"}
    store.append_message(p, "user", "describe this", attachments=[att])

    store.branch_chat("myproj", source_chat, "branch-1", "Branch", 0)

    loaded = store.load("myproj", chat_id="branch-1")
    assert loaded.history[0]["_attachments"] == [att]


def test_branch_chat_with_message_index_negative_one_copies_no_history(store):
    # message_index=-1 is what the UI sends when branching from a chat's
    # client-only greeting message (never persisted, so there is nothing
    # before it to copy) — the branch should still succeed, yielding an
    # empty new chat.
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    store.append_message(p, "user", "hello")

    store.branch_chat("myproj", source_chat, "branch-1", "Branch", -1)

    loaded = store.load("myproj", chat_id="branch-1")
    assert loaded.history == []


def test_branch_chat_does_not_inherit_source_core_memory(store):
    # The source's core memory holds what it learned after the branch point
    # too; inheriting it made an isolated branch answer from a conversation it
    # never had.
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    anchor_id = store.append_message(p, "user", "hello")
    store.update_chat_core_memory("myproj", source_chat, "- learned later")

    store.branch_chat("myproj", source_chat, "branch-1", "Branch", 0, message_id=anchor_id)

    assert store.get_chat_core_memory("myproj", "branch-1") == ""
    assert store.get_chat_core_memory("myproj", source_chat) == "- learned later"


def test_branch_chat_before_message_does_not_inherit_source_core_memory(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    store.append_message(p, "user", "first")
    anchor_id = store.append_message(p, "user", "second")
    store.update_chat_core_memory("myproj", source_chat, "- learned later")

    store.branch_chat_before_message(
        "myproj", source_chat, "branch-edit", "Edited", message_id=anchor_id
    )

    assert store.get_chat_core_memory("myproj", "branch-edit") == ""
    assert store.get_chat_core_memory("myproj", source_chat) == "- learned later"


def test_branch_chat_by_message_id_is_not_shifted_by_mode_entries(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    for mode in ("auto", "auto", "plan"):
        store.append_message(p, "system", f"[MODE] Agent turn started. Current mode: '{mode}'.")
    store.append_message(p, "user", "review main.tex", client_message_id="client-turn-1")
    assistant_id = store.append_message(p, "assistant", "Here is the review.")

    store.branch_chat(
        "myproj",
        source_chat,
        "branch-1",
        "Branch",
        2,
        message_id=assistant_id,
    )

    loaded = store.load("myproj", chat_id="branch-1")
    assert [m["content"] for m in loaded.history] == [
        "[MODE] Agent turn started. Current mode: 'auto'.",
        "[MODE] Agent turn started. Current mode: 'auto'.",
        "[MODE] Agent turn started. Current mode: 'plan'.",
        "review main.tex",
        "Here is the review.",
    ]


def test_branch_chat_by_client_message_id_targets_user_message(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    store.append_message(p, "system", "[MODE] Agent turn started. Current mode: 'auto'.")
    store.append_message(p, "user", "review main.tex", client_message_id="client-turn-1")
    store.append_message(p, "assistant", "Here is the review.")

    store.branch_chat(
        "myproj",
        source_chat,
        "branch-1",
        "Branch",
        0,
        client_message_id="client-turn-1",
    )

    loaded = store.load("myproj", chat_id="branch-1")
    assert [m["content"] for m in loaded.history] == [
        "[MODE] Agent turn started. Current mode: 'auto'.",
        "review main.tex",
    ]


def test_branch_chat_resolves_stale_main_source_by_message_id(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    user_id = store.append_message(p, "user", "review main.tex", client_message_id="client-turn-1")
    store.append_message(p, "assistant", "Here is the review.")

    store.branch_chat(
        "myproj",
        "main",
        "branch-1",
        "Branch",
        0,
        message_id=user_id,
    )

    loaded = store.load("myproj", chat_id="branch-1")
    assert p.current_chat_id == source_chat
    assert [m["content"] for m in loaded.history] == ["review main.tex"]


def test_branch_chat_resolves_stale_main_source_by_client_message_id(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "review main.tex", client_message_id="client-turn-1")
    store.append_message(p, "assistant", "Here is the review.")

    store.branch_chat(
        "myproj",
        "main",
        "branch-1",
        "Branch",
        0,
        client_message_id="client-turn-1",
    )

    loaded = store.load("myproj", chat_id="branch-1")
    assert [m["content"] for m in loaded.history] == ["review main.tex"]


@pytest.mark.parametrize("initial_mode", ["plan", "edit"])
def test_restore_transient_project_mode_never_persists_auto(initial_mode):
    store = _tmp_store()
    store.create(**_base_args(mode=initial_mode))
    store.create_chat("myproj", "chat-myproj", "Chat")
    project = store.load("myproj")
    project.mode = "auto"

    import opalatex.agent_stdin as agent_stdin

    previous_project = agent_stdin.current_project
    previous_store = agent_stdin.current_store
    try:
        agent_stdin.current_project = project
        agent_stdin.current_store = store

        agent_stdin._restore_transient_project_mode(initial_mode, "chat_orchestrator")

        assert project.mode == initial_mode
        assert store.load("myproj").mode == initial_mode
        # The audit entry is diagnostic activity, not a chat message: it must not
        # occupy the model's context window.
        assert any(
            "Transient mode change ('auto') restored" in item["content"]
            for item in store.list_activity("myproj", project.current_chat_id)
        )
        assert not any(message["role"] == "system" for message in project.history)
    finally:
        agent_stdin.current_project = previous_project
        agent_stdin.current_store = previous_store


def test_create_plan_temporary_auto_does_not_save_project_mode(monkeypatch):
    store = _tmp_store()
    store.create(**_base_args(mode="plan"))
    project = store.load("myproj")

    import opalatex.tools as tools
    import opalatex.agent_stdin as agent_stdin

    async def run_approved_plan():
        raw_create_plan = getattr(tools.create_plan, "_func", None) or tools.create_plan
        task = asyncio.create_task(raw_create_plan("1. Inspect\n2. Edit"))
        while not agent_stdin._gui_input_pending:
            await asyncio.sleep(0)
        pending = next(iter(agent_stdin._gui_input_pending.values()))
        pending.set_result('{"response":"yes"}')
        return await task

    monkeypatch.setattr(agent_stdin, "print_event", lambda *_args, **_kwargs: None)
    tools.set_project_context(project, store)
    try:
        result = asyncio.run(run_approved_plan())

        assert "temporarily in 'auto' mode" in result
        assert project.mode == "auto"
        assert store.load("myproj").mode == "plan"
    finally:
        tools.set_project_context(None, None)


def test_supersede_chat_history_hides_suffix_without_deleting_rows(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    first_id = store.append_message(p, "user", "first")
    reply_id = store.append_message(p, "assistant", "first reply")
    second_id = store.append_message(p, "user", "second")

    superseded = store.supersede_chat_history_from_message(
        "myproj", p.current_chat_id, message_id=reply_id, superseded_by="edit-1"
    )

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    assert superseded == [reply_id, second_id]
    assert [m["content"] for m in loaded.history] == ["first"]
    assert loaded.history[0]["id"] == first_id

    # The rows are still on disk, marked and attributable to the edit that replaced them.
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    try:
        rows = conn.execute(
            "SELECT id, deleted_at, superseded_by FROM project_history WHERE project = ? ORDER BY id",
            ("myproj",),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == [first_id, reply_id, second_id]
    assert rows[0][1] == ""
    assert all(r[1] != "" and r[2] == "edit-1" for r in rows[1:])


def test_supersede_chat_history_accepts_client_message_id(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "first")
    target_id = store.append_message(p, "user", "second", client_message_id="cid-2")
    store.append_message(p, "assistant", "second reply")

    superseded = store.supersede_chat_history_from_message(
        "myproj", p.current_chat_id, client_message_id="cid-2"
    )

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    assert superseded[0] == target_id
    assert [m["content"] for m in loaded.history] == ["first"]


def _three_turns_with_memory_writes(store):
    """A chat whose every turn appends to the per-chat core memory mid-turn,
    the way `append_core_memory` does: after the question, before the answer."""
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    chat = p.current_chat_id
    ids = {}
    memory = ""
    for n in (1, 2, 3):
        ids[f"u{n}"] = store.append_message(p, "user", f"question {n}", client_message_id=f"cid-{n}")
        memory = memory + ("\n" if memory else "") + f"- learned in turn {n}"
        store.update_chat_core_memory("myproj", chat, memory)
        ids[f"r{n}"] = store.append_message(p, "assistant", f"answer {n}")
    return p, chat, ids


def test_supersede_restores_core_memory_written_by_superseded_turns(store):
    # Editing a question used to hide the old answers but keep what their turns
    # wrote to core memory, which the next prompt presents as established fact.
    _, chat, ids = _three_turns_with_memory_writes(store)

    store.supersede_chat_history_from_message("myproj", chat, message_id=ids["u2"])

    assert store.get_chat_core_memory("myproj", chat) == "- learned in turn 1"


def test_supersede_from_an_answer_drops_the_memory_its_turn_wrote(store):
    _, chat, ids = _three_turns_with_memory_writes(store)

    store.supersede_chat_history_from_message("myproj", chat, message_id=ids["r3"])

    assert store.get_chat_core_memory("myproj", chat) == (
        "- learned in turn 1\n- learned in turn 2"
    )


def test_supersede_of_the_first_message_restores_the_memory_before_it(store):
    _, chat, ids = _three_turns_with_memory_writes(store)

    store.supersede_chat_history_from_message("myproj", chat, client_message_id="cid-1")

    assert store.get_chat_core_memory("myproj", chat) == ""


def test_supersede_ignores_rows_already_superseded_before_the_cut(store):
    p, chat, ids = _three_turns_with_memory_writes(store)
    store.supersede_chat_history_from_message("myproj", chat, message_id=ids["u3"])
    retry_id = store.append_message(p, "user", "question 3 edited")
    store.update_chat_core_memory(
        "myproj", chat, "- learned in turn 1\n- learned in turn 2\n- learned in the retry"
    )
    store.append_message(p, "assistant", "answer 3 edited")

    store.supersede_chat_history_from_message("myproj", chat, message_id=retry_id)

    assert store.get_chat_core_memory("myproj", chat) == (
        "- learned in turn 1\n- learned in turn 2"
    )


def test_supersede_leaves_core_memory_alone_when_no_snapshot_was_recorded(store):
    # Rows stored before snapshots existed carry NULL: the memory at that point
    # is unknown, and wiping the chat's memory would lose what is still valid.
    import sqlite3

    _, chat, ids = _three_turns_with_memory_writes(store)
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE project_history SET core_memory_snapshot = NULL")
        conn.commit()
    finally:
        conn.close()
    before = store.get_chat_core_memory("myproj", chat)

    store.supersede_chat_history_from_message("myproj", chat, message_id=ids["u2"])

    assert store.get_chat_core_memory("myproj", chat) == before


def test_branch_before_message_inherits_core_memory_as_of_the_cut(store):
    _, chat, ids = _three_turns_with_memory_writes(store)

    store.branch_chat_before_message("myproj", chat, "branch-edit", "Edited", message_id=ids["u3"])

    assert store.get_chat_core_memory("myproj", "branch-edit") == (
        "- learned in turn 1\n- learned in turn 2"
    )
    assert store.get_chat_core_memory("myproj", chat).endswith("- learned in turn 3")


def test_branch_through_a_message_inherits_core_memory_as_of_that_message(store):
    _, chat, ids = _three_turns_with_memory_writes(store)

    store.branch_chat("myproj", chat, "through-answer", "B1", 0, message_id=ids["r2"])
    # A kept question whose answer is not copied carries nothing of that answer's turn.
    store.branch_chat("myproj", chat, "through-question", "B2", 0, message_id=ids["u2"])

    assert store.get_chat_core_memory("myproj", "through-answer") == (
        "- learned in turn 1\n- learned in turn 2"
    )
    assert store.get_chat_core_memory("myproj", "through-question") == "- learned in turn 1"


def test_an_edit_inside_a_branch_restores_the_copied_snapshots(store):
    _, chat, ids = _three_turns_with_memory_writes(store)
    copied = store.branch_chat_before_message(
        "myproj", chat, "branch-edit", "Edited", message_id=ids["u3"]
    )
    branch_u2 = next(m["id"] for m in copied if m["content"] == "question 2")

    store.supersede_chat_history_from_message("myproj", "branch-edit", message_id=branch_u2)

    assert store.get_chat_core_memory("myproj", "branch-edit") == "- learned in turn 1"


def test_supersede_chat_history_requires_a_stable_anchor(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "first")

    with pytest.raises(ValueError):
        store.supersede_chat_history_from_message("myproj", p.current_chat_id)

    with pytest.raises(ValueError):
        store.supersede_chat_history_from_message(
            "myproj", p.current_chat_id, client_message_id="does-not-exist"
        )

    loaded = store.load("myproj", chat_id=p.current_chat_id)
    assert [m["content"] for m in loaded.history] == ["first"]


def test_supersede_chat_history_hides_future_activity(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "first")
    store.append_activity(p, "thought", "first thought", agent="chat_orchestrator")
    reply_id = store.append_message(p, "assistant", "first reply")
    store.append_activity(p, "stream_chunk", "future stream", agent="chat_orchestrator")

    store.supersede_chat_history_from_message("myproj", p.current_chat_id, message_id=reply_id)

    activity = store.list_activity("myproj", p.current_chat_id)
    assert [(item["event"], item["content"]) for item in activity] == [
        ("thought", "first thought"),
    ]


def test_branch_chat_before_message_copies_earlier_messages(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    source_chat = p.current_chat_id
    att = {"type": "image", "data": "abc123", "mime": "image/jpeg", "name": "shot.jpg"}
    store.append_message(p, "user", "first", attachments=[att])
    store.append_message(p, "assistant", "first reply")
    anchor_id = store.append_message(p, "user", "second")

    copied = store.branch_chat_before_message(
        "myproj", source_chat, "branch-edit", "Edited", message_id=anchor_id
    )

    loaded = store.load("myproj", chat_id="branch-edit")
    source = store.load("myproj", chat_id=source_chat)
    assert [m["content"] for m in copied] == ["first", "first reply"]
    assert [m["content"] for m in loaded.history] == ["first", "first reply"]
    assert loaded.history[0]["_attachments"] == [att]
    # Branching must not touch the source chat.
    assert [m["content"] for m in source.history] == ["first", "first reply", "second"]


def test_branch_chat_before_message_requires_a_stable_anchor(store):
    store.create(**_base_args())
    store.create_chat("myproj", "chat-myproj", "Chat")
    p = store.load("myproj")
    store.append_message(p, "user", "first")

    with pytest.raises(ValueError):
        store.branch_chat_before_message("myproj", p.current_chat_id, "branch-x", "Edited")


# ---------------------------------------------------------------------------
# 7. list_projects ordering
# ---------------------------------------------------------------------------

def test_list_projects_most_recent_first(store):
    store.create(**_base_args(name="old_proj"))
    store.create(**_base_args(name="new_proj"))
    projects = store.list_projects()
    names = [p["name"] for p in projects]
    # new_proj was created after old_proj, so it should appear first
    assert names.index("new_proj") < names.index("old_proj")


def test_create_initializes_shadow_git(store, tmp_path):
    proj_dir = tmp_path / "my_new_project"
    p = store.create(
        name="shadow_git_test",
        mode="hybrid",
        model="fake/model",
        project_name="Shadow Git Test",
        project_path=str(proj_dir),
    )
    # Check that the internal shadow git directory exists.
    git_dir = proj_dir / ".opalatex" / ".shadowgit"
    assert git_dir.exists()
    assert git_dir.is_dir()


def test_set_project_context_loads_env_and_propagates_keys(tmp_path):
    import os
    from opalatex.tools import set_project_context
    from opalatex.project import ProjectData
    
    proj_dir = tmp_path / "my_env_project"
    proj_dir.mkdir()
    env_file = proj_dir / ".env"
    env_file.write_text("CUSTOM_VAR=my_value\nOPENAI_API_KEY=file_key\nOPENAI_API_BASE=file_base\nWORKER_API_KEY=file_w_key\nWORKER_API_BASE=file_w_base\n")
    
    # 1. Test loading from file
    p = ProjectData(name="test", project_path=str(proj_dir))
    set_project_context(p)
    
    assert os.environ.get("CUSTOM_VAR") == "my_value"
    assert os.environ.get("OPENAI_API_KEY") == "file_key"
    assert os.environ.get("OPENAI_API_BASE") == "file_base"
    assert os.environ.get("WORKER_API_KEY") == "file_w_key"
    assert os.environ.get("WORKER_API_BASE") == "file_w_base"
    
    # Clean up custom var
    os.environ.pop("CUSTOM_VAR", None)
    
    # 2. Test session properties overriding env
    p_with_keys = ProjectData(
        name="test",
        project_path=str(proj_dir),
        api_key="session_key",
        api_base="session_base",
        worker_api_key="session_w_key",
        worker_api_base="session_w_base"
    )
    set_project_context(p_with_keys)
    assert os.environ.get("OPENAI_API_KEY") == "session_key"
    assert os.environ.get("OPENAI_API_BASE") == "session_base"
    assert os.environ.get("WORKER_API_KEY") == "session_w_key"
    assert os.environ.get("WORKER_API_BASE") == "session_w_base"


def test_store_load_saves_and_loads_api_credentials(store, tmp_path):
    proj_dir = tmp_path / "creds_project"
    proj_dir.mkdir(exist_ok=True)
    p = store.create(
        name="creds_proj",
        mode="plan",
        model="fake/model",
        project_name="Creds Project",
        project_path=str(proj_dir),
        api_key="my_orch_key",
        api_base="my_orch_base",
        worker_api_key="my_worker_key",
        worker_api_base="my_worker_base"
    )
    
    loaded = store.load("creds_proj")
    assert loaded.api_key == "my_orch_key"
    assert loaded.api_base == "my_orch_base"
    assert loaded.worker_api_key == "my_worker_key"
    assert loaded.worker_api_base == "my_worker_base"

    # Now modify and save
    loaded.api_key = "new_orch_key"
    loaded.worker_api_key = "new_worker_key"
    store.save(loaded)

    reloaded = store.load("creds_proj")
    assert reloaded.api_key == "new_orch_key"
    assert reloaded.worker_api_key == "new_worker_key"


def test_provider_specific_api_credentials_are_saved_and_loaded(store, tmp_path):
    openai_dir = tmp_path / "openai_project"
    gemini_dir = tmp_path / "gemini_project"
    openai_dir.mkdir()
    gemini_dir.mkdir()

    store.create(
        name="openai_creds",
        mode="plan",
        model="openai/gpt-4o",
        project_name="OpenAI Creds",
        project_path=str(openai_dir),
        api_key="openai_key",
    )
    store.create(
        name="gemini_creds",
        mode="plan",
        model="gemini/gemini-2.5-flash",
        project_name="Gemini Creds",
        project_path=str(gemini_dir),
        api_key="gemini_key",
    )

    assert "OPENAI_API_KEY=openai_key" in (openai_dir / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=gemini_key" in (gemini_dir / ".env").read_text(encoding="utf-8")

    assert store.load("openai_creds").api_key == "openai_key"
    assert store.load("gemini_creds").api_key == "gemini_key"


def test_legacy_openai_api_key_is_still_loaded_for_non_openai_models(store, tmp_path):
    proj_dir = tmp_path / "legacy_project"
    proj_dir.mkdir()
    (proj_dir / ".env").write_text("OPENAI_API_KEY=legacy_key\nOPENAI_API_BASE=legacy_base\n", encoding="utf-8")

    store.create(
        name="legacy_creds",
        mode="plan",
        model="gemini/gemini-2.5-flash",
        project_name="Legacy Creds",
        project_path=str(proj_dir),
    )

    loaded = store.load("legacy_creds")
    assert loaded.api_key == "legacy_key"
    assert loaded.api_base == "legacy_base"




def test_a_legacy_think_value_is_not_reported_as_a_live_setting(store, tmp_path):
    """Rows written before thinking and inference params became model-catalog
    settings carry a `think` boolean and per-project sampling values. Reading them
    back as project settings is what let a stale value keep the Thinking panel
    closed for a model that was reasoning anyway, so they are dropped on every
    read path instead. `num_ctx` is not retired and must survive."""
    import json
    import sqlite3

    project_path = str(tmp_path / "legacy")
    os.makedirs(project_path)
    project = store.create(**_base_args(
        name="legacy", project_path=project_path, project_name="Legacy",
    ))

    # Write the pre-consolidation shape straight into the row.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE projects SET model_params=?, worker_model_params=? WHERE name=?",
            (
                json.dumps({
                    "think": True, "stream": True, "temperature": 0.4,
                    "top_k": 40, "reasoning_effort": "low", "num_ctx": 8192,
                }),
                json.dumps({"think": False, "stream": True}),
                project.name,
            ),
        )

    reloaded = store.load(project.name)
    assert "think" not in reloaded.model_params
    assert "think" not in reloaded.worker_model_params
    # Retired inference parameters cannot shadow the model catalog.
    for retired in ("temperature", "top_k", "reasoning_effort"):
        assert retired not in reloaded.model_params
    # Everything the project still owns on the row is untouched.
    assert reloaded.model_params["num_ctx"] == 8192
    assert reloaded.model_params["stream"] is True

    listed = [p for p in store.list_projects() if p["name"] == project.name][0]
    assert "think" not in listed["model_params"]
    assert "think" not in listed["worker_model_params"]
