---
name: retro
description: Conduct a retrospective on a coding session and suggest improvements to the agent's environment, most severe first.
disable-model-invocation: true
argument-hint: "Which session? (default: this chat)"
---

The user has asked for a **retrospective**. You are suggesting improvements to the coding agent's **environment**, so future runs go better.

## Steps

1. Call `load_skill("writing-for-agents")` for the writing guide.

2. Read the primary sources for the session the user names. By default that is this chat: what you did, the tool calls that failed, the corrections the user made. For an earlier chat, use `search_conversation_history`, or the handoff document if the user points you to one.

3. Look for candidates in these categories:

   - **Navigation**: how easy was it to find the right files? Are there hidden dependencies between files? Would a **navigation pointer** help? _Use when_ the session took long to find a piece of information.
   - **Automated checks**: could an automated check (lint, types, tests, a filesystem linter) catch an error the agent made? Read the project's own check commands first (`package.json` / `pyproject.toml` scripts, the CI workflow), so a check that exists but sits unwired or silently broken is the finding, not a reinvention. A project with no **guardrail** (no pre-commit hook and no CI job running its lint/type/test command) is itself a finding. _Use when_ the agent made a mistake a check could have caught, or the project has no guardrail.
   - **Coding standards**: should the **reviewer agent** (the `code-review` skill) enforce a new rule, or should a rule be removed or clarified? Classify the violation first: a **mechanical** one (a fixed syntactic pattern, a banned API, an import shape, a file-location rule) gets a deterministic check (a custom lint rule, a pre-commit hook or a CI job, whichever is cheapest here). Default to building the check over writing the rule. Keep `CODING_STANDARDS.md` for genuine **judgement calls** that no check could replace. _Use when_ the review failed to catch a mistake.
   - **AGENTS.md**: are there steering instructions that belong in the coding standards or in automated checks instead? GilzaBots puts `AGENTS.md` in the prompt of every turn, so its size is paid on every turn. _Use when_ the file is large.
   - **Tool economy**: did the agent make expensive tool calls that could be streamlined (whole-file reads where a search would do, reading that a subagent could have done)? Is any custom tooling (a CLI, an MCP connector) particularly token-inefficient? _Use when_ the agent made an expensive call.
   - **No-ops**: instructions in steering files that don't change the agent's behaviour. _Use when_ the steering files are large and unwieldy.
   - **Information access**: chances to give the agent more information: teeing dev-server logs to a file it can read, read-only access to third-party services, a connector. _Use when_ a crucial piece of information was out of the agent's reach.

4. Present the candidates to the user in order of severity, each with the concrete change you propose (the file and the text, or the check to add). Make a change only once the user picks it.

## Reference

### Implementation vs review

All work goes through two stages: implementation and review. The implementing agent carries the most **context pressure**: it explores, writes code and debugs failures. The reviewing agent carries the least: it receives a diff, so it needs no exploration, and rarely writes code. So the reviewer imposes the coding standards, not the implementer.

### Files

- `AGENTS.md`: in the context of every agent working in this project. Use it very sparingly, usually only for **navigation pointers** to other files.
- `CODING_STANDARDS.md`: read during review, not implementation. Add navigation pointers to a docs folder once it passes about 1,000 lines.
- Docs: reference files, pointed to by other files. Look for existing docs before writing new ones.
- Skills: for reference the agent should reach on its own (the description sits in its context) or for commands the user runs with `/name`. Follow the `writing-for-agents` guide.
