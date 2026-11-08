---
name: prototype
description: Build a throwaway prototype to answer a design question. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like.
---

# Prototype

A prototype is **throwaway code that answers a question**. The question decides the shape.

## Pick a branch

Identify which question is being answered, from the user's prompt, the surrounding code, or by asking:

- **"Does this logic / state model feel right?"** → [LOGIC.md](LOGIC.md). One shareable HTML file (free-play buttons plus tabbed guided walkthroughs) that pushes the state model through the cases that are hard to reason about on paper, and that a non-developer can drive.
- **"What should this look like?"** → [UI.md](UI.md). Several radically different UI variations on a single route, switchable through a URL search parameter and a floating bottom bar.

Both files are in this skill's directory: read the one for your branch with `read_file`.

The two branches produce very different artifacts, so getting this wrong wastes the whole prototype. If the question is genuinely ambiguous and the user isn't answering, default to the branch that better matches the surrounding code (a backend module → logic; a page or component → UI) and state the assumption at the top of the prototype.

## Rules for both branches

1. **Throwaway from day one, and clearly marked as such.** Put the prototype next to the module or page it prototypes for, so the context is obvious, but name it so a casual reader sees it is a prototype. For throwaway UI routes, follow the project's existing routing convention.
2. **Trivial to run.** A UI prototype starts with one command from the project's task runner (`pnpm <name>`, `python <path>`, ...). A logic demo is one HTML file the user opens; `open_file` shows it in the IDE's HTML preview.
3. **No persistence by default.** State lives in memory. Persistence is what a prototype may be _checking_, not something it depends on. If the question involves a database, use a scratch database or a local file named "PROTOTYPE, wipe me".
4. **Skip the polish.** No tests, no error handling beyond what keeps it runnable, no abstractions. The point is to learn something fast.
5. **Surface the state.** After every action (logic) or on every variant switch (UI), print or render the full relevant state so the user sees what changed.
6. **Capture it when done.** Fold the validated decision into the real code, then keep the prototype as a **primary source**: commit it to a throwaway `prototype/<name>` branch, out of main, and leave a pointer to that branch on the implementation ticket. Record the answer too (the verdict and the question it settled) in the ticket or a commit. The main branch keeps only the validated decision.
