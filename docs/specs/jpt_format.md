# The `.jpt` presentation format — normative specification

Status: implemented and shipping. Format version **1**. The editor that reads
and writes it is described in §2.18 of
[PROJECT_DESIGN.md](../../PROJECT_DESIGN.md); this file specifies the *file*.

## 0. How to use this document

This is written for whoever adds the next feature to the presentation editor —
in practice, an AI agent working from a one-line request like "add tables to
slides" or "let a slide have a layout". It exists so that such a change lands
as an extension of a format rather than as a mutation of one, and so that the
same questions are not answered differently twice.

Read in this order:

| If you are… | Read |
| --- | --- |
| Adding a field to an existing element | §6, §12.1 |
| Adding a whole element type | §6, §12.2, and the `equation` type as the worked precedent |
| Adding a slide-level or deck-level field | §6, §12.3 |
| Writing a deck **from an agent** | §17 — do not hand-write geometry |
| Writing a tool or agent that edits a deck | §7, §9, §13, §17 |
| Deciding whether something is a format change at all | §11, §12.4 |

The normative sources are
[`gui_src/src/slides/model.js`](../../gui_src/src/slides/model.js) for the deck
model and [`gilzabots/jpt/package.py`](../../gilzabots/jpt/package.py) for its
on-disk container. Where this document and those files disagree, the code is
right and this document is a bug. Every behaviour stated below is covered by
the suites in §14.

## 1. Conformance language

**MUST**, **MUST NOT**, **SHOULD** and **MAY** are used in the RFC 2119 sense.
"Reader" means anything that parses a `.jpt`; "writer" means anything that
emits one. The editor is both.

## 2. File identity

| Property | Value |
| --- | --- |
| Extension | `.jpt` — one suffix, not `.deck.json`. It is what routes the file to the deck editor, and what makes a presentation name, sort, filter and round-trip through a file dialog like every other document. |
| Encoding | Binary ZIP/ZIP64 container. `deck.json` is UTF-8 without BOM. |
| Media type | `application/vnd.gilzabots.presentation+zip`, repeated verbatim in the uncompressed `mimetype` member. |
| Syntax | The package layout in §2.1, containing one deck object in `deck.json`. A `.jpt` is *not* a generic ZIP or JSON document: it opens on a canvas, never in Monaco. |
| Syntax highlighting | Surfaces that expose the logical `deck.json` (a diff, checkpoint preview or agent read) map `jpt` to the JSON grammar. |
| JSON trailing newline | Exactly one. `serializeDeck` appends `\n`. |
| JSON indentation | Two spaces (`JSON.stringify(deck, null, 2)`). |

### 2.1 Package layout

```text
mimetype                         stored, ASCII media type
assets/<sha256>.<extension>      stored, zero or more image/video payloads
deck.json                        deflated, canonical deck JSON
```

An internal binary is addressed from the deck as
`jpt:assets/<sha256>.<extension>`. The digest is lowercase SHA-256 of the
uncompressed bytes, so the same asset referenced ten times is present once.
Image sources, video sources and posters, and theme/slide background images all
participate. Assets use ZIP's **stored** method: PNG, JPEG and video are already
compressed, and storing them lets `/api/jpt/asset` seek to a video byte range
without inflating everything before it. `deck.json` is last so a small text edit
does not shift the large member data that precedes it.

A conforming writer MUST:

- replace every `data:` URI and project-local path in an asset-bearing field
  with an internal `jpt:` reference;
- abort atomically when a local asset is missing, escapes the project root, or
  an internal reference has no member;
- use fixed member timestamps, stable member order and canonical JSON, so the
  same logical input produces the same bytes;
- reject duplicate, encrypted, absolute or traversal member names; and
- preserve safe members in unknown namespaces while treating `mimetype`,
  `deck.json` and `assets/` as reserved.

HTTP(S) player URLs such as YouTube and Vimeo identify network services rather
than files and remain URLs. Such playback necessarily requires that service;
all local file dependencies are inside the JPT.

Readers MUST accept the legacy serialization in which the whole `.jpt` is
plain UTF-8 JSON. Writers always emit the package form, so the first save
upgrades a legacy deck atomically. An empty legacy file is a creation-state
input and becomes a packaged default deck when written; packaged files always
contain `deck.json`.

## 3. Document model

```
deck
├── version, title, width, height        scalars
├── theme                                background, color, accent, fontFamily
└── slides[]                             at least one
    ├── id, background, notes
    └── elements[]                       paint order = array order
        ├── id, type, x, y, w, h, rotation, opacity     (every element)
        └── …type-specific payload…                     (§4)
```

### 3.1 Deck

| Key | Type | Meaning |
| --- | --- | --- |
| `version` | integer ≥ 1 | Format version. Currently always `1`; see §12.4 before changing it. Preserved verbatim, and nothing branches on it yet. |
| `title` | string | Deck title. Used for the PPTX title, the `<title>` of an HTML/PDF export, and the default export file name. |
| `width`, `height` | number > 0 | The slide size in deck units (§5). Default `1280 × 720`. A deck that changes them is honoured everywhere, including the PPTX layout and the PDF `@page`; nothing assumes 16:9. |
| `theme` | object | Deck-wide defaults, below. |
| `slides` | array, ≥ 1 | The slides, in presentation order. |

### 3.2 Theme

