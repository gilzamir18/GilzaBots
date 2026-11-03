#!/usr/bin/env python3
"""Run a plan of elementary operations over text files and check the answer.

The worker does not read, count or compare by eye. It writes a short plan — one
operation per line, each result named — and this script executes it
deterministically, then evaluates the `check` lines that state the answer:

    s1 = section paper_v1.tex "Method"
    s2 = section paper_v2.tex "Method"
    s3 = diff s1 s2
    s4 = cites s1
    s5 = cites s2
    check all(subset s4 s5, s3 <= 40)

Commands:
    ops.py catalog                      list the operations and the check grammar
    ops.py validate PLAN                parse and type-check the plan, run nothing
    ops.py run PLAN                     validate, execute, print one compact report
    ops.py show PLAN NAME [--offset N]  page the full value of one named result

Output is kept under --max-chars (default 1800) because the tool that runs this
script cuts its output at 2000 characters; anything longer is paged by `show`.
An invalid plan is refused with the line number and what to change — it is
never repaired silently. Exit code: 0 run finished and every check passed,
1 a check failed, 2 the plan is invalid or a step could not run.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shlex
import sys
import unicodedata
from dataclasses import dataclass, field

MAX_CHARS_DEFAULT = 1800
MAX_STEPS_DEFAULT = 12
PREVIEW_ITEMS = 12

MARKDOWN_SUFFIXES = {".md", ".markdown"}
LATEX_SUFFIXES = {".tex", ".ltx", ".sty", ".cls", ".bib", ".bbx", ".cbx"}


class PlanError(Exception):
    """The plan is invalid or a step cannot run; the message says what to change."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    path: str          # as written in the plan, for display
    start: int         # 1-indexed, inclusive
    end: int           # inclusive; end == start - 1 for an empty segment
    lines: list[str]
    latex: bool        # comments are stripped by the extractors when true

    def describe(self) -> str:
        span = f"{self.start}-{self.end}" if self.end > self.start else f"{self.start}"
        if self.end < self.start:
            span = f"empty (before {self.start})"
        return f"segment {self.path}:{span} ({len(self.lines)} lines)"


@dataclass
class Items:
    items: list[str]

    def describe(self) -> str:
        return f"{len(self.items)} items" + (f": {_preview(self.items)}" if self.items else "")


@dataclass
class Number:
    value: int

    def describe(self) -> str:
        return f"number {self.value}"


@dataclass
class Diff:
    a: Segment
    b: Segment
    added: int
    removed: int
    blocks: list[tuple[int, int, int, int]] = field(default_factory=list)
    hunks: list[list[str]] = field(default_factory=list)

    def describe(self) -> str:
        if not self.blocks:
            return "identical"
        return (
            f"changed blocks: {len(self.blocks)}, +{self.added} -{self.removed} lines: "
            f"{summarize_blocks(self.blocks, 4)}"
        )


def size(value) -> int:
    """The number a comparison like `s3 <= 40` reads from a value."""
    if isinstance(value, Segment):
        return len(value.lines)
    if isinstance(value, Items):
        return len(value.items)
    if isinstance(value, Number):
        return value.value
    if isinstance(value, Diff):
        return value.added + value.removed
    raise TypeError(value)


def _preview(items: list[str], limit: int = PREVIEW_ITEMS) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f", ... {len(items) - limit} more" if len(items) > limit else "")


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# Line diff — standalone copy of gilzabots/text_diff.py (a skill script cannot
# import the application package). tests/test_text_diff.py pins the two to the
# same output.
# ---------------------------------------------------------------------------

def _normalize(line: str) -> str:
    return " ".join(line.split())


def describe_range(side: str, start: int, end: int) -> str:
    if end < start:
        return f"{side} none (before {start})"
    if end == start:
        return f"{side} {start}"
    return f"{side} {start}-{end}"


def line_diff(a_lines, b_lines, a_first=1, b_first=1, context=3, ignore_whitespace=False):
    """Return (added, removed, blocks, hunks) with original line numbers."""
    a_key = [_normalize(l) for l in a_lines] if ignore_whitespace else list(a_lines)
    b_key = [_normalize(l) for l in b_lines] if ignore_whitespace else list(b_lines)
    matcher = difflib.SequenceMatcher(None, a_key, b_key, autojunk=False)

    added = removed = 0
    blocks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        blocks.append((a_first + i1, a_first + i2 - 1, b_first + j1, b_first + j2 - 1))
    if not blocks:
        return 0, 0, [], []

    width = len(str(max(a_first + len(a_lines), b_first + len(b_lines))))

    def row(sign, a_no, b_no, text):
        a_col = f"a{a_no:<{width}}" if a_no is not None else " " * (width + 1)
        b_col = f"b{b_no:<{width}}" if b_no is not None else " " * (width + 1)
        return f"{sign} {a_col} {b_col}  {text.rstrip()}"

    hunks = []
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
    return added, removed, blocks, hunks


