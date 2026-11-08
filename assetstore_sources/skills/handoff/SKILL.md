---
name: handoff
description: Compact the current conversation into a handoff document for another agent or session to pick up.
disable-model-invocation: true
argument-hint: What will the next session be used for?
---

Write a handoff document that summarises the current conversation so a fresh agent can continue the work.

Save it to the operating system's temporary directory, not the project: `%TEMP%` on Windows, `$TMPDIR` (else `/tmp`) elsewhere; read the variable with `run_command` when you don't know its value. Write `handoff-<topic>-<yyyymmdd-hhmm>.md` there with `write_file` and give the user the absolute path; a new chat or another agent opens it with `read_file`.

Include a **Suggested skills** section naming the skills the next agent should load (with `load_skill`) or the user should run (with `/name`).

Do not duplicate what other artifacts already hold (specs, plans, ADRs, tickets, commits, diffs): point to them by path or URL.

Redact sensitive information: API keys, passwords, tokens, personal data. Write `<REDACTED>` in their place.

If the user passed arguments, they describe what the next session will focus on: tailor the document to that.
