---
name: tex-to-jpt
description: Converts a LaTeX or Beamer presentation into a self-contained editable .jpt package — mapping frames to slides, packaging rendered pictures, and carrying mathematics across as real equations. Also the reference for the .jpt outline format and presentation tools.
---

# Converting a `.tex` presentation into a `.jpt` deck

A `.tex` deck is a *flow* document that a compiler turns into pages. A `.jpt`
deck is a *canvas*: absolutely positioned boxes the user drags. The conversion
is therefore a re-authoring, not a translation, and the way to get it right is
to extract what each frame **says** and let the layout engine decide where it
goes.

The model you author is JSON, but the `.jpt` file on disk is not a plain JSON
file. It is a ZIP/ZIP64 package containing `deck.json` plus content-addressed
members such as `assets/<sha256>.png`, referenced from the model as
`jpt:assets/<sha256>.png`. `read_file` deliberately returns the logical
`deck.json`, so agents can inspect the deck without loading packaged media.
Use the presentation tools to write it; never unzip/rebuild the package or use
line-position tools on a `.jpt`.

You never write coordinates. Not once. If you find yourself computing an `x`,
you have taken a wrong turn — see §2.

## 1. Before anything: what the user gets, and what they lose

Say this up front, briefly, and then convert. A `.jpt` cannot carry:

| Beamer feature | What happens |
| --- | --- |
| `\pause`, `\onslide`, `\only<2->`, `\alt` | **Flattened.** Every overlay's content appears at once. If a frame's whole point is a build, offer to split it into consecutive slides. |
| `\ref`, `\cite`, `\label`, numbered equations | Dropped. A deck has no counters and no bibliography. Write the number as text if it matters. |
| Themes, `\usetheme`, footlines, navigation bars | **Partly carried.** `set_presentation_theme` applies a theme from the Asset Store — `madrid` reproduces Beamer's most-used look, a blue band carrying the frame title and a footline with the deck title and slide number. Navigation bars and sidebars have no equivalent. Offer the closest theme rather than inventing colours. |
| `\usebackgroundtemplate{\includegraphics{…}}` | **Carried.** Render or copy the picture, then set it deck-wide in the outline's `theme.backgroundImage`, with `backgroundOpacity` around `0.4` if text has to sit over it. A per-frame background goes on that slide instead, and `backgroundImage: null` turns it off for one slide. |
| `\note{}` | **Kept** — becomes the slide's speaker notes. |
| Inline math inside a sentence (`$x^2$` in a bullet) | Not renderable in a text box. See §5. |
| `\includegraphics` | Kept, as an image element. |
| TikZ / PGFPlots | Rendered to PNG and placed as an image. See §3. |

## 2. The workflow

```
read the .tex  →  render the pictures  →  build the outline  →  create_presentation
                                                                       ↓
                                          fix what it reports  ←  errors? (it did not write)
                                                                       ↓
                                                              check_presentation
```

1. **`read_file`** the `.tex`. If it `\input`s or `\include`s other files, read
   those too — frames often live in one file per section.
2. **Render the pictures first** (§3), so the outline can reference real paths.
   `create_presentation` treats a missing image as an *error* and refuses to
   write the deck, which is what stops a deck of broken picture boxes.
3. **Build the outline** (§4) and call `create_presentation`.
4. **Read what it says.** Errors mean nothing was written; fix and call again.
   Warnings mean it was written and something is worth a second look.
4b. **Offer a theme.** `set_presentation_theme(path, theme="madrid")` gives a
   converted Beamer deck a look close to the one it had. Call it with an unknown
   name to see the catalogue.
5. **`check_presentation`** at the end, and tell the user what remains.

## 3. TikZ and PGFPlots become images

Use the script in this skill. It harvests the deck's own preamble — packages,
`\usetikzlibrary`, `\definecolor`, `\newcommand` — into a `standalone` document,
compiles it with the tectonic GilzaBots ships, and rasters the result. Compiling
a picture without its preamble either fails cryptically or silently draws the
wrong thing.

