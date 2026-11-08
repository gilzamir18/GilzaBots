"""Hooks and restored files around a compaction.

`gilzabots/hooks.py` runs the user's commands from `<gilzabots home>/hooks.json`
(Claude Code's format and exit-code contract); `gilzabots/compaction.py` runs the
PreCompact/PostCompact hooks around a compaction and re-reads the files the
conversation was working on into the new context. The hook tests run real
commands through the system shell.
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenticblocks.blocks.llm.compaction import CompactionError
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from gilzabots import compaction, hooks
from gilzabots.compaction import attach_compaction_extensions, compaction_payload, restore_recent_files


SUMMARY_REPLY = "<summary>1. Primary Request and Intent: ship it.</summary>"


def _answer(content):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _script(tmp_path, name, body):
    """A Python hook script and the shell command that runs it."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


@pytest.fixture
def hooks_file(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    monkeypatch.setattr(hooks, "hooks_file_path", lambda: str(path))

    def write(config):
        path.write_text(json.dumps({"hooks": config}), encoding="utf-8")
        return str(path)

    return write


def _call(call_id, name, path):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps({"path": path})}}


def _history_with_files(*calls):
    history = [{"role": "user", "content": "work on the files"}]
    for i, (name, path) in enumerate(calls):
        history.append({"role": "assistant", "content": "", "tool_calls": [_call(f"c{i}", name, path)]})
        history.append({"role": "tool", "tool_call_id": f"c{i}", "name": name, "content": "ok"})
    history.append({"role": "assistant", "content": "done"})
    return history


# ── configuration ────────────────────────────────────────────────────────────

def test_no_file_means_no_hooks(hooks_file):
    assert hooks.load_hooks() == {}


@pytest.mark.parametrize("config,message", [
    ({"Nope": []}, "unknown hook event"),
    ({"PreCompact": {}}, "must be a list"),
    ({"PreCompact": [{"hooks": []}]}, "non-empty list"),
    ({"PreCompact": [{"matcher": "(", "hooks": [{"command": "x"}]}]}, "regular expression"),
    ({"PreCompact": [{"hooks": [{"type": "prompt", "command": "x"}]}]}, "unsupported hook type"),
    ({"PreCompact": [{"hooks": [{"command": ""}]}]}, "non-empty string"),
    ({"PreCompact": [{"hooks": [{"command": "x", "timeout": 0}]}]}, "timeout"),
])
def test_a_broken_configuration_is_reported_with_its_place(hooks_file, config, message):
    hooks_file(config)
    with pytest.raises(hooks.HookConfigError, match=message):
        hooks.load_hooks()


def test_unreadable_json_is_an_error(tmp_path, monkeypatch):
    path = tmp_path / "hooks.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(hooks, "hooks_file_path", lambda: str(path))
    with pytest.raises(hooks.HookConfigError, match="cannot be read"):
        hooks.load_hooks()


# ── running commands ─────────────────────────────────────────────────────────

def test_a_hook_receives_the_event_and_its_output_is_returned(tmp_path, hooks_file):
    command = _script(tmp_path, "echo.py", (
        "import json, os, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(data['hook_event_name'], data['trigger'], os.environ['GILZABOTS_HOOK_EVENT'])\n"
    ))
    hooks_file({"PreCompact": [{"matcher": "auto", "hooks": [{"command": command}]}]})

    results = asyncio.run(hooks.run_hooks("PreCompact", "auto", {"trigger": "auto"}, cwd=str(tmp_path)))

    assert [r.outcome for r in results] == ["ok"]
    assert results[0].output == "PreCompact auto PreCompact"


def test_the_matcher_selects_by_trigger(tmp_path, hooks_file):
    command = _script(tmp_path, "ok.py", "print('ran')\n")
    hooks_file({"PreCompact": [{"matcher": "auto", "hooks": [{"command": command}]}]})
    assert asyncio.run(hooks.run_hooks("PreCompact", "manual", {}, cwd=str(tmp_path))) == []


