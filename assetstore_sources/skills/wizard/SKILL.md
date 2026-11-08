---
name: wizard
description: Generate an interactive wizard script that walks a human through steps only they can perform. Use when provisioning infrastructure, setting up credentials or CI secrets, walking an unfamiliar third-party dashboard, or running a one-off migration or cutover; not for steps you can perform yourself.
---

# Wizard

A **wizard** is a script that walks a human, step by step, through a manual procedure that is tedious to do by hand and tedious to re-explain to an agent every time. It opens each URL, says exactly what to click and copy, captures the values, writes them where they belong (`.env`, GitHub secrets), confirms at every stage, and shows how many stages are left. It may configure third-party services, run a one-off migration, or move the project from one state to another.

The UX is already solved by [templates/wizard.py](templates/wizard.py) (in this skill's directory): stage-by-stage progress, confirmation gates, cross-platform URL opening (WSL included), hidden secret entry, idempotent `.env` upserts, `gh secret` / `gh variable` writes, and a closing summary. It needs only Python's standard library. **Your job is only to scope the procedure and author its stages.** The library above the `STAGES` marker is identical in every wizard, and that consistency is the point: never hand-edit it.

A wizard is ephemeral by default: built for one run, saved to a scratch path or `scripts/`, deleted when the job is done. Commit it only when the user wants a repeatable setup path in the repo.

## Process

### 1. Scope the procedure

Work out every manual step the human must take and every value captured along the way. Read the repo first; don't ask cold:

- For setup: `.env`, `.env.example`, `.env.*`, the README, `docker-compose*`, framework configuration and `.github/workflows/*` (every `secrets.*` / `vars.*` reference is a value the wizard must produce).
- For a migration or transition: the current state, the target state and the irreversible actions between them.

Show the user the ordered list of stages and the values each produces, and confirm: they may add, drop or reorder.

**Done when** every stage is named in order, and for each captured value you know (a) where the human gets it, (b) where it is written (`.env`, a GitHub secret, both, or nowhere: some stages are pure actions), and (c) whether it is secret (hidden entry) or public.

### 2. Map each stage's journey

For each stage, write the precise path a human follows: which URL to open, what to do there, where a value is shown, which variable it fills ("Dashboard → Developers → API keys → Reveal test key → copy"). Where you don't know the current UI or the exact command, say so and ask the user or check the docs (`web_search`): never invent steps that may not exist.

**Done when** every stage traces to concrete instructions a stranger could follow.

### 3. Author the wizard

Copy `templates/wizard.py` to the target path (read it, then `write_file`). Replace the example stage in `run_wizard` with one `stage(...)` per step, in dependency order, using the library helpers: `stage`, `say` / `step` / `note`, `open_url`, `ask` / `ask_secret`, `write_env`, `set_secret` / `set_var`, `pause` / `confirm`. Set `TOTAL_STAGES` to the number of stages.

Hold the bar the template sets: open the URL before asking for its value, use `ask_secret` for anything secret, `write_env` every persisted value, `set_secret` only the values CI actually needs, and `confirm` before any irreversible action. Each `stage` clears the screen so only the current step is visible: keep a stage to one focused task, so nothing the human needs scrolls away.

### 4. Verify and hand off

- Check it compiles: `run_command('python -m py_compile "<script>"')`.
- Don't run it end to end yourself: it opens browsers and blocks on human input. Trace it statically: every value from step 1 is captured and lands where step 1 said, and every `set_secret` name exactly matches a `secrets.*` reference in CI.
- Offer to start it for the user with `run_interactive_command('python "<script>"')`, which opens the terminal popup where they answer the prompts; or tell them how to run it themselves. If it is a repeatable setup path, commit it and link it from the README, so the next person runs the script instead of asking an agent.
