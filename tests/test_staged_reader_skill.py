"""Unit tests for the staged-reader skill and its plan/collect helper script."""

import importlib.util
import os
import subprocess
import sys

import pytest

from gilzabots.assetstore import list_assets
from gilzabots.skills import discover_skills, parse_skill_md

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILL_DIR = os.path.join(_REPO_ROOT, "skills", "staged-reader")
_SCRIPT = os.path.join(_SKILL_DIR, "scripts", "staged_reader.py")

_spec = importlib.util.spec_from_file_location("staged_reader", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

census = _mod.census
suggest_window = _mod.suggest_window


def _run(*args):
    return subprocess.run(
        [sys.executable, _SCRIPT, *args], capture_output=True, text=True
    )


def _parse_report(stdout: str) -> dict:
    """Turn the script's `KEY: value` output into a dict."""
    out = {}
    for line in stdout.splitlines():
        if ": " in line and not line.startswith(" "):
            key, _, value = line.partition(": ")
            out[key.strip()] = value.strip()
    return out


def _write_lines(path, count, text="line {i} content"):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(1, count + 1):
            f.write(text.format(i=i) + "\n")
    return str(path)


# ---------------------------------------------------------------------------
# Skill manifest
# ---------------------------------------------------------------------------

def test_skill_manifest_is_discoverable_and_declares_worker_model():
    meta = parse_skill_md(_SKILL_DIR)
    assert meta is not None
    assert meta["name"] == "staged-reader"
    assert meta["model"] == "worker"
    # The description is the only thing the orchestrator sees when routing, so it
    # has to name both the trigger (a file too large to read) and the mechanism.
    desc = meta["description"].lower()
    assert "window" in desc
    assert "large" in desc or "too large" in desc

    body = meta["body"]
    assert "read_content_pos" in body
    assert "NEXT_START" in body
    # The whole point of the skill: never fall back to a whole-file read.
    assert "Never call `read_file` on the target file" in body


def test_skill_ships_a_light_profile_body():
    light = parse_skill_md(_SKILL_DIR, profile="light")
    full = parse_skill_md(_SKILL_DIR)
    assert light is not None
    assert light["body"] != full["body"]
    assert len(light["body"]) < len(full["body"])
    # Frontmatter always comes from the canonical SKILL.md.
    assert light["name"] == "staged-reader"
    assert light["model"] == "worker"
    for token in ("NEXT_START", "EOF", "read_content_pos", "MAX_PASSES"):
        assert token in light["body"]


def test_skill_is_discovered_from_the_repo_skills_dir():
    names = {s["name"] for s in discover_skills()}
    assert "staged-reader" in names


def test_skill_is_registered_in_the_asset_store():
    entry = [a for a in list_assets("skill") if a["id"] == "staged-reader"]
    assert entry, "staged-reader is not registered in the asset store"
    zip_path = entry[0]["_zip"]
    assert os.path.isfile(zip_path)
    import zipfile
    names = zipfile.ZipFile(zip_path).namelist()
    assert "staged-reader/SKILL.md" in names
    assert "staged-reader/scripts/staged_reader.py" in names


def test_orchestrator_prompts_teach_the_staged_read_loop():
    """Every orchestrator prompt variant must drive the loop the same way."""
    base = os.path.join(_REPO_ROOT, "skills", "chat-orchestrator")
    for variant in ("SKILL.md", "SKILL.light.md"):
        with open(os.path.join(base, variant), encoding="utf-8") as f:
            text = f.read()
        assert "NEXT_START" in text, variant
        assert "EOF: yes" in text, variant
        assert "BUDGET REACHED" in text, variant


# ---------------------------------------------------------------------------
# census / suggest_window
# ---------------------------------------------------------------------------

def test_census_counts_lines_bytes_and_longest_line(tmp_path):
    path = tmp_path / "sample.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("short\n")
        f.write("x" * 40 + "\n")
        f.write("mid\n")

    total_lines, size, max_len, max_line, total_chars, decode_errors = census(str(path))
    assert total_lines == 3
    assert size == os.path.getsize(path)
    assert max_len == 41  # 40 chars plus the newline
    assert max_line == 2
    assert total_chars == len("short\n") + 41 + len("mid\n")
    assert decode_errors == 0


def test_census_rejects_binary_files(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"PK\x03\x04\x00\x00binary payload")
    with pytest.raises(ValueError, match="looks binary"):
        census(str(path))


def test_census_falls_back_to_latin1_and_reports_it(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes(b"ok line\n" + "condensação".encode("latin-1") + b"\n")
    total_lines, _, _, _, _, decode_errors = census(str(path))
    assert total_lines == 2
    assert decode_errors == 1


def test_suggest_window_keeps_a_window_inside_the_budget():
    # 1,000 lines of 100 chars each, 12,000-char budget -> ~120 lines per window.
    window = suggest_window(total_lines=1000, total_chars=100_000, max_len=100, budget_chars=12_000)
    assert window == 120
    assert window * 100 <= 12_000


def test_suggest_window_shrinks_when_a_single_line_blows_the_budget():
    window = suggest_window(total_lines=100, total_chars=1_000, max_len=50_000, budget_chars=12_000)
    assert window == _mod.MIN_WINDOW_LINES


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def test_plan_reports_totals_and_a_contiguous_schedule(tmp_path):
    path = _write_lines(tmp_path / "big.log", 1000)
    res = _run("plan", path, "--window", "200", "--max-windows", "10")
    assert res.returncode == 0, res.stderr

    report = _parse_report(res.stdout)
    assert report["TOTAL_LINES"] == "1,000"
    assert report["WINDOW_LINES"].startswith("200")
    assert report["WINDOWS_FROM_1"] == "5"
    assert report["NEXT_START"] == "1"

    rows = [line.strip() for line in res.stdout.splitlines() if line.startswith("  ")]
    assert rows[0] == "1: 1-200"
    assert rows[-1] == "5: 801-1000"
    # Windows must tile the file with no gap and no overlap.
    ends = [int(r.split(": ")[1].split("-")[1]) for r in rows]
    starts = [int(r.split(": ")[1].split("-")[0]) for r in rows]
    assert all(s == e + 1 for s, e in zip(starts[1:], ends[:-1]))


def test_plan_honours_start_and_clips_the_last_window(tmp_path):
    path = _write_lines(tmp_path / "big.log", 450)
    res = _run("plan", path, "--window", "200", "--start", "301")
    assert res.returncode == 0, res.stderr
    report = _parse_report(res.stdout)
    assert report["WINDOWS_FROM_301"] == "1"
    assert report["NEXT_START"] == "301"
    assert "1: 301-450" in res.stdout


def test_plan_derives_the_window_from_the_budget_when_not_forced(tmp_path):
    path = _write_lines(tmp_path / "wide.log", 500, text="{i}:" + "y" * 97)
    res = _run("plan", path, "--budget-chars", "5000")
    assert res.returncode == 0, res.stderr
    window = int(_parse_report(res.stdout)["WINDOW_LINES"].split(" ")[0].replace(",", ""))
    assert 1 <= window <= 50  # ~100 chars per line against a 5,000-char budget


def test_plan_warns_when_one_line_exceeds_the_budget(tmp_path):
    path = tmp_path / "minified.json"
    with open(path, "w", encoding="utf-8") as f:
        f.write("small\n")
        f.write("z" * 30_000 + "\n")
    res = _run("plan", str(path), "--budget-chars", "12000")
    assert res.returncode == 0, res.stderr
    assert "WARNING: line 2" in res.stdout
    assert "truncated by read_content_pos" in res.stdout


def test_plan_fails_loudly_on_a_missing_file(tmp_path):
    res = _run("plan", str(tmp_path / "nope.log"))
    assert res.returncode == 1
    assert "file not found" in res.stderr


def test_plan_refuses_binary_input(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    res = _run("plan", str(path))
    assert res.returncode == 1
    assert "looks binary" in res.stderr


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def _notes(tmp_path, ranges):
    d = tmp_path / "notes"
    d.mkdir(exist_ok=True)
    for start, end in ranges:
        (d / f"win_{start}-{end}.md").write_text(f"finding for {start}-{end}\n", encoding="utf-8")
    return str(d)


def test_collect_merges_notes_in_line_order(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 200), (201, 400), (401, 600)])
    res = _run("collect", notes_dir)
    assert res.returncode == 0, res.stderr

    report = _parse_report(res.stdout)
    assert report["WINDOWS"] == "3"
    assert report["COVERED"] == "1-600"
    assert report["GAPS"] == "none"

    digest = open(report["OUTPUT"], encoding="utf-8").read()
    assert digest.index("Lines 1-200") < digest.index("Lines 201-400") < digest.index("Lines 401-600")
    assert "finding for 401-600" in digest


def test_collect_sorts_numerically_not_lexicographically(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 100), (101, 200), (1001, 1100)])
    res = _run("collect", notes_dir)
    assert res.returncode == 0, res.stderr
    digest = open(_parse_report(res.stdout)["OUTPUT"], encoding="utf-8").read()
    # Lexicographic sorting would put "win_1001-..." between 1-100 and 101-200.
    assert digest.index("Lines 101-200") < digest.index("Lines 1001-1100")


