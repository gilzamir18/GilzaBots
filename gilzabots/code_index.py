"""Multi-language code symbol index with SQLite persistence.

Uses tree-sitter-language-pack as the primary extraction engine — supports
any language with a tree-sitter grammar (300+ languages, including future ones
as new grammars are published). Falls back to regex patterns for languages
whose grammar hasn't been downloaded yet.

The index persists to SQLite inside the project's .gilzabots/ directory and
is rebuilt incrementally on file mtime changes. Every write_file / edit_file
call triggers a single-file reindex so the index stays fresh within a cycle.

Usage (singleton — same pattern as AGENT_PROGRESS):
    from .code_index import CODE_INDEX
    CODE_INDEX.set_project(path)     # called once by the orchestrator
    CODE_INDEX.build()               # incremental full scan
    CODE_INDEX.rebuild_file(path)    # called by edit_file / write_file
    symbols  = CODE_INDEX.search("authenticate")
    callers  = CODE_INDEX.find_callers("login")
    snapshot = CODE_INDEX.project_snapshot()

Agents query it through the `find_symbol` and `code_outline` tools
(gilzabots/tools.py), which point it at the project and bring it up to date
before every query, so a file changed by any means is re-read by mtime.

Python goes through the stdlib `ast`; LaTeX and BibTeX through their own
patterns; everything else through the regex fallback. The tree-sitter path is
kept but off (`_TREE_SITTER_ENABLED`), for two reasons found when the index was
first wired to tools:

- It had never run: it passed `str` to `Parser.parse`, which takes bytes, and
  the TypeError sent every file to the regex fallback.
- Once fixed, walking a large tree (this project's App.jsx) crashed the process
  with a segmentation fault, non-deterministically, on Python 3.14 with
  py-tree-sitter 0.26 and tree-sitter-language-pack 1.20 -- reading the same
  node attributes passed or crashed from run to run. The index runs inside the
  IDE's server process, where a native crash takes the whole application down.

Should it be turned back on, it must also stay offline (PROJECT_DESIGN 2.2):
`get_parser` downloads a missing grammar, so only grammars already in the
pack's cache may be used, which `_get_ts_parser` enforces.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: str          # function | class | method | interface | enum | const | type | module
    file: str          # relative to project root
    line: int          # 1-based
    signature: str
    exported: bool
    calls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Language detection — extension → tree-sitter language name
# ---------------------------------------------------------------------------

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".zig": "zig",
    ".lua": "lua",
    ".scala": "scala",
    ".ex": "elixir", ".exs": "elixir",
    ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml",
    ".clj": "clojure", ".cljs": "clojure",
    ".elm": "elm",
    ".erl": "erlang", ".hrl": "erlang",
    ".dart": "dart",
    ".r": "r", ".R": "r",
    ".jl": "julia",
    ".nim": "nim",
    ".v": "v",
    ".odin": "odin",
    ".gleam": "gleam",
    ".zig": "zig",
    ".sol": "solidity",
    ".proto": "proto",
    ".graphql": "graphql", ".gql": "graphql",
    ".vue": "vue",
    ".svelte": "svelte",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".html": "html", ".htm": "html",
    ".sh": "bash", ".bash": "bash",
    ".sql": "sql",
    ".tf": "terraform", ".hcl": "hcl",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".md": "markdown",
}

# Kinds that contain callable code — used to infer exported status
_CALLABLE_KINDS = {"function", "method"}

# tree-sitter node kind → our Symbol kind
_TS_KIND_MAP: dict[str, str] = {
    "function_definition": "function",
    "function_declaration": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "class_definition": "class",
    "class_declaration": "class",
    "struct_item": "class",
    "struct_declaration": "class",
    "impl_item": "class",
    "interface_declaration": "interface",
    "interface_definition": "interface",
    "trait_item": "interface",
    "enum_item": "enum",
    "enum_declaration": "enum",
    "const_item": "const",
    "const_declaration": "const",
    "variable_declaration": "const",
    "type_alias_declaration": "type",
    "type_item": "type",
    "type_definition": "type",
    "module_declaration": "module",
    "mod_item": "module",
    "fn_item": "function",
    "function_item": "function",
    "arrow_function": "function",
    "lexical_declaration": "const",
}

# Node kinds that carry a symbol name in a child identifier
_NAME_BEARING_KINDS = set(_TS_KIND_MAP.keys())

# Node kinds that represent call sites
_CALL_KINDS = {"call", "call_expression", "method_call", "function_call",
               "invocation_expression", "call_statement"}

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", "dist", "build", "target", ".gilzabots",
    ".next", ".nuxt", "coverage", ".tox",
}

# LaTeX and BibTeX have no tree-sitter path here: their outline is extracted by
# patterns (`_extract_latex`, `_extract_bibtex`), which is what an IDE for them
# needs anyway -- sections, labels, macros and citation keys.
_LATEX_EXTS = {".tex", ".sty", ".cls", ".ltx"}
_BIBTEX_EXTS = {".bib"}

# File extensions we index
_INDEXED_EXTS = set(_EXT_TO_LANG.keys()) | _LATEX_EXTS | _BIBTEX_EXTS

# A walk stops after this many indexed files, and files larger than this are
# skipped: generated bundles and data dumps hold no symbols worth listing, and
# a first build has to stay quick on a large tree.
_MAX_INDEXED_FILES = 20000
_MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Regex fallback patterns (for languages without a downloadable grammar)
# ---------------------------------------------------------------------------

@dataclass
class _RegexPattern:
    regex: re.Pattern
    name_group: int
    kind: str
    exported_prefix: tuple[str, ...]


_GENERIC_PATTERNS: list[_RegexPattern] = [
    _RegexPattern(
        re.compile(r"^(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
        1, "function", ("pub ",)),
    _RegexPattern(
        re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"),
        1, "function", ("export ",)),
    _RegexPattern(
        re.compile(r"^(?:async\s+)?def\s+(\w+)"),
        1, "function", ()),
    _RegexPattern(
        re.compile(r"^(?:export\s+)?(?:abstract\s+|sealed\s+|data\s+)?class\s+(\w+)"),
        1, "class", ("export ", "public ")),
    _RegexPattern(
        re.compile(r"^(?:export\s+)?interface\s+(\w+)"),
        1, "interface", ("export ", "public ")),
    _RegexPattern(
        re.compile(r"^(?:pub\s+)?(?:struct|enum|trait|type)\s+(\w+)"),
        1, "class", ("pub ",)),
    _RegexPattern(
        re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)"),
        1, "function", ()),
    # JavaScript/TypeScript: `const name = (...) =>` and `const name = function`.
    _RegexPattern(
        re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|\w+\s*=>)"),
        1, "function", ("export ",)),
]

# Indented `name(args) {` inside a class body (JavaScript, TypeScript, Java,
# C#, ...). Control-flow keywords share that shape and are excluded by name.
_METHOD_RE = re.compile(
    r"^\s+(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*(\w+)\s*\([^;]*\)\s*(?::[^{]+)?\{\s*$"
)
_NOT_METHOD_NAMES = {"if", "for", "while", "switch", "catch", "function", "return", "with", "else", "do", "try"}

_CALL_RE = re.compile(r"\b(\w+)\s*\(")


# ---------------------------------------------------------------------------
# Extraction: tree-sitter path
# ---------------------------------------------------------------------------

def _lang_name(ext: str) -> Optional[str]:
    return _EXT_TO_LANG.get(ext.lower())


# Off: see the module docstring. A native crash here would end the IDE process.
_TREE_SITTER_ENABLED = False

_DOWNLOADED_GRAMMARS: Optional[frozenset] = None


def _downloaded_grammars() -> frozenset:
    """Grammars already in tree-sitter-language-pack's cache, read once."""
    global _DOWNLOADED_GRAMMARS
    if _DOWNLOADED_GRAMMARS is None:
        try:
            import tree_sitter_language_pack as lp
            _DOWNLOADED_GRAMMARS = frozenset(lp.downloaded_languages())
        except Exception:
            _DOWNLOADED_GRAMMARS = frozenset()
    return _DOWNLOADED_GRAMMARS