def test_exit_code_two_blocks_and_stops_the_later_hooks(tmp_path, hooks_file):
    block = _script(tmp_path, "block.py", "import sys\nsys.stderr.write('no backup')\nsys.exit(2)\n")
    later = _script(tmp_path, "later.py", "print('should not run')\n")
    hooks_file({"PreCompact": [{"hooks": [{"command": block}, {"command": later}]}]})

    results = asyncio.run(hooks.run_hooks("PreCompact", "manual", {}, cwd=str(tmp_path)))

    assert [r.outcome for r in results] == ["blocked"]
    assert results[0].message == "no backup"


def test_exit_code_two_does_not_block_an_event_that_cannot_be_blocked(tmp_path, hooks_file):
    command = _script(tmp_path, "two.py", "import sys\nsys.exit(2)\n")
    hooks_file({"PostCompact": [{"hooks": [{"command": command}]}]})
    results = asyncio.run(hooks.run_hooks("PostCompact", "manual", {}, cwd=str(tmp_path)))
    assert [r.outcome for r in results] == ["error"]


def test_other_failures_are_reported_and_do_not_block(tmp_path, hooks_file):
    fail = _script(tmp_path, "fail.py", "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
    slow = _script(tmp_path, "slow.py", "import time\ntime.sleep(10)\n")
    big = _script(tmp_path, "big.py", f"print('x' * {hooks.MAX_HOOK_OUTPUT_CHARS + 1})\n")
    hooks_file({"PreCompact": [{"hooks": [
        {"command": fail}, {"command": slow, "timeout": 1}, {"command": big},
    ]}]})

    results = asyncio.run(hooks.run_hooks("PreCompact", "manual", {}, cwd=str(tmp_path)))

    assert [r.outcome for r in results] == ["error", "timeout", "error"]
    assert results[0].message == "boom"
    assert "not used" in results[2].message and results[2].output == ""


# ── restored files ───────────────────────────────────────────────────────────

def test_recent_files_are_restored_most_recent_first_and_skips_are_named(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "b.tex").write_text("\\section{B}\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    history = _history_with_files(
        ("read_file", "a.py"),
        ("edit_file", str(tmp_path / "b.tex")),
        ("read_file", "bin.dat"),
        ("read_file", "gone.py"),
        ("read_file", str(outside)),
        ("read_document", "a.py"),
        ("read_file", "a.py"),
    )

    block, restored, skipped = restore_recent_files(history, str(tmp_path), "gpt-4o", 100_000)

    assert restored == ["a.py", "b.tex"]
    assert {s["path"]: s["reason"] for s in skipped} == {
        str(outside): "outside the project folder",
        "gone.py": "no longer exists",
        "bin.dat": "not a text file",
    }
    assert block.index('<file path="a.py">') < block.index('<file path="b.tex">')
    assert "print('a')" in block and "\\section{B}" in block
    assert "secret" not in block


def test_large_files_and_the_budget_skip_instead_of_cutting(tmp_path, monkeypatch):
    (tmp_path / "big.txt").write_text("word " * 400, encoding="utf-8")
    (tmp_path / "s1.txt").write_text("word " * 30, encoding="utf-8")
    (tmp_path / "s2.txt").write_text("word " * 30, encoding="utf-8")
    monkeypatch.setattr(compaction, "MAX_TOKENS_PER_RESTORED_FILE", 100)
    monkeypatch.setattr(compaction, "MAX_RESTORED_TOKENS", 50)
    history = _history_with_files(("read_file", "s2.txt"), ("read_file", "s1.txt"), ("read_file", "big.txt"))

    block, restored, skipped = restore_recent_files(history, str(tmp_path), "gpt-4o", None)

    assert restored == ["s1.txt"]
    reasons = {s["path"]: s["reason"] for s in skipped}
    assert reasons["big.txt"].startswith("too large to restore")
    assert reasons["s2.txt"] == "the restoration budget was already spent"
    assert block.count("<file path=") == 1


def test_at_most_five_files_are_restored(tmp_path):
    calls = []
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text(f"file {i}", encoding="utf-8")
        calls.append(("read_file", f"f{i}.txt"))
    _block, restored, _skipped = restore_recent_files(_history_with_files(*calls), str(tmp_path), "gpt-4o", None)
    assert restored == ["f6.txt", "f5.txt", "f4.txt", "f3.txt", "f2.txt"]


# ── end to end through the agent ─────────────────────────────────────────────

def _agent_on(tmp_path, history):
    agent = MemGPTAgentBlock(name="chat_orchestrator", system_prompt="s", model="gpt-4o", max_context_tokens=100_000)
    agent.internal_history = history
    sent = []

    async def fake(messages, **_kw):
        sent.append(messages)
        return _answer(SUMMARY_REPLY)

    agent._acompletion = fake
    project = SimpleNamespace(name="proj", project_path=str(tmp_path), current_chat_id="chat-1")
    attach_compaction_extensions(agent, project)
    return agent, sent


def test_hooks_and_restored_files_shape_the_compacted_context(tmp_path, hooks_file):
    (tmp_path / "main.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    pre = _script(tmp_path, "pre.py", (
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "assert 'work on the files' in data['transcript']\n"
        "print('Keep the exact error messages.')\n"
    ))
    post = _script(tmp_path, "post.py", (
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print('Reminder: the summary said ' + data['summary'][:18])\n"
    ))
    hooks_file({"PreCompact": [{"hooks": [{"command": pre}]}], "PostCompact": [{"hooks": [{"command": post}]}]})
    agent, sent = _agent_on(tmp_path, _history_with_files(("read_file", "main.py")))

    event = asyncio.run(agent.compact("focus on main"))

    assert "focus on main\n\nKeep the exact error messages." in sent[0][1]["content"]
    content = agent.internal_history[0]["content"]
    assert '<file path="main.py">\ndef main():\n    return 42\n' in content
    assert "Additional context from a PostCompact hook" in content
    assert "Reminder: the summary said 1. Primary" in content

    payload = compaction_payload(event)
    assert payload["restored_files"] == ["main.py"]
    assert [h["outcome"] for h in payload["hooks"]] == ["ok", "ok"]