def test_collect_reports_a_gap_so_partial_coverage_is_not_mistaken_for_complete(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 200), (401, 600)])
    res = _run("collect", notes_dir)
    assert res.returncode == 0, res.stderr
    report = _parse_report(res.stdout)
    assert report["GAPS"] == "201-400"
    assert "Gaps: 201-400" in open(report["OUTPUT"], encoding="utf-8").read()


def test_collect_reports_overlapping_windows(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 200), (150, 300)])
    res = _run("collect", notes_dir)
    assert res.returncode == 0, res.stderr
    report = _parse_report(res.stdout)
    assert report["GAPS"] == "none"
    assert report["OVERLAPS"] == "150-200"


def test_collect_ignores_unrelated_files_but_not_its_own_digest(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 200)])
    open(os.path.join(notes_dir, "scratch.txt"), "w").write("ignore me")

    first = _run("collect", notes_dir)
    assert first.returncode == 0, first.stderr
    assert _parse_report(first.stdout)["IGNORED_FILES"] == "scratch.txt"

    # Re-running must not list the digest it just wrote as an ignored stray file.
    second = _run("collect", notes_dir)
    assert second.returncode == 0, second.stderr
    assert _parse_report(second.stdout)["IGNORED_FILES"] == "scratch.txt"
    assert _parse_report(second.stdout)["WINDOWS"] == "1"