def _get_ts_parser(lang: str):
    """Return a tree-sitter Parser for *lang*, or None if unavailable.

    Only a grammar already in the cache is used: `get_parser` downloads a
    missing one, and indexing must not reach the network.
    """
    if not _TREE_SITTER_ENABLED or lang not in _downloaded_grammars():
        return None
    try:
        import tree_sitter_language_pack as lp
        return lp.get_parser(lang)
    except Exception:
        return None


# Definitions whose body holds further definitions worth listing (methods of a
# class, items of a module). A function body is not descended into: its locals
# are not part of the outline.
_TS_CONTAINER_KINDS = {"class", "interface", "module"}

_TS_NAME_KINDS = ("identifier", "name", "property_identifier", "field_identifier",
                  "type_identifier", "constant")


def _ts_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ts_name(node, src_bytes: bytes) -> Optional[str]:
    for child in node.children:
        if child.type in _TS_NAME_KINDS:
            return _ts_text(child, src_bytes)
    # `const name = ...` keeps its name one level down, in the declarator.
    for child in node.children:
        if child.type == "variable_declarator":
            for grandchild in child.children:
                if grandchild.type in _TS_NAME_KINDS:
                    return _ts_text(grandchild, src_bytes)
    return None


# Both traversals below use an explicit stack. Recursion is not an option: a
# deeply nested tree (JSX several hundred levels deep, as in this project's own
# App.jsx) reaches Python's recursion limit inside py-tree-sitter's C getters,
# and that crashed the whole process with a segmentation fault instead of
# raising RecursionError.