```bash
# what is in there
python3 skills/tex-to-jpt/scripts/tikz_to_image.py --tex slides.tex --list

# all of them, into figures/
python3 skills/tex-to-jpt/scripts/tikz_to_image.py --tex slides.tex --out figures --dpi 300

# just the third one
python3 skills/tex-to-jpt/scripts/tikz_to_image.py --tex slides.tex --index 3 --out figures
```

Run it with `run_python_script`. It prints one path per picture; those paths go
straight into the outline's `image` fields — `create_presentation` packages the
pictures as content-addressed internal assets and writes `jpt:` references into
`deck.json`. The deck the user ends up with is one self-contained file while the
PNGs stay on disk as the source to re-render from.

Notes:

- **300 dpi** is the right default: a slide is 1280 units wide and a projector
  is not, so a 150-dpi picture looks soft on the wall. Raise `--dpi` for a
  picture that is mostly fine detail; lower it for one that is mostly area.
- The picture keeps its transparent background, so it sits on any slide colour.
- If a picture fails to compile, the script prints the compiler's own first
  error. It is nearly always a package or macro the picture needed and the
  harvest missed — add the missing `\usepackage` to the *source's* preamble and
  run it again, or tell the user which picture could not be converted rather
  than shipping a deck with a hole in it.
- A picture that is really a *plot of data* (pgfplots reading a `.csv`) converts
  fine, but say so: the deck now holds a picture of the plot, and the data is no
  longer live.

## 4. Mapping frames to slides

One frame is normally one slide. The outline is a JSON object; `layout` picks
the shape and the rest is content.

| In the `.tex` | In the outline |
| --- | --- |
| `\titlepage` / `\maketitle`, with `\title`, `\subtitle`, `\author` | `{"layout": "title", "title": …, "subtitle": …}` — put the author in `subtitle` when there is no other place for it |
| `\section{X}` (and `\section` frames from `\AtBeginSection`) | `{"layout": "section", "title": "X"}` |
| `\begin{frame}{Title}` + `itemize` / `enumerate` | `{"layout": "bullets", "title": "Title", "bullets": [...]}` |
| A frame of prose | `{"layout": "text", "title": …, "text": …}` |
| A frame whose point is one formula | `{"layout": "equation", "title": …, "equation": "…", "caption": "…"}` |
| `\includegraphics` or a converted TikZ picture | `{"layout": "image", "title": …, "image": "figures/x.png", "caption": …}` |
| `\begin{columns}` with two `\column` | `{"layout": "two_columns", "title": …, "left": {…}, "right": {…}}` |
| A picture beside text | `{"layout": "image_text", "title": …, "image": …, "bullets": [...], "side": "left"}` |
| `\begin{quote}` or an epigraph frame | `{"layout": "quote", "quote": …, "attribution": …}` |
| `\note{...}` | `"notes": "..."` on that slide |
| A frame nothing else fits | `{"layout": "blank", "title": …, "elements": [...]}` — raw elements, last resort |

`left` and `right` (and any block) hold **exactly one** of `bullets`, `text`,
`equation`, `image`, plus an optional `caption`.

Rules that keep the result readable:

- **Strip the markup, keep the words.** `\textbf{x}` → `x`, `\emph{x}` → `x`,
  `~` → a space, `\\` inside a bullet → drop it, `---` → `—`. A bullet is plain
  text; LaTeX commands left in it render as themselves.
- **A nested `itemize` nests.** A sub-point is `{"text": "…", "level": 1}` (or
  the same string with a leading tab), up to five levels. Never prefix a bullet
  with `–` or `•`: the box draws its own markers, and a typed one arrives in
  front of them. `"bulletStyle": "number"` turns an `enumerate` into a real
  numbered list rather than hand-typed digits.
- **Seven bullets is the ceiling**, and the linter says so. A frame with twelve
  `\item`s wants to be two slides; propose that rather than shrinking the type.
- **Do not invent content.** No summaries the author did not write, no filler.
  If a frame is genuinely empty, it becomes an empty slide and you say so.

## 5. The mathematics — what `.jpt` actually supports