| Key | Type | Meaning |
| --- | --- | --- |
| `background` | CSS colour | Slide background where the slide does not override it. |
| `color` | CSS colour | Default foreground for text and equations. |
| `backgroundImage` | string | A picture drawn over `background` on every slide that does not override it. Same sources as an image element's `src`. |
| `backgroundFit` | `cover` \| `contain` \| `fill` | How that picture fills the slide. Default `cover`. |
| `backgroundOpacity` | number 0–1 | The picture's opacity over the colour — how a photograph is dimmed enough to read text over it. |
| `accent` | CSS colour | The default colour of the header and footer bands. |
| `headerHeight` | number ≥ 0 | Height of the band drawn **behind a slide title**; `0` (the default) means none. The band appears only on slides that have a title, because the band *is* the title's background — which is what keeps a cover or a section divider from carrying an empty coloured bar. |
| `headerColor` | colour or `null` | `null` uses `accent`. |
| `titleColor` | colour or `null` | The colour of text whose `role` is `title`. `null` uses `color`. |
| `footerHeight` | number ≥ 0 | Height of the footer band; `0` means none. Drawn on every slide. |
| `footerColor`, `footerTextColor` | colour or `null` | |
| `footerText` | `''` \| `'title'` | `'title'` puts the deck title on the left of the footer band and the slide number on the right, the way Beamer's footline does. |
| `fontFamily` | CSS font stack | Default text font. The first family is what the PPTX export names. |

Unknown theme keys survive a round-trip (§6, I2).

### 3.3 Slide

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | non-empty string | Stable identity. Referenced by every operation in §9. |
| `background` | CSS colour or `null` | `null` inherits `theme.background`. |
| `backgroundImage` | string or `null` | **Three states**: `""` (the default) inherits the theme's picture, `null` is an explicit *no picture* that overrides it, and a string is this slide's own. The third state is what lets a deck with a themed photograph keep one plain slide for a dense table. |
| `backgroundFit` | `cover` \| `contain` \| `fill` | Read only when the slide has its own picture; otherwise the theme's. |
| `backgroundOpacity` | number 0–1 | Likewise. |
| `notes` | string | Speaker notes. Exported to PPTX notes; never drawn on the slide. |
| `elements` | array | Paint order: **later elements are drawn on top**. Z-order is array order and nothing else — there is no `z` field, and adding one would create two sources of truth. |

### 3.4 Element — fields every type carries

| Key | Type | Meaning |
| --- | --- | --- |
| `id` | non-empty string | Unique within its slide. Writers SHOULD make it unique within the deck. |
| `type` | `text` \| `equation` \| `image` \| `video` \| `shape` | Selects the payload and the renderer. |
| `x`, `y` | number | Top-left corner in deck units, before rotation. |
| `w`, `h` | number ≥ 1 | Size in deck units, before rotation. |
| `rotation` | number | Degrees clockwise about the box centre, CSS convention. |
| `opacity` | number 0–1 | Applied to the whole element. |

## 4. Element payloads

### 4.1 `text`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `role` | `null` \| `'title'` | `null` | What this text *is*, when it is more than text. A title takes `theme.titleColor` and is what a header band is drawn behind. Without a role a renderer would have to guess which box is the title from its weight and position, and it would guess wrong on the first deck that puts a caption at the top. |
| `text` | string | `''` | Plain text. `\n` is a hard line break; the box wraps on width. A **leading tab is a nesting level** (§4.1.1), and nothing else in the string has meaning. No markup, and no per-run formatting — formatting is per box (§13). |
| `bullet` | `null` \| `'disc'` \| `'dash'` \| `'number'` | `null` | The list marker drawn in front of every non-empty line (§4.1.1). |
| `fontSize` | number | `28` | Deck units, which equal CSS pixels at 100% zoom. |
| `fontFamily` | string \| `null` | `null` | `null` inherits `theme.fontFamily`. |
| `color` | colour \| `null` | `null` | `null` inherits `theme.color`. |
| `bold`, `italic`, `underline` | boolean | `false` | |
| `align` | `left` \| `center` \| `right` | `left` | |
| `valign` | `top` \| `middle` \| `bottom` | `top` | Real vertical alignment inside the box. |
| `lineHeight` | number | `1.3` | Multiple of the font size. |

#### 4.1.1 Lists

A bulleted list is **one text box**, not one box per item: a browser wraps a
paragraph better than an author places one, and a list split across five
elements is five things to realign every time the words change.

Two fields carry it, and both are read through `textLinesOf()` in `model.js` —
never worked out again by a renderer (I7b):

- **`bullet` is the style, and it belongs to the box.** Formatting is per box
  (§13), and a marker is formatting. `disc` walks `• ◦ ▪` down the levels, the
  way PowerPoint and Beamer both do; `dash` stays `–` at every depth, because a
  dashed list is a deliberately flat look; `number` walks `1. / a. / i.`,
  counting per level and restarting a sub-list each time its parent moves on.
