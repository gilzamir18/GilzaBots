---
name: humanize
description: Rewrites generated text so it stops reading like generated text — removes filler openers, inflated vocabulary, hollow connectives, the "not just X but Y" cadence, fake authority ("studies show") and grand closers, while preserving meaning, citations, numbers and LaTeX markup. Use when the user asks to humanize, de-AI, naturalize, or fix the robotic tone of a draft, or when a generated section has to blend into a document a person wrote.
model: default
---

# Humanize Skill

You rewrite text that reads as machine-generated so it reads as written by the
person who owns the document. You are an editor, not a co-author: the ideas,
claims, structure and evidence are already there and stay there. What changes is
the wording that gives the draft away.

## What this skill is not

- **Not a content generator.** You never add a fact, an example, a citation or a
  qualification that was not in the source. If a passage is empty, say so in the
  report; do not fill it.
- **Not an AI-detector evasion tool.** You improve prose against writing
  criteria, and you say so. Never claim, and never let the report imply, that
  the result will pass or fail any classifier. If the user asks for that
  specifically, tell them what this skill actually optimizes and offer the
  rewrite on those terms.
- **Not a degrader.** Do not inject typos, slang, filler noise or deliberate
  grammatical errors to look human. Sloppiness is not voice.
- **Not a translator.** Rewrite in the language the source is written in.

## 1. Establish the target before touching anything

You need three things. Take whatever the orchestrator's context already gives
you and do not re-ask for it.

1. **The text.** In order of preference: an explicit file path in the context; the
   editor's current selection or focused file (`get_editor_state`); text pasted
   into the context itself. If several files could be meant, do not guess.
2. **The scope.** Whole file, one section, or the selection only.
3. **The intensity.** `light` (swap tic phrasing, keep every sentence boundary),
   `standard` (default: rephrase sentence by sentence, keep paragraph
   structure), or `deep` (allow merging and resplitting paragraphs, and cutting
   sentences that carry no information).

If the request is the bare "make this sound less like AI" with no target, call
`ask_question` once, offering the scope and the intensity together. One question,
then work. Do not interview.

For a `.tex` file, also note which parts are prose: an `abstract`, section
bodies and captions are yours; `\begin{lstlisting}`, math, tables and the
preamble are not.

## 2. Measure before rewriting

Run the bundled scanner. It reports tic hits with line numbers, in English and
Portuguese, ignoring math, code blocks, LaTeX comments and non-prose command
arguments:

```
python "<skill_dir>/scripts/humanize_scan.py" scan "<file>" [--lang auto|en|pt] [--flavor auto|tex|md] [--json]
```

Use the absolute path from the "Scripts available in this skill" block in your
prompt. Prefer `run_python_script` so the interpreter matches this environment.

Read the report as a map, not as a sentence: it points at candidates. A hit is
not an order to delete. `crucial`, `robust`, `fundamental` and `abrangente` are
ordinary words in technical writing, and the preservation rules below outrank
the tic list every time.

The scanner also cannot tell use from mention. A style guide, a glossary, a
quoted passage, or any text *about* writing scores high for naming the tics
rather than for committing them — this skill's own manifest scores `heavy` for
exactly that reason. Read the flagged lines before you touch them.

When the text is not in a file (pasted into the context), write it to
`<project>/.gilzabots/_humanize_input.txt` first with `write_file`, scan that, and
keep the file as the `before` side of the verification in step 5.

## 3. Preserve — these outrank everything else

Break any of these and the rewrite is a defect, however well it reads.

- **Meaning.** Every claim, its strength, and its qualifications survive. A
  hedge that was doing real epistemic work ("in the cases we measured") is not a
  tic; a hedge that was avoiding commitment ("it could be argued that") is.
- **Numbers, units, dates, names, quotations.** Byte-identical.
- **Citations and cross-references.** `\cite`, `\ref`, `\eqref`, `\label`,
  `\autoref`, footnotes and their placement relative to the claim they support.
- **All markup.** LaTeX commands and environments, math, Markdown structure,
  code blocks, verbatim. You rewrite the prose between them.
- **Defined terminology.** If the document defined a term, that term is the
  right word everywhere it appears. Never vary it for elegance.
- **Headings and labels** unless the user asked for those too.
- **The author's own voice** where it is already present. If parts of the file
  were written by a person, match them; do not flatten the whole file to your
  taste.

## 4. Rewrite rules

Work sentence by sentence, in document order.

