// ─────────────────────────────────────────────────────────────────────────────
// model.js
//
// The deck document model: a plain JSON tree of slides holding absolutely
// positioned elements. Unlike the LaTeX modes, which derive a model from a
// source file they must write back byte-for-byte, here the JSON is the logical
// document. The backend stores it as deck.json beside binary assets in the JPT
// package; this module deliberately knows only the model it edits.
//
// Two properties shape every function below:
//
//   1. Every operation is pure and returns a new deck. Nothing mutates in
//      place, so undo/redo is a stack of deck references and React re-renders
//      on identity.
//   2. Unknown keys survive. A deck written by a newer version of GilzaBots —
//      or by an agent that set a field this build does not know — round-trips
//      unchanged instead of being silently stripped. `serialize` writes known
//      keys in a fixed order and appends the rest, so diffs stay readable.
//
// The operation set is deliberately the vocabulary an agent will be given
// later (addSlide, updateElement, ...): one validated, diffable entry point
// per edit, rather than free-form mutation of the JSON.
// ─────────────────────────────────────────────────────────────────────────────

// Logical deck units. Elements are positioned in these; the canvas scales.
// 1280x720 keeps 16:9 at a size where integer coordinates are precise enough
// that nothing needs sub-pixel rounding.
export const DECK_W = 1280;
export const DECK_H = 720;

export const ELEMENT_TYPES = ['text', 'image', 'shape', 'equation', 'video'];
export const SHAPE_KINDS = ['rect', 'ellipse', 'triangle', 'line', 'arrow'];
export const BULLET_STYLES = ['disc', 'dash', 'number'];

// Five nesting levels, the same depth PowerPoint offers. A cap exists at all
// because the level is written as leading tabs, and a file that arrives with
// forty of them must still lay out inside its box rather than off the slide.
export const MAX_BULLET_LEVEL = 4;

export const BACKGROUND_FITS = ['cover', 'contain', 'fill'];

// A background is a colour with an optional picture over it, and the two are
// separate fields rather than one polymorphic `background`. Three reasons: a
// deck written by this build still opens in an older one, which ignores keys it
// does not know (I2) and draws the colour rather than "[object Object]"; the
// colour is what shows through a picture set to less than full opacity, which
// is how a photograph is dimmed enough to read text over; and every surface
// that already reads `background` keeps reading it unchanged.
// A theme is the deck's whole visual identity, not just what is behind the
// slides: colours, type, an optional picture, and the bands a Beamer-style
// theme draws at the top and the bottom. Every field defaults to what the deck
// looked like before themes existed, so an old deck opens unchanged and a theme
// is something the user chooses rather than something they inherit.
export const DEFAULT_THEME = {
  background: '#ffffff',
  backgroundImage: '',
  backgroundFit: 'cover',
  backgroundOpacity: 1,
  color: '#1a1a1a',
  accent: '#2f6fb3',
  fontFamily: 'Inter, Segoe UI, system-ui, sans-serif',
  // Chrome. `0` means no band at all, which is the default: a deck that never
  // asked for a header must not grow one.
  headerHeight: 0,
  headerColor: null,       // null = the accent
  titleColor: null,        // null = the ordinary text colour
  footerHeight: 0,
  footerColor: null,       // null = the accent
  footerTextColor: '#ffffff',
  // What the footer band says: '' for nothing, 'title' for the deck title on
  // the left and the slide number on the right, the way Beamer's footline does.
  footerText: '',
};

// The title band the grid reserves, in deck units. A header taller than this
// would cover the title it is meant to sit behind.
export const TITLE_BAND_BOTTOM = 180;