This is the part most conversions get wrong, so it is worth reading in full.

A `.jpt` deck has a **real equation element**. It stores the LaTeX *source* and
renders it with KaTeX as MathML, at the size the layout picks. That means a
formula stays editable and correctable in the slide editor — it is not a
picture of a formula, and it must never be converted to one.

### 5.1 Where equations go

```json
{"layout": "equation", "title": "The result",
 "equation": "\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}",
 "caption": "by squaring and changing to polar coordinates"}
```

and inside a column, as a block:

```json
{"layout": "two_columns", "title": "Two views",
 "left":  {"bullets": ["Polar coordinates", "Feynman's trick"]},
 "right": {"equation": "I^2 = \\iint e^{-(x^2+y^2)}\\,dA"}}
```

### 5.2 The rules

1. **No delimiters.** The field is already a math context. Write
   `E = mc^2`, never `$E = mc^2$`, `\[E = mc^2\]` or `\begin{equation}…`
   wrapped around it for that reason alone. `check_presentation` reports `$` as
   an **error**, because KaTeX never sees the delimiters as math.
2. **No `\label`, no `\ref`, no `\tag` you did not mean.** `\label` and `\ref`
   are undefined control sequences in KaTeX and fail the whole formula. Strip
   them. (`\tag{3}` does work, if you want a literal number beside the formula.)
3. **Macros do not exist.** Every equation is rendered in isolation, with no
   preamble: a `\newcommand{\R}{\mathbb{R}}` from the `.tex` is not available,
   and defining it in one equation does not carry to the next. **Expand macros
   at conversion time** — `\R` becomes `\mathbb{R}` in every formula that used
   it. (If you must, repeat `\def\R{\mathbb{R}}` inside each equation.)
4. **No siunitx.** `\SI{3}{\meter}` fails; write `3\,\mathrm{m}`.
5. **`displayMode` is on by default**, which is what you want on a slide: the
   limits of a sum sit above and below it. Set `"displayMode": false` only for
   a formula that must read inline.
6. **Never set `fontSize`, `w` or `h` on an equation.** The layout sizes it and
   the editor re-measures the box the moment the deck is opened.

### 5.3 What KaTeX accepts (verified against the version this app ships)

| Works | Fails — rewrite it |
| --- | --- |
| `\begin{align}`, `\begin{aligned}`, `\begin{equation}`, `\begin{gather}`, `\begin{split}` | `\label`, `\ref`, `\eqref` |
| `\begin{cases}`, `\begin{matrix}`, `pmatrix`, `bmatrix`, `vmatrix`, `Vmatrix`, `smallmatrix`, `array` | `\SI`, `\si` and the rest of siunitx |
| `\text{…}`, `\mathbb`, `\mathcal`, `\mathfrak`, `\bm`, `\boldsymbol` | Any macro from the document's preamble |
| `\textcolor{red}{…}`, `\substack`, `\overbrace`, `\underbrace`, `\xrightarrow`, `\tag` | `tikzpicture` inside math (render it as a picture instead, §3) |
| `\left…\right`, `\frac`, `\dfrac`, `\sqrt[n]`, `\underline` | `\newcommand` naming something KaTeX already defines (`\R`, for one) |

A multi-line derivation belongs in **one** equation element using `aligned`,
not in several elements stacked by hand:

```json
{"equation": "\\begin{aligned} (a+b)^2 &= a^2 + 2ab + b^2 \\\\ &= a^2 + b^2 + 2ab \\end{aligned}"}
```

### 5.4 Inline math in a bullet

A text box renders plain text: `$x^2$` in a bullet appears as those five
characters. `check_presentation` reports it as `math-in-text`. Three honest
options, in order of preference:

1. **Unicode**, when the formula is small: `x²`, `α`, `≤`, `√2`, `θ₀`. This is
   what keeps a bullet a bullet.
2. **Promote it**: the formula becomes an `equation` slide or the other column
   of a `two_columns`, with the sentence beside it.
3. **Rephrase**: "the square of x" reads better on a slide than `x^2` does in
   the middle of a line anyway.