def test_collect_honours_an_explicit_output_path(tmp_path):
    notes_dir = _notes(tmp_path, [(1, 50)])
    out = str(tmp_path / "final_digest.md")
    res = _run("collect", notes_dir, "--output", out)
    assert res.returncode == 0, res.stderr
    assert _parse_report(res.stdout)["OUTPUT"] == out
    assert os.path.isfile(out)


def test_collect_fails_when_there_is_nothing_to_merge(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = _run("collect", str(empty))
    assert res.returncode == 1
    assert "no window notes" in res.stderr

    missing = _run("collect", str(tmp_path / "absent"))
    assert missing.returncode == 1
    assert "notes directory not found" in missing.stderr


# ---------------------------------------------------------------------------
# The cursor protocol SKILL.md documents, checked against the real tool
# ---------------------------------------------------------------------------
# SKILL.md §4 tells the worker how to derive READ / TOTAL_LINES / NEXT_START / EOF
# from read_content_pos's own output. If the tool ever stops emitting that trailing
# note the way the skill describes, these tests fail here instead of the worker
# silently skipping a capped remainder.

_NOTE_RE = __import__("re").compile(
    r"\[Showing lines (\d+)-(\d+) of ([\d,]+) total lines"
)


def _cursor_from_tool_result(result: str, requested_start: int, requested_end: int) -> dict:
    """Apply SKILL.md §4 verbatim to a read_content_pos result."""
    if result.startswith("No content read:"):
        return {"read": None, "total": None, "next_start": None, "eof": True}
    m = _NOTE_RE.search(result)
    if not m:
        # No trailing note at all => the window reached the end of the file.
        return {
            "read": (requested_start, requested_end),
            "total": None,
            "next_start": None,
            "eof": True,
        }
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", ""))
    return {"read": (start, end), "total": total, "next_start": end + 1, "eof": False}


@pytest.fixture
def paging(monkeypatch):
    """Drive tools.read_content_pos with a controlled context window."""
    import asyncio
    from gilzabots import token_usage, tools

    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    token_usage.set_context_scope("staged-reader-tests")

    class _Session:
        def __init__(self, num_ctx):
            self.model_params = {"num_ctx": num_ctx}
            self.history = []

    def _drive(target, start, end, num_ctx=128000, used_tokens=1000):
        monkeypatch.setattr(tools, "_PROJECT_SESSION", _Session(num_ctx))
        token_usage.set_context_scope("reset")
        token_usage.set_context_scope("staged-reader-tests")
        token_usage.record_context_tokens(used_tokens)
        monkeypatch.setattr(tools, "_resolve_path", lambda p: str(target))
        return asyncio.run(tools.read_content_pos._func(str(target), start, end))

    yield _drive

    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")


def test_window_that_reaches_eof_carries_no_note_and_is_read_as_eof(tmp_path, paging):
    target = tmp_path / "small.log"
    target.write_text("".join(f"line {i}\n" for i in range(1, 21)), encoding="utf-8")

    cursor = _cursor_from_tool_result(paging(target, 11, 20), 11, 20)
    assert cursor["eof"] is True
    assert cursor["next_start"] is None


def test_mid_file_window_yields_the_next_start(tmp_path, paging):
    target = tmp_path / "big.log"
    target.write_text("".join(f"line {i}\n" for i in range(1, 1001)), encoding="utf-8")

    cursor = _cursor_from_tool_result(paging(target, 1, 200), 1, 200)
    assert cursor["eof"] is False
    assert cursor["read"] == (1, 200)
    assert cursor["total"] == 1000
    assert cursor["next_start"] == 201


def test_a_capped_window_advances_by_what_was_read_not_by_what_was_asked(tmp_path, paging):
    target = tmp_path / "big.log"
    target.write_text("".join(f"line {i}\n" for i in range(1, 1001)), encoding="utf-8")

    # A tight budget forces read_content_pos to cap the window well short of line 1000.
    result = paging(target, 1, 1000, num_ctx=2000, used_tokens=1500)
    cursor = _cursor_from_tool_result(result, 1, 1000)

    assert cursor["eof"] is False
    read_start, read_end = cursor["read"]
    assert read_start == 1
    assert read_end < 1000, "expected the tool to cap this window"
    assert cursor["next_start"] == read_end + 1
    # Trusting `start + window` here would skip every line between read_end and 1000.
    assert cursor["next_start"] < 1001
    assert "capped to fit the remaining context budget" in result


def test_start_beyond_eof_is_reported_as_eof(tmp_path, paging):
    target = tmp_path / "small.log"
    target.write_text("line 1\nline 2\n", encoding="utf-8")

    cursor = _cursor_from_tool_result(paging(target, 50, 60), 50, 60)
    assert cursor["eof"] is True
    assert cursor["read"] is None
