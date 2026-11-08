---
name: to-spec
description: "Turn the current conversation into a spec and publish it to the project's issue tracker: no interview, just synthesis of what was already discussed."
disable-model-invocation: true
---

This skill takes the current conversation and your understanding of the codebase and produces a spec. Do NOT interview the user: synthesise what you already know.

Where specs go and how they are labelled is in `docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`, written by `/setup-engineering-skills`. If they don't exist, tell the user to run `/setup-engineering-skills`; when they don't want to, use the local-markdown convention (`.scratch/<feature-slug>/spec.md`) and say so.

## Process

1. Explore the repo to understand the current state of the code, if you haven't already. Use the vocabulary of `GLOSSARY.md` throughout the spec, and respect the ADRs in the area you're touching.

2. Sketch the seams at which the feature will be tested. Prefer existing seams to new ones, and use the highest seam possible. If new seams are needed, propose them at the highest point you can. The fewer seams across the codebase, the better: the ideal number is one. When the active skills include `codebase-design`, load it for the vocabulary.

   Check with the user that these seams match their expectations.

3. Write the spec with the template below, then publish it to the tracker the tracker doc describes (on a local tracker, write `.scratch/<feature-slug>/spec.md`). Apply the `ready-for-agent` triage label (or its local `Status:` line); no further triage is needed. Give the user the spec's path or URL.

<spec-template>

## Problem Statement

The problem the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories, each in the form:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see the balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list should be extremely extensive and cover every aspect of the feature.

## Implementation Decisions

The implementation decisions that were made. This can include:

- the modules that will be built or modified;
- the interfaces of those modules that will change;
- technical clarifications from the developer;
- architectural decisions;
- schema changes;
- API contracts;
- specific interactions.

Do NOT include specific file paths or code snippets: they go stale fast.

Exception: when a prototype produced a snippet that encodes a decision more precisely than prose can (a state machine, a reducer, a schema, a type shape), inline it within the relevant decision and note that it came from a prototype. Trim it to the decision-rich parts.

## Testing Decisions

The testing decisions that were made:

- what makes a good test here (test external behaviour, not implementation details);
- which modules will be tested;
- prior art for the tests (similar tests already in the codebase).

## Out of Scope

What is out of scope for this spec.

## Further Notes

Any further notes about the feature.

</spec-template>