- **A leading tab is a nesting level**, capped at `MAX_BULLET_LEVEL` (4, five
  levels — PowerPoint's depth). One string still holds the content, so there is
  no second representation to keep in sync (I4); a tab is what the user's Tab
  key means, what a diff shows legibly, and what an agent writes without being
  taught a new shape.

Consequences a reader has to honour:

| Rule | Why |
| --- | --- |
| **An empty line gets no marker** and does not consume a number. | An empty paragraph in a list is a gap the author left, not an item they forgot to write. PowerPoint does the same. |
| A line is laid out with `padding-left: indent × level + gutter` and `text-indent: −gutter`, the marker drawn in a box exactly `gutter` wide. | That is a hanging indent: a wrapped second line lands under the first *word*, not under the bullet. `bulletMetricsOf()` is the only source of the two lengths, which are `1.5em` and `1.1em` (`1.7em` for numbers, because "10." is not the width of a bullet). |
| **The marker sets `text-indent: 0` on itself.** | `text-indent` is inherited and an `inline-block` is a block container, so the marker would apply the line's negative indent a second time to its own glyph and draw it a whole marker-column outside the box — where `overflow: hidden` clips it. The list then renders with its text correctly indented and no markers at all, which is how it first shipped. Every renderer sets it **inline**, because an export carries no stylesheet. |
| A box with **no** style still indents by its levels. | Tab has to mean something in a plain text box too. The level is changed by Tab / Shift+Tab and Alt+Shift+arrows with the caret in the box, and by the panel's indent commands — which move every line when nothing is being typed (`model.js: indentText`). |
| The marker is **never** in `text`. | A marker typed into the string cannot be restyled, cannot nest, is a character the caret can land inside, and reaches PowerPoint as text the recipient has to delete. Decks written before this field carry theirs that way; the editor offers to strip them when a style is switched on (`stripListMarkers`), as an edit the user makes and can undo — never a repair applied on open (I1). |

The estimator on the authoring side must subtract the same insets before it
asks whether the words fit (`metrics.wrap_lines(..., insets=…)`), or it sizes a
list against width the words do not have.

### 4.2 `equation`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `latex` | string | `''` | **The source, never a rendered picture of it.** KaTeX-compatible LaTeX, without `$` delimiters. |
| `displayMode` | boolean | `true` | KaTeX's own flag: display style puts the limits of a sum above and below, inline style beside. |
| `fontSize` | number | `40` | The size of the rendered mathematics, exactly (see §10.1). |
| `color` | colour \| `null` | `null` | `null` inherits `theme.color`. |

An equation's `w`/`h` are **derived**, not authored: the editor measures the
rendered formula and fits the box to it, and a resize gesture becomes a change
of `fontSize`. A writer that sets `w`/`h` by hand is not wrong — the renderer
centres the formula in whatever box it is given — but the next edit in the
editor will re-fit them. An empty `latex` draws nothing on a projected slide or
in an export, and a dashed prompt in the editor.

### 4.3 `image`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `src` | string | `''` | Canonically, a `jpt:assets/…` member or an `http(s):` URL. Readers also accept a `data:` URI or project-relative path as migration/editing input; the package writer internalizes it on save (§17.4). |
| `alt` | string | `''` | |
| `fit` | `contain` \| `cover` \| `fill` | `contain` | CSS `object-fit`. |

### 4.4 `video`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `src` | non-empty string | — | Either a link to a video *page* — YouTube or Vimeo — or a video *file*: canonically a `jpt:assets/…` member; readers also accept `data:`, `http(s):` and project paths as inputs. Local files are internalized on save. |
| `poster` | string | `''` | A still shown wherever the video cannot play. `''` means none, and the surfaces draw a placeholder instead. |
| `alt` | string | `''` | |
| `fit` | `contain` \| `cover` \| `fill` | `contain` | CSS `object-fit`, applied to the poster and to a file player. An embedded provider's player fills its box; the provider letterboxes inside it. |
| `autoplay` | boolean | `false` | Start **when the slide is shown in presentation mode**, never on the editing canvas. |
| `loop` | boolean | `false` | |
| `muted` | boolean | `false` | |
| `controls` | boolean | `true` | The one switch that is on by default: a presenter who cannot pause a video has lost the slide. |
| `start` | number ≥ 0 | `0` | Seconds into the video to begin at. |

Which of the two kinds a `src` is, is **derived from the string, never stored**.
`videoSourceOf()` in `video.js` and `video_source_of()` in `model.py` are the
one place that decides, for the same reason `arrowsOf()` and `borderOf()` are:
five surfaces — canvas, thumbnails, presentation mode, and the HTML and PPTX
exports — must not disagree about whether a slide holds a player or a `<video>`.
A second stored field could contradict the URL beside it; a derived one cannot.

The deck never stores a provider's *embed* URL, only what the user pasted.
Providers change their embed paths, and a deck written today should still play
in three years — it will if what it kept is the address of the video rather than
the address of a player. `videoEmbedUrl()` builds the player address on demand,
and is where the playback fields become query parameters.

`autoplay` implies muted playback wherever it is honoured. Every browser blocks
a video that starts with sound, so an unmuted autoplay is not a louder slide,
it is a slide whose video never starts. The model keeps `muted` meaning what
the author chose; the renderers mute an autoplaying video regardless, and the
linter says so.

Nothing plays on the editing canvas. A live `<iframe>` takes every pointer
event that reaches it, so a video that played there would be an element the
author could no longer select, drag or resize — the same reason Google Slides
and Keynote show a still while editing.

### 4.5 `shape`

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `shape` | `rect` \| `ellipse` \| `triangle` \| `line` \| `arrow` | `rect` | `arrow` is legacy and means "a line with a head at the end"; it is read through `arrowsOf()`, never rewritten (§6, I1). |
| `fill` | colour | `#2f6fb3` | On a line, the fallback stroke colour. |
| `stroke` | colour \| `null` | `null` | Border colour on a filled shape; the line's own colour on a line. |
| `strokeWidth` | number ≥ 0 | `0` | `0` means no border at all. A border needs **both** a width and a colour — ask `borderOf()`, never the raw fields. |
| `radius` | number ≥ 0 | `0` | Corner radius, `rect` only. |
| `arrowStart`, `arrowEnd` | boolean | `false` | Heads are attributes of a line, so one element covers a rule, an arrow and a double-headed arrow. Ask `arrowsOf()`. |

Borders are drawn **inside** the box, so adding one never grows a shape.

## 5. The coordinate system

Deck units are logical pixels: `1280 × 720` by default, 16:9, integral
coordinates precise enough that nothing needs sub-pixel rounding. Every
position, size and font size in the document is in deck units. The canvas,
the thumbnail rail, presentation mode and the exports each apply **one** scale
to the whole slide — which is what keeps a thumbnail proportional to a
projected slide.

Two conversions exist outside the document and MUST NOT leak into it:

- the canvas fit scale (`deck units → CSS pixels`), and
- the app's accessibility zoom (`CSS pixels → viewport pixels`, see
  `utils/uiScale.js`).

Any new code that reads `clientX`/`clientY` or `getBoundingClientRect()` and
writes the result into the model MUST pass it through `viewportPxToApp` and
divide by the canvas scale first. Three shipped defects came from skipping
exactly that.

## 6. Invariants

These are the rules a change may not break. Each is covered by a test (§14).

