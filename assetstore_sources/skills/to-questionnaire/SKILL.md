---
name: to-questionnaire
description: Turn a decision you can't answer alone into a questionnaire for someone else to fill in.
disable-model-invocation: true
argument-hint: What decision is blocked, and who knows the answer?
---

Turn something the user can't answer alone into a **questionnaire**: a Markdown document they hand to one person to fill in asynchronously, or fill out together in a meeting. The recipient holds knowledge the user lacks; the questionnaire pulls it out of them.

**Grill the send, not the subject.** Interview the user only about the _send_, which they can always answer: who it goes to, and what they need back. The questions in the document then target the **gap** between what the recipient knows and what the user needs.

1. **Who is it going to?** Ask, in one exchange, the recipient's role, expertise and relationship to the user. This fixes the tone and how much context the document must carry. Done when you know who the recipient is and what they know that the user doesn't.

2. **What do you need back?** Ask, in one exchange, the specific decisions or facts the user can't resolve alone and needs from this person. Done when you have a concrete list of what the user must walk away able to do or decide.

3. **Write the questionnaire.** Draft questions aimed at the gap from steps 1–2, following the structure below. Write it with `write_file` to `to-questionnaire-<slug>.md` at the project root (slug from the topic), open it with `open_file`, and report the path. Done when the file exists and every item from step 2 is covered by a question.

## Document structure

Frame it as a **discovery questionnaire**: the user lacks context, the recipient holds it. Order questions most important first, since an async send may only get one pass, and group them under `##` headings by theme once there are more than a handful.

<questionnaire-template>

# <Questionnaire title>

**Purpose:** why this questionnaire exists and the decision riding on it.

**From:** <the user>, **To:** <the recipient>, **How your answers will be used:** <where they go>

## Context

One paragraph orienting a recipient who wasn't in the user's head. Enough to answer well, not a page.

## How to answer

Deadline and rough effort. Partial answers and "I don't know" are useful: flag anything you're unsure of rather than skipping it.

## <Theme heading>

One `##` section per theme, its questions most important first. Every question is one idea, never compound, with an answer stub directly beneath it, and a one-line _why this matters_ only where the question could be misread or invite a throwaway answer.

<question-example>
### What load is the system expected to handle at launch?

_Why this matters: it decides whether we provision for burst traffic now or defer it._

>
</question-example>

## Anything else?

A closing catch-all: anything we didn't ask that we should know?

</questionnaire-template>
