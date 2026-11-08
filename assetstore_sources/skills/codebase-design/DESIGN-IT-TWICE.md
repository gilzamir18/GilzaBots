# Design It Twice

When the user wants to explore alternative interfaces for a chosen deepening candidate, use this parallel-subagent pattern. Based on "Design It Twice" (Ousterhout): your first idea is unlikely to be the best.

Uses the vocabulary in [SKILL.md](SKILL.md): **module**, **interface**, **seam**, **adapter**, **leverage**.

## Process

### 1. Frame the problem space

Before starting any subagent, write a user-facing explanation of the problem space for the chosen candidate:

- the constraints any new interface would need to satisfy;
- the dependencies it would rely on, and which category each falls into (see [DEEPENING.md](DEEPENING.md));
- a rough illustrative code sketch to ground the constraints: not a proposal, just a way to make them concrete.

Show this to the user, then go straight to step 2 in the same turn: the user reads and thinks while the subagents work.

### 2. Start the subagents

Issue three or more `run_agent` calls **in the same response**, so they run at the same time. Each subagent starts with a fresh context and sees only its `task`, so each task is a complete technical brief: the file paths, the coupling details, the dependency category, what sits behind the seam, and the vocabulary of [SKILL.md](SKILL.md) plus the relevant `GLOSSARY.md` terms, so every design names things the project's way. Use `model="main"`: this is judgement-heavy work.

Give each subagent a different design constraint, and ask for a **radically different** interface:

- Agent 1: "Minimise the interface: one to three entry points at most. Maximise leverage per entry point."
- Agent 2: "Maximise flexibility: support many use cases and extension."
- Agent 3: "Optimise for the most common caller: make the default case trivial."
- Agent 4 (if applicable): "Design around ports and adapters for the cross-seam dependencies."

Each subagent reports:

1. The interface (types, methods, parameters, plus invariants, ordering and error modes).
2. A usage example showing how callers use it.
3. What the implementation hides behind the seam.
4. The dependency strategy and adapters (see [DEEPENING.md](DEEPENING.md)).
5. Trade-offs: where leverage is high, where it is thin.

### 3. Present and compare

Present the designs one after another so the user can absorb each, then compare them in prose by **depth** (leverage at the interface), **locality** (where change concentrates) and **seam placement**.

Then give your own recommendation: the strongest design and why. If elements of different designs combine well, propose a hybrid. Be opinionated: the user wants a strong read, not a menu.
