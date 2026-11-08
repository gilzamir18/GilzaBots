---
name: teach
description: Teach the user a new skill or concept over several sessions, using the project as a stateful teaching workspace.
disable-model-invocation: true
argument-hint: What would you like to learn about?
---

The user has asked you to teach them something. This is a stateful request: they intend to learn the topic over several sessions.

## Teaching workspace

Treat the project directory as a teaching workspace. The state of their learning lives in these files (create each one when it is first needed):

- `MISSION.md`: the _reason_ the user is interested in the topic. It grounds all teaching. Format: [MISSION-FORMAT.md](MISSION-FORMAT.md).
- `reference/*.html`: reference materials, the compressed learnings from the lessons: cheat sheets, algorithms, syntax, poses, glossaries. They are the raw units of learning: beautiful documents that print well and are built for quick reference.
- `RESOURCES.md`: resources to ground your teaching in real knowledge, and communities that hold wisdom. Format: [RESOURCES-FORMAT.md](RESOURCES-FORMAT.md).
- `learning-records/*.md`: what the user has learned. The teaching equivalent of ADRs: non-obvious lessons and key insights that may be revised later and that steer future sessions. They are how you find the zone of proximal development. Titled `0001-<dash-case-name>.md`, numbered upwards. Format: [LEARNING-RECORD-FORMAT.md](LEARNING-RECORD-FORMAT.md).
- `GLOSSARY.md`: the canonical vocabulary of the workspace. Format: [GLOSSARY-FORMAT.md](GLOSSARY-FORMAT.md).
- `lessons/*.html`: the lessons. A **lesson** is one self-contained HTML file that teaches one tightly scoped thing tied to the mission. It is the primary unit of teaching here.
- `assets/*`: reusable **components** shared across lessons (see [Assets](#assets)).
- `NOTES.md`: your scratchpad for the user's preferences and working notes.

The `*-FORMAT.md` files are in this skill's directory: read them there with `read_file`.

## Philosophy

To learn at a deep level, the user needs three things:

- **Knowledge**, from high-quality, high-trust resources;
- **Skills**, acquired through highly relevant interactive lessons you devise from that knowledge;
- **Wisdom**, which comes from interacting with other learners and practitioners.

Until `RESOURCES.md` is well populated, your focus is finding high-quality resources (`web_search`, or research subagents started with `run_agent`). Never trust your parametric knowledge.

Some topics need more skills than knowledge (yoga), others more knowledge than skills (theoretical physics).

### Fluency vs storage strength

- **Fluency strength**: in-the-moment retrieval.
- **Storage strength**: long-term retention.

Fluency gives an illusory sense of mastery; storage strength is the goal. Design lessons with desirable difficulty: retrieval practice (recall from memory), spacing (practice distributed over time) and, for skills practice only, interleaving (mixing related topics).

## Lessons

A lesson is one self-contained HTML file in `lessons/`, titled `0001-<dash-case-name>.html` and numbered upwards.

Make it **beautiful**, with clean, readable typography and layout, since the user returns to it to review. Think Tufte.

Keep it short and quick to complete: working memory is small, so stay within it. Each lesson gives the user one tangible win to build on, tied directly to the mission and inside their zone of proximal development.

Open the finished lesson for the user with `open_file` (the IDE previews HTML), or in their browser with the system opener through `run_command` (`start "" "<path>"` on Windows, `open` on macOS, `xdg-open` on Linux) when they prefer it.

Each lesson links (HTML anchors) to other lessons and reference documents, recommends one primary source to read or watch (the best high-trust resource you found), and reminds the user to ask you follow-up questions: you are their teacher.

## Assets

Lessons are built from reusable **components** in `assets/`: stylesheets, quiz widgets, simulators, diagram helpers, anything a second lesson could reuse.

Reuse is the default. Before writing a lesson, list `assets/` and build from what is there. When a lesson needs something new and reusable, write it as a component in `assets/` and link to it; never inline code a future lesson would duplicate. A shared stylesheet is the first component every workspace earns, so the lessons read as one course.

## The mission

Every lesson ties into the mission. If the user is unclear about it, or `MISSION.md` is empty, your first job is to ask why they want to learn this. Without a mission, lessons go abstract and you cannot judge what comes next.

Missions change as the user grows. Confirm with the user before changing it, then update `MISSION.md` and add a learning record for the change.

## Zone of proximal development

Each lesson should challenge the user "just enough". When they name what they want to learn, teach that. Otherwise read their learning records, weigh the mission, and teach the most relevant thing that fits their zone.

## Knowledge

Design each lesson around a skill the user is going to learn, with only the knowledge that skill needs. Teach the knowledge first, then have the user practise through an interactive feedback loop.

Gather knowledge from trusted resources, track them in `RESOURCES.md`, and cite them throughout the lesson (links backing every claim). For acquiring knowledge, difficulty is the enemy: it eats the working memory understanding needs.

## Skills

For skills, difficulty is the tool: effortful retrieval builds storage strength. Teach them through interactive lessons (quizzes, light in-browser tasks) or guided real-world step lists (yoga poses). Each one is a **feedback loop**: the user gets feedback on their performance, as immediately and as automatically as possible.

In quizzes, give every answer the same number of words (and characters, where possible), vary the position of the correct one, and give no clue through formatting or order.

## Wisdom

Wisdom comes from real-world practice outside the learning environment. When a question seems to need wisdom, attempt an answer, but ultimately point the user to a **community**: a forum, a subreddit, a class, a local group where they can test their skills. Look for high-reputation ones. If the user does not want to join a community, respect it and note that in `RESOURCES.md`.

## Reference documents

Create reference documents alongside lessons. Lessons are rarely revisited; references are. They hold the compressed essence of the lessons in a quick-reference format: syntax and snippets for programming, flowcharts for processes, poses and sequences for yoga, routines for fitness, glossaries for any topic with its own nomenclature. Once a glossary exists, every lesson adheres to it.

## `NOTES.md`

When the user states how they want to be taught, or anything you should keep in mind, record it here and consult it when designing lessons.