def _walk_ts(root, src_bytes: bytes, rel_path: str, symbols: list[Symbol]) -> None:
    """Walk a tree-sitter tree in source order and collect symbols."""
    stack = [root]
    while stack:
        node = stack.pop()
        ts_kind = _TS_KIND_MAP.get(node.type)
        if ts_kind:
            name = _ts_name(node, src_bytes)
            if name and len(name) >= 2:
                sig = src_bytes[node.start_byte:min(node.end_byte, node.start_byte + 200)]
                sig = sig.decode("utf-8", errors="replace").replace("\n", " ").strip()[:120]
                symbols.append(Symbol(
                    name=name,
                    kind=ts_kind,
                    file=rel_path,
                    line=node.start_point.row + 1,  # 0-based → 1-based
                    signature=sig,
                    exported=not name.startswith("_"),
                    calls=_collect_calls_ts(node, src_bytes, name),
                ))
                if ts_kind not in _TS_CONTAINER_KINDS:
                    continue
        stack.extend(reversed(node.children))


def _collect_calls_ts(node, src_bytes: bytes, self_name: str) -> list[str]:
    """Collect the names of all call-site identifiers within *node*."""
    calls: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _CALL_KINDS:
            # First named child of a call is usually the function/method name
            for child in current.children:
                if child.type in ("identifier", "field_expression", "member_expression",
                                  "scoped_identifier", "attribute"):
                    # Use only the last segment (method name)
                    callee = _ts_text(child, src_bytes).split(".")[-1].split("::")[-1]
                    if len(callee) >= 2:
                        calls.append(callee)
                    break
        stack.extend(current.children)
    return sorted(set(calls) - {self_name})


def _extract_ts(source: str, lang: str, rel_path: str) -> Optional[list[Symbol]]:
    """Extract symbols using tree-sitter. Returns None if unavailable."""
    parser = _get_ts_parser(lang)
    if parser is None:
        return None
    try:
        src_bytes = source.encode("utf-8", errors="replace")
        tree = parser.parse(src_bytes)
        symbols: list[Symbol] = []
        _walk_ts(tree.root_node, src_bytes, rel_path, symbols)
        return symbols
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Extraction: Python ast path (most accurate for call graphs)
# ---------------------------------------------------------------------------

