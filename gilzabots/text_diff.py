"""Line diff with original line numbers, rendered for a model to read.

`difflib.unified_diff` numbers its hunks relative to the sequences it is given,
so a diff of lines 40-88 against lines 40-90 would report "line 1". A model that
then edits "line 1" edits the wrong place. This module keeps the numbers of the
files the lines came from, on every hunk header and every line, and labels the
two sides `a` and `b` so a reader never has to remember which sign is which file.

Rendered hunk::

    @@ a 13-14 | b 13-15 @@
      a12 b12  \\section{Method}
    - a13      old sentence
    +     b13  new sentence

`skills/elementary-ops/scripts/ops.py` carries a standalone copy of this renderer
(a skill script cannot import the application package); `tests/test_text_diff.py`
pins the two to the same output.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class DiffResult:
    identical: bool
    added: int
    removed: int
    # One entry per changed block: (a_start, a_end, b_start, b_end), 1-indexed and
    # inclusive; an empty side has end == start - 1 (start is the line it precedes).
    blocks: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Rendered hunks, one list of lines per hunk.
    hunks: list[list[str]] = field(default_factory=list)


def _normalize(line: str) -> str:
    return " ".join(line.split())


def describe_range(side: str, start: int, end: int) -> str:
    """`a 13-14`, `a 13`, or `a none (before 13)` for an empty side."""
    if end < start:
        return f"{side} none (before {start})"
    if end == start:
        return f"{side} {start}"
    return f"{side} {start}-{end}"


def line_diff(
    a_lines: list[str],
    b_lines: list[str],
    a_first: int = 1,
    b_first: int = 1,
    context: int = 3,
    ignore_whitespace: bool = False,
) -> DiffResult:
    """Diff two line lists whose first lines are numbered *a_first* / *b_first*."""
    a_key = [_normalize(l) for l in a_lines] if ignore_whitespace else list(a_lines)
    b_key = [_normalize(l) for l in b_lines] if ignore_whitespace else list(b_lines)
    matcher = difflib.SequenceMatcher(None, a_key, b_key, autojunk=False)

    added = removed = 0
    blocks: list[tuple[int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        blocks.append((a_first + i1, a_first + i2 - 1, b_first + j1, b_first + j2 - 1))

    if not blocks:
        return DiffResult(identical=True, added=0, removed=0)

    width = len(str(max(a_first + len(a_lines), b_first + len(b_lines))))

    def row(sign: str, a_no: int | None, b_no: int | None, text: str) -> str:
        a_col = f"a{a_no:<{width}}" if a_no is not None else " " * (width + 1)
        b_col = f"b{b_no:<{width}}" if b_no is not None else " " * (width + 1)
        return f"{sign} {a_col} {b_col}  {text.rstrip()}"

    hunks: list[list[str]] = []
    for group in matcher.get_grouped_opcodes(max(0, context)):
        changed = [op for op in group if op[0] != "equal"]
        a_start = a_first + changed[0][1]
        a_end = a_first + changed[-1][2] - 1
        b_start = b_first + changed[0][3]
        b_end = b_first + changed[-1][4] - 1
        lines = [f"@@ {describe_range('a', a_start, a_end)} | {describe_range('b', b_start, b_end)} @@"]
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    lines.append(row(" ", a_first + i1 + offset, b_first + j1 + offset, a_lines[i1 + offset]))
                continue
            for index in range(i1, i2):
                lines.append(row("-", a_first + index, None, a_lines[index]))
            for index in range(j1, j2):
                lines.append(row("+", None, b_first + index, b_lines[index]))
        hunks.append(lines)

    return DiffResult(identical=False, added=added, removed=removed, blocks=blocks, hunks=hunks)


def summarize_blocks(blocks: list[tuple[int, int, int, int]], limit: int = 8) -> str:
    """`a 13-14 -> b 13-15; a 40 -> b none (before 42)` for the first *limit* blocks."""
    parts = [
        f"{describe_range('a', a1, a2)} -> {describe_range('b', b1, b2)}"
        for a1, a2, b1, b2 in blocks[:limit]
    ]
    if len(blocks) > limit:
        parts.append(f"... {len(blocks) - limit} more")
    return "; ".join(parts)