def summarize_blocks(blocks, limit=8):
    parts = [
        f"{describe_range('a', a1, a2)} -> {describe_range('b', b1, b2)}"
        for a1, a2, b1, b2 in blocks[:limit]
    ]
    if len(blocks) > limit:
        parts.append(f"... {len(blocks) - limit} more")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

def _read_lines(root: str, path: str) -> list[str]:
    full = path if os.path.isabs(path) else os.path.join(root, path)
    if os.path.isdir(full):
        raise PlanError(f"'{path}' is a directory, not a file.")
    if not os.path.isfile(full):
        raise PlanError(f"file not found: '{path}' (relative paths start at {root}).")
    with open(full, "rb") as handle:
        data = handle.read()
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise PlanError(f"'{path}' is a binary file; operations read text files only.")
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        if encoding == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def _is_markdown(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MARKDOWN_SUFFIXES


def _is_latex(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in LATEX_SUFFIXES


_COMMENT_RE = re.compile(r"(?<!\\)%.*")


def _strip_comment(line: str) -> str:
    return _COMMENT_RE.sub("", line)


def _segment(root: str, path: str, start: int | None = None, end: int | None = None) -> Segment:
    lines = _read_lines(root, path)
    first = 1 if start is None else start
    last = len(lines) if end is None else min(end, len(lines))
    if lines and first > len(lines):
        raise PlanError(f"line {first} is beyond the end of '{path}' ({len(lines)} lines).")
    return Segment(path, first, last, lines[first - 1:last], _is_latex(path))


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------

_LATEX_LEVELS = {
    "part": 0, "chapter": 1, "section": 2, "subsection": 3,
    "subsubsection": 4, "paragraph": 5, "subparagraph": 6,
}
_LATEX_HEADING_RE = re.compile(
    r"^\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]*\]\s*)?\{"
)
_LATEX_STOP_RE = re.compile(r"^\s*\\(end\{document\}|appendix\b|bibliography\{|printbibliography\b)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _balanced(text: str, start: int) -> str:
    """Content of the brace group opening just before *start*."""
    depth = 1
    for index in range(start, len(text)):
        char = text[index]
        if char == "{" and text[index - 1] != "\\":
            depth += 1
        elif char == "}" and text[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start:index]
    return text[start:]


def _plain_title(title: str) -> str:
    """Title with LaTeX commands, braces, accents and case removed, spacing collapsed."""
    title = re.sub(r"\\[A-Za-z]+\*?", " ", title)
    title = title.replace("{", " ").replace("}", " ").replace("~", " ")
    title = "".join(c for c in unicodedata.normalize("NFKD", title) if not unicodedata.combining(c))
    return " ".join(title.split()).casefold()


def _headings(lines: list[str], markdown: bool) -> list[tuple[int, int, str]]:
    """(line index, level, title) of every heading, plus (index, -1, '') stops."""
    found = []
    in_fence = False
    for index, raw in enumerate(lines):
        if markdown:
            if _FENCE_RE.match(raw):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            match = _MD_HEADING_RE.match(raw)
            if match:
                found.append((index, len(match.group(1)), match.group(2)))
            continue
        line = _strip_comment(raw)
        match = _LATEX_HEADING_RE.match(line)
        if match:
            found.append((index, _LATEX_LEVELS[match.group(1)], _balanced(line, match.end()).strip()))
        elif _LATEX_STOP_RE.match(line):
            found.append((index, -1, ""))
    return found


def op_section(root: str, path: str, title: str) -> Segment:
    lines = _read_lines(root, path)
    heads = _headings(lines, _is_markdown(path))
    titled = [h for h in heads if h[1] >= 0]
    if not titled:
        raise PlanError(f"no headings found in '{path}'; address it with `lines` or `between`.")
    wanted = _plain_title(title)
    matches = [h for h in titled if _plain_title(h[2]) == wanted]
    if not matches:
        matches = [h for h in titled if wanted and wanted in _plain_title(h[2])]
    if not matches:
        listing = "; ".join(f"L{h[0] + 1} {h[2]}" for h in titled[:15])
        raise PlanError(f"no heading matches \"{title}\" in '{path}'. Headings: {listing}")
    if len(matches) > 1:
        listing = "; ".join(f"L{h[0] + 1} {h[2]}" for h in matches[:10])
        raise PlanError(
            f"\"{title}\" matches {len(matches)} headings in '{path}': {listing}. "
            "Use the full title, or `lines` with one of these line numbers."
        )
    index, level, _ = matches[0]
    end = len(lines)
    for other_index, other_level, _ in heads:
        if other_index > index and other_level <= level:
            end = other_index
            break
    return Segment(path, index + 1, end, lines[index:end], _is_latex(path))


def op_env(root: str, path: str, name: str, nth: int = 1) -> Segment:
    lines = _read_lines(root, path)
    begin = re.compile(r"\\begin\s*\{" + re.escape(name) + r"\}")
    end_re = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
    token_re = re.compile(begin.pattern + "|" + end_re.pattern)
    seen = 0
    depth = 0
    start = None
    for index, raw in enumerate(lines):
        for match in token_re.finditer(_strip_comment(raw)):
            if match.group(0).startswith("\\begin"):
                if depth == 0:
                    seen += 1
                    if seen == nth:
                        start = index
                depth += 1
            else:
                depth = max(0, depth - 1)
                if depth == 0 and start is not None:
                    return Segment(path, start + 1, index + 1, lines[start:index + 1], _is_latex(path))
    if start is not None:
        raise PlanError(f"\\begin{{{name}}} #{nth} at line {start + 1} of '{path}' is never closed.")
    raise PlanError(f"'{path}' has {seen} top-level \\begin{{{name}}}; #{nth} does not exist.")


def op_between(root: str, path: str, start_pattern: str, end_pattern: str) -> Segment:
    lines = _read_lines(root, path)
    start_re = _compile(start_pattern)
    end_re = _compile(end_pattern)
    for index, line in enumerate(lines):
        if start_re.search(line):
            for stop in range(index + 1, len(lines)):
                if end_re.search(lines[stop]):
                    return Segment(path, index + 1, stop + 1, lines[index:stop + 1], _is_latex(path))
            raise PlanError(
                f"start pattern matched line {index + 1} of '{path}', "
                f"but no later line matches the end pattern {end_pattern!r}."
            )
    raise PlanError(f"no line of '{path}' matches the start pattern {start_pattern!r}.")


def _compile(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error as error:
        raise PlanError(f"invalid regular expression {pattern!r}: {error}")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"\\label\s*\{([^}]*)\}")
_REF_RE = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref|nameref|vref)\*?\s*\{([^}]*)\}")
_CITE_RE = re.compile(r"\\(?:[A-Za-z]*cite[A-Za-z]*|nocite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*%?")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")


def _text_lines(seg: Segment) -> list[str]:
    return [_strip_comment(l) for l in seg.lines] if seg.latex else list(seg.lines)


def _keys(regex: re.Pattern, seg: Segment) -> Items:
    out = []
    for line in _text_lines(seg):
        for match in regex.finditer(line):
            out.extend(k.strip() for k in match.group(1).split(",") if k.strip())
    return Items(out)


def op_numbers(seg: Segment) -> Items:
    out = []
    for line in _text_lines(seg):
        line = _LABEL_RE.sub(" ", _REF_RE.sub(" ", _CITE_RE.sub(" ", line)))
        out.extend(_NUMBER_RE.findall(line))
    return Items(out)


def op_words(seg: Segment) -> Number:
    count = 0
    for line in _text_lines(seg):
        line = re.sub(r"\\[A-Za-z]+\*?", " ", line)
        count += len(_WORD_RE.findall(line))
    return Number(count)


def op_headings(seg: Segment) -> Items:
    heads = _headings(seg.lines, _is_markdown(seg.path))
    return Items([" ".join(h[2].split()) for h in heads if h[1] >= 0])


def op_grep(seg: Segment, pattern: str) -> Items:
    regex = _compile(pattern)
    return Items([line.strip() for line in seg.lines if regex.search(line)])


def op_nonblank(seg: Segment) -> Items:
    return Items([line.strip() for line in seg.lines if line.strip()])


def _as_items(value) -> list[str]:
    return value.items if isinstance(value, Items) else op_nonblank(value).items


def op_diff(a: Segment, b: Segment) -> Diff:
    added, removed, blocks, hunks = line_diff(a.lines, b.lines, a.start, b.start)
    return Diff(a, b, added, removed, blocks, hunks)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# Argument kinds: path, text (quoted), regex, range (A-B), int, seg (a segment result),
# set (an items or segment result), any (any result).
@dataclass(frozen=True)
class Op:
    name: str
    args: tuple[str, ...]
    returns: str
    help: str
    optional: int = 0     # trailing args that may be left out


CATALOG = {op.name: op for op in (
    Op("file", ("path",), "segment", "the whole file"),
    Op("lines", ("path", "range"), "segment", "lines A-B of a file (B may be `end`)"),
    Op("section", ("path", "text"), "segment",
       "a LaTeX or Markdown section by title, up to the next heading of the same or higher level; "
       "the title (case and accents ignored) must equal one heading, or be contained in exactly one"),
    Op("env", ("path", "text", "int"), "segment",
       "the N-th top-level \\begin{NAME}...\\end{NAME} (N defaults to 1)", optional=1),
    Op("between", ("path", "regex", "regex"), "segment",
       "from the first line matching START_REGEX to the next line matching END_REGEX"),
    Op("labels", ("seg",), "items", "keys of \\label{...}"),
    Op("refs", ("seg",), "items", "keys of \\ref, \\eqref, \\cref, \\autoref, ..."),
    Op("cites", ("seg",), "items", "keys of \\cite, \\citep, \\parencite, ... (one item per key)"),
    Op("numbers", ("seg",), "items", "numeric tokens in the text (12, 3.5, 40%), outside citation/label keys"),
    Op("headings", ("seg",), "items", "titles of the headings inside the segment"),
    Op("grep", ("seg", "regex"), "items", "lines matching REGEX, stripped"),
    Op("nonblank", ("seg",), "items", "every non-blank line, stripped"),
    Op("words", ("seg",), "number", "word count, LaTeX commands and comments excluded"),
    Op("count", ("any",), "number", "lines of a segment, items of a list, changed lines of a diff"),
    Op("unique", ("set",), "items", "items without repeats, first occurrence kept"),
    Op("diff", ("seg", "seg"), "diff", "line diff with the original line numbers of both sides"),
    Op("common", ("set", "set"), "items", "distinct items in both (a segment counts as its non-blank lines)"),
    Op("only-a", ("set", "set"), "items", "distinct items of the first that the second lacks"),
    Op("only-b", ("set", "set"), "items", "distinct items of the second that the first lacks"),
)}

CHECK_HELP = """\
check EXPR   (at least one; the last check states the answer)
  empty X | nonempty X          X has no items / lines / changes (or has some)
  equal X Y                     same lines or items in the same order (numbers: same value)
  sameset X Y                   same distinct items, order and repeats ignored
  subset X Y                    every distinct item of X is in Y
  contains X 'TEXT'             some line or item of X contains TEXT
  X OP N                        OP is == != < <= > >=; N is an integer or a result name;
                                X counts as count X
  all(P, Q, ...) | any(P, ...) | not(P)"""


def catalog_text() -> str:
    width = max(len(op.name) + 1 + len(" ".join(op.args)) for op in CATALOG.values())
    rows = []
    for op in CATALOG.values():
        args = list(op.args)
        if op.optional:
            args[-op.optional:] = [f"[{a}]" for a in args[-op.optional:]]
        signature = f"{op.name} {' '.join(args)}"
        rows.append(f"  {signature:<{width + 2}} -> {op.returns:<7} {op.help}")
    return (
        "PLAN LINE: NAME = OPERATION ARGS   (NAME: lowercase letters, digits, _)\n"
        "ARG KINDS: path, text ('quoted'), regex (in single quotes), range A-B, int,\n"
        "           seg (a segment result), set (items or segment result), any\n"
        "OPERATIONS:\n" + "\n".join(rows) + "\n" + CHECK_HELP
    )


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RANGE_RE = re.compile(r"^(\d+)-(\d+|end)$")
_KIND_OF = {"segment": "seg", "items": "set", "number": "number", "diff": "diff"}


@dataclass
class Step:
    line: int
    name: str
    op: Op
    args: list[str]


@dataclass
class Check:
    line: int
    source: str
    expr: tuple


@dataclass
class Plan:
    steps: list[Step]
    checks: list[Check]
    types: dict[str, str]


def _suggest(word: str, choices) -> str:
    close = difflib.get_close_matches(word, list(choices), n=1)
    return f" Did you mean '{close[0]}'?" if close else ""


def _check_arg(kind: str, arg: str, types: dict[str, str], where: str) -> None:
    if kind == "path":
        if arg in types:
            raise PlanError(f"{where}: expected a file path, got the result name '{arg}'.")
        return
    if kind == "text":
        return
    if kind == "regex":
        try:
            re.compile(arg)
        except re.error as error:
            raise PlanError(f"{where}: invalid regular expression {arg!r}: {error}.")
        return
    if kind == "range":
        match = _RANGE_RE.match(arg)
        if not match:
            raise PlanError(f"{where}: expected a line range like 10-40 or 10-end, got '{arg}'.")
        if match.group(2) != "end" and int(match.group(2)) < int(match.group(1)):
            raise PlanError(f"{where}: range '{arg}' ends before it starts.")
        if int(match.group(1)) < 1:
            raise PlanError(f"{where}: line numbers start at 1.")
        return
    if kind == "int":
        if not arg.isdigit() or int(arg) < 1:
            raise PlanError(f"{where}: expected a positive integer, got '{arg}'.")
        return
    if arg not in types:
        raise PlanError(
            f"{where}: '{arg}' is not a result defined above.{_suggest(arg, types)} "
            f"Defined: {', '.join(types) or 'none'}."
        )
    got = _KIND_OF[types[arg]]
    accepted = {"seg": {"seg"}, "set": {"seg", "set"}, "any": {"seg", "set", "number", "diff"}}[kind]
    if got not in accepted:
        need = {"seg": "a segment (file/lines/section/env/between)",
                "set": "items or a segment", "any": "a result"}[kind]
        raise PlanError(f"{where}: '{arg}' is {types[arg]}, but this argument needs {need}.")


# -- check expressions --------------------------------------------------------

_TOKEN_RE = re.compile(r"""\s*(?:(<=|>=|==|!=|<|>)|([(),])|'([^']*)'|"([^"]*)"|([^\s(),'"<>=!]+))""")
_COMPARATORS = {"==", "!=", "<", "<=", ">", ">="}


def _tokenize(text: str, where: str) -> list[tuple[str, str]]:
    tokens = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match or match.end() == position:
            raise PlanError(f"{where}: cannot read the check near {text[position:position + 20]!r}.")
        op, punct, single, double, word = match.groups()
        if op:
            tokens.append(("cmp", op))
        elif punct:
            tokens.append(("punct", punct))
        elif single is not None or double is not None:
            tokens.append(("text", single if single is not None else double))
        else:
            tokens.append(("word", word))
        position = match.end()
    return tokens


class _CheckParser:
    UNARY = {"empty", "nonempty"}
    BINARY = {"equal", "sameset", "subset"}

    def __init__(self, tokens, types, where):
        self.tokens = tokens
        self.pos = 0
        self.types = types
        self.where = where

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def take(self, kind=None, value=None):
        token = self.peek()
        if token[0] is None or (kind and token[0] != kind) or (value and token[1] != value):
            want = value or kind or "more"
            raise PlanError(f"{self.where}: expected {want!r} in check, got {token[1]!r}.")
        self.pos += 1
        return token

    def result(self, allow_number=False):
        kind, value = self.take()
        if kind == "word" and value in self.types:
            return ("ref", value)
        if allow_number and kind == "word" and re.fullmatch(r"-?\d+", value):
            return ("int", int(value))
        raise PlanError(
            f"{self.where}: '{value}' is not a result defined above.{_suggest(str(value), self.types)}"
        )

    def parse(self):
        expr = self.predicate()
        if self.pos != len(self.tokens):
            raise PlanError(f"{self.where}: unexpected {self.peek()[1]!r} after the check.")
        return expr

    def predicate(self):
        kind, value = self.peek()
        if kind == "word" and value in ("all", "any", "not"):
            self.take()
            self.take("punct", "(")
            parts = [self.predicate()]
            while self.peek() == ("punct", ","):
                self.take()
                parts.append(self.predicate())
            self.take("punct", ")")
            if value == "not" and len(parts) != 1:
                raise PlanError(f"{self.where}: not(...) takes exactly one condition.")
            return (value, parts)
        if kind == "word" and value in self.UNARY:
            self.take()
            return (value, self.result())
        if kind == "word" and value in self.BINARY:
            self.take()
            return (value, self.result(), self.result())
        if kind == "word" and value == "contains":
            self.take()
            subject = self.result()
            text_kind, text = self.take()
            if text_kind != "text":
                raise PlanError(f"{self.where}: contains needs quoted text, e.g. contains s1 'TODO'.")
            return ("contains", subject, text)
        if kind == "word" and value in self.types:
            left = self.result()
            cmp_kind, op = self.take()
            if cmp_kind != "cmp":
                raise PlanError(f"{self.where}: expected a comparison (<, <=, ==, ...) after '{value}'.")
            return ("cmp", op, left, self.result(allow_number=True))
        known = sorted(self.UNARY | self.BINARY | {"contains", "all", "any", "not"})
        raise PlanError(
            f"{self.where}: '{value}' does not start a condition.{_suggest(str(value), known)} "
            f"Conditions: {', '.join(known)}, or NAME OP N."
        )


def parse_plan(text: str, max_steps: int = MAX_STEPS_DEFAULT) -> Plan:
    steps: list[Step] = []
    checks: list[Check] = []
    types: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"line {number}"
        if line.startswith("check ") or line == "check":
            source = line[len("check"):].strip()
            if not source:
                raise PlanError(f"{where}: check needs a condition.")
            expr = _CheckParser(_tokenize(source, where), types, where).parse()
            checks.append(Check(number, source, expr))
            continue
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not _NAME_RE.match(name):
            raise PlanError(
                f"{where}: expected `NAME = OPERATION ARGS` or `check CONDITION`, got {line!r}."
            )
        if name in types:
            raise PlanError(f"{where}: '{name}' is already defined; give each result a new name.")
        if name in CATALOG or name in ("check", "all", "any", "not", "end"):
            raise PlanError(f"{where}: '{name}' is a reserved word; pick another result name.")
        try:
            parts = shlex.split(rest)
        except ValueError as error:
            raise PlanError(f"{where}: {error} (close every quote).")
        if not parts:
            raise PlanError(f"{where}: missing operation after '='.")
        op_name, args = parts[0], parts[1:]
        op = CATALOG.get(op_name)
        if op is None:
            raise PlanError(
                f"{where}: unknown operation '{op_name}'.{_suggest(op_name, CATALOG)} "
                f"Operations: {', '.join(CATALOG)}."
            )
        low, high = len(op.args) - op.optional, len(op.args)
        if not low <= len(args) <= high:
            expected = str(low) if low == high else f"{low}-{high}"
            raise PlanError(
                f"{where}: {op_name} takes {expected} argument(s) ({' '.join(op.args)}), got {len(args)}."
            )
        for kind, arg in zip(op.args, args):
            _check_arg(kind, arg, types, where)
        types[name] = op.returns
        steps.append(Step(number, name, op, args))
    if not steps:
        raise PlanError("the plan has no steps.")
    if len(steps) > max_steps:
        raise PlanError(
            f"the plan has {len(steps)} steps; the limit is {max_steps}. "
            "Split the question into smaller plans."
        )
    if not checks:
        raise PlanError("the plan has no `check` line. Add one that states the answer, e.g. `check empty s3`.")
    return Plan(steps, checks, types)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_step(step: Step, values: dict, root: str):
    op, args = step.op.name, step.args
    ref = lambda index: values[args[index]]  # noqa: E731
    if op == "file":
        return _segment(root, args[0])
    if op == "lines":
        match = _RANGE_RE.match(args[1])
        end = None if match.group(2) == "end" else int(match.group(2))
        return _segment(root, args[0], int(match.group(1)), end)
    if op == "section":
        return op_section(root, args[0], args[1])
    if op == "env":
        return op_env(root, args[0], args[1], int(args[2]) if len(args) > 2 else 1)
    if op == "between":
        return op_between(root, args[0], args[1], args[2])
    if op == "labels":
        return _keys(_LABEL_RE, ref(0))
    if op == "refs":
        return _keys(_REF_RE, ref(0))
    if op == "cites":
        return _keys(_CITE_RE, ref(0))
    if op == "numbers":
        return op_numbers(ref(0))
    if op == "headings":
        return op_headings(ref(0))
    if op == "grep":
        return op_grep(ref(0), args[1])
    if op == "nonblank":
        return op_nonblank(ref(0))
    if op == "words":
        return op_words(ref(0))
    if op == "count":
        return Number(size(ref(0)))
    if op == "unique":
        return Items(_unique(_as_items(ref(0))))
    if op == "diff":
        return op_diff(ref(0), ref(1))
    first, second = _unique(_as_items(ref(0))), _unique(_as_items(ref(1)))
    if op == "common":
        return Items([i for i in first if i in set(second)])
    if op == "only-a":
        return Items([i for i in first if i not in set(second)])
    if op == "only-b":
        return Items([i for i in second if i not in set(first)])
    raise PlanError(f"line {step.line}: operation '{op}' has no implementation.")


def _sequence(value) -> list:
    if isinstance(value, Segment):
        return value.lines
    if isinstance(value, Items):
        return value.items
    if isinstance(value, Diff):
        return [line for hunk in value.hunks for line in hunk]
    return [value.value]


def _distinct(value) -> list:
    """Distinct items of a value; a segment counts as its non-blank lines."""
    if isinstance(value, (Segment, Items)):
        return _unique(_as_items(value))
    return _unique(_sequence(value))


def evaluate(expr, values) -> tuple[bool, str]:
    """(passed, one-line evidence) for a parsed check expression."""
    kind = expr[0]
    if kind in ("all", "any", "not"):
        results = [evaluate(part, values) for part in expr[1]]
        if kind == "not":
            ok, why = results[0]
            return (not ok), f"not({why})"
        oks = [ok for ok, _ in results]
        passed = all(oks) if kind == "all" else any(oks)
        failing = [why for ok, why in results if not ok]
        if kind == "all":
            why = "all hold" if passed else "; ".join(failing)
        else:
            why = next(why for ok, why in results if ok) if passed else "none holds: " + "; ".join(failing)
        return passed, why

    def value(term):
        return values[term[1]]

    def name(term):
        return term[1]

    if kind in ("empty", "nonempty"):
        n = size(value(expr[1]))
        items = _sequence(value(expr[1]))
        passed = (n == 0) if kind == "empty" else (n > 0)
        detail = f"{name(expr[1])} has {n}"
        if n and isinstance(value(expr[1]), Items):
            detail += f": {_preview(items, 6)}"
        return passed, detail
    if kind == "equal":
        a, b = value(expr[1]), value(expr[2])
        seq_a, seq_b = _sequence(a), _sequence(b)
        if seq_a == seq_b:
            return True, f"{name(expr[1])} equals {name(expr[2])}"
        for index, (x, y) in enumerate(zip(seq_a, seq_b)):
            if x != y:
                return False, f"first difference at position {index + 1}: {x!r} vs {y!r}"
        return False, f"lengths differ: {len(seq_a)} vs {len(seq_b)}"
    if kind in ("sameset", "subset"):
        a, b = _distinct(value(expr[1])), _distinct(value(expr[2]))
        missing = [i for i in a if i not in set(b)]
        extra = [i for i in b if i not in set(a)]
        if kind == "subset":
            if not missing:
                return True, f"all {len(a)} of {name(expr[1])} are in {name(expr[2])}"
            return False, f"missing from {name(expr[2])} ({len(missing)}): {_preview(missing, 6)}"
        if not missing and not extra:
            return True, f"same {len(a)} distinct items"
        parts = []
        if missing:
            parts.append(f"only in {name(expr[1])}: {_preview(missing, 4)}")
        if extra:
            parts.append(f"only in {name(expr[2])}: {_preview(extra, 4)}")
        return False, "; ".join(parts)
    if kind == "contains":
        text = expr[2]
        for index, item in enumerate(_sequence(value(expr[1]))):
            if text in str(item):
                subject = value(expr[1])
                where = f"line {subject.start + index}" if isinstance(subject, Segment) else f"item {index + 1}"
                return True, f"{name(expr[1])} {where} contains {text!r}"
        return False, f"{name(expr[1])} does not contain {text!r}"
    if kind == "cmp":
        _, op, left, right = expr
        a = size(value(left))
        b = right[1] if right[0] == "int" else size(value(right))
        passed = {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        right_label = str(b) if right[0] == "int" else f"{name(right)}={b}"
        return passed, f"{name(left)}={a} {op} {right_label}"
    raise ValueError(kind)


def run_plan(plan: Plan, root: str, until: str | None = None) -> dict:
    values: dict = {}
    for step in plan.steps:
        try:
            values[step.name] = execute_step(step, values, root)
        except PlanError as error:
            raise PlanError(f"line {step.line} ({step.name} = {step.op.name}): {error}")
        if step.name == until:
            break
    return values


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _clip(text: str, max_chars: int, plan_path: str) -> str:
    if len(text) <= max_chars:
        return text
    note = f"\n[output capped at {max_chars} chars; page a result with: show \"{plan_path}\" NAME]"
    return text[: max_chars - len(note)].rsplit("\n", 1)[0] + note


def _diff_tool_hint(value: Diff) -> str:
    a, b = value.a, value.b
    return (
        f"diff_files('{a.path}', '{b.path}', start_a={a.start}, end_a={max(a.end, a.start)}, "
        f"start_b={b.start}, end_b={max(b.end, b.start)})"
    )


def render_run(plan: Plan, values: dict, plan_path: str) -> tuple[str, bool]:
    lines = [f"OPS RUN {plan_path}"]
    for step in plan.steps:
        described = values[step.name].describe()
        lines.append(f"{step.name} = {step.op.name} {shlex.join(step.args)} -> {described}")
    lines.append("CHECKS:")
    failed = 0
    for check in plan.checks:
        passed, why = evaluate(check.expr, values)
        failed += not passed
        lines.append(f"  {'PASS' if passed else 'FAIL'}  {check.source}  [{why}]")
    passed_all = failed == 0
    lines.append(
        "VERDICT: PASS" if passed_all
        else f"VERDICT: FAIL ({failed} of {len(plan.checks)} checks failed)"
    )
    diffs = [(n, v) for n, v in values.items() if isinstance(v, Diff) and v.blocks]
    if diffs:
        name, value = diffs[0]
        lines.append(f"DETAIL: show \"{plan_path}\" {name}  or  {_diff_tool_hint(value)}")
    return "\n".join(lines), passed_all


def render_value(value) -> list[str]:
    if isinstance(value, Segment):
        return [f"L{value.start + i}: {line}" for i, line in enumerate(value.lines)]
    if isinstance(value, Items):
        return [f"{i + 1}. {item}" for i, item in enumerate(value.items)]
    if isinstance(value, Number):
        return [str(value.value)]
    if not value.blocks:
        return ["identical"]
    return [line for hunk in value.hunks for line in hunk]


def render_show(name: str, value, offset: int, max_chars: int, plan_path: str) -> str:
    body = render_value(value)
    if offset >= len(body) and body:
        raise PlanError(f"--offset {offset} is past the end of {name} ({len(body)} lines).")
    header = f"SHOW {name}: {value.describe()}"
    shown, used, index = [], len(header) + 1, offset
    budget = max_chars - 120  # room for the paging note
    while index < len(body):
        cost = len(body[index]) + 1
        if shown and used + cost > budget:
            break
        shown.append(body[index][: max(40, budget - used)])
        used += cost
        index += 1
    text = "\n".join([header, *shown])
    if index < len(body):
        text += (
            f"\n[lines {offset + 1}-{index} of {len(body)}; next page: "
            f"show \"{plan_path}\" {name} --offset {index}]"
        )
    return text


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _load(plan_path: str, max_steps: int) -> Plan:
    if not os.path.isfile(plan_path):
        raise PlanError(f"plan file not found: '{plan_path}'. Write the plan with write_file first.")
    with open(plan_path, "r", encoding="utf-8") as handle:
        return parse_plan(handle.read(), max_steps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a plan of elementary text operations.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog", help="list operations and the check grammar")
    for command in ("validate", "run", "show"):
        p = sub.add_parser(command)
        p.add_argument("plan")
        if command == "show":
            p.add_argument("name")
            p.add_argument("--offset", type=int, default=0)
        p.add_argument("--root", default=os.getcwd(), help="directory relative paths start from")
        p.add_argument("--max-chars", type=int, default=MAX_CHARS_DEFAULT)
        p.add_argument("--max-steps", type=int, default=MAX_STEPS_DEFAULT)
    args = parser.parse_args(argv)

    if args.command == "catalog":
        print(catalog_text())
        return 0
    try:
        plan = _load(args.plan, args.max_steps)
        if args.command == "validate":
            print(f"PLAN OK: {len(plan.steps)} steps, {len(plan.checks)} checks.")
            return 0
        if args.command == "show":
            if args.name not in plan.types:
                raise PlanError(
                    f"'{args.name}' is not a result of this plan.{_suggest(args.name, plan.types)} "
                    f"Results: {', '.join(plan.types)}."
                )
            if args.offset < 0:
                raise PlanError("--offset must be non-negative.")
            values = run_plan(plan, args.root, until=args.name)
            print(render_show(args.name, values[args.name], args.offset, args.max_chars, args.plan))
            return 0
        values = run_plan(plan, args.root)
        text, passed = render_run(plan, values, args.plan)
        print(_clip(text, args.max_chars, args.plan))
        return 0 if passed else 1
    except PlanError as error:
        print(f"PLAN ERROR: {error}")
        print("STATUS: fix the plan and run again (at most 2 attempts), otherwise report BLOCKED.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