def _extract_py_ast(source: str, rel_path: str) -> list[Symbol]:
    """Extract symbols from Python source using the stdlib ast module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: list[Symbol] = []

    def _calls_in(node: ast.AST) -> list[str]:
        names: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    names.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    names.append(child.func.attr)
        return sorted(set(names))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            sig = f"def {node.name}({', '.join(args)})"
            symbols.append(Symbol(
                name=node.name,
                kind="function",
                file=rel_path,
                line=node.lineno,
                signature=sig[:120],
                exported=not node.name.startswith("_"),
                calls=_calls_in(node),
            ))
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol(
                name=node.name,
                kind="class",
                file=rel_path,
                line=node.lineno,
                signature=f"class {node.name}",
                exported=not node.name.startswith("_"),
                calls=[],
            ))

    return symbols


# ---------------------------------------------------------------------------
# Extraction: regex fallback
# ---------------------------------------------------------------------------

def _extract_regex(source: str, rel_path: str) -> list[Symbol]:
    """Generic regex extraction — works for any C/Python/Rust-style syntax."""
    symbols: list[Symbol] = []
    for i, line in enumerate(source.splitlines(), start=1):
        trimmed = line.strip()
        for pat in _GENERIC_PATTERNS:
            m = pat.regex.match(trimmed)
            if not m:
                continue
            name = m.group(pat.name_group)
            if not name or len(name) < 2:
                continue
            exported = any(trimmed.startswith(p) for p in pat.exported_prefix) if pat.exported_prefix else True
            calls = [c for c in _CALL_RE.findall(trimmed) if c != name and len(c) > 1]
            symbols.append(Symbol(
                name=name,
                kind=pat.kind,
                file=rel_path,
                line=i,
                signature=trimmed[:120],
                exported=exported,
                calls=sorted(set(calls)),
            ))
            break
        else:
            m = _METHOD_RE.match(line)
            if m and m.group(1) not in _NOT_METHOD_NAMES and len(m.group(1)) >= 2:
                symbols.append(Symbol(
                    name=m.group(1),
                    kind="method",
                    file=rel_path,
                    line=i,
                    signature=trimmed[:120],
                    exported=not m.group(1).startswith("_"),
                    calls=[],
                ))
    return symbols


# ---------------------------------------------------------------------------
# Extraction: LaTeX and BibTeX
# ---------------------------------------------------------------------------

_LATEX_SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}"
)
_LATEX_LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
_LATEX_MACRO_RE = re.compile(
    r"\\(?:(?:re)?newcommand|providecommand|DeclareMathOperator)\*?\s*\{?\s*\\([A-Za-z@]+)"
    r"|\\def\s*\\([A-Za-z@]+)"
)
_LATEX_ENV_RE = re.compile(r"\\(?:re)?newenvironment\*?\s*\{([^}]+)\}")
_LATEX_FRAME_RE = re.compile(r"\\begin\s*\{frame\}(?:\[[^\]]*\])?\s*\{([^}]*)\}")
_LATEX_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_BIB_ENTRY_RE = re.compile(r"^\s*@(\w+)\s*\{\s*([^,\s]+)\s*,")


def _strip_latex_comment(line: str) -> str:
    """The line up to its first unescaped %."""
    for i, ch in enumerate(line):
        if ch == "%" and (i == 0 or line[i - 1] != "\\"):
            return line[:i]
    return line


def _extract_latex(source: str, rel_path: str) -> list[Symbol]:
    """Sections, frames, labels, macro and environment definitions, and inputs."""
    symbols: list[Symbol] = []

    def add(name: str, kind: str, line_no: int, sig: str) -> None:
        name = name.strip()
        if name:
            symbols.append(Symbol(name=name, kind=kind, file=rel_path, line=line_no,
                                  signature=sig.strip()[:120], exported=True))

    for i, raw in enumerate(source.splitlines(), start=1):
        line = _strip_latex_comment(raw)
        if "\\" not in line:
            continue
        for m in _LATEX_SECTION_RE.finditer(line):
            add(m.group(2), m.group(1), i, m.group(0))
        for m in _LATEX_FRAME_RE.finditer(line):
            add(m.group(1), "frame", i, m.group(0))
        for m in _LATEX_LABEL_RE.finditer(line):
            add(m.group(1), "label", i, m.group(0))
        for m in _LATEX_MACRO_RE.finditer(line):
            add("\\" + (m.group(1) or m.group(2)), "macro", i, line)
        for m in _LATEX_ENV_RE.finditer(line):
            add(m.group(1), "environment", i, m.group(0))
        for m in _LATEX_INPUT_RE.finditer(line):
            add(m.group(1), "input", i, m.group(0))
    return symbols


def _extract_bibtex(source: str, rel_path: str) -> list[Symbol]:
    """One symbol per entry: its citation key."""
    symbols: list[Symbol] = []
    for i, line in enumerate(source.splitlines(), start=1):
        m = _BIB_ENTRY_RE.match(line)
        if m and m.group(1).lower() not in {"comment", "string", "preamble"}:
            symbols.append(Symbol(name=m.group(2), kind="bibentry", file=rel_path, line=i,
                                  signature=f"@{m.group(1)}{{{m.group(2)},", exported=True))
    return symbols


# ---------------------------------------------------------------------------
# Unified extraction entry point
# ---------------------------------------------------------------------------

def _extract(source: str, ext: str, rel_path: str) -> list[Symbol]:
    """Extract symbols from source, choosing the best available strategy."""
    if ext in _LATEX_EXTS:
        return _extract_latex(source, rel_path)
    if ext in _BIBTEX_EXTS:
        return _extract_bibtex(source, rel_path)
    # Python: stdlib ast is more accurate than tree-sitter for call graphs
    if ext == ".py":
        syms = _extract_py_ast(source, rel_path)
        if syms:
            return syms

    # All other languages: try tree-sitter first
    lang = _lang_name(ext)
    if lang:
        syms = _extract_ts(source, lang, rel_path)
        if syms is not None:
            return syms

    # Fallback: generic regex
    return _extract_regex(source, rel_path)


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS symbols (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    file      TEXT    NOT NULL,
    line      INTEGER NOT NULL,
    signature TEXT    NOT NULL,
    exported  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file);

CREATE TABLE IF NOT EXISTS calls (
    caller_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_name TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);

CREATE TABLE IF NOT EXISTS file_mtimes (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);
"""


