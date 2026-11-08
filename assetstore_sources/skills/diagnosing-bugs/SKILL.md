---
name: diagnosing-bugs
description: Diagnosis loop for hard bugs and performance regressions. Use when the user says "diagnose" or "debug this", or reports something broken, throwing, failing or slow.
---

# Diagnosing Bugs

A discipline for hard bugs. Skip a phase only when you can say why.

When exploring the codebase, read `GLOSSARY.md` (if it exists) for a clear mental model of the relevant modules, and check the ADRs in the area you're touching.

## Redact

This skill has you show commands, outputs and captured artifacts. **Redact every secret first**: write `<REDACTED>` in its place. Build loops against environment variables so the credential stays in the environment rather than in what you show. Captured artifacts carry auth headers: quote only the lines that carry the signal.

If the redacted output is not enough to diagnose the bug, say so and ask the user.

## Phase 1: Build a feedback loop

**This is the skill.** Everything else is mechanical. With a **tight** pass/fail signal for the bug (one that goes **red** on _this_ bug), you will find the cause; bisection, hypothesis testing and instrumentation all just consume it. Without one, no amount of staring at code will save you.

Spend disproportionate effort here. **Be aggressive. Be creative. Refuse to give up.**

### Ways to build one, roughly in this order

1. **Failing test** at whatever seam reaches the bug: unit, integration, end to end.
2. **HTTP script** (curl or a short Python `requests` script) against a running dev server; start the server with `run_background_command` and read its output with `read_terminal`.
3. **CLI invocation** with a fixture input, diffing stdout against a known-good snapshot (`diff_files` compares two outputs).
4. **Headless browser script** (Playwright, Puppeteer) that drives the UI and asserts on the DOM, the console or the network.
5. **Replay a captured trace.** Save a real request, payload or event log to disk and replay it through the code path in isolation.
6. **Throwaway harness.** A minimal subset of the system (one service, mocked dependencies) that exercises the bug's code path with a single function call.
7. **Property / fuzz loop.** For "sometimes wrong output", run a thousand random inputs and look for the failure mode.
8. **Bisection harness.** When the bug appeared between two known states (commit, dataset, version), automate "boot at state X, check, repeat" so `git bisect run` can drive it.
9. **Differential loop.** Run the same input through the old and new version (or two configurations) and diff the outputs.
10. **Human-in-the-loop script.** Last resort. When a person must click, drive _them_ with a structured script: copy [templates/hitl_loop.py](templates/hitl_loop.py) (in this skill's directory), edit its steps, and run it with `run_interactive_command` so the user answers in the terminal popup. Its `KEY=VALUE` summary comes back to you.

Build the right loop, and the bug is 90% fixed.

### Tighten the loop

Treat the loop as a product. Once you have _a_ loop, **tighten** it:

- Faster? Cache setup, skip unrelated initialisation, narrow the test scope.
- Sharper? Assert on the specific symptom, not "didn't crash".
- More deterministic? Pin time, seed the random generator, isolate the filesystem, freeze the network.

A 30-second flaky loop is barely better than none; a 2-second deterministic one is tight.

### Non-deterministic bugs

The goal is not a clean repro but a **higher reproduction rate**. Loop the trigger a hundred times, parallelise, add stress, narrow timing windows, inject sleeps. A 50% flake is debuggable; 1% is not, so keep raising the rate.

### When you genuinely cannot build a loop

Stop and say so. List what you tried. Ask the user for (a) access to an environment that reproduces it, (b) a redacted captured artifact (HAR file, log dump, core dump, screen recording with timestamps), or (c) permission to add temporary production instrumentation. Do **not** hypothesise without a loop.

### Completion criterion: a tight loop that goes red

Phase 1 is done when you can name **one command** (a script path, a test invocation, a curl) that you have **already run at least once**, showing the invocation and its redacted output, and that is:

- [ ] **Red-capable**: it drives the actual bug's code path and asserts the **user's exact symptom**, so it goes red on this bug and green once fixed. "Runs without erroring" is not enough.
- [ ] **Deterministic**: the same verdict every run (for flaky bugs, a pinned, high reproduction rate).
- [ ] **Fast**: seconds, not minutes.
- [ ] **Agent-runnable**: you run it unattended; a human in the loop only through the template above.

If you catch yourself reading code to build a theory before this command exists, **stop**: jumping straight to a hypothesis is the exact failure this skill prevents. No red-capable command, no Phase 2.

## Phase 2: Reproduce and minimise

Run the loop. Watch it go red.

Confirm:

- [ ] The loop produces the failure the **user** described, not a different failure nearby. Wrong bug, wrong fix.
- [ ] The failure reproduces across several runs (or, for non-deterministic bugs, at a rate high enough to debug against).
- [ ] You have captured the exact symptom (error message, wrong output, slow timing) so later phases can verify the fix addresses it.

### Minimise

Shrink the repro to the **smallest scenario that still goes red**. Cut inputs, callers, configuration, data and steps **one at a time**, re-running the loop after each cut, and keep only what the failure depends on. A minimal repro shrinks the hypothesis space in Phase 3 and becomes the regression test in Phase 5.

Done when **every remaining element is load-bearing**: removing any one makes the loop go green.

## Phase 3: Hypothesise

Generate **three to five ranked hypotheses** before testing any of them: a single hypothesis anchors on the first plausible idea.

Each one is **falsifiable**: it states its prediction.

> Format: "If <X> is the cause, then <changing Y> will make the bug disappear / <changing Z> will make it worse."

If you cannot state the prediction, it is a vibe: discard or sharpen it.

**Show the ranked list to the user before testing.** They often re-rank instantly ("we just deployed a change to #3") or know hypotheses already ruled out. Don't block on it: when the user doesn't answer, proceed with your ranking.

## Phase 4: Instrument

Each probe maps to a specific prediction from Phase 3. **Change one variable at a time.**

1. **Debugger or REPL inspection** where the environment supports it: one breakpoint beats ten logs.
2. **Targeted logs** at the boundaries that distinguish hypotheses.
3. Never "log everything and grep".

**Tag every debug log** with a unique prefix such as `[DEBUG-a4f2]`, so cleanup is one `search_code`. Untagged logs survive; tagged logs die.

**Performance branch.** For regressions, logs are usually wrong. Establish a baseline measurement first (timing harness, profiler, query plan), then bisect. Measure first, fix second.

## Phase 5: Fix and regression test

Write the regression test **before the fix**, but only if there is a **correct seam** for it: one where the test exercises the **real bug pattern** as it occurs at the call site. A seam that is too shallow (a single-caller test when the bug needs several callers, a unit test that cannot replicate the chain that triggered the bug) gives false confidence.

**If no correct seam exists, that itself is the finding.** Note it: the architecture is preventing the bug from being locked down (a candidate for `/improve-codebase-architecture`).

With a correct seam:

1. Turn the minimised repro into a failing test at that seam.
2. Watch it fail. If you forced the red by mutating code or a fixture, `diff_files` against a pristine copy to prove the mutation landed before you trust it.
3. Apply the fix.
4. Watch it pass.
5. Re-run the Phase 1 loop against the original, un-minimised scenario.

## Phase 6: Cleanup

Before declaring done:

- [ ] The original repro no longer reproduces (re-run the Phase 1 loop).
- [ ] The regression test passes (or the missing seam is documented).
- [ ] Every `[DEBUG-...]` probe is removed (`search_code` the prefix).
- [ ] Throwaway harnesses are deleted, or moved to a clearly marked debug location.
- [ ] The hypothesis that turned out correct is stated in the commit or PR message, so the next debugger learns from it.
