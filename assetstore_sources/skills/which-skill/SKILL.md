---
name: which-skill
description: Ask which skill or flow of the engineering pack fits your situation. A router over the pack's skills.
disable-model-invocation: true
argument-hint: What are you trying to do?
---

# Which skill?

Nobody remembers every skill, so ask.

Before stating what a skill does, or recommending that a step be skipped, read that skill's `SKILL.md` (installed skills live under the project's `.gilzabots/skills/<name>/`; `/skills` lists them): the summaries here are for orientation only. If a skill you would recommend is not installed, say so: the user installs it from the Asset Store.

A **flow** is a path through the skills. Most work travels one **main flow**, and a few **on-ramps** merge onto it. Everything else is standalone, or a vocabulary layer underneath.

## The main flow: idea → ship

1. **`/grill-with-docs`** sharpens the idea by interview, recording what it learns in `GLOSSARY.md` and ADRs. Start here whenever there is a project to leave that paper trail in. (`/grill-me` is the same interview without the files.)
2. **Can every question be settled in conversation?** If one needs a runnable answer (state, business logic, a UI you have to see), detour through **`prototype`** (the agent reaches for it, or ask for a prototype), then bring the verdict back into the thread. A prototype that grows its own directory is the moment for **`/handoff`** in each direction.
3. **Is this a multi-session build?**
   - **Yes** → **`/to-spec`** turns the thread into a spec, then **`/to-tickets`** splits it into tracer-bullet tickets, each declaring its **blocking edges**. Then **`/implement`**: one ticket per chat, starting a new chat between tickets (each ticket is self-contained, so the last one's context is disposable), or the whole spec in one run, where `/implement` works the ticket frontier one ticket at a time on one integration branch.
   - **No** → **`/implement`** right here, in the same chat.

   Either way the code is built by driving **`tdd`** (one red → green slice at a time) and closes with **`code-review`**, a two-axis review (Standards and Spec) run as two parallel subagents. Both are model-invoked: `/implement` loads them, and you can run either on its own (`/tdd`, `/code-review` against a fixed point). When the work goes up as a pull request, **`pr`** shapes its body.
4. **`/retro`** closes the loop: after a build, especially one that went sideways, it suggests changes to the agent's **environment** (navigation pointers, automated checks, the coding standards `code-review` enforces, `AGENTS.md`, tooling), not to the code.

### Context hygiene

Keep steps 1–3 in **one unbroken chat** (don't compact or start a new chat until after `/to-tickets`), so the grilling, the spec and the tickets build on the same thinking. Each `/implement` can start fresh from its ticket. Run `/retro` in the chat it looks back on.

The limit is the model's **smart zone**: the part of its context window within which it still reasons sharply (the context indicator in the composer shows how full the window is). If a chat approaches it before `/to-tickets`, don't push on degraded: `/compact` at the nearest phase boundary and carry on.

## On-ramps

- **Bugs and requests piling up** → **`/triage`** moves issues through triage roles and produces agent-ready briefs that `/implement` later picks up. Triage only what arrived raw: tickets `/to-tickets` produced are already agent-ready.
- **Something's broken** → **`diagnosing-bugs`** (model-invoked: describe the bug, or run `/diagnosing-bugs`). For the hard ones: the bug that resists a first look, the intermittent flake, the regression between two known-good states. It refuses to theorise until it has a **tight feedback loop** that goes red on *this* bug, then fixes it with a regression test. Afterwards, `/retro` asks what would have prevented it; when the real finding is that no good seam exists to lock it down, that is a job for `/improve-codebase-architecture`.
- **A huge, foggy effort, too big for one session** → **`/wayfinder`** charts a shared map of **decision tickets** and resolves them one at a time, producing decisions, not deliverables, until the way is clear. Then it merges onto the main flow at `/to-spec`. Save it for exactly that: `/grill-with-docs` handles any idea one session can hold.

## Codebase health

- **`/improve-codebase-architecture`** surveys the codebase for **deepening opportunities** and grills through the one you pick; picking one generates an idea to take into the main flow at `/grill-with-docs`. Run it whenever there's a spare moment.

## Vocabulary underneath

Two model-invoked references beneath the other skills, each the single source of its vocabulary:

- **`domain-modeling`**: sharpen the project's *domain* language, resolve an overloaded word, record a hard-to-reverse decision as an ADR.
- **`codebase-design`**: the deep-module vocabulary (module, interface, depth, seam, adapter, leverage, locality) for designing a module's *shape*. `tdd` and `/improve-codebase-architecture` both speak it.

## Phase boundaries

A **phase** is a chunk of work inside a chat: the grilling, the implementation, the QA. At the **boundary** between two of them there are five options:

- **Continue** in this chat: costs nothing, loses nothing.
- **A new chat** (`/chat new`, or New Chat in the window): empty context, when nothing here matters to what comes next. The old chat stays where it was.
- **`/handoff`**: a portable Markdown file, only for another tool, another directory, a colleague, or a side task forked mid-phase.
- **A subagent**: send a tightly scoped, read-only task to its own context and get a report back (the agent starts it with `run_agent`).
- **`/compact`**: summarise this chat's context and keep going on the summary. The default, at the bottom of the tree.

[PHASE-BOUNDARIES.md](PHASE-BOUNDARIES.md) (in this skill's directory) has the ordered decision tree and the reasoning behind each branch. Decide **at** a boundary; mid-phase, continue, or split the rest into subagents.

## Standalone

- **`/grill-me`**: the same interview as `/grill-with-docs`, saving nothing. For a plan, a design or a piece of writing with no project to record into.
- **`grilling`**: the interview routine itself (rounds, the frontier, facts are the agent's job and decisions the user's), which `/grill-me`, `/grill-with-docs`, `/triage`, `/wayfinder` and `/improve-codebase-architecture` all run.
- **`prototype`**: a small throwaway program that answers one design question (does this state model feel right? what should this UI look like?). The answer folds into the real code; the prototype is kept on a `prototype/<name>` branch.
- **`research`**: hand reading legwork to a background subagent that investigates a question against primary sources; the findings land in a cited Markdown file in the project. Material to take into `/grill-with-docs`.
- **`/to-questionnaire`**: when the answer is in **someone else's** head, it writes them a questionnaire to fill in. It interviews you about the **send** (who, what you need back), not the subject.
- **`wizard`**: for steps only a **human** can take (provisioning, credentials, CI secrets, an unfamiliar dashboard, a one-off cutover): an interactive script that opens each URL, captures each value and writes it to `.env` and GitHub secrets.
- **`/wait-what`**: the corrective for a message that didn't land: the agent re-pitches it in plain words with the glossary's vocabulary.
- **`/teach`**: learn a concept over several sessions, with the project as a stateful teaching workspace.
- **`writing-for-agents`**: the reference for writing documents agents consume: skills, `AGENTS.md`, pointed-at docs.

## Precondition

**`/setup-engineering-skills`**: run it once, before the first engineering flow, to configure the issue tracker, the triage labels and the domain-doc layout the other skills assume.
