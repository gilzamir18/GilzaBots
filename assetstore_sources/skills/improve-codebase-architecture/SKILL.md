---
name: improve-codebase-architecture
description: Scan a codebase for deepening opportunities, present them as a visual HTML report, then grill through whichever one you pick.
disable-model-invocation: true
argument-hint: Which module, subsystem or pain point? (optional)
---

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities**: refactors that turn shallow modules into deep ones. The aim is testability and agent-navigability.

This skill is informed by the project's domain model and built on a shared design vocabulary:

- Call `load_skill("codebase-design")` for the architecture vocabulary (**module**, **interface**, **depth**, **seam**, **adapter**, **leverage**, **locality**) and its principles (the deletion test, "the interface is the test surface", "one adapter = hypothetical seam, two = real"). Use these terms exactly in every suggestion; don't drift into "component", "service", "API" or "boundary".
- The domain language in `GLOSSARY.md` names good seams; the ADRs in `docs/adr/` record decisions not to re-litigate.

## Process

### 1. Explore

**Scope before you scan: YAGNI.** Deepening a module pays off by making future changes to it easier, so weigh the recently changed parts of the codebase. Decide _where_ to look before you look:

- If the user named a direction (a module, a subsystem, a pain point), take it.
- Otherwise read a good stretch of history (`inspect_git("log")`) for the hot spots: the files and areas that keep coming up, and look there first. If the changes are scattered with no clear hot spot, widen the net.

Read `GLOSSARY.md` and the ADRs in the area first.

Then walk the codebase with read-only subagents, so the exploration's reading stays out of your context: one `run_agent` per area (or per question below), issued in the same response so they run together. Each task names its area, pastes the questions below and the codebase-design vocabulary, and asks for findings with file:line. Explore organically rather than by rigid heuristics, noting where there is friction:

- Where does understanding one concept require bouncing between many small modules?
- Where are modules **shallow**, with an interface nearly as complex as the implementation?
- Where have pure functions been extracted just for testability, while the real bugs hide in how they are called (no **locality**)?
- Where do tightly coupled modules leak across their seams?
- Which parts are untested, or hard to test through their current interface?

Apply the **deletion test** to anything that looks shallow: would deleting it concentrate complexity, or just move it? "Yes, concentrates" is the signal you want.

### 2. Present the candidates as an HTML report

Write one self-contained HTML file outside the project, so nothing lands in the repo: the OS temporary directory (`%TEMP%` on Windows, `$TMPDIR` or `/tmp` elsewhere), named `architecture-review-<timestamp>.html` so each run gets a fresh file. Open it with `open_file` (the IDE previews HTML) or the system opener through `run_command`, and give the user the absolute path.

The report uses **Tailwind from a CDN** for layout and **Mermaid from a CDN** for graph-shaped diagrams, mixed with hand-built CSS/SVG visuals for the editorial ones. Each candidate gets a **before/after visualisation**. Be visual.

For each candidate, a card with:

- **Files**: the files and modules involved;
- **Problem**: why the current architecture causes friction;
- **Solution**: what would change, in plain English;
- **Benefits**: in terms of locality and leverage, and how the tests would improve;
- **Before / After diagram**: side by side, illustrating the shallowness and the deepening;
- **Recommendation strength**: `Strong`, `Worth exploring` or `Speculative`, as a badge.

End with a **Top recommendation** section: the candidate you would tackle first, and why.

**Domain words come from GLOSSARY.md; architecture words from codebase-design.** If the glossary defines "Order", write "the Order intake module", not "the FooBarHandler" and not "the Order service".

**ADR conflicts**: surface a candidate that contradicts an ADR only when the friction is real enough to reopen the decision, and mark it in the card (_"contradicts ADR-0007, but worth reopening because…"_). Don't list every theoretical refactor an ADR forbids.

[HTML-REPORT.md](HTML-REPORT.md) (in this skill's directory) has the full scaffold, the diagram patterns and the style guidance: read it before writing the report.

Do not propose interfaces yet. Once the file is written, ask the user: "Which of these would you like to explore?"

### 3. Grilling loop

When the user picks a candidate, call `load_skill("grilling")` and walk the decision tree with them: constraints, dependencies, the shape of the deepened module, what sits behind the seam, which tests survive.

Side effects happen inline as decisions crystallise; call `load_skill("domain-modeling")` to keep the domain model current:

- **Naming a deepened module after a concept not in `GLOSSARY.md`?** Add the term (create the file lazily).
- **Sharpening a fuzzy term in conversation?** Update `GLOSSARY.md` right there.
- **The user rejects the candidate for a load-bearing reason?** Offer an ADR: _"Want me to record this as an ADR so future architecture reviews don't re-suggest it?"_ Only when a future explorer would need the reason; skip ephemeral ("not worth it right now") and self-evident ones.
- **Exploring alternative interfaces for the deepened module?** Follow DESIGN-IT-TWICE.md in the codebase-design skill's directory: parallel subagents, each designing a radically different interface.
