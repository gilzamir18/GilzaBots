---
name: wayfinder
description: Plan a huge chunk of work (more than one session can hold) as a shared map of decision tickets on the issue tracker, and resolve them one at a time until the way to the destination is clear.
disable-model-invocation: true
argument-hint: A loose idea to chart, or the map to work through
---

A loose idea has arrived, too big for one session and wrapped in fog: the way from here to the **destination** isn't visible yet. Wayfinding finds that way instead of charging at the destination. This skill charts the way as a **shared map** on the project's issue tracker, then works its **decision tickets** (questions whose resolution is a decision, not slices of a build) one at a time until the route is clear.

The destination varies per effort, and naming it is the first act of charting: it shapes every ticket. It may be a spec to hand off and iterate on, a decision to lock before planning starts, or a change made in place such as a data migration. The map is domain-agnostic: engineering work, course content, whatever fits the shape.

## Plan, don't do

Wayfinder **plans** by default: each ticket resolves a decision, and the map is done when the way is clear, with nothing left to decide before someone goes and does the thing. The pull to just do the work usually signals that you have reached the edge of the map and it is time to hand off. An effort can override this in its **Notes**, carrying execution into the map, but absent that, produce decisions, not deliverables.

## Refer by name

Every map and ticket has a **name**: its title. In everything the user reads (narration, the map's Decisions so far), refer to it by name, never by a bare id, number or slug. A wall of `#42, #43, #44` is illegible. The id or path rides inside the name as its link.

## The map

The map is the canonical artifact. Where it and its tickets physically live, and how blocking and frontier queries work, depends on the tracker: follow the **Wayfinding operations** section of `docs/agents/issue-tracker.md` (written by `/setup-engineering-skills`). Without that file, use the local-markdown convention: the map at `.scratch/<effort>/map.md`, one file per ticket at `.scratch/<effort>/issues/NN-<slug>.md`, each with `Type:`, `Status:` and `Blocked by:` lines near the top.

The map is an **index**, not a store. It lists the decisions made and points at the tickets that hold their detail; a decision lives in exactly one place, its ticket, so the map never restates it, only gists it and links.

### The map body

The whole map at low resolution, read once per session. Open tickets are **not** listed in it: they are found by query (or, locally, by listing the issues folder).

```markdown
## Destination

<what reaching the end of this map looks like: the spec, decision or change this effort is finding its way to. One or two lines; every session orients to it before choosing a ticket.>

## Notes

<domain; skills every session should load; standing preferences for this effort>

## Decisions so far

<!-- the index: one line per closed ticket, enough to judge relevance; follow the link for the detail -->

- [<closed ticket title>](link): <one-line gist of the answer>

## Not yet specified

<!-- see "Fog of war": in-scope fog you can't ticket yet -->

## Out of scope

<!-- see "Out of scope": work ruled beyond the destination -->
```

### Tickets

Each ticket is a child of the map; its body is the question, sized to one session:

```markdown
## Question

<the decision or investigation this ticket resolves>
```

Each ticket has a type, `research`, `prototype`, `grilling` or `task` (see [Ticket types](#ticket-types)): a `wayfinder:<type>` label on a real tracker, a `Type:` line locally. These are the only labels a map and its tickets carry, never a triage label like `ready-for-agent`: they are decisions, not implementation work.

A session **claims** a ticket before any work, so concurrent sessions skip it: on a real tracker by assigning it to the user driving the map; locally with `Status: claimed`.

Blocking uses the tracker's native dependency relationship where there is one, because it shows the frontier in the tracker's own UI; locally, a `Blocked by:` line. A ticket is **unblocked** when every ticket blocking it is closed; the **frontier** is the open, unblocked, unclaimed tickets: the edge of the known.

The answer is not part of the body: it is recorded on resolution (see [Work through the map](#work-through-the-map)). Assets created while resolving a ticket are linked from it, not pasted in.

## Ticket types

Every ticket is either **HITL** (human in the loop, worked _with_ the user, who speaks for themselves) or **AFK** (driven by you alone). A HITL ticket resolves only through that live exchange: you never stand in for the user's side of it (a grilling that answers its own questions is broken).

- **Research** (AFK): reading documentation, third-party APIs or local resources to surface a fact a decision waits on. Resolved with `load_skill("research")` (a background subagent). Use when knowledge outside the project is needed.
- **Prototype** (HITL): raise the fidelity of the discussion with a cheap, rough, concrete artifact to react to (an outline, a stub, UI or logic code), with `load_skill("prototype")`. The prototype is linked as an asset. Use when "how should it look" or "how should it behave" is the key question.
- **Grilling** (HITL): conversation. The default. Load both `grilling` and `domain-modeling`.
- **Task** (HITL or AFK): manual work that must happen before a _decision_ can be made: signing up for a service so its API can be judged, provisioning access, moving data so its shape can be seen. The one type that _does_ rather than decides, and it earns its place by unblocking a decision. Do it yourself where you can (AFK); otherwise give the user a precise checklist (HITL). Resolved when the work is done; the answer records what was done and any facts later tickets need (where credentials live, new URLs, row counts).

## Fog of war

The map is _deliberately_ incomplete: don't chart what you can't yet see. Beyond the live tickets lies the **fog of war**: the decisions and investigations you can tell are coming but can't pin down yet, because they hang on open questions. Resolving a ticket clears the fog ahead of it, graduating what is now specifiable into new tickets, until the way to the destination is clear and no tickets remain.

The map's **Not yet specified** section records that dim view: the suspected question, the area to revisit. Everything in it is in scope, just not sharp enough to ticket. Write as loosely or as fully as the view allows.

**Fog or ticket?** The test is whether you can state the question precisely now, _not_ whether you can answer it now.

- **Ticket** when the question is already sharp, even if it is blocked.
- **Not yet specified** when you can't phrase it that sharply yet. Don't pre-slice the fog into ticket-sized pieces: one patch may graduate into several tickets, or none.

**Not yet specified** excludes what is decided (Decisions so far), what is already a live ticket, and what is out of scope.

## Out of scope

Fog only gathers _toward_ the destination. Work beyond the destination is **out of scope**: it gets its own section on the map, for work consciously ruled out of _this_ effort. It never graduates; it returns only if the destination is redrawn, and then as a fresh effort.

When an existing ticket turns out to sit past the destination, **close it** and leave one line in **Out of scope**: the gist, why it is out, and a link to the closed ticket. It stays out of **Decisions so far**, which records the route actually walked.

## Invocation

Two modes. Either way, **never resolve more than one ticket per session**, except research tickets.

### Chart the map

The user arrives with a loose idea.

1. **Name the destination.** Load `grilling` and `domain-modeling` and pin down what the map finds its way to: the spec, decision or change. The destination fixes the scope, so it is settled first.
2. **Map the frontier.** Grill again, **breadth-first**: fan out across the whole space rather than deep on one thread, surfacing the open decisions and the first steps takeable now. **If this surfaces no fog** (the way is already clear, the journey small enough for one session), there is no need for a map: stop and ask the user how they would like to proceed.
3. **Create the map**: Destination and Notes filled in, Decisions so far empty, the fog sketched into **Not yet specified**.
4. **Create the tickets you can specify now**, then wire the blocking edges in a **second pass** (tickets need ids or numbers before they can reference each other), writing cross-references with real ids. Everything you can't specify yet stays in **Not yet specified**.
5. **Start the research.** For each `research` ticket, start a background subagent as the research skill describes, all in the same response so they run in parallel, and record each finding on its ticket (or in a linked notes file) when the reports come back.
6. Stop: charting is one session's work, and it resolves nothing by hand.

### Work through the map

The user arrives with a map (path, number or URL), and optionally a ticket. Without one, you pick the next decision.

1. Read the **map**: the low-resolution view, not every ticket body.
2. Choose the ticket: the one the user named, otherwise the first frontier ticket. **Claim it** before any work.
3. Resolve it as its type says (see [Ticket types](#ticket-types)): read the type, since the body never states it. Read any related or closed ticket in full when needed, and load the skills the map's **Notes** name. When in doubt, load `grilling` and `domain-modeling`.
4. Record the resolution: post the answer as a resolution comment (locally, under an `## Answer` heading in the ticket, with `Status: resolved`), close the ticket, and append a pointer to the map's **Decisions so far**.
5. Add the newly surfaced tickets (create, then wire); graduate any fog the answer made specifiable, clearing each graduated patch from **Not yet specified**. If the answer shows that a ticket sits beyond the destination, rule it out of scope instead of resolving it. If the decision invalidates other parts of the map, update or delete those tickets.

The user may run unblocked tickets in parallel, so expect other sessions to edit the tracker at the same time.