- **I1 — Exact logical and deterministic physical round-trip.** Parsing
  `deck.json` and serializing it straight back MUST reproduce its bytes exactly.
  Rewriting an already-canonical package with no logical change MUST reproduce
  the package bytes exactly. A corollary: legacy model spellings are *read*
  through a helper (`arrowsOf`, `borderOf`, `isLineShape`) and never normalized
  away at parse time. The one intentional migration is a legacy plain-JSON JPT
  or a noncanonical local/data asset reference becoming a package reference on
  save.
- **I2 — Unknown keys survive.** A deck, slide, theme or element key this build
  does not know MUST round-trip unchanged. This is what lets a newer build, or
  an agent, set a field without an older build destroying it.
- **I3 — Stable key order.** `serializeDeck` writes known keys in the fixed
  order declared by `DECK_KEY_ORDER`, `SLIDE_KEY_ORDER` and
  `ELEMENT_KEY_ORDER`, then anything else in insertion order. New keys MUST be
  added to the relevant list, or they sort after everything and diffs get
  noisy.
- **I4 — `deck.json` is the model source of truth.** Binary package members are
  opaque payloads named by its asset references, not a second model. There is
  no derived slide representation to keep in sync: parsing the logical model is
  `JSON.parse`. Nothing may introduce a rendered cache, denormalized index, or
  a `z` field beside array order.
- **I5 — Repair, never reject.** A structurally odd but parseable deck MUST be
  repaired (§7). Only malformed JSON, and a top-level value that is not an
  object, may throw. A hand-edited file must never lock the user out of their
  own presentation.
- **I6 — Every mutation is a pure operation.** Edits go through the functions
  in §9, which return a new deck with structural sharing and never mutate in
  place. Undo/redo is a stack of deck references, and React re-renders on
  identity.
- **I7 — One renderer.** `SlideElementView.jsx` is the only place that turns
  model fields into pixels for the editing canvas, the thumbnail rail and
  presentation mode. A surface may not grow its own renderer.
- **I7b — One resolver per composite field.** Where a value is assembled from
  several fields and a fallback — a border (`borderOf`), a line's arrowheads
  (`arrowsOf`), a slide's background (`backgroundOf`) — exactly one function
  computes it and every surface asks that function. Five renderers each doing
  their own `slide.background || theme.background` is five places for them to
  disagree, and eventually they do.
- **I8 — The source is stored, not the render.** An equation stores LaTeX, an
  image stores its bytes or its path, a shape stores its geometry. Nothing
  stores a rendered picture of something the deck can describe: the source is
  what stays correctable, readable by an agent, and re-renderable by a newer
  library.

## 7. Reading a deck

The backend container reader first extracts `deck.json` (or passes through a
legacy plain-JSON file) without reading packaged media into memory.
`parseDeck(text)` then throws only on malformed JSON, or when the top-level
value is not a JSON object (message: `deck file must contain a JSON object`).
Everything else is repaired. The table is the measured behaviour, and a reader
that is not the editor SHOULD reproduce it:

| Input | Result |
| --- | --- |
| `{}` | A valid deck: version `1`, title `Untitled presentation`, `1280 × 720`, the default theme, one empty slide. |
| `slides` missing, empty, or not an array | One empty slide. |
| An element that is not an object | Dropped. |
| Unknown `type` | Degraded to `text`; the element's own keys survive (see §12.5 — this is the one forward-compatibility limit). |
| Unknown `shape` | `rect`. |
| Missing or non-numeric `x`/`y`/`rotation` | `0`. |
| `w`/`h` missing, non-numeric, zero or negative | Clamped to ≥ 1 (fallbacks 100 and 40). |
| `opacity` outside 0–1 | Clamped. |
| Missing `id` | A fresh generated id. Duplicate ids are **kept as they are**: a writer that emits duplicates gets undefined selection behaviour. |
| Non-string `text` / `latex` | Coerced with `String()`. |
| Non-string `notes` | `''`. |
| Non-numeric equation `fontSize` | `40`. |
| Non-numeric `version` | `1`. Any other integer is preserved. |

## 8. Writing a deck

`serializeDeck(deck)` is the only sanctioned writer of the logical JSON. It
reorders keys (I3), pretty-prints with two spaces, and appends one newline.
`write_packaged_jpt()` is the sanctioned physical writer: it internalizes
assets and writes the verified package to a temporary file before replacing
the destination. A writer that is not the editor MUST produce `deck.json` that satisfies
[`jpt.schema.json`](jpt.schema.json), which describes what a conforming writer
emits for format version 1. (The schema is deliberately stricter than the
reader: the reader repairs, the writer has no excuse.)

Validate with any Draft 2020-12 validator, for example:

```bash
unzip -p deck.jpt deck.json | python3 -c "import sys,json,jsonschema;jsonschema.Draft202012Validator(
  json.load(open('docs/specs/jpt.schema.json'))).validate(json.load(sys.stdin))"
```

## 9. The operation vocabulary

Every edit is one of these, and they are deliberately the vocabulary an agent
will be given: one validated, diffable entry point per edit rather than
free-form mutation of the JSON.

| Group | Operations |
| --- | --- |
| Slides | `addSlide`, `deleteSlide`, `duplicateSlide`, `moveSlide`, `setSlideNotes`, `setSlideBackground` |
| Elements | `addElement`, `addElements`, `updateElement`, `deleteElement`, `reorderElement` |
| Construction | `createDeck`, `createSlide`, `createElement`, `newId` |
| Clipboard | `serializeClipboard`, `parseClipboard`, `cloneElements`, `pastePlacement`, `boundsOf` |
| Reading | `findSlide`, `findElement`, `arrowsOf`, `borderOf`, `isLineShape` |

Rules for adding one:

1. It MUST be pure: take a deck, return a new deck, mutate nothing.
2. It MUST return **the same deck object** when its target does not exist, so
   callers can compare by identity to know whether anything changed.
