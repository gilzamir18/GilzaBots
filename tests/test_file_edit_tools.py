from types import SimpleNamespace
import asyncio
import shutil
import subprocess
import threading

import pytest


def test_write_content_pos_inserts_before_line(tmp_path):
    from gilzabots.tools import set_project_context, write_content_pos

    target = tmp_path / "main.tex"
    target.write_text("line 1\nline 3\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(write_content_pos, "_func", None) or write_content_pos
    result = asyncio.run(raw("main.tex", "line 2", 2))

    assert "Successfully inserted content at line 2" in result
    assert target.read_text(encoding="utf-8") == "line 1\nline 2\nline 3\n"


def test_replace_content_range_replaces_inclusive_lines(tmp_path):
    from gilzabots.tools import replace_content_range, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("a\nold 1\nold 2\nd\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(replace_content_range, "_func", None) or replace_content_range
    result = asyncio.run(raw("main.tex", 2, 3, "new 1\nnew 2"))

    assert "Successfully replaced lines 2-3" in result
    assert target.read_text(encoding="utf-8") == "a\nnew 1\nnew 2\nd\n"


def test_replace_content_range_deletes_lines_with_empty_content(tmp_path):
    from gilzabots.tools import replace_content_range, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("keep\nremove 1\nremove 2\nkeep too\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(replace_content_range, "_func", None) or replace_content_range
    asyncio.run(raw("main.tex", 2, 3, ""))

    assert target.read_text(encoding="utf-8") == "keep\nkeep too\n"


def test_mutating_tool_returns_blocked_result_in_plan_mode(tmp_path):
    from gilzabots.tools import set_project_context, write_file

    target = tmp_path / "main.tex"
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan"))

    raw = getattr(write_file, "_func", None) or write_file
    result = asyncio.run(raw("main.tex", "hello\n"))

    assert result.startswith("Execution blocked: In 'plan' mode")
    assert not target.exists()


def test_read_file_falls_back_to_cp1252(tmp_path):
    from gilzabots.tools import read_file, set_project_context

    target = tmp_path / "latex_output.txt"
    target.write_bytes("Introdução\nConclusão\n".encode("cp1252"))
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(read_file, "_func", None) or read_file
    result = asyncio.run(raw("latex_output.txt"))

    assert result == "Introdução\nConclusão\n"


def test_read_content_pos_falls_back_to_cp1252(tmp_path):
    from gilzabots.tools import read_content_pos, set_project_context

    target = tmp_path / "latex_output.txt"
    target.write_bytes("Linha 1\nIntrodução\nConclusão\n".encode("cp1252"))
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(read_content_pos, "_func", None) or read_content_pos
    result = asyncio.run(raw("latex_output.txt", 2, 3))

    assert result == "Introdução\nConclusão\n"


def test_read_content_pos_beyond_eof_returns_recoverable_diagnostic(tmp_path):
    from gilzabots.tools import read_content_pos, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("line 1\nline 2\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(read_content_pos, "_func", None) or read_content_pos
    result = asyncio.run(raw("main.tex", 700, 800))

    assert "No content read" in result
    assert "total lines: 2" in result
    assert "Use start_pos <= 2" in result


def test_read_content_pos_rejects_invalid_line_ranges(tmp_path):
    from gilzabots.tools import read_content_pos, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("line 1\nline 2\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(read_content_pos, "_func", None) or read_content_pos

    with pytest.raises(ValueError, match="1-indexed positive integers"):
        asyncio.run(raw("main.tex", 0, 1))

    with pytest.raises(ValueError, match="greater than or equal"):
        asyncio.run(raw("main.tex", 2, 1))


def test_search_code_finds_text_with_line_numbers(tmp_path):
    from gilzabots.tools import search_code, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("Intro\n\\section{Metodologia}\nBody\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(search_code, "_func", None) or search_code
    result = asyncio.run(raw("Metodologia", "main.tex"))

    assert "main.tex:2: \\section{Metodologia}" in result


def test_search_code_supports_regex_for_latex_sections(tmp_path):
    from gilzabots.tools import search_code, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("\\section{Introdução}\nText\n\\section{Metodologia}\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(search_code, "_func", None) or search_code
    result = asyncio.run(raw(r"\\section\{Metodologia\}", ".", True))

    assert "main.tex:3: \\section{Metodologia}" in result


def test_search_code_limits_results(tmp_path):
    from gilzabots.tools import search_code, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("match\nmatch\nmatch\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(search_code, "_func", None) or search_code
    result = asyncio.run(raw("match", ".", False, False, 2))

    assert result.count("main.tex:") == 2
    assert "Stopped after 2 matches" in result


def test_search_code_filters_by_file_pattern(tmp_path):
    from gilzabots.tools import search_code, set_project_context

    (tmp_path / "main.tex").write_text("needle\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("needle\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    raw = getattr(search_code, "_func", None) or search_code
    result = asyncio.run(raw("needle", ".", False, False, 50, "*.tex"))

    assert "main.tex:1: needle" in result
    assert "notes.txt" not in result


def test_get_helper_tools_includes_search_code():
    from gilzabots.tools import get_helper_tools

    names = {getattr(tool, "name", None) for tool in get_helper_tools()}

    assert "search_code" in names


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_mutating_tool_does_not_create_shadow_git_checkpoint(tmp_path):
    from gilzabots.tools import set_project_context, write_file

    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="auto"))

    raw = getattr(write_file, "_func", None) or write_file
    asyncio.run(raw("main.tex", "hello\n"))

    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == "hello\n"
    assert not (tmp_path / ".gilzabots" / ".shadowgit").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_mutating_tool_preserves_preexisting_changes_without_checkpointing(tmp_path):
    from gilzabots.tools import set_project_context, write_file

    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="auto"))
    (tmp_path / "user.tex").write_text("user edit\n", encoding="utf-8")

    raw = getattr(write_file, "_func", None) or write_file
    asyncio.run(raw("agent.tex", "agent edit\n"))

    assert (tmp_path / "user.tex").read_text(encoding="utf-8") == "user edit\n"
    assert (tmp_path / "agent.tex").read_text(encoding="utf-8") == "agent edit\n"
    assert not (tmp_path / ".gilzabots" / ".shadowgit").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_agent_turn_cleanup_preserves_preexisting_uncommitted_changes(tmp_path):
    from gilzabots.vcs import begin_agent_turn_checkpoint, finalize_agent_turn_checkpoint

    target = tmp_path / "user.tex"
    target.write_text("user edit\n", encoding="utf-8")

    start_checkpoint = begin_agent_turn_checkpoint(str(tmp_path))
    assert start_checkpoint
    assert finalize_agent_turn_checkpoint(str(tmp_path), start_checkpoint)

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    status = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "status",
            "--porcelain",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" not in log
    assert "Agent turn end checkpoint" not in log
    assert target.read_text(encoding="utf-8") == "user edit\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_agent_turn_finalize_without_start_does_not_create_orphan_checkpoint(tmp_path):
    from gilzabots.vcs import finalize_agent_turn_checkpoint

    target = tmp_path / "agent.tex"
    target.write_text("agent edit\n", encoding="utf-8")

    assert not finalize_agent_turn_checkpoint(str(tmp_path), None)

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert "Agent turn end checkpoint" not in log.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_labeled_agent_turn_without_changes_removes_start_and_end(tmp_path):
    from gilzabots.vcs import begin_agent_turn_checkpoint, finalize_agent_turn_checkpoint

    start_checkpoint = begin_agent_turn_checkpoint(str(tmp_path), "worker:command-line")
    assert start_checkpoint
    assert finalize_agent_turn_checkpoint(str(tmp_path), start_checkpoint, "worker:command-line")

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "Agent turn start checkpoint: worker:command-line" not in log
    assert "Agent turn end checkpoint: worker:command-line" not in log


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_agent_turn_cancelled_during_start_does_not_leave_orphan(monkeypatch, tmp_path):
    import gilzabots.vcs as vcs

    start_committed = threading.Event()
    release_start = threading.Event()
    real_begin = vcs.begin_agent_turn_checkpoint

    def delayed_begin(project_path, agent_label=None):
        checkpoint_id = real_begin(project_path, agent_label)
        start_committed.set()
        release_start.wait(timeout=5)
        return checkpoint_id

    monkeypatch.setattr(vcs, "begin_agent_turn_checkpoint", delayed_begin)
    checkpoint = vcs.AsyncAgentTurnCheckpoint(str(tmp_path))
    task = asyncio.create_task(checkpoint.begin())

    assert await asyncio.to_thread(start_committed.wait, 5)
    task.cancel()
    release_start.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" not in log
    assert "Agent turn end checkpoint" not in log


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_agent_turn_finalization_survives_repeated_cancellation(monkeypatch, tmp_path):
    import gilzabots.vcs as vcs

    checkpoint = vcs.AsyncAgentTurnCheckpoint(str(tmp_path))
    assert await checkpoint.begin()
    (tmp_path / "partial.tex").write_text("partial change\n", encoding="utf-8")

    finalize_started = threading.Event()
    release_finalize = threading.Event()
    real_finalize = vcs.finalize_agent_turn_checkpoint_details

    def delayed_finalize(project_path, start_checkpoint=None, agent_label=None):
        finalize_started.set()
        release_finalize.wait(timeout=5)
        return real_finalize(project_path, start_checkpoint, agent_label)

    monkeypatch.setattr(vcs, "finalize_agent_turn_checkpoint_details", delayed_finalize)
    task = asyncio.create_task(checkpoint.finalize())

    assert await asyncio.to_thread(finalize_started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_finalize.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    status = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.gilzabots' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "status",
            "--porcelain",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" in log
    assert "Agent turn end checkpoint" in log
    assert status == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_agent_turn_reports_the_files_it_changed(tmp_path):
    from gilzabots.vcs import begin_agent_turn_checkpoint, finalize_agent_turn_checkpoint_details

    (tmp_path / "edited.tex").write_text("before\n", encoding="utf-8")
    (tmp_path / "untouched.tex").write_text("same\n", encoding="utf-8")
    (tmp_path / "removed.tex").write_text("bye\n", encoding="utf-8")
    # A change made before the turn is not the turn's work.
    (tmp_path / "user.tex").write_text("user edit\n", encoding="utf-8")

    start_checkpoint = begin_agent_turn_checkpoint(str(tmp_path))
    assert start_checkpoint
    (tmp_path / "edited.tex").write_text("after\n", encoding="utf-8")
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "new file.tex").write_text("new\n", encoding="utf-8")
    (tmp_path / "removed.tex").unlink()

    result = finalize_agent_turn_checkpoint_details(str(tmp_path), start_checkpoint)

    assert result.saved
    # Deleted files are left out: there is nothing left to mark.
    assert result.changed_files == ("chapters/new file.tex", "edited.tex")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_agent_turn_without_net_diff_reports_no_changed_files(tmp_path):
    from gilzabots.vcs import begin_agent_turn_checkpoint, finalize_agent_turn_checkpoint_details

    target = tmp_path / "main.tex"
    target.write_text("original\n", encoding="utf-8")
    start_checkpoint = begin_agent_turn_checkpoint(str(tmp_path))
    target.write_text("changed\n", encoding="utf-8")
    target.write_text("original\n", encoding="utf-8")

    result = finalize_agent_turn_checkpoint_details(str(tmp_path), start_checkpoint)

    assert result.saved
    assert result.changed_files == ()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_async_agent_turn_checkpoint_exposes_changed_files(tmp_path):
    import gilzabots.vcs as vcs

    checkpoint = vcs.AsyncAgentTurnCheckpoint(str(tmp_path))
    assert await checkpoint.begin()
    (tmp_path / "agent.tex").write_text("agent edit\n", encoding="utf-8")

    assert await checkpoint.finalize()
    assert checkpoint.changed_files == ("agent.tex",)


def test_write_content_pos_on_missing_file_points_at_write_file(tmp_path):
    """A create-a-file attempt must name the tool that can actually do it.

    The bare "file not found" this used to raise reads like a wrong path, so
    orchestrators retried the same call (or the same path with flipped
    separators) instead of switching to write_file.
    """
    from gilzabots.tools import set_project_context, write_content_pos

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    raw = getattr(write_content_pos, "_func", None) or write_content_pos

    with pytest.raises(ValueError, match="write_file"):
        asyncio.run(raw("brand_new.tex", "\\documentclass{article}", 1))

    assert not (tmp_path / "brand_new.tex").exists()


def test_replace_content_range_on_missing_file_points_at_write_file(tmp_path):
    from gilzabots.tools import replace_content_range, set_project_context

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    raw = getattr(replace_content_range, "_func", None) or replace_content_range

    with pytest.raises(ValueError, match="write_file"):
        asyncio.run(raw("brand_new.tex", 1, 2, "content"))


def test_write_content_pos_description_states_the_file_must_exist():
    from gilzabots.tools import write_content_pos

    description = (write_content_pos.description or "").lower()
    assert "must already exist" in description
    assert "write_file" in description


# A single line of LaTeX or code is full of backslash sequences that look like
# escapes. The file tools used to "repair" any single-line content containing
# `\n`, `\t` or `\r`, so `\textbf` reached the file as a tab and "extbf". In the
# saved tool calls of a real installation that repair fired 26 times, always on
# valid LaTeX. What the agent sends is what the file gets.
VERBATIM_LINES = [
    r"\textbf{Resultado} \\",
    r"\renewcommand{\arraystretch}{1.2}",
    r"\newline texto com \theta e \right)",
    r"\resizebox{0.92\textwidth}{!}{%",
    'print("a\\nb\\tc")',
    r'pattern = re.compile(r"\d+\t\\")',
]


def _raw_tool(tool):
    return getattr(tool, "_func", None) or tool


@pytest.mark.parametrize("line", VERBATIM_LINES)
def test_replace_content_range_writes_a_single_line_verbatim(tmp_path, line):
    from gilzabots.tools import replace_content_range, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("a\nold\nc\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    asyncio.run(_raw_tool(replace_content_range)("main.tex", 2, 2, line))

    assert target.read_text(encoding="utf-8") == f"a\n{line}\nc\n"


@pytest.mark.parametrize("line", VERBATIM_LINES)
def test_write_content_pos_writes_a_single_line_verbatim(tmp_path, line):
    from gilzabots.tools import set_project_context, write_content_pos

    target = tmp_path / "main.tex"
    target.write_text("a\nc\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    asyncio.run(_raw_tool(write_content_pos)("main.tex", line, 2))

    assert target.read_text(encoding="utf-8") == f"a\n{line}\nc\n"


@pytest.mark.parametrize("line", VERBATIM_LINES)
def test_write_file_writes_single_line_content_verbatim(tmp_path, line):
    from gilzabots.tools import set_project_context, write_file

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    asyncio.run(_raw_tool(write_file)("macros.tex", line))

    assert (tmp_path / "macros.tex").read_text(encoding="utf-8") == line


def test_create_docx_file_keeps_latex_in_a_single_line(tmp_path):
    docx = pytest.importorskip("docx")
    from gilzabots.tools import create_docx_file, set_project_context

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    text = r"A perda depende de $\theta$ e de \textbf{tudo}."

    asyncio.run(_raw_tool(create_docx_file)("out.docx", text))

    paragraphs = [p.text for p in docx.Document(str(tmp_path / "out.docx")).paragraphs]
    assert text in paragraphs


# ── edit_file: a passage found by its text ───────────────────────────────────

def _edit(tmp_path, *args, **kwargs):
    from gilzabots.tools import edit_file, set_project_context

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    return asyncio.run(_raw_tool(edit_file)(*args, **kwargs))


def test_edit_file_replaces_a_unique_passage_and_shows_the_region(tmp_path):
    target = tmp_path / "main.tex"
    target.write_text("\\begin{document}\n\\section{Intro}\nTexto.\n\\end{document}\n", encoding="utf-8")

    result = _edit(tmp_path, "main.tex", "\\section{Intro}", "\\section{Introdução}")

    assert target.read_text(encoding="utf-8") == "\\begin{document}\n\\section{Introdução}\nTexto.\n\\end{document}\n"
    assert "Edited" in result
    assert "2\t\\section{Introdução}" in result
    assert "1\t\\begin{document}" in result


@pytest.mark.parametrize("line", VERBATIM_LINES)
def test_edit_file_writes_backslash_sequences_verbatim(tmp_path, line):
    target = tmp_path / "main.tex"
    target.write_text("a\nPLACEHOLDER\nc\n", encoding="utf-8")

    _edit(tmp_path, "main.tex", "PLACEHOLDER", line)

    assert target.read_text(encoding="utf-8") == f"a\n{line}\nc\n"


def test_edit_file_refuses_an_ambiguous_passage_and_names_its_lines(tmp_path):
    target = tmp_path / "main.py"
    original = "x = 1\nprint(x)\nx = 1\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "main.py", "x = 1", "x = 2")

    assert "occurs 2 times" in str(error.value)
    assert "lines 1, 3" in str(error.value)
    assert target.read_text(encoding="utf-8") == original


def test_edit_file_replace_all_changes_every_occurrence(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("x = 1\nprint(x)\nx = 1\n", encoding="utf-8")

    result = _edit(tmp_path, "main.py", "x = 1", "x = 2", replace_all=True)

    assert target.read_text(encoding="utf-8") == "x = 2\nprint(x)\nx = 2\n"
    assert "Replaced 2 occurrences" in result


def test_edit_file_points_at_a_near_miss_without_editing_it(tmp_path):
    target = tmp_path / "main.py"
    original = "def f():\n    return 1\n"
    target.write_text(original, encoding="utf-8")

    # Indentation lost: never matched loosely, but the line is named.
    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "main.py", "def f():\nreturn 1", "def f():\n    return 2")

    assert "does not occur" in str(error.value)
    assert "starts at line 1" in str(error.value)
    assert target.read_text(encoding="utf-8") == original


def test_edit_file_deletes_with_an_empty_new_string(tmp_path):
    target = tmp_path / "main.tex"
    target.write_text("keep\n% TODO remove\nkeep too\n", encoding="utf-8")

    _edit(tmp_path, "main.tex", "% TODO remove\n", "")

    assert target.read_text(encoding="utf-8") == "keep\nkeep too\n"


def test_edit_file_matches_lf_text_in_a_crlf_file_and_keeps_crlf(tmp_path):
    # The read tools show CRLF files with LF line breaks; that is the text the
    # model copies into old_string.
    target = tmp_path / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    _edit(tmp_path, "win.txt", "one\ntwo", "one\n2\nTWO")

    assert target.read_bytes() == b"one\r\n2\r\nTWO\r\nthree\r\n"


def test_edit_file_keeps_the_encoding_and_bom_it_found(tmp_path):
    legacy = tmp_path / "legacy.tex"
    legacy.write_bytes("Seção antiga\n".encode("cp1252"))
    _edit(tmp_path, "legacy.tex", "antiga", "nova")
    assert legacy.read_bytes() == "Seção nova\n".encode("cp1252")

    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + "título\n".encode("utf-8"))
    _edit(tmp_path, "bom.txt", "título", "subtítulo")
    assert bom.read_bytes() == b"\xef\xbb\xbf" + "subtítulo\n".encode("utf-8")


def test_edit_file_refuses_characters_the_file_encoding_cannot_hold(tmp_path):
    legacy = tmp_path / "legacy.tex"
    original = "Seção\n".encode("cp1252")
    legacy.write_bytes(original)

    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "legacy.tex", "Seção", "Seção 🚀")

    assert "cp1252" in str(error.value)
    assert legacy.read_bytes() == original


@pytest.mark.parametrize("old,new,message", [
    ("", "x", "old_string is empty"),
    ("same", "same", "identical"),
])
def test_edit_file_rejects_calls_that_change_nothing(tmp_path, old, new, message):
    (tmp_path / "main.tex").write_text("same\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "main.tex", old, new)
    assert message in str(error.value)


def test_edit_file_does_not_create_files_or_edit_binaries_or_packages(tmp_path):
    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "missing.tex", "a", "b")
    assert "write_file" in str(error.value)
    assert not (tmp_path / "missing.tex").exists()

    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00\x00data")
    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "image.png", "data", "x")
    assert "binary" in str(error.value)

    (tmp_path / "deck.jpt").write_bytes(b"PK")
    with pytest.raises(ValueError) as error:
        _edit(tmp_path, "deck.jpt", "PK", "x")
    assert "edit_presentation" in str(error.value)


def test_edit_file_is_a_gated_workspace_write_tool():
    from gilzabots.tools import edit_file, get_workspace_action_tools
    from gilzabots.agent_stdin import ALL_TOOLS_MAP

    assert edit_file in get_workspace_action_tools()
    assert ALL_TOOLS_MAP["edit_file"] is edit_file


def test_edit_file_is_blocked_in_plan_mode(tmp_path):
    from gilzabots.tools import set_project_context

    target = tmp_path / "main.tex"
    target.write_text("old\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="plan"))
    from gilzabots.tools import edit_file

    result = asyncio.run(_raw_tool(edit_file)("main.tex", "old", "new"))

    assert "Execution blocked" in result
    assert target.read_text(encoding="utf-8") == "old\n"


# ── Syntax check after a write ───────────────────────────────────────────────

def test_an_edit_that_breaks_python_says_so_with_the_line(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    out = _edit(tmp_path, "app.py", "    return 1", "    return (1")

    assert "Syntax check: app.py parsed as Python before this edit and no longer does" in out
    assert "line" in out
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def f():\n    return (1\n"


def test_a_clean_edit_and_a_file_that_was_already_broken_report_nothing(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert "Syntax check" not in _edit(tmp_path, "ok.py", "x = 1", "x = 2")

    (tmp_path / "broken.py").write_text("def f(:\n    pass\nvalue = 1\n", encoding="utf-8")
    assert "Syntax check" not in _edit(tmp_path, "broken.py", "value = 1", "value = (")


def test_line_tools_and_write_file_report_a_new_syntax_error(tmp_path):
    from gilzabots.tools import replace_content_range, set_project_context, write_content_pos, write_file

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    (tmp_path / "data.json").write_text('{\n  "a": 1\n}\n', encoding="utf-8")
    out = asyncio.run(_raw_tool(replace_content_range)("data.json", 2, 2, '  "a": 1,'))
    assert "no longer does — invalid JSON" in out

    (tmp_path / "mod.py").write_text("a = 1\n", encoding="utf-8")
    out = asyncio.run(_raw_tool(write_content_pos)("mod.py", "if True\n", 2))
    assert "parsed as Python before this edit" in out

    out = asyncio.run(_raw_tool(write_file)("new.py", "def g(:\n"))
    assert "Syntax check: new.py does not parse as Python" in out
    assert "Syntax check" not in asyncio.run(_raw_tool(write_file)("fine.py", "def g():\n    pass\n"))


def test_formats_that_depend_on_project_setup_need_a_version_that_parsed(tmp_path):
    from gilzabots.tools import set_project_context, write_file

    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    # JSX in a .js file and YAML custom tags parse or not depending on the
    # project's tooling: a new file has nothing to compare with.
    assert "Syntax check" not in asyncio.run(_raw_tool(write_file)("view.js", "const x = <div/>;\n"))
    assert "Syntax check" not in asyncio.run(_raw_tool(write_file)("stack.yaml", "a: !Ref b\n"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_javascript_is_checked_with_node_when_an_edit_breaks_it(tmp_path):
    (tmp_path / "util.js").write_text("export const limit = 10;\n", encoding="utf-8")

    out = _edit(tmp_path, "util.js", "const limit = 10;", "const limit = ;")

    assert "parsed as JavaScript before this edit and no longer does" in out
    assert "SyntaxError" in out


def test_yaml_is_checked_against_its_earlier_version(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "ci.yml").write_text("jobs:\n  test:\n    runs-on: ubuntu\n", encoding="utf-8")

    out = _edit(tmp_path, "ci.yml", "    runs-on: ubuntu", "    runs-on: [ubuntu")

    assert "parsed as YAML before this edit" in out


# ── Line edits refuse a file that changed since it was read ──────────────────

def _bump(path, text):
    """Change *path* the way the editor or a command would, with a new mtime."""
    import os
    stat = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_a_line_edit_after_an_outside_change_is_refused(tmp_path):
    from gilzabots.tools import read_content_pos, replace_content_range, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    asyncio.run(_raw_tool(read_content_pos)("main.tex", 1, 3))

    # The user inserts a line above in the editor: line 2 is no longer "b".
    _bump(target, "a\nnew\nb\nc\n")

    with pytest.raises(ValueError) as error:
        asyncio.run(_raw_tool(replace_content_range)("main.tex", 2, 2, "B"))
    assert "changed since you last read it" in str(error.value)
    assert target.read_text(encoding="utf-8") == "a\nnew\nb\nc\n"

    # Reading again is what makes the line numbers trustworthy again.
    asyncio.run(_raw_tool(read_content_pos)("main.tex", 1, 4))
    asyncio.run(_raw_tool(replace_content_range)("main.tex", 3, 3, "B"))
    assert target.read_text(encoding="utf-8") == "a\nnew\nB\nc\n"


def test_the_agents_own_writes_do_not_trip_the_check(tmp_path):
    from gilzabots.tools import (
        read_file, replace_content_range, set_project_context, write_content_pos,
    )

    target = tmp_path / "main.tex"
    target.write_text("a\nb\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    asyncio.run(_raw_tool(read_file)("main.tex"))

    asyncio.run(_raw_tool(replace_content_range)("main.tex", 1, 1, "A"))
    asyncio.run(_raw_tool(write_content_pos)("main.tex", "top", 1))
    _edit(tmp_path, "main.tex", "b", "B")
    asyncio.run(_raw_tool(replace_content_range)("main.tex", 1, 1, "TOP"))

    assert target.read_text(encoding="utf-8") == "TOP\nA\nB\n"


def test_search_results_count_as_a_read_and_unseen_files_are_not_checked(tmp_path):
    from gilzabots.tools import replace_content_range, search_code, set_project_context, write_content_pos

    target = tmp_path / "main.tex"
    target.write_text("x\nneedle\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))

    # Never read by a tool: nothing to compare with, so the edit goes through.
    _bump(target, "x\nneedle\n")
    asyncio.run(_raw_tool(write_content_pos)("main.tex", "first", 1))

    _bump(target, "first\nx\nneedle\n")
    asyncio.run(_raw_tool(search_code)("needle"))
    asyncio.run(_raw_tool(replace_content_range)("main.tex", 3, 3, "found"))
    assert target.read_text(encoding="utf-8") == "first\nx\nfound\n"


def test_edit_file_is_not_blocked_by_an_outside_change(tmp_path):
    from gilzabots.tools import read_file, set_project_context

    target = tmp_path / "main.tex"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    asyncio.run(_raw_tool(read_file)("main.tex"))
    _bump(target, "inserted\nalpha\nbeta\n")

    _edit(tmp_path, "main.tex", "beta", "BETA")
    assert target.read_text(encoding="utf-8") == "inserted\nalpha\nBETA\n"
