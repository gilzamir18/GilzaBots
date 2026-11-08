"""read_file must refuse a read that cannot fit the remaining context window.

An unbounded whole-file read is the fastest way to destroy a turn: the result is
appended to history, MemGPT cannot evict the current user message to make room,
and the provider then truncates the request from the front -- dropping the very
question being answered. The tool fails with a diagnostic instead, so the model
is told to page through the file rather than silently receiving partial data.
"""
import asyncio

import pytest

from gilzabots import token_usage, tools


def _read_file(path):
    """Call read_file past the @gilzabots_tool FunctionBlock/permission wrapper."""
    return asyncio.run(tools.read_file._func(path))


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    token_usage.set_context_scope("read-file-tests")
    yield
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")


class _Session:
    def __init__(self, num_ctx):
        self.model_params = {"num_ctx": num_ctx}
        self.history = []


def _use_window(monkeypatch, num_ctx, used_tokens):
    monkeypatch.setattr(tools, "_PROJECT_SESSION", _Session(num_ctx))
    token_usage.record_context_tokens(used_tokens)


def test_small_file_is_read_normally(tmp_path, monkeypatch):
    target = tmp_path / "main.tex"
    target.write_text("\\documentclass{article}\n", encoding="utf-8")
    _use_window(monkeypatch, num_ctx=128000, used_tokens=1000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    assert "documentclass" in _read_file(str(target))


def test_oversized_file_is_refused_with_a_paging_diagnostic(tmp_path, monkeypatch):
    # A prose/source file has no streaming skill behind it, so the model is told
    # to page through it. Structured data/log files take the routing branch below.
    target = tmp_path / "thesis.tex"
    target.write_text("x" * 400_000, encoding="utf-8")
    # 128K window, almost all of it already occupied.
    _use_window(monkeypatch, num_ctx=128000, used_tokens=120_000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    with pytest.raises(ValueError) as excinfo:
        _read_file(str(target))

    message = str(excinfo.value)
    assert "does not fit the remaining context budget" in message
    assert "read_content_pos" in message
    assert "search_code" in message


def test_oversized_log_routes_the_orchestrator_to_a_skill(tmp_path, monkeypatch):
    target = tmp_path / "Experiment_Batch.jsonl"
    target.write_text("x" * 400_000, encoding="utf-8")
    _use_window(monkeypatch, num_ctx=128000, used_tokens=120_000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))
    tools.set_worker_context(False)

    with pytest.raises(ValueError) as excinfo:
        _read_file(str(target))

    message = str(excinfo.value)
    assert "large data/log file" in message
    assert "run_skill" in message


def test_oversized_log_does_not_tell_a_worker_to_use_run_skill(tmp_path, monkeypatch):
    # Skill workers are not given the run_skill tool, so the orchestrator-facing
    # advice would send them chasing a tool they cannot call -- which is what
    # pushed a small model into improvising broken run_command retries instead.
    target = tmp_path / "Experiment_Batch.jsonl"
    target.write_text("x" * 400_000, encoding="utf-8")
    _use_window(monkeypatch, num_ctx=128000, used_tokens=120_000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))
    tools.set_worker_context(True)
    try:
        with pytest.raises(ValueError) as excinfo:
            _read_file(str(target))
    finally:
        tools.set_worker_context(False)

    message = str(excinfo.value)
    assert "large data/log file" in message
    assert "run_skill" not in message.replace("do NOT have a run_skill tool", "")
    assert "run_command" in message


def test_worker_context_flag_round_trips():
    tools.set_worker_context(True)
    assert tools.in_helper_context() is True
    tools.set_worker_context(False)
    assert tools.in_helper_context() is False


def test_exhausted_window_reports_exhaustion_rather_than_paging(tmp_path, monkeypatch):
    target = tmp_path / "notes.tex"
    target.write_text("y" * 5_000, encoding="utf-8")
    _use_window(monkeypatch, num_ctx=8192, used_tokens=8192)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    with pytest.raises(ValueError) as excinfo:
        _read_file(str(target))

    assert "context window is exhausted" in str(excinfo.value)


def test_budget_uses_the_measured_occupancy_not_the_history_estimate(monkeypatch):
    # project_history excludes tool results and the system prompt, so the old
    # char/4 estimate reported a nearly free window during a full turn.
    session = _Session(num_ctx=128000)
    session.history = [{"role": "user", "content": "hi"}]
    monkeypatch.setattr(tools, "_PROJECT_SESSION", session)

    token_usage.record_context_tokens(120_000)

    assert tools.used_context_tokens() == 120_000
    # 8000 tokens left, half of it granted to a single tool result.
    assert tools.free_context_chars() == pytest.approx(8000 * 4 * 0.5, rel=0.01)


def test_budget_falls_back_to_the_estimate_before_any_measurement(monkeypatch):
    session = _Session(num_ctx=8192)
    session.history = [{"role": "user", "content": "a" * 400}]
    monkeypatch.setattr(tools, "_PROJECT_SESSION", session)

    assert tools.used_context_tokens() > 0
    assert tools.free_context_chars() > 0