3. The plural form is the primitive when a group must land together
   (`addElements`, not a loop over `addElement`): one edit, one history entry,
   one re-serialization.
4. It goes in `model.js`, not in a component. A mutation reachable only through
   the UI is one an agent cannot perform.

`deleteSlide` on the last remaining slide empties it instead of removing it: a
deck with no slides has no canvas to draw.

## 10. The rendering contract

One model, five surfaces, and they must agree: the editing canvas
(`SlideCanvas.jsx`), the thumbnail rail and presentation mode (both via
`SlideView`), the standalone HTML export, and the printed PDF. The first three
share `SlideElementView.jsx` (I7); the two exports re-implement drawing in
`export.js`, because a self-contained file cannot ship React — which makes
`export.js` the one place a change to a renderer must be mirrored, and the
reason §12.2's checklist names it twice.

### 10.1 Sizes must mean the same thing everywhere

`fontSize` is a size in deck units and MUST render at exactly that size on
every surface. The live trap: `katex.min.css`, loaded once for the whole IDE,
sizes `.katex` at `1.21em`, so a formula would be a fifth larger in the editor
than in an export that carries no stylesheet. `.deck-equation .katex {
font-size: 1em }` neutralizes it in the app, and the exports emit the same rule.
Any future element that borrows a global stylesheet inherits this problem and
MUST solve it the same way.

Ordinary text may contain Unicode mathematical characters without becoming an
`equation` element. Every CSS renderer MUST pass its selected prose stack
through `fontFamilyWithMathFallback`: the prose face remains first, while STIX
Two Math and platform math faces sit before the final generic family. This is
what makes combining accents and operators such as `n⃗` and `−` render as
characters rather than missing-glyph rectangles. The PDF wrapper MUST declare
the bundled STIX face and wait for `document.fonts.ready` before printing.

### 10.2 Editing chrome

Handles, guides, outlines and inline fields are drawn *inside* the scaled
canvas and multiply their lengths by `--deck-chrome` (`1/scale`) so they keep a
constant on-screen size. They carry explicit colours rather than `--vscode-*`
tokens: this chrome sits on the slide, which is white whatever the IDE theme
is. New chrome MUST follow both rules.

### 10.3 Live editing

An in-progress edit is **local state, not a deck edit**: a drag renders a draft
rect and commits once on pointer up; a formula being typed renders from a draft
string and reaches the deck on Enter, Escape or blur. Committing per frame or
per keystroke would re-serialize the whole document dozens of times a second
and put one undo entry per pixel. Any new interactive element type MUST follow
this shape.

Two consequences an editing surface has to answer for, and a text box does:

- **The surface owns its DOM while it is open.** React reconciles against the
  last thing it rendered, which during an edit is the *committed* text — so a
  deck change while a box is open (a style toggle from the panel) would rewrite
  the box back to that text and lose the edit. The open box is filled by
  `lines.js: renderLines` and carries no React children; only markers and
  indents are re-applied when the box's style changes, never the words.
- **Undo inside the surface is the surface's own** (`textHistory.js`). The
  deck's history has one entry per finished edit, and the browser's native
  `contenteditable` stack does not survive the script mutations this editor
  makes on every keystroke. A text box keeps snapshots of its string and caret,
  coalescing runs of typing and breaking on a pause or a boundary, and **hands
  the keystroke on when it runs out**: it commits, closes, and the deck's undo
  takes the change before it, so Ctrl+Z never dead-ends.

## 11. Export obligations

A new element type is not finished until it has an answer for each of these,
and "it silently disappears" is not one of them.

| Target | Obligation |
| --- | --- |
| HTML (`deckToHtml`) | Draw it with inline CSS/SVG/MathML only. No external stylesheet, no font file, no script. Anything the element loads by URL must be reachable from `inlineDeckAssets`, which fetches every project reference and embeds it before the export is written — a new field naming a file belongs in that walk, or the exported deck breaks the moment it is sent to someone. |
| PDF (`exportPdf`) | Same element markup as HTML, printed from a hidden iframe, with `live` false — the page is paper, so anything that plays or animates MUST have a still. The wrapper loads the editor's web-font stylesheet and may reference same-origin assets (the math font's `@font-face` does). In the desktop host, success means `pdfPrintingFinished` reported a completed write; closing or merely opening a dialog MUST NOT be reported as success. |
| PPTX (`exportPptx`) | Map it to a real PowerPoint object where one exists. Where none exists, export the **source** in the box the element occupied and record the gap in §2.18 of PROJECT_DESIGN.md. Never rasterize something the deck stores as source (I8). |

What each type answers today:

| Type | HTML | PDF | PPTX |
| --- | --- | --- | --- |
| `text`, `shape`, `image` | inline CSS/SVG | same | text box, autoshape, picture |
| `equation` | MathML, laid out by the reading browser | same | **OMML** — PowerPoint's own equation object, converted from the deck's LaTeX by way of KaTeX's MathML (`omml.js`). The shape is written inside an `mc:AlternateContent`, so a reader that understands the extension gets an editable formula and one that does not gets the LaTeX source. LaTeX that KaTeX cannot parse, and MathML the converter has no mapping for, fall back to the source text rather than to a guess. |
| `video` | the real player: an `<iframe>` for a provider, a `<video>` for a file | the poster, or a plate naming the video, wrapped in a link to it — a printed deck outlives the room it was shown in, and a reader who cannot play it should still be able to reach it | a real media object: `type: 'online'` for a provider, an embedded media part for a file. **Known gap**: the cover PowerPoint shows before playback must be a PNG, so a poster in any other format is omitted and PowerPoint draws its own play plate. |

## 12. Extending the format

### 12.1 Adding a field to an existing element type

1. Add it to that type's `*_DEFAULTS` in `model.js`. The default MUST reproduce
   today's behaviour, so old decks keep rendering as they do now.
