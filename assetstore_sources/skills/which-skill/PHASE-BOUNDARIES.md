# Phase boundaries

A **phase** is a chunk of work inside a chat: the grilling, the implementation, the QA. The definition is fuzzy on purpose: a phase ends when you think *"ok, we're done with that"*.

The **phase boundary** is the gap between two phases, and it is the only place this decision belongs. Mid-phase there is no decision to make: continue, or split the work that's left into subagents. Compacting mid-phase makes the agent lose the thread.

## The five options

| Option         | What it does                                                        |
| -------------- | ------------------------------------------------------------------- |
| **Continue**   | Stay in this chat. No context switch at all.                         |
| **A new chat** | Start from an empty context (`/chat new`). The old chat is kept.     |
| **`/handoff`** | Write a portable Markdown file that seeds a session anywhere.        |
| **Subagent**   | Send a read-only task to its own context and get a report back.      |
| **`/compact`** | Summarise this chat's context and continue from the summary.         |

## The tree

Work top to bottom at the boundary. The first **yes** wins.

**1. Can you continue in this chat?** Two things make the answer yes: the next phase needs this phase as a **primary source**, or enough of the model's **smart zone** is left for the next phase to fit. Grilling → implementation is the standard yes: the implementation wants the reasoning verbatim, not a summary of it. Continuing costs nothing and loses nothing, so rule it out before anything else.

**2. Is the context irrelevant to what comes next?** Is everything in this chat (the exploration, the decisions, the dead ends) disposable? Then **start a new chat**. It is the cheapest move on the board: no time, and the whole window back. The old chat stays in the list, so it can be reopened.

The cost of getting this wrong is one-way. Leave a *relevant* context behind and you lose the **why** behind what you built; reading the diff back does not return it.

**3. Do you need to hand off?** `/handoff` is narrow. You need it only when you are:

- moving to **another tool** (another agent harness),
- moving to **another directory** or project,
- sending the work to a **colleague**,
- or forking a side task you found **mid-phase** without derailing what you are doing.

That list is the whole clause. What `/handoff` buys is **portability**: a file that travels. If nothing is travelling, you don't need it.

**4. Can the task be done unattended?** Is it scoped tightly enough to run with no steering, and does it only read? Then it is a **subagent**'s job, and this chat stays untouched. Automated review is the standard case: a subagent reads the diff and reports, and nobody is needed while it does.

**5. Otherwise, `/compact`.** Relevant context, same tool, same directory, and you need to stay in the loop: the tree lands here, and often. Pass it an instruction (`/compact we're going to QA this area`) so the summary keeps what the next phase needs.

`/compact` is the **default, not the first reach**. It sits at the bottom because the four questions above it are all cheaper or more precise. The failure when people start here is a fresh context that is confidently wrong about a decision the summary flattened.
