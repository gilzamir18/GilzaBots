#!/usr/bin/env python3
"""Turn the TikZ/PGFPlots pictures in a .tex file into PNG images.

Why this is a script and not an instruction in the skill body: every step below
is a place where a model improvising shell commands gets it wrong, and each
mistake produces a *plausible* failure that costs a round trip to diagnose.

  * The picture must be compiled with the **preamble it was written for**.
    A `tikzpicture` using `\\usetikzlibrary{arrows.meta}` or a `\\definecolor`
    from the deck's preamble compiles to a cryptic error without them, and a
    picture using a `\\newcommand` from the same preamble compiles to the wrong
    drawing. This harvests them from the source rather than guessing.
  * `standalone` with a border is what crops the page to the drawing. Compiling
    the picture inside an article class yields an A4 page with a small figure
    in the corner, which then converts to a mostly-empty PNG.
  * The engine is the one GilzaBots ships (`bin/tectonic`), found the same way
    the IDE finds it, so a picture compiles here exactly as it does in the
    editor.
  * The raster step prefers PyMuPDF, which is a hard dependency of GilzaBots and
    therefore always present, over `pdftoppm`, which is not.

Usage:

    python3 tikz_to_image.py --tex slides.tex --list
    python3 tikz_to_image.py --tex slides.tex --out figures --dpi 300
    python3 tikz_to_image.py --tex slides.tex --index 2 --out figures
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Preamble lines a picture may depend on. Harvested in source order, so a
# \definecolor that uses a package loaded above it still resolves.
PREAMBLE_PATTERNS = (
    r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\}",
    r"\\usetikzlibrary\{[^}]*\}",
    r"\\usepgfplotslibrary\{[^}]*\}",
    r"\\pgfplotsset\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\definecolor\{[^}]*\}\{[^}]*\}\{[^}]*\}",
    r"\\newcommand\*?\{?\\[A-Za-z@]+\}?(?:\[\d+\])?(?:\[[^\]]*\])?\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\renewcommand\*?\{?\\[A-Za-z@]+\}?(?:\[\d+\])?\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\tikzset\{(?:[^{}]|\{[^{}]*\})*\}",
    r"\\pgfdeclare[a-zA-Z]*\{(?:[^{}]|\{[^{}]*\})*\}",
)

PICTURE_ENVIRONMENTS = ("tikzpicture", "pgfpicture")

# Packages a standalone picture needs even when the source did not name them,
# because beamer loads them itself and a picture written under beamer may rely
# on that.
IMPLIED_PACKAGES = ("tikz", "pgfplots", "amsmath", "amssymb")


def read_text(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"cannot read {path} as text")


def strip_comments(tex: str) -> str:
    """Drop LaTeX comments, keeping escaped percent signs."""
    return re.sub(r"(?<!\\)%.*", "", tex)


def preamble_of(tex: str) -> str:
    """Everything before \\begin{document}, or the whole file when there is none
    (a fragment \\input from elsewhere has no document environment)."""
    index = tex.find(r"\begin{document}")
    return tex if index == -1 else tex[:index]


def harvest_preamble(tex: str) -> list[str]:
    """The preamble lines a picture from this source may depend on."""
    source = strip_comments(preamble_of(tex))
    found: list[tuple[int, str]] = []
    for pattern in PREAMBLE_PATTERNS:
        for match in re.finditer(pattern, source):
            found.append((match.start(), match.group(0)))
    # Source order, de-duplicated: a package loaded twice is harmless, but a
    # \newcommand defined twice is a fatal "already defined".
    seen: set[str] = set()
    out: list[str] = []
    for _, line in sorted(found):
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def find_pictures(tex: str) -> list[str]:
    """Every TikZ/PGF picture in `tex`, outermost first, comments stripped.

    Nesting is handled by counting, because a `tikzpicture` may contain another
    one (a node holding a small inset diagram) and a non-greedy regex would cut
    the outer picture short at the inner picture's \\end.
    """
    source = strip_comments(tex)
    pictures: list[str] = []
    for env in PICTURE_ENVIRONMENTS:
        begin = re.compile(r"\\begin\{" + env + r"\}")
        end = re.compile(r"\\end\{" + env + r"\}")
        position = 0
        while True:
            start = begin.search(source, position)
            if not start:
                break
            depth = 1
            cursor = start.end()
            while depth:
                next_begin = begin.search(source, cursor)
                next_end = end.search(source, cursor)
                if not next_end:
                    return pictures        # unterminated: nothing usable after it
                if next_begin and next_begin.start() < next_end.start():
                    depth += 1
                    cursor = next_begin.end()
                else:
                    depth -= 1
                    cursor = next_end.end()
            pictures.append(source[start.start():cursor])
            position = cursor
    return pictures


def standalone_document(picture: str, preamble: list[str], *, border: int = 4) -> str:
    """The picture as its own croppable document."""
    lines = list(preamble)
    loaded = " ".join(lines)
    for package in IMPLIED_PACKAGES:
        if not re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b" + package + r"\b", loaded):
            lines.insert(0, f"\\usepackage{{{package}}}")
    body = "\n".join(lines)
    return (
        f"\\documentclass[border={border}pt]{{standalone}}\n"
        f"{body}\n"
        "\\begin{document}\n"
        f"{picture}\n"
        "\\end{document}\n"
    )


# ─── toolchain ───────────────────────────────────────────────────────────────

def find_engine() -> list[str] | None:
    """The LaTeX engine to compile with, as an argv prefix.

    Tectonic first and by the same lookup the IDE uses (`gilzabots/latex_compiler
    .py: get_tectonic_path`): GilzaBots ships it in `bin/`, so it is present even
    where the machine has no TeX installation at all.
    """
    exe = "tectonic.exe" if sys.platform == "win32" else "tectonic"
    # Walk up from scripts/ to the installation root. The skill sits at
    # <install>/skills/tex-to-jpt/scripts/, so the bundled binary at
    # <install>/bin/ is three levels up — and a couple more are searched
    # because a skill may also live inside a project's own skills directory.
    root = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(root, "bin", exe)
        if os.path.isfile(candidate):
            return [candidate]
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    for name in ("tectonic", "latexmk", "pdflatex", "xelatex", "lualatex"):
        path = shutil.which(name)
        if path:
            return [path]
    return None


def engine_args(engine: list[str], tex_path: str, out_dir: str) -> list[str]:
    name = os.path.basename(engine[0]).lower()
    if name.startswith("tectonic"):
        return [*engine, "--outdir", out_dir, "--keep-logs", tex_path]
    if name.startswith("latexmk"):
        return [*engine, "-pdf", "-interaction=nonstopmode", f"-outdir={out_dir}", tex_path]
    return [*engine, "-interaction=nonstopmode", "-halt-on-error",
            f"-output-directory={out_dir}", tex_path]


def compile_to_pdf(document: str, work_dir: str) -> str:
    """Compile `document` and return the path of the PDF it produced."""
    engine = find_engine()
    if not engine:
        raise SystemExit(
            "no LaTeX engine found. GilzaBots ships tectonic in bin/; run this "
            "script from inside the installation, or install tectonic/pdflatex."
        )
    tex_path = os.path.join(work_dir, "picture.tex")
    with open(tex_path, "w", encoding="utf-8") as handle:
        handle.write(document)

    result = subprocess.run(
        engine_args(engine, tex_path, work_dir),
        cwd=work_dir, capture_output=True, text=True, timeout=180,
    )
    pdf_path = os.path.join(work_dir, "picture.pdf")
    if not os.path.exists(pdf_path):
        # The compiler's own words, not a summary of them: the fix is almost
        # always a missing package or macro named in that log.
        log = ""
        log_path = os.path.join(work_dir, "picture.log")
        if os.path.exists(log_path):
            log = read_text(log_path)
        detail = _first_error(log) or (result.stderr or result.stdout).strip()
        raise SystemExit(f"the picture did not compile:\n{detail[:1500]}")
    return pdf_path


def _first_error(log: str) -> str:
    """The first TeX error and the lines under it, which is what names the
    missing package or macro."""
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("!"):
            return "\n".join(lines[index:index + 12])
    return ""


def pdf_to_png(pdf_path: str, png_path: str, dpi: int = 300) -> str:
    """Raster the first page of `pdf_path` into `png_path`."""
    try:
        import pymupdf                                    # noqa: PLC0415
    except ImportError:
        try:
            import fitz as pymupdf                        # noqa: PLC0415
        except ImportError:
            pymupdf = None
    if pymupdf is not None:
        with pymupdf.open(pdf_path) as document:
            document[0].get_pixmap(dpi=dpi).save(png_path)
        return png_path

    tool = shutil.which("pdftoppm")
    if not tool:
        raise SystemExit("neither PyMuPDF nor pdftoppm is available to raster the PDF")
    stem = os.path.splitext(png_path)[0]
    subprocess.run(
        [tool, "-png", "-r", str(dpi), "-singlefile", pdf_path, stem],
        check=True, capture_output=True, timeout=120,
    )
    return png_path


def convert(picture: str, preamble: list[str], png_path: str, *, dpi: int = 300,
            border: int = 4) -> str:
    document = standalone_document(picture, preamble, border=border)
    with tempfile.TemporaryDirectory() as work_dir:
        pdf_path = compile_to_pdf(document, work_dir)
        os.makedirs(os.path.dirname(os.path.abspath(png_path)) or ".", exist_ok=True)
        return pdf_to_png(pdf_path, png_path, dpi=dpi)


# ─── cli ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tex", required=True, help="the .tex file to scan")
    parser.add_argument("--out", default="figures", help="directory for the PNGs")
    parser.add_argument("--name", default="", help="base name (default: the .tex stem)")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--border", type=int, default=4, help="pt of whitespace around the drawing")
    parser.add_argument("--index", type=int, default=None,
                        help="convert only this picture (1-based, as --list numbers them)")
    parser.add_argument("--list", action="store_true", help="list the pictures and exit")
    args = parser.parse_args(argv)

    tex = read_text(args.tex)
    pictures = find_pictures(tex)
    if not pictures:
        print("no tikzpicture or pgfpicture found")
        return 0

    if args.list:
        for number, picture in enumerate(pictures, start=1):
            first = next((line.strip() for line in picture.splitlines()[1:] if line.strip()), "")
            print(f"{number}: {len(picture)} chars — {first[:70]}")
        return 0

    preamble = harvest_preamble(tex)
    stem = args.name or os.path.splitext(os.path.basename(args.tex))[0]
    chosen = ([(args.index, pictures[args.index - 1])]
              if args.index else list(enumerate(pictures, start=1)))
    if args.index and not 1 <= args.index <= len(pictures):
        raise SystemExit(f"--index {args.index} out of range: {len(pictures)} picture(s)")

    for number, picture in chosen:
        png_path = os.path.join(args.out, f"{stem}-tikz-{number}.png")
        convert(picture, preamble, png_path, dpi=args.dpi, border=args.border)
        print(png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
