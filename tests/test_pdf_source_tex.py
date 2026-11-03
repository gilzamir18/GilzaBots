from gilzabots.latex_compiler import find_pdf_source_tex


ROOT_DOCUMENT = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"


def _write(tmp_path, rel_path, content="%PDF-1.4"):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_finds_root_document_with_same_stem(tmp_path):
    _write(tmp_path, "papers/report.tex", ROOT_DOCUMENT)
    _write(tmp_path, "papers/report.pdf")

    assert find_pdf_source_tex(str(tmp_path), "papers/report.pdf") == "papers/report.tex"


def test_extension_match_is_case_insensitive(tmp_path):
    _write(tmp_path, "main.tex", ROOT_DOCUMENT)
    _write(tmp_path, "main.PDF")

    assert find_pdf_source_tex(str(tmp_path), "main.PDF") == "main.tex"


def test_pdf_without_source_has_none(tmp_path):
    _write(tmp_path, "downloaded-paper.pdf")

    assert find_pdf_source_tex(str(tmp_path), "downloaded-paper.pdf") == ""


def test_fragment_is_not_a_source(tmp_path):
    # An included chapter cannot be compiled on its own, so it does not
    # produce the PDF next to it.
    _write(tmp_path, "chapter.tex", "\\section{Intro}\nText\n")
    _write(tmp_path, "chapter.pdf")

    assert find_pdf_source_tex(str(tmp_path), "chapter.pdf") == ""


def test_source_in_another_folder_is_not_matched(tmp_path):
    _write(tmp_path, "src/main.tex", ROOT_DOCUMENT)
    _write(tmp_path, "out/main.pdf")

    assert find_pdf_source_tex(str(tmp_path), "out/main.pdf") == ""


def test_paths_outside_the_project_are_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write(tmp_path, "outside.tex", ROOT_DOCUMENT)
    _write(tmp_path, "outside.pdf")

    assert find_pdf_source_tex(str(project), "../outside.pdf") == ""


def test_non_pdf_and_missing_arguments(tmp_path):
    _write(tmp_path, "main.tex", ROOT_DOCUMENT)

    assert find_pdf_source_tex(str(tmp_path), "main.tex") == ""
    assert find_pdf_source_tex(str(tmp_path), "") == ""
    assert find_pdf_source_tex("", "main.pdf") == ""