// ─── ids ─────────────────────────────────────────────────────────────────────
// Short, collision-resistant, and readable in a JSON diff. Not a UUID: these
// end up in every element of a hand-inspectable file.
let idCounter = 0;
export function newId(prefix = 'e') {
  idCounter += 1;
  const rand = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${rand}${idCounter.toString(36)}`;
}

// ─── defaults ────────────────────────────────────────────────────────────────

const TEXT_DEFAULTS = {
  text: '',
  // What this text *is*, when it is more than text. A title follows the theme's
  // title colour and sits inside the header band; everything else follows the
  // ordinary text colour. Without a role the renderer would have to guess which
  // box is the title from its position and weight, and it would guess wrong on
  // the first deck that puts a caption at the top.
  role: null,              // null | 'title'
  fontSize: 28,
  fontFamily: null,      // null = inherit from the deck theme
  color: null,           // null = inherit
  bold: false,
  italic: false,
  underline: false,
  align: 'left',         // left | center | right
  valign: 'top',         // top | middle | bottom
  lineHeight: 1.3,
  // The list marker drawn in front of every non-empty line, or `null` for
  // none. A property of the *box*, like every other formatting field (§13 of
  // the format spec): what varies per line is the nesting level, and that
  // lives in the text as leading tabs. Storing the marker as a field rather
  // than as characters in the text is what makes a list re-styleable, and what
  // lets an indent mean something to an export instead of being three spaces.
  bullet: null,          // null | 'disc' | 'dash' | 'number'
};

// An equation is stored as the LaTeX the user typed, never as a rendered
// picture: the source is what can be corrected a year later, what an agent can
// read, and what survives a font or renderer change. `displayMode` is KaTeX's
// own flag — display style sets limits above and below a sum where inline style
// puts them beside it, which on a slide is a visible difference, not a nuance.
const EQUATION_DEFAULTS = {
  latex: '',
  displayMode: true,
  fontSize: 40,
  color: null,           // null = inherit from the deck theme
};

const IMAGE_DEFAULTS = {
  src: '',               // project-relative path, resolved by the canvas
  alt: '',
  fit: 'contain',        // contain | cover | fill
};

// A video is one `src` and a handful of playback switches, because that is what
// the user has: a link they copied, or a file in the project. Which of the two
// it is — and therefore whether it becomes a provider's player or a `<video>`
// element — is decided from the string by `videoSourceOf` in video.js rather
// than stored as a second field the two could disagree about.
//
// `poster` is the still shown wherever the video cannot play: the editing
// canvas, the thumbnail rail, and a PDF, which is paper. Without one those
// surfaces show a placeholder that names the video, which is honest but is not
// the frame the author chose.
const VIDEO_DEFAULTS = {
  src: '',
  poster: '',            // '' = no still; the surfaces draw a placeholder
  alt: '',
  fit: 'contain',        // contain | cover | fill, as for an image
  // Playback. `autoplay` means "when this slide is shown in presentation
  // mode", not "when the editor opens the file" — nothing plays on the canvas.
  autoplay: false,
  loop: false,
  muted: false,
  controls: true,
  start: 0,              // seconds into the video to begin at
};

// A filled shape carries its border alongside its fill rather than as a
// separate element, so moving or resizing the shape can never leave an outline
// behind. `strokeWidth: 0` — the default — means no border at all; on a line
// the same two fields are the line's own colour and thickness.
export const DEFAULT_STROKE = '#1a1a1a';
export const DEFAULT_STROKE_WIDTH = 2;

const SHAPE_DEFAULTS = {
  shape: 'rect',
  fill: '#2f6fb3',
  stroke: null,
  strokeWidth: 0,
  radius: 0,
  // Arrowheads are attributes of a line, not a separate kind, so one element
  // covers a plain rule, a single arrow and a double-headed arrow without the
  // user having to swap shapes to change an end.
  arrowStart: false,
  arrowEnd: false,
};

function typeDefaults(type) {
  if (type === 'text') return TEXT_DEFAULTS;
  if (type === 'image') return IMAGE_DEFAULTS;
  if (type === 'shape') return SHAPE_DEFAULTS;
  if (type === 'equation') return EQUATION_DEFAULTS;
  if (type === 'video') return VIDEO_DEFAULTS;
  return {};
}

// Key order used by `serialize`. Geometry first, then type-specific payload:
// a human scanning the file wants to know where a box is before what is in it.
const ELEMENT_KEY_ORDER = [
  'id', 'type', 'x', 'y', 'w', 'h', 'rotation', 'opacity',
  'text', 'role', 'bullet', 'latex', 'displayMode',
  'fontSize', 'fontFamily', 'color', 'bold', 'italic', 'underline',
  'align', 'valign', 'lineHeight',
  'src', 'alt', 'fit',
  'poster', 'autoplay', 'loop', 'muted', 'controls', 'start',
  'shape', 'fill', 'stroke', 'strokeWidth', 'radius', 'arrowStart', 'arrowEnd',
];
const SLIDE_KEY_ORDER = [
  'id', 'background', 'backgroundImage', 'backgroundFit', 'backgroundOpacity',
  'notes', 'elements',
];
const DECK_KEY_ORDER = ['version', 'title', 'width', 'height', 'theme', 'slides'];

// ─── construction ────────────────────────────────────────────────────────────

export function createElement(type, patch = {}) {
  if (!ELEMENT_TYPES.includes(type)) {
    throw new Error(`unknown element type: ${type}`);
  }
  return {
    id: newId(type[0]),
    type,
    x: 100, y: 100, w: 400, h: 120,
    rotation: 0,
    opacity: 1,
    ...typeDefaults(type),
    ...patch,
  };
}

export function createSlide(patch = {}) {
  return {
    id: newId('s'),
    background: null,      // null = inherit from the deck theme
    // '' = inherit from the theme too. A slide that wants *no* picture where
    // the theme has one sets `backgroundImage: null`, which is why the two
    // empty values are distinguished rather than both meaning "unset".
    backgroundImage: '',
    backgroundFit: 'cover',
    backgroundOpacity: 1,
    notes: '',
    elements: [],
    ...patch,
  };
}

// A new deck opens on a title slide, because an empty white rectangle gives
// the user nothing to click and no sense of the canvas size.
export function createDeck(title = 'Untitled presentation') {
  return {
    version: 1,
    title,
    width: DECK_W,
    height: DECK_H,
    theme: { ...DEFAULT_THEME },
    slides: [
      createSlide({
        elements: [
          createElement('text', {
            x: 120, y: 240, w: 1040, h: 120,
            text: title,
            fontSize: 64, bold: true, align: 'center',
          }),
          createElement('text', {
            x: 120, y: 380, w: 1040, h: 60,
            text: '',
            fontSize: 28, align: 'center', color: '#666666',
          }),
        ],
      }),
    ],
  };
}

// ─── parse / serialize ───────────────────────────────────────────────────────

function num(value, fallback) {
  return Number.isFinite(value) ? value : fallback;
}

function normalizeElement(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const type = ELEMENT_TYPES.includes(raw.type) ? raw.type : 'text';
  const base = createElement(type);
  // Spread `raw` last so unknown keys survive, then repair the fields the
  // renderer cannot cope with being absent or non-numeric.
  const el = { ...base, ...raw, type };
  el.id = typeof raw.id === 'string' && raw.id ? raw.id : base.id;
  el.x = num(el.x, 0);
  el.y = num(el.y, 0);
  el.w = Math.max(1, num(el.w, 100));
  el.h = Math.max(1, num(el.h, 40));
  el.rotation = num(el.rotation, 0);
  el.opacity = Math.min(1, Math.max(0, num(el.opacity, 1)));
  if (type === 'shape') {
    if (!SHAPE_KINDS.includes(el.shape)) el.shape = 'rect';
    el.strokeWidth = Math.max(0, num(el.strokeWidth, 0));
  }
  if (type === 'text') {
    if (typeof el.text !== 'string') el.text = String(el.text ?? '');
    if (el.role !== 'title') el.role = null;
    if (!BULLET_STYLES.includes(el.bullet)) el.bullet = null;
  }
  if (type === 'equation') {
    if (typeof el.latex !== 'string') el.latex = String(el.latex ?? '');
    el.displayMode = el.displayMode !== false;
    el.fontSize = Math.max(1, num(el.fontSize, EQUATION_DEFAULTS.fontSize));
  }
  if (type === 'image' || type === 'video') {
    if (typeof el.src !== 'string') el.src = '';
    if (!BACKGROUND_FITS.includes(el.fit)) el.fit = 'contain';
  }
  if (type === 'video') {
    if (typeof el.poster !== 'string') el.poster = '';
    for (const flag of ['autoplay', 'loop', 'muted']) el[flag] = el[flag] === true;
    // The one switch that is on unless the deck says otherwise: a video with
    // no controls cannot be paused, and a presenter who cannot pause a video
    // has lost the slide.
    el.controls = el.controls !== false;
    el.start = Math.max(0, num(el.start, 0));
  }
  return el;
}

function normalizeSlide(raw) {
  if (!raw || typeof raw !== 'object') return createSlide();
  const slide = { ...createSlide(), ...raw };
  slide.id = typeof raw.id === 'string' && raw.id ? raw.id : newId('s');
  slide.elements = Array.isArray(raw.elements)
    ? raw.elements.map(normalizeElement).filter(Boolean)
    : [];
  if (typeof slide.notes !== 'string') slide.notes = '';
  if (slide.backgroundImage != null && typeof slide.backgroundImage !== 'string') {
    slide.backgroundImage = '';
  }
  if (!BACKGROUND_FITS.includes(slide.backgroundFit)) slide.backgroundFit = 'cover';
  slide.backgroundOpacity = Math.min(1, Math.max(0, num(slide.backgroundOpacity, 1)));
  return slide;
}

// Turns whatever is in the file into a deck this build can render. Throws only
// on malformed JSON — a structurally odd but parseable deck is repaired rather
// than rejected, so a hand-edited file never locks the user out of the editor.
export function parseDeck(json) {
  const raw = typeof json === 'string' ? JSON.parse(json) : json;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('deck file must contain a JSON object');
  }
  const deck = { ...createDeck(), ...raw };
  deck.version = num(raw.version, 1);
  deck.width = Math.max(1, num(raw.width, DECK_W));
  deck.height = Math.max(1, num(raw.height, DECK_H));
  deck.theme = { ...DEFAULT_THEME, ...(raw.theme && typeof raw.theme === 'object' ? raw.theme : {}) };
  if (typeof deck.theme.backgroundImage !== 'string') deck.theme.backgroundImage = '';
  deck.theme.headerHeight = Math.max(0, num(deck.theme.headerHeight, 0));
  deck.theme.footerHeight = Math.max(0, num(deck.theme.footerHeight, 0));
  if (deck.theme.footerText !== 'title') deck.theme.footerText = '';
  if (!BACKGROUND_FITS.includes(deck.theme.backgroundFit)) deck.theme.backgroundFit = 'cover';
  deck.theme.backgroundOpacity = Math.min(1, Math.max(0, num(deck.theme.backgroundOpacity, 1)));
  deck.slides = Array.isArray(raw.slides) && raw.slides.length
    ? raw.slides.map(normalizeSlide)
    : [createSlide()];
  if (typeof deck.title !== 'string') deck.title = 'Untitled presentation';
  return deck;
}

// Reorders an object's keys: the known ones in declared order, then anything
// else in insertion order. Purely cosmetic, and the reason a deck edited by
// this build stays diff-legible against one written by another.
function ordered(obj, keyOrder) {
  const out = {};
  for (const key of keyOrder) {
    if (Object.prototype.hasOwnProperty.call(obj, key)) out[key] = obj[key];
  }
  for (const key of Object.keys(obj)) {
    if (!Object.prototype.hasOwnProperty.call(out, key)) out[key] = obj[key];
  }
  return out;
}

export function serializeDeck(deck) {
  const out = ordered({
    ...deck,
    slides: deck.slides.map(slide => ordered({
      ...slide,
      elements: slide.elements.map(el => ordered(el, ELEMENT_KEY_ORDER)),
    }, SLIDE_KEY_ORDER)),
  }, DECK_KEY_ORDER);
  return `${JSON.stringify(out, null, 2)}\n`;
}

// ─── operations ──────────────────────────────────────────────────────────────
// Each returns a new deck, or the same deck when the target does not exist —
// callers compare by identity to know whether anything changed.

function mapSlide(deck, slideId, fn) {
  const index = deck.slides.findIndex(s => s.id === slideId);
  if (index === -1) return deck;
  const next = fn(deck.slides[index]);
  if (next === deck.slides[index]) return deck;
  const slides = deck.slides.slice();
  slides[index] = next;
  return { ...deck, slides };
}

export function addSlide(deck, { at = deck.slides.length, slide = createSlide() } = {}) {
  const slides = deck.slides.slice();
  const index = Math.min(Math.max(0, at), slides.length);
  slides.splice(index, 0, slide);
  return { ...deck, slides };
}

// A deck with no slides has no canvas to draw, so the last one is never
// removed — it is emptied instead.
export function deleteSlide(deck, slideId) {
  if (deck.slides.length <= 1) {
    return mapSlide(deck, slideId, slide => ({ ...slide, elements: [], notes: '' }));
  }
  const slides = deck.slides.filter(s => s.id !== slideId);
  if (slides.length === deck.slides.length) return deck;
  return { ...deck, slides };
}

export function duplicateSlide(deck, slideId) {
  const index = deck.slides.findIndex(s => s.id === slideId);
  if (index === -1) return deck;
  const source = deck.slides[index];
  const copy = {
    ...source,
    id: newId('s'),
    elements: cloneElements(source.elements),
  };
  return addSlide(deck, { at: index + 1, slide: copy });
}

export function moveSlide(deck, from, to) {
  if (from === to) return deck;
  const slides = deck.slides.slice();
  if (from < 0 || from >= slides.length) return deck;
  const [moved] = slides.splice(from, 1);
  slides.splice(Math.min(Math.max(0, to), slides.length), 0, moved);
  return { ...deck, slides };
}

export function setSlideNotes(deck, slideId, notes) {
  return mapSlide(deck, slideId, slide => ({ ...slide, notes: String(notes ?? '') }));
}

export function setSlideBackground(deck, slideId, background) {
  return mapSlide(deck, slideId, slide => ({ ...slide, background }));
}

/**
 * The deck-wide background, in the theme.
 *
 * Applying a background to a *presentation* rather than to a slide is a theme
 * edit, which is why it is one operation and not a loop: every slide that has
 * not overridden the background follows it, and one slide that has keeps what
 * it chose. Slides that carried their own picture are cleared only when the
 * caller asks, because "use this background" usually means the deck, not "throw
 * away the exception I set on slide 7".
 */
/**
 * Applying a whole theme to a deck.
 *
 * Two things happen, and the second is the reason this is an operation rather
 * than an object assignment: the theme's fields replace the deck's, and each
 * slide's title is *marked* as one. A deck made before themes existed has no
 * roles, so without that pass a theme with a white title colour would leave
 * every title in the old dark ink, invisible on its own header band.
 *
 * Nothing else about the slides is touched. A user who coloured one heading by
 * hand keeps that colour, because an explicit colour still wins over the
 * theme's.
 */
export function applyTheme(deck, theme) {
  const next = { ...deck.theme, ...theme };
  const marksTitles = (Number(next.headerHeight) || 0) > 0 || !!next.titleColor;
  if (!marksTitles) return { ...deck, theme: next };
  return {
    ...deck,
    theme: next,
    slides: deck.slides.map((slide) => {
      const title = titleElementOf(slide);
      if (!title || title.role === 'title') return slide;
      return {
        ...slide,
        elements: slide.elements.map(el => (el.id === title.id ? { ...el, role: 'title' } : el)),
      };
    }),
  };
}

export function setThemeBackground(deck, patch, { clearSlides = false } = {}) {
  const theme = { ...deck.theme, ...patch };
  if (!clearSlides) return { ...deck, theme };
  return {
    ...deck,
    theme,
    slides: deck.slides.map(slide => ({ ...slide, backgroundImage: '', background: null })),
  };
}

export function addElement(deck, slideId, element) {
  return addElements(deck, slideId, [element]);
}

// The plural is the primitive, not a loop over the singular: a paste of five
// elements is one edit, one history entry and one re-serialization, and the
// group has to land on the slide together or the undo that follows would take
// it apart one element at a time.
export function addElements(deck, slideId, elements) {
  if (!elements.length) return deck;
  return mapSlide(deck, slideId, slide => ({
    ...slide,
    elements: [...slide.elements, ...elements],
  }));
}

export function updateElement(deck, slideId, elementId, patch) {
  return mapSlide(deck, slideId, slide => {
    const index = slide.elements.findIndex(el => el.id === elementId);
    if (index === -1) return slide;
    const elements = slide.elements.slice();
    elements[index] = normalizeElement({ ...elements[index], ...patch });
    return { ...slide, elements };
  });
}

export function deleteElement(deck, slideId, elementId) {
  return mapSlide(deck, slideId, slide => {
    const elements = slide.elements.filter(el => el.id !== elementId);
    if (elements.length === slide.elements.length) return slide;
    return { ...slide, elements };
  });
}

// Z-order is array order: later elements paint on top.
export function reorderElement(deck, slideId, elementId, direction) {
  return mapSlide(deck, slideId, slide => {
    const from = slide.elements.findIndex(el => el.id === elementId);
    if (from === -1) return slide;
    const last = slide.elements.length - 1;
    const to = {
      front: last, back: 0, forward: Math.min(last, from + 1), backward: Math.max(0, from - 1),
    }[direction];
    if (to === undefined || to === from) return slide;
    const elements = slide.elements.slice();
    const [moved] = elements.splice(from, 1);
    elements.splice(to, 0, moved);
    return { ...slide, elements };
  });
}

// ─── clipboard ───────────────────────────────────────────────────────────────
// Copied elements travel through the *system* clipboard as a tagged JSON
// envelope rather than through a variable inside the editor. An in-memory
// clipboard cannot cross two windows, and copying a diagram out of one
// presentation and into another is the case users reach for first.
//
// The tag is what makes a paste unambiguous: text that is not this envelope is
// not coerced into elements, it is reported as not-a-payload and the caller
// decides what to do with it (paste it as a text box, or ignore it). Nothing
// guesses.

export const CLIPBOARD_KIND = 'gilzabots.slides.elements';
export const CLIPBOARD_VERSION = 1;

/** Distance between a pasted copy and the original it landed on top of. */
export const PASTE_OFFSET = 24;

// How much of a pasted group has to stay on the slide.
const PASTE_KEEP_VISIBLE = 32;

// How far the stair-step of repeated pastes is allowed to walk before it goes
// back to the top. Without a bound, holding Ctrl+V would march the copies off
// the slide.
const PASTE_CASCADE_LIMIT = 24;

export function serializeClipboard(elements) {
  return JSON.stringify({
    kind: CLIPBOARD_KIND,
    version: CLIPBOARD_VERSION,
    elements: elements.map(el => ordered(el, ELEMENT_KEY_ORDER)),
  }, null, 2);
}

/**
 * The elements carried by a clipboard payload, or `null` when the text is not
 * one.
 *
 * Returning null rather than throwing is the contract: the caller is looking at
 * whatever the user last copied anywhere on the machine, and most of the time
 * that is ordinary text, not a failure.
 */
export function parseClipboard(text) {
  if (typeof text !== 'string' || !text.includes(CLIPBOARD_KIND)) return null;
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  if (raw.kind !== CLIPBOARD_KIND || !Array.isArray(raw.elements)) return null;
  const elements = raw.elements.map(normalizeElement).filter(Boolean);
  return elements.length ? elements : null;
}

/**
 * Copies of `elements` with fresh ids, optionally displaced.
 *
 * Ids are regenerated here rather than at copy time, so the same payload can be
 * pasted repeatedly without the second paste colliding with the first — and so
 * a payload written by another window never carries an id that already exists
 * in this deck.
 */
export function cloneElements(elements, { dx = 0, dy = 0 } = {}) {
  return elements.map(el => ({
    ...el,
    id: newId(el.type[0]),
    x: el.x + dx,
    y: el.y + dy,
  }));
}

/** The upright bounding box of a group of elements. */
export function boundsOf(elements) {
  if (!elements.length) return { x: 0, y: 0, w: 0, h: 0 };
  const x = Math.min(...elements.map(el => el.x));
  const y = Math.min(...elements.map(el => el.y));
  const right = Math.max(...elements.map(el => el.x + el.w));
  const bottom = Math.max(...elements.map(el => el.y + el.h));
  return { x, y, w: right - x, h: bottom - y };
}

/**
 * Where a pasted group should land, as the displacement to apply to it.
 *
 * Two behaviours, because the two ways of asking for a paste mean different
 * things. A paste aimed at a point — the canvas context menu — puts the group
 * where the user pointed, centred on the cursor. A paste with no point behind
 * it (Ctrl+V) lands the group back at its own coordinates, which is what makes
 * copying an element from one slide to another preserve the layout; only when
 * something already sits exactly there does it step aside, cascading so a run
 * of pastes leaves a readable stack instead of one invisible pile.
 *
 * The result is clamped, so a paste can never drop a group entirely outside
 * the slide where the user would have to hunt for it.
 */
export function pastePlacement(deck, slide, elements, { at = null } = {}) {
  if (!elements.length) return { dx: 0, dy: 0 };
  const box = boundsOf(elements);

  let dx = 0;
  let dy = 0;
  if (at) {
    dx = at.x - box.x - box.w / 2;
    dy = at.y - box.y - box.h / 2;
  } else {
    const taken = new Set(slide.elements.map(el => `${Math.round(el.x)}:${Math.round(el.y)}`));
    let step = 0;
    while (step < PASTE_CASCADE_LIMIT
      && taken.has(`${Math.round(box.x + step * PASTE_OFFSET)}:${Math.round(box.y + step * PASTE_OFFSET)}`)) {
      step += 1;
    }
    if (step >= PASTE_CASCADE_LIMIT) step = 0;
    dx = step * PASTE_OFFSET;
    dy = step * PASTE_OFFSET;
  }

  // Same rule the canvas applies to a drag: a group keeps at least a corner on
  // the slide. Clamping the whole bounding box moves the group as one, so the
  // arrangement the user copied survives the correction.
  const keep = Math.min(PASTE_KEEP_VISIBLE, box.w, box.h);
  const x = Math.min(Math.max(box.x + dx, keep - box.w), deck.width - keep);
  const y = Math.min(Math.max(box.y + dy, keep - box.h), deck.height - keep);
  return { dx: Math.round(x - box.x), dy: Math.round(y - box.y) };
}

/**
 * Which ends of a line carry an arrowhead.
 *
 * `shape: 'arrow'` predates the explicit attributes and still means "a line
 * with a head at the end". It is read here rather than rewritten at parse time:
 * normalizing it away would change the bytes of a file nobody edited, and
 * byte-exact round-tripping is the invariant this format is built on. Every
 * surface that draws a line — the canvas, the thumbnails, presentation mode,
 * the HTML and PPTX exports — asks this one function, so none of them can
 * disagree about where the heads go.
 */
export function arrowsOf(el) {
  const legacyEnd = el.shape === 'arrow';
  return {
    start: el.arrowStart === true,
    end: el.arrowEnd === undefined ? legacyEnd : el.arrowEnd === true,
  };
}

/** True for the shapes drawn as a stroke rather than as a filled area. */
export function isLineShape(el) {
  return el.type === 'shape' && (el.shape === 'line' || el.shape === 'arrow');
}

/**
 * The border of a filled shape as `{ color, width }`, or null when it has none.
 *
 * The companion of `arrowsOf`: one place decides whether a border exists, so
 * the canvas, the thumbnails, presentation mode and the three exports cannot
 * disagree about it. A border needs both a width and a colour — a width with
 * no colour is not a black border, it is a shape whose border was never
 * configured, and drawing `1px solid null` is how that used to reach the DOM.
 *
 * Lines are excluded: there `stroke` and `strokeWidth` already describe the
 * line itself, and an outline around a stroke is not a thing the model has.
 */
export function borderOf(el) {
  if (el?.type !== 'shape' || isLineShape(el)) return null;
  const width = Number.isFinite(el.strokeWidth) ? el.strokeWidth : 0;
  if (width <= 0 || !el.stroke) return null;
  return { color: el.stroke, width };
}

/**
 * The background a slide actually draws, as `{ color, image, fit, opacity }`.
 *
 * The companion of `arrowsOf` and `borderOf`: one function decides, so the
 * canvas, the thumbnails, presentation mode and the three exports cannot
 * disagree about what is behind the elements. Each field falls back to the
 * theme independently, which is what lets a deck carry one picture and a single
 * slide override only the colour over it.
 */
/**
 * The chrome a theme draws behind every slide, or null when it draws none.
 *
 * Resolved in one place for the same reason `backgroundOf` is: the canvas, the
 * thumbnails, presentation mode and the three exports all draw these bands, and
 * a header that is 180 units tall in the editor and 190 in the export is a
 * theme that does not survive being shown.
 */
export function chromeOf(deck, slide) {
  const theme = deck?.theme ?? DEFAULT_THEME;
  // The header band is the title's background, so it is drawn where there is a
  // title to sit in it and nowhere else. That is what keeps a cover or a
  // section divider clean instead of carrying an empty coloured bar, and it is
  // also what Beamer does: the headline carries the frame title.
  const hasTitle = slide ? !!titleElementOf(slide) : true;
  const header = hasTitle ? Math.max(0, Number(theme.headerHeight) || 0) : 0;
  const footer = Math.max(0, Number(theme.footerHeight) || 0);
  if (!header && !footer) return null;
  return {
    header,
    headerColor: theme.headerColor || theme.accent || DEFAULT_THEME.accent,
    footer,
    footerColor: theme.footerColor || theme.accent || DEFAULT_THEME.accent,
    footerTextColor: theme.footerTextColor || '#ffffff',
    footerText: theme.footerText === 'title' ? 'title' : '',
  };
}

/**
 * The colour a text element draws in: its own, then the theme's colour for its
 * role, then the theme's ordinary text colour.
 */
export function textColorOf(el, theme) {
  if (el.color) return el.color;
  if (el.role === 'title' && theme?.titleColor) return theme.titleColor;
  return theme?.color ?? DEFAULT_THEME.color;
}

// ─── bullets ─────────────────────────────────────────────────────────────────
// A bulleted list is a text box with a `bullet` style and one line per item;
// the nesting level of a line is the number of tab characters in front of it.
//
// Why tabs in the text rather than an array of items: the box already stores
// its content as one string that every surface wraps the same way, and a
// second representation for the same words would be a second thing to keep in
// sync (I4). A tab is also what the user's Tab key means, what a diff shows
// legibly, and what an agent writes without being taught a new shape.
//
// Everything that draws a list asks the two functions below, and nothing works
// the level or the marker out for itself (I7b): the canvas, the thumbnails,
// presentation mode, the HTML export and the PPTX export must agree about
// which line is a sub-point and what stands in front of it.

// Markers per level, cycling. `disc` walks the three shapes PowerPoint and
// Beamer both use; `dash` stays a dash at every depth, because a dashed list is
// a deliberately flat look and changing its glyph halfway would undo that.
const DISC_MARKERS = ['\u2022', '\u25e6', '\u25aa'];
const DASH_MARKERS = ['\u2013'];
// Arabic, then lower alpha, then lower roman — Word's and PowerPoint's ladder.
const NUMBER_FORMATS = ['arabic', 'alpha', 'roman'];

// The marker column and one level of indentation, as multiples of the font
// size, so a list keeps its proportions when the type is resized. Numbers get
// a wider column because "10." is not the width of a bullet.
const BULLET_INDENT_EM = 1.5;
const BULLET_GUTTER_EM = { disc: 1.1, dash: 1.1, number: 1.7 };

const ROMAN = [
  [10, 'x'], [9, 'ix'], [5, 'v'], [4, 'iv'], [1, 'i'],
];

function roman(value) {
  let n = value;
  let out = '';
  // Only the units above ten are ever needed on a slide, and a list that
  // reaches xl is a list that wanted to be two slides.
  for (const [amount, glyph] of ROMAN) {
    while (n >= amount) { out += glyph; n -= amount; }
  }
  return out || String(value);
}

function alpha(value) {
  // a…z, then aa, ab — the same wrap a spreadsheet column uses.
  let n = value;
  let out = '';
  while (n > 0) {
    const rest = (n - 1) % 26;
    out = String.fromCharCode(97 + rest) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

function numberMarker(level, ordinal) {
  const format = NUMBER_FORMATS[level % NUMBER_FORMATS.length];
  if (format === 'alpha') return `${alpha(ordinal)}.`;
  if (format === 'roman') return `${roman(ordinal)}.`;
  return `${ordinal}.`;
}

/**
 * The lines of a text element: `{ level, text, marker }`, one per hard break.
 *
 * `level` is the leading tabs, clamped; `text` is the line without them; and
 * `marker` is what the style puts in front of it — empty on a blank line,
 * because an empty paragraph in a list is a gap the author left and not an
 * item they forgot to write. Numbering counts only the lines that get one, and
 * a deeper level restarts each time its parent moves on, which is what makes
 * `1. / a. / b. / 2.` come out right.
 */
export function textLinesOf(el) {
  const style = BULLET_STYLES.includes(el?.bullet) ? el.bullet : null;
  const raw = typeof el?.text === 'string' ? el.text : String(el?.text ?? '');
  const counters = [];
  return raw.split('\n').map((line) => {
    const tabs = /^\t*/.exec(line)[0].length;
    const level = Math.min(tabs, MAX_BULLET_LEVEL);
    const text = line.slice(tabs);
    if (!style || !text.trim()) return { level, text, marker: '' };
    counters.length = level + 1;              // a deeper list restarts
    counters[level] = (counters[level] || 0) + 1;
    if (style === 'number') return { level, text, marker: numberMarker(level, counters[level]) };
    const markers = style === 'dash' ? DASH_MARKERS : DISC_MARKERS;
    return { level, text, marker: markers[level % markers.length] };
  });
}

/**
 * The two lengths a list is laid out with, in deck units: the marker column
 * (`gutter`) and one step of nesting (`indent`).
 *
 * A line is drawn with `padding-left: indent * level + gutter` and
 * `text-indent: -gutter`, which is what puts the marker in its own column and
 * lands a wrapped second line under the first word rather than under the
 * marker. A box with no style still indents by `level`, so Tab means something
 * in plain text too.
 */
export function bulletMetricsOf(el) {
  const style = BULLET_STYLES.includes(el?.bullet) ? el.bullet : null;
  const fontSize = Number(el?.fontSize) || TEXT_DEFAULTS.fontSize;
  return {
    style,
    indent: fontSize * BULLET_INDENT_EM,
    gutter: style ? fontSize * BULLET_GUTTER_EM[style] : 0,
  };
}

/** The left inset of one line, in deck units: where its *text* starts. */
export function lineInsetOf(el, level) {
  const { indent, gutter } = bulletMetricsOf(el);
  return indent * level + gutter;
}

/**
 * The same text with any hand-typed list markers taken off the front of its
 * lines.
 *
 * Decks predating the `bullet` field — and every deck a Beamer conversion
 * wrote before it existed — carry their markers as characters: `"\u2022  Point"`.
 * Turning the field on for such a box would draw a second marker in front of
 * the first, so the editor offers this when the user switches a style on. It
 * is a command the user issues and can undo, never something a file gets on
 * being opened: rewriting text nobody edited would break the round-trip (I1).
 */
export function stripListMarkers(text) {
  return String(text ?? '').split('\n').map((line) => {
    const tabs = /^\t*/.exec(line)[0];
    const body = line.slice(tabs.length);
    // A marker is one of the glyphs a person reaches for, or a number, and it
    // is only a marker when whitespace follows it: "-5 degrees" keeps its sign.
    const stripped = body.replace(/^([\u2022\u25e6\u25aa\u25cf\u00b7*+\-\u2013\u2014]|\d+[.)]|[a-z][.)])\s+/i, '');
    return tabs + stripped;
  }).join('\n');
}

/**
 * The same text with every line moved one level in or out.
 *
 * The command form of what Tab does to the line the caret is in: with no caret
 * to speak of — the box is selected, not open — the only sensible target is
 * the whole box, and that is what the panel's indent buttons do when nothing
 * is being typed. Levels clamp rather than wrap, so outdenting a flat list is
 * a no-op instead of a surprise.
 */
export function indentText(text, delta) {
  return String(text ?? '').split('\n').map((line) => {
    const tabs = /^\t*/.exec(line)[0].length;
    const level = Math.min(MAX_BULLET_LEVEL, Math.max(0, tabs + delta));
    return '\t'.repeat(level) + line.slice(tabs);
  }).join('\n');
}

/**
 * The element on a slide that is its title, or null.
 *
 * Used when a theme is applied to a deck whose slides predate roles: the
 * topmost bold text sitting in the band the grid reserves for a title is a
 * title, and marking it is what lets the theme colour it. A deck built by the
 * layout engine already carries the role and never reaches this.
 */
export function titleElementOf(slide) {
  const tagged = slide.elements.find(el => (
    el.type === 'text' && el.role === 'title' && el.y + el.h <= TITLE_BAND_BOTTOM
  ));
  if (tagged) return tagged;
  const candidates = slide.elements.filter(el => (
    el.type === 'text' && el.bold && el.text.trim() && el.y + el.h <= TITLE_BAND_BOTTOM
  ));
  if (candidates.length !== 1) return null;
  return candidates[0];
}

export function backgroundOf(deck, slide) {
  const theme = deck?.theme ?? DEFAULT_THEME;
  // Three states, not two: `''` (and absent) inherits the theme's picture,
  // `null` is an explicit *no picture* that overrides it, and a string is the
  // slide's own. Without the third state a deck with a themed photograph could
  // never have one plain slide for a dense table.
  const inherits = slide?.backgroundImage === '' || slide?.backgroundImage === undefined;
  const source = inherits ? theme : slide;
  return {
    color: slide?.background || theme.background,
    image: (inherits ? theme.backgroundImage : slide.backgroundImage) || '',
    fit: source?.backgroundFit || 'cover',
    opacity: source?.backgroundOpacity ?? 1,
  };
}

/** True for an asset source that already travels with the file or is a service URL. */
export function isPortableSource(src) {
  return !src || /^(data:|jpt:|https?:|blob:)/.test(src);
}

/**
 * Every asset the deck refers to by a local path it cannot yet carry, de-duplicated.
 *
 * The editor uses this to say which assets will be moved into the JPT package
 * on save. A project reference and an internal member draw identically, so the
 * status strip is the only visible difference before that save.
 */
export function externalSourcesOf(deck) {
  const out = new Set();
  const consider = (src) => { if (src && !isPortableSource(src)) out.add(src); };
  consider(deck.theme?.backgroundImage);
  for (const slide of deck.slides) {
    consider(slide.backgroundImage);
    for (const el of slide.elements) {
      if (el.type === 'image') consider(el.src);
      // A video contributes both its still and, when it is a file in the
      // project rather than a link to a provider, the film itself. A deck sent
      // to someone else loses either one the same way it loses a picture.
      if (el.type === 'video') {
        consider(el.poster);
        consider(el.src);
      }
    }
  }
  return [...out];
}

export function findSlide(deck, slideId) {
  return deck.slides.find(s => s.id === slideId) ?? null;
}

export function findElement(deck, slideId, elementId) {
  return findSlide(deck, slideId)?.elements.find(el => el.id === elementId) ?? null;
}
