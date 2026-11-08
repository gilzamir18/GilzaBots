---
name: grill-with-docs
description: A relentless interview to sharpen a plan or design, which records the glossary and ADRs as it goes.
disable-model-invocation: true
argument-hint: What plan or idea should be grilled?
---

Call `load_skill("grilling")` and `load_skill("domain-modeling")`, then run the grilling interview on what the user brought (the arguments, or the plan already in this conversation) while applying the domain-modeling discipline throughout: challenge terms against `GLOSSARY.md`, sharpen fuzzy language, check claims against the code, update the glossary the moment a term is resolved, and offer an ADR when a decision meets all three of its criteria.

When the interview is done, the next step is `/to-spec` to turn it into a spec, or `/implement` when the work is small enough to build in this chat. Suggest only skills of this pack (`/which-skill` lists them); no other skill name exists.