def test_a_pre_compact_hook_can_block_the_compaction(tmp_path, hooks_file):
    block = _script(tmp_path, "block.py", "import sys\nsys.stderr.write('chat not backed up')\nsys.exit(2)\n")
    hooks_file({"PreCompact": [{"hooks": [{"command": block}]}]})
    agent, sent = _agent_on(tmp_path, _history_with_files())
    before = list(agent.internal_history)

    with pytest.raises(CompactionError, match="chat not backed up"):
        asyncio.run(agent.compact())
    assert sent == [] and agent.internal_history == before


def test_a_broken_hooks_file_fails_the_compaction_with_its_reason(tmp_path, hooks_file):
    hooks_file({"Unknown": []})
    agent, sent = _agent_on(tmp_path, _history_with_files())
    with pytest.raises(CompactionError, match="unknown hook event"):
        asyncio.run(agent.compact())
    assert sent == []


def test_the_hooks_command_lists_the_configuration(tmp_path, hooks_file, monkeypatch):
    from gilzabots import terminal as T
    from gilzabots.cli_commands import REPLState, _registry

    lines = []
    monkeypatch.setattr(T, "info", lambda *a, **_k: lines.append(" ".join(map(str, a))))
    monkeypatch.setattr(T, "error", lambda *a, **_k: lines.append("ERROR " + " ".join(map(str, a))))
    monkeypatch.setattr(T.console, "print", lambda *a, **_k: lines.append(" ".join(map(str, a))))
    state = REPLState(SimpleNamespace(), None)

    asyncio.run(_registry.dispatch(state, "/hooks", []))
    assert "No hooks are configured" in lines[-1]

    hooks_file({"PreCompact": [{"matcher": "auto", "hooks": [{"command": "save.sh", "timeout": 30}]}]})
    asyncio.run(_registry.dispatch(state, "/hooks", []))
    assert lines[-1].strip() == "PreCompact [auto] save.sh (timeout 30 s)"
    assert not _registry.is_cli_only("/hooks")