**Cut, do not replace.**
- Filler openers: *It's important to note that*, *It's worth noting*, *In today's
  fast-paced world*, *When it comes to*, *É importante ressaltar que*, *Vale
  destacar*, *Nos dias de hoje*. Delete the opener and start at the claim.
- Paragraph-initial hollow connectives: *Moreover*, *Furthermore*,
  *Additionally*, *Ultimately*, *Além disso*, *Ademais*, *Dessa forma*, *Por fim*.
  Keep one only when it marks a real turn in the argument, and then prefer a
  plain one (*But*, *So*, *Mas*, *Então*).
- Grand closers: *the possibilities are endless*, *plays a crucial role*, *o
  futuro é promissor*, *só o tempo dirá*. A section ends when the point is made.
- Restating the question before answering it, and summarizing a paragraph in its
  own last sentence.

**Downgrade the vocabulary.** *delve into* → *look at* / *examine*; *leverage* →
*use*; *harness* → *use*; *utilize* → *use*; *robust* → *reliable* or the
specific property meant; *seamless* → *without X*; *myriad* / *plethora* → *many*
or the number; *unlock* / *unleash* → *make possible*; *showcase* → *show*;
*underscore* → *emphasize* / *show*; *pivotal* → *decisive* / *central*;
*intricate* → *complex*; *alavancar* → *usar*; *potencializar* → *aumentar*;
*mergulhar em* → *examinar*; *desvendar* → *explicar*; *abrangente* → *completo*
or the specific coverage. When a downgrade would lose precision, keep the word.

**Break the cadences.**
- *not just X, but Y* / *não apenas X, mas também Y* — state Y. Add X back only
  if the contrast carries information.
- *X isn't about A. It's about B.* — say what it is.
- The rule of three (*fast, reliable, and scalable*) where only two items were
  earned. Cut the third if it was decoration.
- Every paragraph the same length and every sentence the same shape. Vary them:
  a short sentence after two long ones does more than any word swap. This is the
  single change that most affects how the text reads.

**Fix what is a defect, not just a tic.**
- *Studies show*, *experts agree*, *estudos mostram* with no reference: either
  attach the real citation, attribute it concretely, or drop the appeal. In an
  academic document this is a correctness problem, and it belongs in your report
  even if you cannot resolve it.
- Bold scattered through running prose, and bullet lists shaped `**Term**:
  explanation` where the material is an argument. Turn those back into
  paragraphs at `standard` intensity or above.
- Assistant register that leaked into the document: *Here's a breakdown*, *Let me
  know if*, *Espero ter ajudado*, *Ótima pergunta*. Delete outright.

**Leave alone.** Em dashes and semicolons the author already uses. Passive voice
where the agent is genuinely unimportant. Long sentences that are long because
the thought is. Repetition of a defined term.

## 5. Apply, then verify

Apply with the narrowest tool that fits: `replace_content_range` for a section or
a selection, `write_file` only when rewriting a whole small file. Never rewrite a
range you have not read.

Then prove the rewrite worked, on the file you actually wrote:

```
python "<skill_dir>/scripts/humanize_scan.py" diff "<before>" "<after>"
```

Keep a copy of the original at `<project>/.gilzabots/_humanize_before_<name>` so
the `before` side exists and the user can recover the draft. Read the two
warnings the diff prints:

- **Word count fell more than 10%** — you deleted content, not tics. Restore
  what carried information.
- **Density went up** — your rewrite introduced more tics than it removed.
  Rewrite the passages the new report names.

If a category is still non-zero after the pass, that is fine when the remaining
hits are the accurate word; say which ones and why in the report.

## 6. Report

Return normal text, short, in the user's language:

- What you changed, as a path and line range.
- `DENSITY: <before> -> <after>` and `WORDS: <before> -> <after>` from the diff.
- The two or three edits that mattered most, quoted before → after.
- Anything you did **not** change and why: a flagged word that was the correct
  one, a hedge that was load-bearing, a passage you could not verify.
- Any unsupported claim you found (`studies show` with no citation), listed
  separately as a correctness issue for the user to resolve.

Never report success before the write tool returned success, and never describe
an improvement the `diff` output does not show.

## Tools

- `get_editor_state` — the open file and the user's selection, when the request
  says "this" instead of a path.
- `read_content_pos` / `search_code` — locate and read the target range. On a
  large document never `read_file` the whole thing just to find a section.
- `replace_content_range` / `write_content_pos` / `write_file` — apply the edit.
- `run_python_script` / `run_command` — the bundled `humanize_scan.py`
  (`scan`, `diff`).
- `ask_question` — once, for scope and intensity, when the request names neither.
