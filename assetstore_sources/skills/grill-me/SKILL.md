---
name: grill-me
description: A relentless interview to sharpen a plan or design, with no files written.
disable-model-invocation: true
argument-hint: What plan or idea should be grilled?
---

Call `load_skill("grilling")` and run that interview on what the user brought: the arguments, or the plan already in this conversation. Write nothing to the project; this interview leaves no paper trail. When the user is working on a project whose language and decisions should be recorded as you go, suggest `/grill-with-docs` instead.

When the interview is done and the plan is to be built, the next step is `/to-spec` (or `/implement` for small work). Suggest only skills of this pack (`/which-skill` lists them); no other skill name exists.