## 6. Writing, reading and fixing

```
create_presentation(path="talk.jpt", outline_json="{…}")
```

- **Errors mean nothing was written.** Text that cannot fit at a readable size,
  an element off the slide, a missing image, `$` in a formula. Fix the outline
  and call again. Do not work around an error by shrinking type by hand — you
  cannot; the layout owns sizes.
- **Warnings mean it was written.** Too many bullets, low contrast, a wordy
  slide, math left in a text box. Judge each one and tell the user what you
  left as it is.
- **Local assets are always packaged.** Project paths and `data:` sources used
  by images, backgrounds, video posters or video files become internal `jpt:`
  members on every save. There is no size exemption and no opt-out. A missing
  file, path outside the project, `blob:` source or dangling internal reference
  aborts the atomic write instead of leaving a broken dependency.
- **Hosted players remain services.** YouTube, Vimeo and other HTTP(S) player
  URLs are links, not local package members, so playback still needs the
  corresponding network service.

To change a deck that already exists, use `edit_presentation` rather than
rewriting the file — the user may have edited it since:

```json
[{"op": "add_slide", "at": 3, "slide": {"layout": "equation", "title": "Proof", "equation": "…"}},
 {"op": "set_notes", "slide": "<slide id>", "notes": "pause here"},
 {"op": "update_element", "element": "<element id>", "patch": {"color": "#2f6fb3"}},
 {"op": "delete_element", "element": "<element id>"},
 {"op": "reorder_element", "element": "<element id>", "direction": "front"}]
```

`read_file` the `.jpt` first to learn the ids. It returns the package's logical
JSON model, whose `id` fields are the identifiers needed by these operations;
it does not return ZIP bytes or expand the packaged media. Prefer
`edit_presentation`, because it preserves internal members and rewrites the
package atomically. A full `read_file`/`write_file` round-trip is supported when
the whole model must change, but `read_content_pos`, `write_content_pos` and
`replace_content_range` are not valid for a packaged `.jpt`. A failed operation
writes nothing at all, so the user's deck is never left half-edited.

## 7. A worked fragment

```latex
\begin{frame}{Scaled dot-product attention}
  \begin{columns}
    \column{0.45\textwidth}
      \begin{itemize}
        \item Queries and keys of dimension $d_k$
        \item The $\sqrt{d_k}$ keeps softmax out of saturation
      \end{itemize}
    \column{0.55\textwidth}
      \[ \mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V \]
  \end{columns}
  \note{Mention the O(n^2) cost here.}
\end{frame}
```

becomes

```json
{"layout": "two_columns",
 "title": "Scaled dot-product attention",
 "left": {"bullets": ["Queries and keys of dimension dₖ",
                      "The √dₖ keeps softmax out of saturation"]},
 "right": {"equation": "\\mathrm{Attention}(Q,K,V) = \\mathrm{softmax}\\!\\left(\\frac{QK^{\\top}}{\\sqrt{d_k}}\\right)V"},
 "notes": "Mention the O(n^2) cost here."}
```

Note what happened: the `\[…\]` delimiters were dropped, the inline `$d_k$` in
the bullets became Unicode subscripts, `\note` became `notes`, and no
coordinate was written anywhere.

## 8. Troubleshooting

| It says | It means | Do this |
| --- | --- | --- |
| `latex-syntax … remove them` | `$` inside an `equation` field | Delete the delimiters |
| `latex-syntax … unbalanced braces` | A `{` never closed, usually from a truncated copy | Re-read the source formula |
| `text-overflow` (error) | The frame has more words than a slide holds | Split it, or cut — never shrink |
| `missing-image` | The path is wrong, or the picture was never rendered | Run the script (§3), use the path it printed |
| `math-in-text` | `$…$` left in a bullet | §5.4 |
| `tiny-type` | Something set a font size by hand | Remove it; the layout decides |
| `title-drift` | A raw element is impersonating a title | Use `title`, not a hand-placed text box |
| `the picture did not compile` | Missing package or macro for that picture | Read the printed error; add the package to the source preamble |
