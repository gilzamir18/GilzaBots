---
name: setup-engineering-skills
description: "Configure this project for the engineering skills: its issue tracker, triage label vocabulary and domain doc layout. Run once before first using the other engineering skills."
disable-model-invocation: true
---

# Setup Engineering Skills

Scaffold the per-project configuration the engineering skills (`to-spec`, `to-tickets`, `implement`, `triage`, `wayfinder`, `code-review`) assume:

- **Issue tracker**: where tickets live (GitHub, GitLab, local Markdown, or anything else the user describes);
- **Triage labels**: the strings used for the five canonical triage roles;
- **Domain docs**: where `GLOSSARY.md` and the ADRs live, and the rules for reading them.

This is a conversation, not a script: explore, present what you found, confirm with the user, then write.

## Process

### 1. Explore

Look at the project's starting state. Read what exists; don't assume:

- `git remote -v` (through `run_command`): is this a GitHub or GitLab repo? Which one?
- `AGENTS.md` at the project root: does it exist, and does it already have an `## Agent skills` section? GilzaBots puts this file in the main agent's prompt every turn, so it is where the configuration's pointers go.
- `GLOSSARY.md` and `GLOSSARY-MAP.md` at the root, and the older names `CONTEXT.md` / `CONTEXT-MAP.md`. If an older name exists, offer to rename it (`git mv`) to its `GLOSSARY` name.
- `docs/adr/` and any `src/*/docs/adr/`.
- `docs/agents/`: does this skill's earlier output already exist?
- `.scratch/`: a sign that a local-Markdown tracker is already in use.
- Is the `triage` skill installed? (`/skills` lists them.) That decides whether Section B runs at all.
- Monorepo signals: `pnpm-workspace.yaml`, a `workspaces` field in `package.json`, several packages each with its own `src/`. Their absence means single-context, which is almost every project.

### 2. Present the findings and ask

Summarise what is present and what is missing. Then take the sections in order: one section, one answer, then the next.

Lead each section with the recommended answer, so the user can accept it in a word. Give a one-line explainer only when the choice genuinely branches; skip a section when exploration already settled it (Section B when `triage` isn't installed, Section C when there is no monorepo). `ask_question` with the options suits these.

**Section A: Issue tracker.**

> The issue tracker is where tickets live for this project. `to-tickets`, `triage` and `to-spec` read from it and write to it, so they need to know whether to call `gh issue create`, write a Markdown file under `.scratch/`, or follow some other workflow.

If `git remote` points at GitHub, propose GitHub; if at GitLab (`gitlab.com` or a self-hosted host), propose GitLab. Otherwise, or if the user prefers, offer:

- **GitHub**: the repo's GitHub Issues, through the `gh` CLI;
- **GitLab**: the repo's GitLab Issues, through the `glab` CLI;
- **Local Markdown**: files under `.scratch/<feature>/` in this project (good for solo projects or repos without a remote);
- **Other** (Jira, Linear, …): ask the user to describe the workflow in one paragraph, and record it as prose.

The choice is recorded in `docs/agents/issue-tracker.md`. The GitHub and GitLab templates carry a "PRs as a request surface" flag, off by default: leave it off and don't raise it; the user can flip it in the file later.

A GitHub or GitLab tracker needs a reachable repo. When the user picks one and no remote points at that host, get the repo URL from them and wire it up (`git init` if needed, `git remote add origin <url>`). Done when `gh repo view` / `glab repo view` prints the project from this directory; if it can't, stop and tell the user what is missing.

**Section B: Triage label vocabulary.** Skip it when the `triage` skill isn't installed.

Otherwise ask exactly one question:

> Do you want to keep the default triage labels? (recommended: **yes**)

The defaults are the five canonical roles, each label equal to its name: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. On **yes**, write them as they are. Only on **no** (usually because the tracker already uses other names, such as `bug:triage`) collect the overrides, so `triage` applies the existing labels instead of creating duplicates.

**Section C: Domain docs.** Default to **single-context** (one `GLOSSARY.md` and `docs/adr/` at the root): it fits almost every project, so write it without asking. Offer **multi-context** (a root `GLOSSARY-MAP.md` pointing to one `GLOSSARY.md` per context) only when exploration found monorepo signals, and confirm the layout.

### 3. Confirm the drafts

Show the user a draft of:

- the `## Agent skills` block for `AGENTS.md` (below);
- `docs/agents/issue-tracker.md`, `docs/agents/domain.md`, and `docs/agents/triage-labels.md` (the last only when `triage` is installed).

Let them edit before anything is written.

### 4. Write

Edit `AGENTS.md` at the project root, creating it when it doesn't exist. If an `## Agent skills` section already exists, update it in place rather than appending a duplicate, and leave the user's other sections untouched. The block:

```markdown
## Agent skills

### Issue tracker

[one-line summary of where tickets are tracked]. See `docs/agents/issue-tracker.md`.

### Triage labels

[one-line summary of the label vocabulary]. See `docs/agents/triage-labels.md`.

### Domain docs

[one-line summary of the layout: "single-context" or "multi-context"]. See `docs/agents/domain.md`.
```

Include `### Triage labels`, and write `docs/agents/triage-labels.md`, only when `triage` is installed and Section B ran. When Section B ran on GitHub or GitLab, create each configured label the tracker lacks (`gh label create` / `glab label create`).

Write the docs from the seed templates in this skill's directory (read them with `read_file`):

- [issue-tracker-github.md](issue-tracker-github.md): GitHub;
- [issue-tracker-gitlab.md](issue-tracker-gitlab.md): GitLab;
- [issue-tracker-local.md](issue-tracker-local.md): local Markdown;
- [triage-labels.md](triage-labels.md): the label mapping (only when `triage` is installed);
- [domain.md](domain.md): the domain-doc reading rules and layout.

For an "other" tracker, write `docs/agents/issue-tracker.md` from the user's description.

### 5. Done

Tell the user the setup is complete and which engineering skills now read these files. They can edit `docs/agents/*.md` directly later; running this skill again is only needed to switch trackers or start over.
