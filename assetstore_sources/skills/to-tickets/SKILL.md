---
name: to-tickets
description: Break a plan, spec or the current conversation into tracer-bullet tickets, each declaring its blocking edges, published to the configured tracker.
disable-model-invocation: true
argument-hint: Spec path, issue number or URL (optional)
---

# To Tickets

Break a plan, spec or conversation into **tickets**: tracer-bullet vertical slices, each declaring the tickets that **block** it.

Where tickets go and how they are labelled is in `docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`, written by `/setup-engineering-skills`. If they don't exist, tell the user to run it; when they don't want to, use the local-markdown convention below and say so.

## Process

### 1. Gather context

Work from what is already in the conversation. If the user passed a reference (a spec path, an issue number or URL), fetch it and read its full body and comments.

### 2. Explore the codebase (optional)

If you have not explored the codebase yet, do so to understand the current state of the code. Ticket titles and descriptions use the vocabulary of `GLOSSARY.md` and respect the ADRs in the area.

Look for opportunities to prefactor the code to make the implementation easier: "make the change easy, then make the easy change".

### 3. Draft vertical slices

Break the work into **tracer-bullet** tickets.

<vertical-slice-rules>

- Each slice cuts a narrow but COMPLETE path through every layer (schema, API, UI, tests): vertical, NOT a horizontal slice of one layer.
- A completed slice is demoable or verifiable on its own.
- Each slice fits in a single fresh context window.
- Any prefactoring comes first.

</vertical-slice-rules>

Give each ticket its **blocking edges**: the other tickets that must be done before it can start. A ticket with no blockers can start immediately.

**Wide refactors are the exception to vertical slicing.** A **wide refactor** is one mechanical change (rename a column, retype a shared symbol) whose **blast radius** fans across the whole codebase, so a single edit breaks thousands of call sites and no vertical slice can land green. Sequence it as **expand–contract** instead. First expand: add the new form beside the old, so nothing breaks. Then migrate the call sites in batches sized by blast radius (per package, per directory), each batch its own ticket blocked by the expand, keeping CI green batch to batch because the old form still exists. Finally contract: delete the old form once no caller remains, in a ticket blocked by every migrate batch. When even the batches can't stay green alone, keep the sequence but let them share an integration branch that all block a final integrate-and-verify ticket; green is promised only there.

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each ticket:

- **Title**: a short descriptive name;
- **Blocked by**: which other tickets (if any) must be done first;
- **What it delivers**: the end-to-end behaviour this ticket makes work.

Ask the user:

- Does the granularity feel right (too coarse, too fine)?
- Are the blocking edges right: does each ticket depend only on tickets that genuinely gate it?
- Should any tickets be merged or split?

Iterate until the user approves the breakdown.

### 5. Publish the tickets

Publish the approved tickets the way the tracker doc says. The tickets are the same either way; only the shape of the blocking edges changes:

- **Local files** → one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` in dependency order (blockers first), using the local template below: one ticket per file, never a combined file.
- **A real issue tracker (GitHub, GitLab, …)** → one issue per ticket, through its CLI with `run_command` (`gh issue create`, `glab issue create`), in dependency order so each ticket's edges can reference real identifiers. Use the platform's native blocking relationship where it has one; otherwise put a "Blocked by" section in the body. If the source was an existing issue, make each ticket its sub-issue. Apply the `ready-for-agent` label unless told otherwise: the tickets are agent-ready by construction.

The **frontier** is every ticket whose blockers are all done. For a purely linear chain, that is top to bottom.

Do NOT close or modify any parent issue.

<local-ticket-template>

# <NN>: <Ticket title>

**What to build:** the end-to-end behaviour this ticket makes work, from the user's perspective, not a layer-by-layer implementation list.

**Blocked by:** the numbers and titles of the tickets that gate this one, or "None (can start immediately)".

**Status:** ready-for-agent

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2

</local-ticket-template>

<issue-template>

## Parent

A reference to the parent issue (omit when the source was not an existing issue).

## What to build

The end-to-end behaviour this ticket makes work, from the user's perspective.

## Acceptance criteria

- [ ] Criterion 1
- [ ] Criterion 2

## Blocked by

A reference to each blocking ticket, or "None (can start immediately)". Omit this section when the blockers were set as native edges.

</issue-template>

In either form, avoid specific file paths and code snippets: they go stale fast. Exception: a snippet a prototype produced that encodes a decision more precisely than prose (a state machine, a reducer, a schema, a type shape), trimmed to the decision-rich parts and marked as coming from a prototype.

## Next step

After publishing, tell the user the next step is `/implement` (one ticket per chat, or the whole spec in one run). Suggest only skills of this pack (`/which-skill` lists them); no other skill name exists.