2. Add the key to `ELEMENT_KEY_ORDER` (I3), beside its siblings.
3. Repair it in `normalizeElement` if the renderer cannot cope with it being
   absent or of the wrong type.
4. Read it in `SlideElementView.jsx` **and** in `export.js`'s `elementToHtml`
   and `exportPptx`.
5. Give it a control in `SlideEditor.jsx`'s properties panel, with strings in
   `en.json` and `pt-BR.json`. A field the model can carry and the user cannot
   reach is a bug that hides for months — `slide.background` was exactly that.
6. If it names a file, add it to `inlineDeckAssets` (§11).
6. Add a round-trip test and a rendering assertion (§14).

A field whose default changes existing output is not a new field, it is a
format change — see §12.4.

### 12.2 Adding a new element type

The `equation` type is the worked precedent; read it end to end before
starting. The checklist, in dependency order:

| # | File | Change |
| --- | --- | --- |
| 1 | `model.js` | Add the name to `ELEMENT_TYPES`; add `<TYPE>_DEFAULTS`; return it from `typeDefaults()`; add its keys to `ELEMENT_KEY_ORDER`; add a repair branch to `normalizeElement`. |
| 2 | `<type>.js` (new, optional) | Any pure logic the type needs — rendering to markup, measuring, validation. Keep it DOM-free where it can be, so the pure suite can test it; put the DOM-dependent part behind its own function, as `measureEquation` is. |
| 3 | `SlideElementView.jsx` | A `<Type>Body` component and one line in the dispatcher. This is the **only** place that draws (I7). |
| 4 | `SlideCanvas.jsx` | Only if the type needs an editing gesture of its own. Follow §10.3: draft locally, commit once. |
| 5 | `SlideEditor.jsx` | Toolbar button, insert size, properties group, and any keyboard shortcut. Insertion MUST go through `insertElement` so placement, selection and history stay uniform. |
| 6 | `export.js` | A branch in `elementToHtml` (HTML + PDF) and one in `exportPptx` (§11). |
| 7 | `slides.css` | Styles for the element and any chrome, following §10.2. |
| 8 | `i18n/locales/en.json`, `pt-BR.json` | Every user-visible string. English is the default; no hardcoded UI text. |
| 9 | `__tests__/deck.test.js` | Round-trip, defaults, repair, and the export markup (§14). |
| 10 | `test/browser/run.py` | Whatever only a browser can answer: does the click reach it, does the caret land, does it survive a scale change. |
| 11 | `PROJECT_DESIGN.md` §2.18 | The decisions and the trade-offs, in the same voice as the bullets already there. |
| 12 | This file | §4 payload table, the schema, and §12.5 if the addition changes what older builds do. |

### 12.3 Adding a slide-level or deck-level field

Same shape as §12.1, against `createSlide`/`createDeck`, `normalizeSlide`/
`parseDeck`, and `SLIDE_KEY_ORDER`/`DECK_KEY_ORDER`. Prefer a slide field over
a deck field when a user could plausibly want it to vary per slide, and prefer
a deck field over repeating the same value on every slide.

### 12.4 When to bump `version`

Do not bump it for anything additive. New optional keys, new element types and
new operations are all format version 1, because I2 and the defaults rule mean
an older build still opens the file and a newer build still opens an old one.

Bump it only when a document that is *valid* under version 1 would be
*misread* under the new rules — a key whose meaning changes, a coordinate
system change, a required field with no backward-compatible default. That
change also requires: a migration in `parseDeck` keyed on `version`, a new
schema file beside this one, and a decision — written down — about what an old
build does when it meets the new version.

### 12.5 The one forward-compatibility limit

Unknown *keys* survive (I2), but an unknown element **`type` does not**: this
build degrades it to `text` while keeping its foreign keys, so a deck written
by a newer build and re-saved by an older one loses the type and keeps the
data. That is a deliberate trade — rendering nothing for an unrecognized type
would make the element invisible and undeletable — and it has two consequences
for anyone shipping a new type:

1. Decks using it SHOULD not be expected to survive a round trip through an
   older build.
2. If a future change makes unknown types survive (an `unknown` passthrough
   element that renders a placeholder), that is worth doing *before* the next
   type is added, not after.

## 13. Deliberately not in the format

Each of these was considered and left out. Re-proposing one is fine; doing it
without saying so is not.

| Not in the format | Why |
| --- | --- |
| Rich text runs (mixed bold inside one box) | Formatting is per box. Per-run formatting means a text model, a caret model, and a serializer for both — a real feature, not a field. Lists (§4.1.1) are the deliberate exception in shape but not in kind: what varies there is per *line*, and a line's level is one character of the string the box already holds. |
| A `z` field | Z-order is array order (§3.3). Two sources of truth for one thing is how they disagree. |
| Rendered output of any kind (SVG of an equation, a rasterized shape) | I8. It goes stale against the source, and it bloats a file that is meant to stay diffable. |
| Slide layouts / masters / theme editor | Not implemented. When it arrives it belongs at deck level with a per-slide override, following §12.3. |
| `.pptx` import | Export only. Import is a parser for a format an order of magnitude larger than this one. |
| Multi-element selection in the model | The clipboard format is already a *list* and the geometry already has the marquee hit-test; what is missing is editor state, not format. |
| Animations, transitions, per-element timing | No representation, and no renderer that could honour them in three surfaces plus two exports. |

## 14. Test obligations

A change to the format is not done until each of these is green. They are cheap
and they are the reason the invariants in §6 are still true.

```bash
cd gui_src
npm run test:slides     # node --test, no browser: the model, geometry, exports
npm run test:browser    # headless Chrome over CDP, two interface scales
python -m pytest        # from the repository root: the backend, unaffected but not optional
```

What belongs where:

