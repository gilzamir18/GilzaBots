---
name: implement
description: Implement a piece of work from a spec or a set of tickets, test-first, closing with a code review.
disable-model-invocation: true
argument-hint: Spec or ticket reference (path, number or URL)
---

Implement the work the user describes in the spec or tickets.

If the user passed a ticket reference, fetch it the way `docs/agents/issue-tracker.md` says (a local `.scratch/...` file with `read_file`; a GitHub or GitLab issue with its CLI through `run_command`) and state its title before starting. If the reference is ambiguous, ask.

## One ticket, or a whole spec

- **One ticket, or a small spec** → build it here, as below.
- **A spec with several tickets** → the tickets are a **task graph** with blocking edges, not a list of steps. Work the **frontier**, every ticket whose blockers are done, one ticket at a time, on one integration branch (create it first with `run_command`). Between tickets, re-read the next ticket instead of relying on what the last one left in your context; when the context grows long, `/compact` at a ticket boundary is the moment. Exploration a ticket needs (the relevant files, external docs) can run ahead in read-only subagents: issue several `run_agent` calls in the same response, one per question, and build from their reports.

## Building a ticket

1. Call `load_skill("tdd")` and build at the seams agreed in the spec or ticket, one red → green slice at a time.
2. Run the type checker and the single test file you are working on regularly, and the full test suite once at the end of the ticket. The commands are usually in `AGENTS.md`, `pyproject.toml`, `package.json` or the CI workflow.
3. Mark the ticket done the way the tracker closes work (on a local tracker, set its `Status:` line to `done`).

## Closing

Once the work is done, call `load_skill("code-review")` and review it against the point the work started from (the integration branch's base, or the commit before the first change). Fix what the review raises.

Then commit the work to the current branch with `run_command`, with a message that names the tickets it resolves.
