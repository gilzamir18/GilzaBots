---
name: code-review
description: "Review the changes since a fixed point (commit, branch, tag or merge-base) on two axes, Standards (the project's documented coding standards) and Spec (what the originating ticket or spec asked for), as two parallel subagents. Use when the user wants a branch, a PR or work-in-progress reviewed, or asks to \"review since X\"."
---

Two-axis review of the diff between `HEAD` and a fixed point:

- **Standards**: does the code follow this project's documented coding standards?
- **Spec**: does the code faithfully implement the originating ticket or spec?

Both axes run as **parallel subagents** (`run_agent`), each in a fresh context so neither pollutes the other, and this skill aggregates their reports.

Where tickets live is in `docs/agents/issue-tracker.md` (written by `/setup-engineering-skills`). If it does not exist, treat specs as files in the repo, and say so.

## Process

### 1. Pin the fixed point

The fixed point is whatever the user named: a commit SHA, a branch, a tag, `main`, `HEAD~5`. If they named none, ask.

Confirm it resolves and the diff is non-empty before going further, with `run_command`: `git rev-parse <fixed-point>`, then `git diff --stat <fixed-point>...HEAD` (three dots: against the merge-base). A bad ref or an empty diff fails here, not inside two subagents.

Save the change where the subagents can read it, since their tools read files but run no commands: `git diff <fixed-point>...HEAD > .gilzabots/review/change.diff` and `git log <fixed-point>..HEAD --oneline > .gilzabots/review/commits.txt` (create the folder first). Uncommitted work belongs to the review only if the user says so; then diff against the working tree instead (`git diff <fixed-point>`).

### 2. Identify the spec source

Look for the originating spec, in this order:

1. Ticket references in the commit messages (`#123`, `Closes #45`, a `.scratch/<feature>/issues/NN-*.md` path), fetched the way the tracker doc says.
2. A path the user passed as an argument.
3. A spec file under `docs/`, `specs/` or `.scratch/` matching the branch name or the feature.
4. Otherwise ask the user where the spec is. If there is none, the **Spec** axis is skipped and reported as "no spec available".

### 3. Identify the standards sources

Find every file that documents how code should be written in this project (`search_code`, `get_project_overview`): `CODING_STANDARDS.md` and `CONTRIBUTING.md` must be on the list when they exist, along with `AGENTS.md` and any style guide under `docs/`.

On top of whatever the project documents, the Standards axis always carries the **smell baseline** below: a fixed set of Fowler code smells (_Refactoring_, chapter 3) that applies even when a project documents nothing. Two rules bind it:

- **The project overrides.** A documented project standard always wins; where it endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature Envy"), never a hard violation. Skip anything tooling already enforces.

Each smell reads _what it is_ → _how to fix it_:

- **Mysterious Name**: a function, variable or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design is murky.
- **Duplicated Code**: the same logic shape in more than one hunk or file of the change. → extract the shared shape and call it from both.
- **Feature Envy**: a method that reaches into another object's data more than its own. → move it onto the data it envies.
- **Data Clumps**: the same few fields or parameters keep travelling together (a type wanting to be born). → bundle them into one type and pass that.
- **Primitive Obsession**: a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches**: the same `switch` / `if` cascade on the same type recurs across the change. → replace it with polymorphism, or one map both sites share.
- **Shotgun Surgery**: one logical change forces scattered edits across many files of the diff. → gather what changes together into one module.
- **Divergent Change**: one file or module edited for several unrelated reasons. → split it so each module changes for one reason.
- **Speculative Generality**: abstraction, parameters or hooks for needs the spec doesn't have. → delete it; inline it back until a real need shows.
- **Message Chains**: long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man**: a class or function that mostly delegates onward. → cut it and call the real target directly.
- **Refused Bequest**: a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance; use composition.

### 4. Start both subagents in parallel

Issue both `run_agent` calls **in the same response**, with `model="main"`, and wait for both reports. A subagent sees only its `task`, so each task is self-contained:

**Standards task**:

- the absolute paths of `change.diff` and `commits.txt`, with the instruction to read them with `read_file`, and the touched files themselves where the hunks need context;
- the list of standards files from step 3, **plus the smell baseline from step 3 pasted in full**;
- the brief: "Report, per file or hunk where relevant, (a) every place the diff violates a documented standard: cite the standard (file and rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls: documented-standard breaches can be hard, baseline smells are always judgement calls, and a documented standard overrides the baseline. Skip anything tooling enforces. Under 400 words."

**Spec task**:

- the same two paths and the same reading instruction;
- the path of the spec, or its fetched contents pasted in;
- the brief: "Report (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff nobody asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."

If there is no spec, start only the Standards subagent and note the skip in the report.

### 5. Aggregate

Present the two reports under `## Standards` and `## Spec`, verbatim or lightly cleaned. Do **not** merge or re-rank the findings: the two axes are separate on purpose.

End with a one-line summary: the number of findings per axis and the worst issue _within each axis_. Don't pick a single winner across axes: that is the re-ranking the separation exists to prevent. Delete `.gilzabots/review/` afterwards.

## Why two axes

A change can pass one axis and fail the other:

- Code that follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Code that does exactly what the ticket asked but breaks the project's conventions → **Spec pass, Standards fail.**

Reporting them separately stops one axis from masking the other.
