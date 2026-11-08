---
name: domain-modeling
description: Build and sharpen a project's domain model. Use when discussing project terminology, writing or editing GLOSSARY.md, or recording or editing an ADR.
---

# Domain Modeling

Actively build and sharpen the project's domain model while you design. This is the _active_ discipline: challenging terms, inventing edge-case scenarios, and writing the glossary and the decisions down the moment they crystallise. (Merely _reading_ `GLOSSARY.md` for vocabulary is not this skill: any skill does that in one line. This skill is for changing the model, not consuming it.)

## File structure

Most projects have a single context:

```
/
├── GLOSSARY.md
├── docs/
│   └── adr/
│       ├── 0001-event-sourced-orders.md
│       └── 0002-postgres-for-write-model.md
└── src/
```

If a `GLOSSARY-MAP.md` exists at the root, the project has several contexts, and the map points to where each one lives:

```
/
├── GLOSSARY-MAP.md
├── docs/
│   └── adr/                          ← system-wide decisions
├── src/
│   ├── ordering/
│   │   ├── GLOSSARY.md
│   │   └── docs/adr/                 ← context-specific decisions
│   └── billing/
│       ├── GLOSSARY.md
│       └── docs/adr/
```

Create files lazily, only when you have something to write: a `GLOSSARY.md` when the first term is resolved, `docs/adr/` when the first ADR is needed.

## During the session

### Challenge against the glossary

When the user uses a term that conflicts with the existing language in `GLOSSARY.md`, call it out immediately: "Your glossary defines 'cancellation' as X, but you seem to mean Y. Which is it?"

### Sharpen fuzzy language

When the user uses a vague or overloaded term, propose a precise canonical one: "You're saying 'account': do you mean the Customer or the User? Those are different things."

### Discuss concrete scenarios

When domain relationships are being discussed, stress-test them with specific scenarios. Invent scenarios that probe edge cases and force precision about the boundaries between concepts.

### Cross-reference with code

When the user states how something works, check whether the code agrees (`search_code`, `find_symbol`). Surface a contradiction: "Your code cancels entire Orders, but you just said partial cancellation is possible. Which is right?"

### Update GLOSSARY.md inline

When a term is resolved, update `GLOSSARY.md` right there with `edit_file` (or `write_file` for the first term). Capture terms as they happen; don't batch them. Use the format in [GLOSSARY-FORMAT.md](GLOSSARY-FORMAT.md) (in this skill's directory).

`GLOSSARY.md` holds no implementation details. It is not a spec, a scratch pad or a store of implementation decisions: it is a glossary and nothing else.

### Offer ADRs sparingly

Offer an ADR only when all three are true:

1. **Hard to reverse**: changing your mind later has a meaningful cost.
2. **Surprising without context**: a future reader will wonder "why did they do it this way?"
3. **The result of a real trade-off**: there were genuine alternatives and one was picked for specific reasons.

If any of the three is missing, skip the ADR. Use the format in [ADR-FORMAT.md](ADR-FORMAT.md).
