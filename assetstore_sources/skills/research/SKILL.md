---
name: research
description: Investigate a question against high-trust primary sources and save the findings as a cited Markdown file in the project. Use when the user wants a topic researched, docs or API facts gathered, or reading legwork delegated to a background agent.
---

Hand the reading to a **background subagent**, so you keep working while it reads.

1. Start it with `run_agent(task=..., description="research <topic>", background=true)`. It begins with a fresh context and sees only `task`, so the task holds the whole job: the question, what the answer will be used for, any lead you already have, and the brief below.
2. Keep working on whatever else the user needs. Before you answer them about the research, collect the report with `wait_agents([<id>])`: a background subagent still running when your turn ends is stopped.
3. Write the findings to one Markdown file with `write_file`. Save it where the project already keeps such notes (look first: `docs/research/`, `notes/`, `.scratch/`); match that convention, and if there is none, choose a sensible place and tell the user where. Every claim keeps the citation the subagent gave it.

The brief to put in `task`:

> Investigate the question against **primary sources**: official documentation, source code, specifications, first-party APIs, the paper itself, not a secondary write-up of them. Follow every claim back to the source that owns it. Report the findings as Markdown, each claim with its source (URL, or file:line for code), and list separately what you could not establish and why.

For a question with several independent parts, start one subagent per part in the same response, then merge their reports into the file.
