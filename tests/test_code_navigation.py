"""find_symbol and code_outline, and the code index behind them."""
import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from gilzabots import code_index
from gilzabots.code_index import CodeIndex, _extract


def _names(symbols):
    return [(s.kind, s.name, s.line) for s in symbols]


# ── Extraction ───────────────────────────────────────────────────────────────

def test_python_definitions_and_calls_come_from_ast():
    source = "class Store:\n    def load(self, name):\n        return fetch(name)\n\ndef helper():\n    return Store().load('x')\n"
    symbols = _extract(source, ".py", "store.py")
    assert _names(symbols) == [("class", "Store", 1), ("function", "helper", 5), ("function", "load", 2)]
    assert "load" in next(s for s in symbols if s.name == "helper").calls


def test_javascript_methods_and_arrow_functions_are_found_by_pattern():
    source = (
        "export class Store {\n"
        "  async load(name) {\n"
        "    if (name) {\n"
        "      return fetchIt(name);\n"
        "    }\n"
        "  }\n"
        "}\n"
        "export const arrow = (z) => z * 2;\n"
        "export function helper(x) {\n"
    )
    names = _names(_extract(source, ".tsx", "a.tsx"))
    assert ("class", "Store", 1) in names
    assert ("method", "load", 2) in names
    assert ("function", "arrow", 8) in names
    assert ("function", "helper", 9) in names
    assert not any(name == "if" for _, name, _ in names)


def test_latex_outline_skips_comments():
    source = (
        "\\newcommand{\\R}{\\mathbb{R}}\n"
        "\\newcommand\\vect[1]{\\mathbf{#1}}\n"
        "% \\section{Commented out}\n"
        "\\section{Introdução}\\label{sec:intro}\n"
        "\\subsection*{Trabalhos relacionados}\n"
        "\\begin{frame}[t]{Atenção}\n"
        "\\input{capitulos/metodo}\n"
        "Custo de 50\\% % \\label{not:this}\n"
    )
    assert _names(_extract(source, ".tex", "main.tex")) == [
        ("macro", "\\R", 1),
        ("macro", "\\vect", 2),
        ("section", "Introdução", 4),
        ("label", "sec:intro", 4),
        ("subsection", "Trabalhos relacionados", 5),
        ("frame", "Atenção", 6),
        ("input", "capitulos/metodo", 7),
    ]


def test_bibtex_entries_are_their_citation_keys():
    source = "@article{vaswani2017,\n  title={Attention}\n}\n@comment{ignored,\n@String{acm = {ACM}}\n"
    assert _names(_extract(source, ".bib", "refs.bib")) == [("bibentry", "vaswani2017", 1)]


def test_tree_sitter_stays_off():
    # It crashed the process on a deep tree, and get_parser downloads grammars.
    assert code_index._get_ts_parser("javascript") is None


# ── The index ────────────────────────────────────────────────────────────────

def test_build_is_incremental_prunes_heavy_dirs_and_forgets_deleted_files(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("function hidden() {}\n", encoding="utf-8")
    (tmp_path / "big.py").write_text("def huge():\n    pass\n" + "#" * 200, encoding="utf-8")
    monkeypatch.setattr(code_index, "_MAX_FILE_BYTES", 100)

    index = CodeIndex()
    index.set_project(str(tmp_path))
    try:
        assert index.build()["indexed"] == 1
        assert [s.name for s in index.search("alpha")] == ["alpha"]
        assert index.search("hidden") == [] and index.search("huge") == []
        assert index.build() == {"indexed": 0, "skipped": 1, "removed": 0, "total": 1}

        (tmp_path / "a.py").unlink()
        assert index.build()["removed"] == 1
        assert index.search("alpha") == []
    finally:
        index.close()


def test_the_index_answers_from_a_worker_thread(tmp_path):
    # Agent tools run through asyncio.to_thread; sqlite refused a connection
    # made in another thread.
    (tmp_path / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
    index = CodeIndex()
    index.set_project(str(tmp_path))
    try:
        index.build()
        found = []
        worker = threading.Thread(target=lambda: found.extend(index.search("alpha")))
        worker.start()
        worker.join()
        assert [s.name for s in found] == ["alpha"]
    finally:
        index.close()


# ── The tools ────────────────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path):
    from gilzabots.code_index import CODE_INDEX
    from gilzabots.tools import set_project_context

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "config.py").write_text(
        "def load_config(path):\n    return parse(path)\n\n\ndef parse(path):\n    return {}\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("from pkg.config import load_config\n\ndef main():\n    load_config('x')\n", encoding="utf-8")
    (tmp_path / "web.js").write_text("export function render(x) {\n  return x;\n}\n", encoding="utf-8")
    (tmp_path / "main.tex").write_text("\\newcommand{\\R}{\\mathbb{R}}\n\\section{Método}\\label{sec:metodo}\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain\n", encoding="utf-8")
    set_project_context(SimpleNamespace(project_path=str(tmp_path)))
    yield tmp_path
    CODE_INDEX.close()


def _call(tool, *args):
    return asyncio.run((getattr(tool, "_func", None) or tool)(*args))


def test_find_symbol_returns_the_definition_and_its_python_callers(project):
    from gilzabots.tools import find_symbol

    out = _call(find_symbol, "load_config")
    assert f"function load_config — {os.path.join('pkg', 'config.py')}:1" in out
    assert "def load_config(path)" in out
    assert "function main — app.py:3" in out


def test_find_symbol_does_not_claim_no_callers_outside_python(project):
    from gilzabots.tools import find_symbol

    out = _call(find_symbol, "render")
    assert "web.js:1" in out
    assert "Callers are tracked for Python only" in out
    assert "No indexed" not in out


def test_find_symbol_finds_latex_labels_and_macros_without_the_backslash(project):
    from gilzabots.tools import find_symbol

    assert "label sec:metodo — main.tex:2" in _call(find_symbol, "sec:metodo")
    assert "macro \\R — main.tex:1" in _call(find_symbol, "R")


def test_find_symbol_lists_similar_names_or_points_at_search_code(project):
    from gilzabots.tools import find_symbol

    assert "Similar names: load_config" in _call(find_symbol, "load_conf")
    assert "search_code" in _call(find_symbol, "does_not_exist_anywhere")


def test_code_outline_of_a_file_and_of_the_project(project):
    from gilzabots.tools import code_outline

    out = _call(code_outline, "pkg/config.py")
    assert "1  function    load_config" in out
    assert "5  function    parse" in out

    tree = _call(code_outline, ".")
    assert "app.py: main:3" in tree
    assert "main.tex: \\R:1, Método:2" in tree
    assert "notes.txt" not in tree

    assert "not indexed" in _call(code_outline, "notes.txt")


def test_code_outline_refuses_paths_outside_the_project(project):
    from gilzabots.tools import code_outline

    with pytest.raises(ValueError) as error:
        _call(code_outline, str(project.parent))
    assert "outside the project" in str(error.value)


def test_navigation_tools_reach_helpers_and_orchestrators():
    from gilzabots.tools import code_outline, find_symbol, get_helper_tools, get_code_navigation_tools

    assert get_code_navigation_tools() == [find_symbol, code_outline]
    worker_tools = get_helper_tools()
    assert find_symbol in worker_tools and code_outline in worker_tools
