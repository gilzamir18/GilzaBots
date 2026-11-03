"""read_content_pos must page through large files without ever reproducing the
unbounded-read overflow read_file refuses to perform.

read_file's own refusal message points models at search_code -> read_content_pos as the
paging escape hatch. If that escape hatch had no budget of its own, a model that asked
for a wide start_pos/end_pos range (or simply retried the same range read_file just
refused) would sail through with zero protection. read_content_pos instead caps the
returned slice to the remaining context budget and reports, in the tool result itself,
the total line count, how many lines remain, and the start_pos to resume from -- so a
capped page is never mistaken for the full requested range.
"""
import asyncio

import pytest

from gilzabots import token_usage, tools


def _read_content_pos(path, start_pos, end_pos):
    """Call read_content_pos past the @gilzabots_tool FunctionBlock/permission wrapper."""
    return asyncio.run(tools.read_content_pos._func(path, start_pos, end_pos))


@pytest.fixture(autouse=True)
def clean_context(monkeypatch):
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    token_usage.set_context_scope("read-content-pos-tests")
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


def test_range_reaching_eof_is_returned_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "notes.tex"
    target.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    _use_window(monkeypatch, num_ctx=128000, used_tokens=1000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    result = _read_content_pos(str(target), 2, 3)

    assert result == "line 2\nline 3\n"


def test_partial_range_with_plenty_of_budget_reports_remaining_lines(tmp_path, monkeypatch):
    target = tmp_path / "big.tex"
    target.write_text("".join(f"line {i}\n" for i in range(1, 101)), encoding="utf-8")
    _use_window(monkeypatch, num_ctx=128000, used_tokens=1000)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    result = _read_content_pos(str(target), 1, 10)

    assert result.startswith("line 1\n")
    assert "line 10\n" in result
    assert "90 more line(s) remain" in result
    assert f"read_content_pos({str(target)!r}, 11, <end_pos>)" in result


def test_oversized_range_is_capped_to_budget_with_continuation_hint(tmp_path, monkeypatch):
    target = tmp_path / "Experiment_Batch.jsonl"
    target.write_text("".join(f"line {i:06d} " + "x" * 90 + "\n" for i in range(1, 2001)), encoding="utf-8")
    # 128K window, almost all of it already occupied -> a tiny per-call budget.
    _use_window(monkeypatch, num_ctx=128000, used_tokens=127_900)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    result = _read_content_pos(str(target), 1, 2000)

    assert "capped to fit the remaining context budget" in result
    assert "you asked through line 2000" in result
    assert "more line(s) remain" in result
    # Forward progress: at least one line was returned, and it's a strict prefix
    # of the requested range so a follow-up call with a higher start_pos is
    # guaranteed to make further progress instead of looping forever.
    assert result.startswith("line 000001 ")


def test_exhausted_window_reports_exhaustion_rather_than_paging(tmp_path, monkeypatch):
    target = tmp_path / "notes.tex"
    target.write_text("line 1\nline 2\n", encoding="utf-8")
    _use_window(monkeypatch, num_ctx=8192, used_tokens=8192)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    with pytest.raises(ValueError) as excinfo:
        _read_content_pos(str(target), 1, 2)

    assert "context window is exhausted" in str(excinfo.value)


def test_single_line_longer_than_the_whole_budget_still_makes_progress(tmp_path, monkeypatch):
    target = tmp_path / "minified.json"
    target.write_text("x" * 5_000 + "\n" + "line 2\n", encoding="utf-8")
    # Small window so a single 5,000-char line cannot fit the per-call budget.
    _use_window(monkeypatch, num_ctx=2000, used_tokens=0)
    monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))

    result = _read_content_pos(str(target), 1, 2)

    assert "was itself too long to fit and was cut short" in result
    assert len(result) < 5_000 + 200  # cut down, not the full 5,000-char line plus note
