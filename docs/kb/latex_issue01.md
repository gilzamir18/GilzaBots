# KB-01: LaTeX Compilation of Independent Files in Subdirectories

**Date:** 2026-06-30  
**Status:** Resolved  
**Affected components:** `gilzabots/latex_compiler.py`, `gilzabots/ide_server.py`

---

## Problem Description

When a user opened and compiled a LaTeX file that was:
1. **Independent** (contained `\documentclass`) but **not** the designated project `main_file`, AND
2. Located inside a **subdirectory** of the project root (e.g., `AulasUVA/RaciocinioProbabilisticoAula02/Aula02.tex`)

The following incorrect behaviors occurred:

- **First compile attempt:** The IDE showed the PDF of the project's `main_file` instead of the opened file's own PDF.
- **Second compile attempt:** Tectonic ran and wrote the PDF, but the IDE reported failure:
  ```
  error: primary input not available (?!)
  error: failed to open input file "Aula02.tex"
  Error: PDF file was not generated despite 0 exit code.
  ```

---

## Root Cause Analysis

Three separate bugs were found, each responsible for one stage of the failure.

### Bug 1 — `determine_main_file_for_compilation` returned only the basename

**File:** `gilzabots/latex_compiler.py`

The function was returning `os.path.basename(file_path)` (e.g., `"Aula02.tex"`) instead of the
path relative to `project_dir`. This basename was later joined with `project_dir`:

```python
# Before (wrong)
abs_main_file = os.path.join(project_dir, "Aula02.tex")
# → G:\...\AulasUVA\Aula02.tex   ← file does not exist here
```

**Fix:** Use `os.path.relpath()` to return a path relative to `project_dir`:

```python
file_rel_path = os.path.relpath(file_path, project_dir).replace('\\', '/')
# → "RaciocinioProbabilisticoAula02/Aula02.tex"

abs_main_file = os.path.join(project_dir, file_rel_path)
# → G:\...\AulasUVA\RaciocinioProbabilisticoAula02\Aula02.tex   ← correct
```

### Bug 2 — `pdf_path` was computed at the project root instead of alongside the `.tex` file

**File:** `gilzabots/latex_compiler.py`, function `compile_latex`

After Tectonic ran, the code verified the PDF existence at the wrong path:

```python
# Before (wrong)
pdf_path = os.path.join(project_dir, f"{base_no_ext}.pdf")
# → G:\...\AulasUVA\Aula02.pdf   ← does not exist
```

Tectonic **always writes the PDF into the same directory as the input `.tex` file**, so
the actual PDF was at `AulasUVA\RaciocinioProbabilisticoAula02\Aula02.pdf`.

**Fix:**

```python
# After (correct)
pdf_path = os.path.join(os.path.dirname(abs_main_file), f"{base_no_ext}.pdf")
# → G:\...\AulasUVA\RaciocinioProbabilisticoAula02\Aula02.pdf   ← correct
```

### Bug 3 — New (unsaved) files were not written to disk before compilation

**File:** `gilzabots/latex_compiler.py`, function `compile_latex`

The original guard prevented saving a file that did not yet exist on disk:

```python
# Before (wrong)
if file_path and os.path.exists(file_path):
    with open(file_path, "w") as f: ...
```

For a brand-new file created only in the editor, Tectonic could not find it.

**Fix:** Always save and create directories as needed:

```python
# After (correct)
if file_path:
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(tex_content)
```

---

## Key Concepts Formalized

| Concept | Definition |
|---|---|
| **Independent file** | A `.tex` file that contains `\documentclass` (excluding commented lines). Compiles standalone. |
| **Dependent file** | A `.tex` file without `\documentclass`. It is a fragment included by another file via `\input`, `\include`, or `\subfile`. |
| **Included in main** | A dependent file reachable (directly or recursively) from the project `main_file`. Compiling it triggers compilation of `main_file`. |
| **Independent non-main** | An independent file that is not the project `main_file`. Compiling it produces its **own PDF** in its own directory. |

---

## Files Changed

- [`gilzabots/latex_compiler.py`](file:///c:/Users/gilza/projetos/GilzaBots/gilzabots/latex_compiler.py)
  - Added `is_independent()`, `get_includes()`, `is_recursively_included()`, `determine_main_file_for_compilation()`
  - Fixed `pdf_path` computation in `compile_latex()` to use `os.path.dirname(abs_main_file)`
  - Fixed file save guard in `compile_latex()` to write new files to disk before compilation

- [`gilzabots/ide_server.py`](file:///c:/Users/gilza/projetos/GilzaBots/gilzabots/ide_server.py)
  - Replaced `guess_main_file` in `/api/latex/compile`, `/api/latex/check-pdf`, and `/api/latex/synctex`
    with `determine_main_file_for_compilation`
