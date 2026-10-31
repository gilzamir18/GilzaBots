---
name: elementary-ops
description: Answers questions about text files by composing deterministic elementary operations — address a section, environment or line range; extract labels, citations, references, numbers, headings or matching lines; diff two files or passages; compare lists — and checks the answer with logical conditions (all/any/not, subset, empty, counts). Use to compare two files, two versions or two passages of LaTeX, Markdown or plain text, or to verify that an edit kept its citations, labels, references or numbers, instead of reading and comparing by eye.
model: worker
role: helper
---

# Elementary Ops Skill

You answer a question about text files without comparing anything by eye. You
write a short **plan** of elementary operations, a script executes it
deterministically, and `check` lines turn the results into PASS or FAIL. Your
job is to choose the operations and to read the verdict; the script does the
reading, counting and comparing, and its evidence is what your answer rests on.

## When this skill does not apply

The operations address, extract, diff and compare. A request that cannot be
reduced to them — judging whether an argument is convincing, rewriting prose,
translating — is not answered by a plan. Do not force one: report
`VERDICT: NOT APPLICABLE` with one sentence saying why, and stop.

## Input

From the context: the file(s) or passages, and the question. A named file you
cannot find (check with `get_project_overview` or `search_code`) → report
`STATUS: BLOCKED` naming it; never guess a path. A question you cannot turn into
a checkable one → `ask_question` **once**.

## Protocol

1. **Question.** Rewrite the request as one verifiable question with a yes/no or
   list answer, e.g. *"Does every citation of Method in v1 still appear in v2?"*
2. **Plan.** Choose operations **only** from the catalog below, at most 12 steps,
   results named `s1`, `s2`, ... End with at least one `check` that states the
   answer.
3. **Write** the plan with `write_file` to `.opalatex/ops/<short-name>.plan`
   inside the project.
4. **Run** it:
   `run_python_script("<skill_dir>/scripts/ops.py", 'run ".opalatex/ops/<short-name>.plan"')`.
   `run` validates the whole plan before executing anything. A `PLAN ERROR`
   names the line and what to change: fix that line and run again, **at most 2
   corrections**, then report `STATUS: BLOCKED` with the last error.
5. **Read** the `CHECKS` and `VERDICT` lines. When you need detail, page one
   result with `show` (the report prints the exact command) or call
   `diff_files` with the arguments printed after `DETAIL:`. Judge meaning only
   on these small outputs, one at a time — never read two whole passages to
   compare them yourself.
6. **Verify.** If the task asked you to fix what a check found, fix it, then run
   the same plan again. The final answer must rest on a PASS/FAIL line from
   that last run, not on your impression of the edit.
7. **Report** in the format below.

Exit codes of `run`: `0` every check passed, `1` a check failed (a valid
answer, not an error), `2` the plan is invalid or a step could not run.

## Plan syntax

One step per line, `NAME = OPERATION ARGS`. Blank lines and lines starting with
`#` are ignored. Quote text with spaces; write regular expressions in **single
quotes** so backslashes stay literal (`'\\cite\{'`). Paths are relative to the
project directory.

| Operation | Arguments | Result | What it gives |
|---|---|---|---|
| `file` | path | segment | the whole file |
| `lines` | path A-B | segment | lines A to B (`B` may be `end`) |
| `section` | path "Title" | segment | a LaTeX/Markdown section up to the next heading of the same or higher level; case and accents ignored; the title must equal one heading or be contained in exactly one |
| `env` | path NAME [N] | segment | the N-th top-level `\begin{NAME}`…`\end{NAME}` (N = 1 by default) |
| `between` | path 'START' 'END' | segment | first line matching START through the next line matching END |
| `labels` / `refs` / `cites` | seg | items | keys of `\label`, of `\ref`-like commands, of `\cite`-like commands (one item per key; LaTeX comments skipped) |
| `numbers` | seg | items | numeric tokens (`12`, `3.5`, `40%`) outside citation/label keys |
| `headings` | seg | items | titles of the headings inside the segment |
| `grep` | seg 'REGEX' | items | matching lines, stripped |
| `nonblank` | seg | items | every non-blank line, stripped |
| `words` | seg | number | word count, LaTeX commands and comments excluded |
| `count` | any | number | lines of a segment, items of a list, changed lines of a diff |
| `unique` | set | items | items without repeats |
| `diff` | seg seg | diff | line diff with the original line numbers of both sides |
| `common` / `only-a` / `only-b` | set set | items | distinct items in both / only in the first / only in the second |

`seg` is a segment result; `set` is an items result or a segment (which counts
as its non-blank lines). `python "<skill_dir>/scripts/ops.py" catalog` prints
this table.

Conditions for `check`:

| Condition | True when |
|---|---|
| `empty X` / `nonempty X` | X has no items, lines or changes / has some |
| `equal X Y` | same lines or items in the same order |
| `sameset X Y` | same distinct items, order and repeats ignored |
| `subset X Y` | every distinct item of X is in Y |
| `contains X 'TEXT'` | some line or item of X contains TEXT |
| `X OP N` | `count X` compared with N (an integer or another result); OP is `==` `!=` `<` `<=` `>` `>=` |
| `all(P, Q, ...)` / `any(P, ...)` / `not(P)` | logical combination |

Each check prints its evidence — the missing items, the first difference, the
counts — so a FAIL already says what to look at.

## Examples

Did the revision of Method keep its citations and change little?

```plan
s1 = section paper_v1.tex "Method"
s2 = section paper_v2.tex "Method"
s3 = cites s1
s4 = cites s2
s5 = diff s1 s2
check subset s3 s4
check s5 <= 40
```

Is every `\ref` in the document backed by a `\label`?

```plan
s1 = file main.tex
s2 = refs s1
s3 = labels s1
s4 = only-a s2 s3
check empty s4
```

Did a rewrite of the abstract keep every number?

```plan
s1 = env draft.tex abstract
s2 = env final.tex abstract
s3 = numbers s1
s4 = numbers s2
check sameset s3 s4
```

Are two passages of the same file identical apart from spacing, and does the
second still mention the dataset?

```plan
s1 = lines notes.md 10-40
s2 = lines notes.md 120-150
s3 = nonblank s1
s4 = nonblank s2
check all(equal s3 s4, contains s2 'dataset')
```

## Report (normal text, ends every run)

```
ELEMENTARY OPS REPORT
QUESTION: <the verifiable question>
PLAN: <plan path>
RESULTS:
- <the step lines that matter, as printed>
CHECKS:
- <PASS|FAIL  condition  [evidence], as printed>
VERDICT: <PASS | FAIL | NOT APPLICABLE>
ANSWER: <one or two sentences that follow from the checks>
STATUS: <DONE | BLOCKED: reason>
```

Never report a check you did not run, and never state a result the script did
not print.