- **`src/slides/__tests__/deck.test.js`** — anything that can be made pure:
  round-trip byte-exactness including unknown keys, the defaults, every repair
  rule in §7, the invariants of each operation, geometry, clipboard payloads,
  and the *markup* an export produces (`deckToHtml` is a pure function of the
  deck).
- **`tests/test_jpt_package.py` and `tests/test_jpt_api.py`** — deterministic
  container layout, content-addressed assets, atomic failure, traversal
  rejection, legacy migration, the logical file API, and video byte ranges.
- **`test/browser/run.py`** — what only a real browser can answer: whether a
  click reaches an element, whether focus survives the press that opened an
  editor, whether a box fitted to measured content is actually the size of that
  content, whether anything is clipped at a different interface scale. Use real
  input (`Input.dispatchMouseEvent`), never synthetic `dispatchEvent`: a test
  that invents its own events proves nothing.

## 15. A complete deck

Verbatim `deck.json` output of `serializeDeck`, showing all five element types.
This is the logical model a conforming package writer includes.

```json
{
  "version": 1,
  "title": "Gauss integral",
  "width": 1280,
  "height": 720,
  "theme": {
    "background": "#ffffff",
    "color": "#1a1a1a",
    "accent": "#2f6fb3",
    "fontFamily": "Inter, Segoe UI, system-ui, sans-serif"
  },
  "slides": [
    {
      "id": "s_intro",
      "background": null,
      "notes": "Say the punchline slowly.",
      "elements": [
        {
          "id": "t_title",
          "type": "text",
          "x": 120,
          "y": 240,
          "w": 1040,
          "h": 120,
          "rotation": 0,
          "opacity": 1,
          "text": "Gauss integral",
          "role": "title",
          "bullet": null,
          "fontSize": 64,
          "fontFamily": null,
          "color": null,
          "bold": true,
          "italic": false,
          "underline": false,
          "align": "center",
          "valign": "top",
          "lineHeight": 1.3
        },
        {
          "id": "q_gauss",
          "type": "equation",
          "x": 240,
          "y": 430,
          "w": 800,
          "h": 150,
          "rotation": 0,
          "opacity": 1,
          "latex": "\\int_{-\\infty}^{\\infty} e^{-x^2}\\,dx = \\sqrt{\\pi}",
          "displayMode": true,
          "fontSize": 56,
          "color": null
        },
        {
          "id": "r_rule",
          "type": "shape",
          "x": 240,
          "y": 600,
          "w": 800,
          "h": 24,
          "rotation": 0,
          "opacity": 1,
          "shape": "line",
          "fill": "#2f6fb3",
          "stroke": null,
          "strokeWidth": 4,
          "radius": 0,
          "arrowStart": false,
          "arrowEnd": true
        },
        {
          "id": "i_plot",
          "type": "image",
          "x": 980,
          "y": 120,
          "w": 220,
          "h": 160,
          "rotation": 0,
          "opacity": 1,
          "src": "figures/bell.png",
          "alt": "Bell curve",
          "fit": "contain"
        }
      ]
    }
  ]
}
```

## 16. The clipboard envelope

Not part of the file format, but the same objects travel through it, so it is
specified here. Copied elements are written to the **system** clipboard — not
to a variable inside the editor — as a tagged JSON envelope, which is what lets
a diagram copied in one window paste into another:

```json
{
  "kind": "gilzabots.slides.elements",
  "version": 1,
  "elements": []
}
```

`elements` holds the copied elements themselves, each serialized in
`ELEMENT_KEY_ORDER` exactly as it appears in a file.

`parseClipboard` returns `null` for text that is not this envelope rather than
coercing it: most of what is on a user's clipboard is ordinary text, and the
caller decides what that becomes. Ids are regenerated at **paste** time by
`cloneElements`, not at copy time, so the same payload can be pasted repeatedly
without the second paste colliding with the first.

## 17. The agent authoring API

`gilzabots/jpt/` is how an agent writes a deck, and §12's rule about extensions
applies to it too: a new element type is not finished until this API can author
it. The layering is deliberate — an agent that picks coordinates produces a
valid file and an unusable presentation, so it does not get to pick them.

| Layer | Module | What it owns |
| --- | --- | --- |
| Intent → geometry | `layout.py` | The grid, the ten layouts, auto-fitted type |
| The check | `lint.py` | Every defect an audience would notice |
| The format | `model.py` | Construction, strict validation, byte-exact `serialize()` |
| The package | `package.py` | Atomic ZIP writing, internal asset addressing, legacy reading |
| Estimation | `metrics.py` | Text and formula sizes, without a browser |

### 17.1 The three tools

| Tool | Safety | What it does |
| --- | --- | --- |
| `create_presentation(path, outline_json, title="")` | write | Compiles an outline, lints it, writes it only if there are no **errors**. |
| `edit_presentation(path, operations_json)` | write | Applies §9 operations to an existing deck, lints, writes. A failed operation writes nothing. |
| `set_presentation_theme(path, theme, fields_json)` | write | Applies a theme from the Asset Store by name, and/or explicit theme fields. |
| `check_presentation(path)` | safe | Lints a deck and reports. Never writes; answers in plan mode. |

A **named theme replaces** the deck's look — every field it does not set returns
to the default — while explicit `fields_json` **merges**, because the first is a
choice of theme and the second is a tweak to one. Applying a theme also marks
each slide's title with `role: 'title'`, which is what lets a theme colour
titles without colouring everything else.

Backgrounds are set from the outline (per slide, or deck-wide through `theme`)
and changed afterwards with `{"op": "set_background", …}`, which takes
`background`, `backgroundImage`, `backgroundFit` and `backgroundOpacity`, and
`"all": true` to write them onto the theme instead of one slide.

### 17.2 The outline

