---
name: grilling
description: Relentless interview that stress-tests a plan, decision or idea until every branch of its design tree is resolved. Use when the user wants their thinking stress-tested or says "grill".
---

Interview the user relentlessly until you reach a shared understanding. Map it as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you have not heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then end your turn and wait for the user's answers before the next round.

Format a round like this:

```
❓ **Q1** - **<question title>**: <question body, possibly several paragraphs, possibly with choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body>

➡️ <your recommended answer>
```

Word each question so "yes" accepts your recommended answer.

Each round of answers reshapes the tree: settled decisions push the frontier outward and unblock the questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (the files, the history, the docs, the web), look it up instead of asking. For a lookup that would take long reading, start a read-only subagent with `run_agent(task=..., description=..., background=true)` and keep going: a running lookup is an unsettled prerequisite, so only the questions downstream of it wait. Ask the rest of the frontier now, and collect the report with `wait_agents` before the round that needs it. The _decisions_ are the user's: put each one to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing silently assumed. Do not act on the result until the user confirms you have reached a shared understanding.
