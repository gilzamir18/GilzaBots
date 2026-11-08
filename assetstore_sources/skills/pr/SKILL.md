---
name: pr
description: The shape of a pull request body. Use when writing or editing a PR description.
---

Write the PR body with this template:

```markdown
## Summary

<diagram, diff sketch or tree>

## Evidence

- **Before:** <screenshot / output / failing test run>
  **After:** <screenshot / output / passing test run>

## Merge Danger

**Door:** <one-way or two-way>

<optional: description>

**Blast Radius:** <one-word description>

<optional: potential consequences of the merge>
```

Skip preambles and keep the prose brief. Use the domain language of `GLOSSARY.md`.

## Summary

Pick the **smallest view that makes the key point clear**, and put each visual next to the one or two sentences it supports:

- **pseudocode** for logic or an algorithm;
- a **call tree** for runtime control flow;
- a **component tree** for UI structure, with the state and module boundaries that matter;
- a **shallow file tree** for file responsibilities or a broad refactor;
- a **Mermaid diagram** (a sequence or a flowchart) for interaction, control flow or data flow;
- a **`diff`-styled sketch** of any of the above when the point is what changes and the surrounding shape already exists: `+` and `-` lines on the tree, the call stack or the control flow, not on the code;
- the **whole block** only when most of it is new, when omitted context would hide ownership or order, or when the reader needs a copyable target shape.

Keep only the calls, files, props, states and boundaries the reader needs to see this change. One or two visuals usually do it; never all of them.

## Evidence

Concrete evidence that the change works, before and after:

- **Screenshots** are the strongest, when the environment allows them and the change is visual.
- **Execution evidence** is next: test results, console output. Show the exact test that failed before and passes now.

## Merge Danger

Say whether the change is a **one-way or two-way door**. A two-way door can be walked back; a one-way door cannot. A PR that is cheap to roll back is lower risk; destructive actions and hard-to-reverse decisions are one-way doors.

The **blast radius** is the potential scope of the change's impact: layout shifts, breakage for consumers, data, mobile, performance. Consider all of them.

## Publishing

With GitHub, create the PR with `run_command` (`gh pr create --title "..." --body-file <file>`, writing the body to a file first so the shell does not mangle it); on GitLab, `glab mr create`. Show the user the body before publishing it.