```json
{
  "title": "Deck title",
  "theme": { "background": "#ffffff", "color": "#1a1a1a", "accent": "#2f6fb3" },
  "slide_numbers": false,
  "slides": [
    { "layout": "title", "title": "…", "subtitle": "…" },
    { "layout": "section", "title": "…" },
    { "layout": "bullets", "title": "…", "notes": "…",
      "bullets": ["…", { "text": "a sub-point", "level": 1 }],
      "bulletStyle": "disc" },
    { "layout": "text", "title": "…", "text": "…" },
    { "layout": "equation", "title": "…", "equation": "e^{i\\pi} + 1 = 0", "caption": "…" },
    { "layout": "image", "title": "…", "image": "figures/plot.png", "caption": "…" },
    { "layout": "two_columns", "title": "…",
      "left": { "bullets": ["…"] }, "right": { "equation": "…" } },
    { "layout": "image_text", "title": "…", "image": "…", "bullets": ["…"], "side": "left" },
    { "layout": "quote", "quote": "…", "attribution": "…" },
    { "layout": "blank", "title": "…", "elements": [],
      "background": "#0b1020", "backgroundImage": "figures/photo.jpg",
      "backgroundFit": "cover", "backgroundOpacity": 0.4 }
  ]
}
```

A bare array of slides is accepted in place of the object, because that is the
shape `create_pptx_file` takes and an agent that knows one tool should not have
to relearn the other.

A **block** is what fills a region: an object holding exactly one of `bullets`,
`text`, `equation` or `image`, plus an optional `caption`. `two_columns` takes
one per side; the single-block layouts take theirs at the top level of the
slide, which is why `{"layout": "bullets", "bullets": [...]}` needs no nesting.

A **bullet item** is a string, a string carrying its own leading tabs, or
`{"text": …, "level": 0-4}` — three spellings of one thing, because an author
writing an outline should not have to learn a representation. The block also
takes `"bulletStyle"`: `disc` (the default), `dash` or `number`.

Three rules an author must not break:

1. **Never write `x`, `y`, `w`, `h` or `fontSize`.** They are computed. A slide
   that needs geometry the layouts cannot express uses the `elements` escape
   hatch, and then owns the consequences the linter reports.
2. **`equation` holds LaTeX with no `$` delimiters.** The field is already a
   math context; `$x$` is a syntax error the linter reports as one.
3. **Never type a marker into a bullet.** `"•  Point"` and `"– Sub-point"` are
   what a list looked like before the format had `bullet` (§4.1.1); written now
   they draw a marker in front of a marker. The style draws them, and the level
   nests them.

### 17.3 What the linter checks

| Code | Level | Meaning |
| --- | --- | --- |
| `off-slide` | error | The element is entirely outside the slide. |
| `crosses-edge` | warning | Part of it will be clipped. |
| `text-overflow` | error / warning | The text does not fit its box; an error when it misses by more than 15%. |
| `tiny-type` | error | Below the readable floor (16 units) for a projected slide. |
| `small-type` | warning | Under 20 units. Legible, but not comfortable. |
| `low-contrast` | warning | Under WCAG's 3:1 for large text. Over a background **picture** this is measured rather than assumed: the picture is decoded, the region under the glyphs is sampled (with the same wrapping and alignment the renderer applies), and the finding is raised only when more than 6% of that area is within 3:1 of the ink. |
| `latex-syntax` | error | `$` delimiters, unbalanced braces, mismatched `\begin`/`\end`. |
| `missing-image` | error | No `src`, or a project-relative path that does not exist. |
| `missing-background` | error | The slide's background picture does not exist in the project. |
| `text-over-picture` | warning | The background picture could not be read — a remote URL, or a format this build cannot decode — so legibility is the author's to check. |
| `overlap` | warning | Two content elements share more than 18% of the smaller one. A shape behind text is exempt: that is a card, not a collision. |
| `too-many-bullets`, `wordy` | warning | More than 7 bullets or ~70 words on one slide. |
| `title-drift` | warning | A title sits where most slides do not put theirs. |
| `empty-slide`, `empty-text`, `empty-equation` | warning | A placeholder nobody filled in. |

`create_presentation` refuses to write a deck with any **error** and reports
the findings instead. Warnings are written and reported, because the author is
better placed than the linter to know whether nine bullets are the point.

### 17.4 Local assets are always packaged

A deck the user builds by hand initially holds picked pictures as data URIs; an
agent commonly names `figures/plot.png` or `media/demo.mp4`. Both are editing
inputs, not canonical storage. Every save walks image elements, video files and
posters, and theme/slide backgrounds, copies their bytes into `assets/`, and
replaces the source with a content-addressed `jpt:` URI. Thus a JPT with local
media is one movable file without putting base64 films in the JSON the editor
parses.

- Source files on disk are **left where they are**. They remain inputs the user
  may re-render or edit; the package owns a snapshot of their bytes.
- Identical sources used any number of times become one member. There is no
  4 MB exception: large local video is precisely why assets live as stored ZIP
  members instead of data URIs.
- A missing file, a path outside the project, a `blob:` URL, or a dangling
  `jpt:` member aborts the atomic write. A document promised as self-contained
  cannot silently retain a broken local dependency.
- `"embed_images": false` no longer disables packaging. It is accepted in old
  outlines but has no effect. `{"op": "embed_images"}` remains as an
  idempotent compatibility operation; every save already performs it for all
  supported local media.
- YouTube/Vimeo and other HTTP(S) services stay URLs. They are links to a remote
  player, not local files, and playback therefore still needs the network.

### 17.5 What it deliberately does not do

- **It does not render.** KaTeX and the DOM live on the JS side, so `latex-syntax`
  is shallow (§17.3) and text sizes are estimated (`metrics.py`). Both are
  documented at their call sites, and neither can silently drop content.
- **It does not repair.** Reading is strict: absent keys take their defaults,
  present-but-wrong keys raise. The editor repairs; an author must not.
- **It does not invent content.** No auto-generated summaries, no filler
  bullets, no stock imagery. The outline is what the agent means to say.
