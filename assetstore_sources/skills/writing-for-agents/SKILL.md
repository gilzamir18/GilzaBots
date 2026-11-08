---
name: writing-for-agents
description: Reference for writing documents agents consume. Use when creating or editing a skill (SKILL.md) or AGENTS.md, or any doc an agent is pointed at.
---

Reference for writing any document an agent consumes: a skill, the project's `AGENTS.md` (GilzaBots puts it in the main agent's prompt every turn), a doc reached by a pointer. The packaging differs; the writing does not. The same levers make each one predictable, because the agent takes the same _process_ every run rather than producing the same output.

When the document is a skill, also read [SKILL-MECHANICS.md](SKILL-MECHANICS.md) (in this skill's directory) for frontmatter, the invocation choice and router skills.

## Context pointers

A **context pointer** is a reference held in the agent's context that names some out-of-context material and encodes the condition for reaching it. A skill's description is one; a line in `AGENTS.md` naming a doc is the same object. The pointer's _wording_, not its target, decides when the agent reaches the material, and how reliably. A must-have target behind a weakly worded pointer is a variance bug: sharpen the wording first, and inline the material only if sharpening fails.

A pointer does two jobs: say what the material is, and list the **branches** that should trigger reaching it (a branch is a distinct case the document handles, so different runs take different paths through it). Every word of an always-loaded pointer costs on every turn, so it earns harder pruning than the body:

- **Front-load the leading word**: the pointer is where it does its triggering work.
- **One trigger per branch.** Synonyms that rename one branch are that branch written twice: collapse them.
- **Cut identity the body already carries.**

## The two loads

Every document and pointer spends one of two budgets:

- **Context load**: the cost of always-loaded material on the agent's window (an `AGENTS.md` line, a skill description), spending tokens and attention whether or not it fires.
- **Cognitive load**: the cost on the human, who has to know which documents exist and when to reach for each. The human is the index. It is the price of human agency: spend it where human judgement matters, remove it where it does not.

Material reached only through a pointer escapes context load at the price of the pointer's line; material with no pointer at all rides entirely on cognitive load.

## Information hierarchy

A document is built from **steps** (ordered actions the agent performs) and **reference** (definitions, rules, facts consulted on demand). They mix freely. The core decision is where each piece sits on the **information hierarchy**, ranked by how immediately the agent needs it:

1. **In-file step**: what the agent does, in order.
2. **In-file reference**: consulted on demand. Often a legitimately flat peer set (every rule of a review on one rung).
3. **Disclosed reference**: pushed into a separate file and reached by a context pointer, loaded only when the pointer fires.

Push too little down and the top bloats; push too much and you hide material the agent needs. That tension is the decision.

**Progressive disclosure** is the move down the ladder, so the top stays legible. Branching is the cleanest test: inline what every branch needs, disclose what only some branches reach. Reference that should be disclosed buries the steps around it and turns attending to them into a coin flip.

**Co-location** decides what sits beside a piece once it is placed: keep a concept's definition, rules and caveats under one heading. The document should read like documentation written for the agent.

**Sprawl** is the failure mode: a document simply too long, even when every line is live. The cure is the ladder: disclose reference behind pointers, and split by branch or sequence so each path carries only what it needs.

## Steps and completion criteria

Every step ends on a **completion criterion**, the condition that tells the agent the step is done. Two properties make it a lever:

- **Clarity**: can the agent tell done from not done? A vague bound ("understanding reached") invites **premature completion**, attention slipping to _being done_. Sharpen the bound first; only if it is irreducibly fuzzy _and_ you observe the rush, hide the later steps by splitting the sequence across a real context boundary (a handoff or a subagent; an inline load leaves the later steps in context).
- **Demand**: how much it requires. "Every modified model accounted for" forces thorough work where "produce a change list" does not. Demand drives **legwork**, and it binds flat reference as well as steps.

The strongest criteria are both checkable and exhaustive.

## When to split

Splitting one document into two spends one of the two loads, so split only when the cut earns it:

- **By sequence**: split a run of steps where the later steps tempt the agent to rush the one in front of it.
- **By invocation**, skill-specific: see [SKILL-MECHANICS.md](SKILL-MECHANICS.md).

## Leading words

A **leading word** is a compact concept already in the model's pretraining that the agent thinks with while running the document (_lesson_, _fog of war_, _tracer bullet_). Repeated as a token, never as a sentence, it anchors a whole region of behaviour in the fewest tokens. A coined word recruits no priors, so reach for an existing word first.

It anchors twice: in the body (the agent reaches for the same behaviour each time the word appears) and in a pointer (when the same word lives in your prompts, docs and code, the agent reaches the material more reliably).

Hunt for passages that collapse into one: "fast, deterministic, low-overhead" → _tight_; "a loop you believe in" → _red_ (the loop goes red on the bug, or it doesn't).

**Negation** is the failure beside this lever: a prohibition drags the forbidden behaviour into context and makes it more available. State the target behaviour ("write one-line comments") so the banned one is never spoken. Keep a prohibition only as a hard guardrail you cannot phrase positively, and pair it with the positive target.

## Pruning

- Keep each meaning in a **single source of truth**, so changing behaviour is a one-place edit. Duplication costs maintenance and tokens, and inflates a meaning's prominence.
- The **environment** is a source of truth too (scripts in `package.json` or `pyproject.toml`, config files, the directory layout, `--help`). A document that restates it is a **cache**, worth its load only when the lookup is expensive. Cache what looking cannot find: the unwritten convention, the reason behind a choice, the gotcha no config confesses.
- Check every line for **relevance**. Without a pruning discipline the default is **sediment**: stale layers that settle because adding feels safe and removing feels risky.
- Hunt **no-ops** sentence by sentence: an instruction the model already follows by default pays load to say nothing. The test is model-relative; settle disagreements by running the document. Delete the whole sentence, and when a leading word is too weak to beat the default (_be thorough_), use a stronger word (_relentless_).
