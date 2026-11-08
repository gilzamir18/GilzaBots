# Skill mechanics

The skill-specific branch of [writing-for-agents](SKILL.md): what changes when the document is a GilzaBots skill. Everything else about writing it is in `SKILL.md`.

## The file

A skill is a directory with a `SKILL.md`. Its frontmatter is YAML (quote a value that contains `: `):

```yaml
---
name: my-skill                      # the directory name if absent
description: What it is, then when to reach for it.
disable-model-invocation: true      # optional: only the user runs it, as /my-skill
argument-hint: What should it work on?   # optional: shown after /my-skill
---
```

GilzaBots adds `role: helper` (the skill runs as a helper agent through `run_skill` instead of being loaded by the main agent), `model` (which model a helper runs on) and `extends` (a parent skill whose body comes first). Files the body links to (`FORMAT.md`) sit in the skill's directory; `load_skill` and `/name` both report that directory, and the agent reads them there. `.py` files under `scripts/` are listed with their absolute paths.

`SKILL.light.md`, when present, replaces the body for models whose prompt profile is `light`. The frontmatter always comes from `SKILL.md`.

## Invocation

Two choices, trading the two loads:

- A **model-invoked** skill (the default) is listed in the main agent's catalog with its description, so the agent reaches it on its own with `load_skill`, and other skills can load it. The user can still type `/name`. The description is a context pointer that stays loaded every turn of every chat in the project: permanent context load in exchange for discoverability. A model-invoked skill made only of reference is the home for reference several skills share. Write a model-facing description carrying the trigger branches (the pointer rules in `SKILL.md` apply in full).
- A **user-invoked** skill (`disable-model-invocation: true`) is left out of the catalog: only the user typing `/name` reaches it, and `load_skill` refuses it. Zero context load, but cognitive load: the user is the index that must remember it exists. Its `description` becomes human-facing: it is what the `/` menu shows, a one-line summary with no trigger list.

Pick model invocation only when the agent must reach the skill on its own, or another skill must. If it only ever fires by hand, make it user-invoked and pay no context load.

Reference that two user-invoked skills both need can live in neither, since neither can load the other. Put it in a model-invoked reference skill, or in a plain file both point at.

## Dependencies

A skill that loads another with `load_skill` (as `grill-me` loads `grilling`) works only where that skill is installed and active. A skill packaged for the Asset Store lists them as `requires` in its `.metadata`, and the store installs and activates them together.

## Splitting by invocation

Split off a model-invoked skill when a distinct leading word should trigger it on its own (a word you actually use in your prompts), or when another skill must load it. You pay context load for the new description, so that independent reach has to be worth it.

## Router skills

When user-invoked skills multiply past what anyone remembers, a **router skill** cures the piled-up cognitive load: one user-invoked skill that names the others and when to reach for each, so the user remembers one name instead of many. It can only point at them: the user runs the one it recommends.