def _open_db(db_path: str) -> sqlite3.Connection:
    # Agent tools run in worker threads; every use goes through CodeIndex._lock.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# CodeIndex
# ---------------------------------------------------------------------------

class CodeIndex:
    """Project-scoped symbol index backed by SQLite.

    Thread-safe: agent tools run in worker threads (`FunctionBlock` ->
    `asyncio.to_thread`), and every public method holds one lock.
    """

    def __init__(self) -> None:
        self._root: str = ""
        self._db_path: str = ""
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_project(self, root: str) -> None:
        """Point the index at *root*. Opens (or creates) the SQLite DB."""
        root = os.path.abspath(root)
        with self._lock:
            if root == self._root and self._conn is not None:
                return
            db_dir = os.path.join(root, ".gilzabots")
            os.makedirs(db_dir, exist_ok=True)
            if self._conn:
                self._conn.close()
            self._root = root
            self._db_path = os.path.join(db_dir, "code_index.sqlite")
            self._conn = _open_db(self._db_path)

    @property
    def root(self) -> str:
        return self._root

    def _conn_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("CodeIndex: call set_project() before using the index.")
        return self._conn

    def close(self) -> None:
        """Close the SQLite connection to release file locks."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
            self._root = ""
            self._db_path = ""

    # ------------------------------------------------------------------
    # Build / incremental update
    # ------------------------------------------------------------------

    def _indexable_files(self):
        """(path, rel) of every indexable file, pruning skipped directories.

        `os.walk` with pruning, not `rglob`: rglob enumerated node_modules and
        .git in full before the filter threw them away.
        """
        root = self._root
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                if os.path.splitext(filename)[1].lower() not in _INDEXED_EXTS:
                    continue
                path = os.path.join(dirpath, filename)
                yield Path(path), os.path.relpath(path, root)
                count += 1
                if count >= _MAX_INDEXED_FILES:
                    return

    def build(self) -> dict[str, int]:
        """Scan the project, index changed files and drop deleted ones.

        Returns {"indexed": N, "skipped": M, "removed": R, "total": N+M}.
        """
        with self._lock:
            conn = self._conn_or_raise()
            indexed = skipped = 0
            known = {row[0]: row[1] for row in conn.execute("SELECT path, mtime FROM file_mtimes")}
            seen: set[str] = set()

            for p, rel in self._indexable_files():
                try:
                    stat = p.stat()
                except OSError:
                    continue
                seen.add(rel)
                if stat.st_size > _MAX_FILE_BYTES:
                    continue
                mtime = known.get(rel)
                if mtime is not None and abs(mtime - stat.st_mtime) < 0.01:
                    skipped += 1
                    continue
                self._index_file(conn, p, rel)
                conn.execute(
                    "INSERT OR REPLACE INTO file_mtimes(path, mtime) VALUES (?, ?)",
                    (rel, stat.st_mtime),
                )
                indexed += 1

            removed = [rel for rel in known if rel not in seen]
            for rel in removed:
                conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
                conn.execute("DELETE FROM file_mtimes WHERE path = ?", (rel,))

            conn.commit()
            return {"indexed": indexed, "skipped": skipped, "removed": len(removed),
                    "total": indexed + skipped}

    def rebuild_file(self, abs_or_rel_path: str) -> None:
        """Re-index a single file after it has been written or edited."""
        with self._lock:
            if not self._root or self._conn is None:
                return
            conn = self._conn
            root = Path(self._root)
            p = Path(abs_or_rel_path)
            if not p.is_absolute():
                p = root / p
            if not p.exists() or p.suffix.lower() not in _INDEXED_EXTS:
                return

            try:
                rel = str(p.relative_to(root))
            except ValueError:
                return

            self._index_file(conn, p, rel)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO file_mtimes(path, mtime) VALUES (?, ?)",
                    (rel, p.stat().st_mtime),
                )
                conn.commit()
            except Exception:
                pass

    def _index_file(self, conn: sqlite3.Connection, p: Path, rel: str) -> None:
        """Delete old entries for *rel* and insert freshly extracted symbols."""
        try:
            source = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        ext = p.suffix.lower()
        symbols = _extract(source, ext, rel)

        conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
        for sym in symbols:
            cur = conn.execute(
                "INSERT INTO symbols(name, kind, file, line, signature, exported) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sym.name, sym.kind, sym.file, sym.line, sym.signature, int(sym.exported)),
            )
            sym_id = cur.lastrowid
            for callee in sym.calls:
                conn.execute(
                    "INSERT INTO calls(caller_id, callee_name) VALUES (?, ?)",
                    (sym_id, callee),
                )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _row_to_symbol(self, conn: sqlite3.Connection, row: tuple) -> Symbol:
        sym_id, name, kind, file_, line, sig, exported = row
        calls = [r[0] for r in conn.execute(
            "SELECT callee_name FROM calls WHERE caller_id = ?", (sym_id,)
        )]
        return Symbol(name=name, kind=kind, file=file_, line=line,
                      signature=sig, exported=bool(exported), calls=calls)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[Symbol]:
        """Search symbols by name: exact → prefix → substring."""
        with self._lock:
            return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[Symbol]:
        conn = self._conn_or_raise()
        q = query.lower()
        seen: set[int] = set()
        results: list[Symbol] = []

        _BASE = ("SELECT id, name, kind, file, line, signature, exported "
                 "FROM symbols")

        def _fetch(sql: str, params: tuple) -> None:
            for row in conn.execute(sql, params):
                if row[0] in seen or len(results) >= limit:
                    return
                seen.add(row[0])
                results.append(self._row_to_symbol(conn, row))

        _fetch(f"{_BASE} WHERE lower(name) = ? LIMIT ?", (q, limit))
        if len(results) < limit:
            _fetch(f"{_BASE} WHERE lower(name) LIKE ? AND lower(name) != ? LIMIT ?",
                   (q + "%", q, limit - len(results)))
        if len(results) < limit:
            _fetch(f"{_BASE} WHERE lower(name) LIKE ? AND lower(name) NOT LIKE ? LIMIT ?",
                   ("%" + q + "%", q + "%", limit - len(results)))

        return results

    def find_callers(self, callee_name: str, limit: int = 20) -> list[Symbol]:
        """Return all symbols that call *callee_name* (reverse call graph)."""
        with self._lock:
            return self._find_callers(callee_name, limit)

    def _find_callers(self, callee_name: str, limit: int) -> list[Symbol]:
        conn = self._conn_or_raise()
        sql = """
            SELECT DISTINCT s.id, s.name, s.kind, s.file, s.line, s.signature, s.exported
            FROM symbols s
            JOIN calls c ON c.caller_id = s.id
            WHERE lower(c.callee_name) = lower(?)
            LIMIT ?
        """
        return [self._row_to_symbol(conn, row)
                for row in conn.execute(sql, (callee_name, limit))]

    def symbols_in_file(self, rel_path: str) -> list[Symbol]:
        """Return all symbols defined in *rel_path* (relative to project root)."""
        with self._lock:
            conn = self._conn_or_raise()
            rows = conn.execute(
                "SELECT id, name, kind, file, line, signature, exported "
                "FROM symbols WHERE file = ? ORDER BY line",
                (rel_path,),
            ).fetchall()
            return [self._row_to_symbol(conn, row) for row in rows]

    def indexed_files(self, prefix: str = "") -> list[str]:
        """Indexed files under the relative directory *prefix* ("" for all), sorted."""
        with self._lock:
            conn = self._conn_or_raise()
            rows = conn.execute("SELECT path FROM file_mtimes ORDER BY path").fetchall()
        prefix = prefix.strip("/\\").replace("\\", "/")
        files = [r[0] for r in rows]
        if not prefix or prefix == ".":
            return files
        return [f for f in files if f.replace("\\", "/").startswith(prefix + "/")]

    # ------------------------------------------------------------------
    # Context helpers for oracle prompts
    # ------------------------------------------------------------------

    def project_snapshot(self, max_files: int = 100) -> str:
        """Return a symbol-enriched project snapshot for oracle prompts.

        Each indexed file is listed with its exported symbols so the planner
        can reference concrete function/class names instead of guessing.
        """
        with self._lock:
            return self._project_snapshot(max_files)

    def _project_snapshot(self, max_files: int) -> str:
        conn = self._conn_or_raise()
        rows = conn.execute(
            "SELECT DISTINCT file FROM symbols ORDER BY file LIMIT ?", (max_files,)
        ).fetchall()

        if not rows:
            return "(index empty — run build() first)"

        parts: list[str] = []
        for (rel,) in rows:
            syms = conn.execute(
                "SELECT name, kind FROM symbols "
                "WHERE file = ? AND exported = 1 ORDER BY line",
                (rel,),
            ).fetchall()
            if syms:
                sym_str = ", ".join(f"{k} {n}" for n, k in syms[:12])
                if len(syms) > 12:
                    sym_str += f" (+{len(syms) - 12} more)"
                parts.append(f"{rel}  [{sym_str}]")
            else:
                parts.append(rel)

        n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        header = f"# Project index: {n_sym} symbols across {len(rows)} files\n"
        return header + "\n".join(parts)

    def stats(self) -> str:
        with self._lock:
            return self._stats()

    def _stats(self) -> str:
        conn = self._conn_or_raise()
        n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        n_files = conn.execute("SELECT COUNT(DISTINCT file) FROM symbols").fetchone()[0]
        n_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        return f"{n_sym} symbols, {n_calls} call edges across {n_files} files"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

CODE_INDEX = CodeIndex()
