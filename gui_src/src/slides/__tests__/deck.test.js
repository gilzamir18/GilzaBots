// Tests for the deck model, its operations, and the geometry behind direct
// manipulation.
//
// The headline invariant here is the mirror of the LaTeX modes': parsing a
// deck file and serializing it straight back must reproduce it byte for byte,
// including keys this build does not know about. A deck editor that rewrites
// the file on open makes every save a spurious diff, and silently drops fields
// written by a newer version or by an agent.
//
// Run with:  npm run test:slides     (node --test, no extra dependencies)

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  addElement, addElements, addSlide, applyTheme, arrowsOf, backgroundOf, borderOf,
  boundsOf, bulletMetricsOf, chromeOf, CLIPBOARD_KIND, externalSourcesOf,
  indentText, isPortableSource, MAX_BULLET_LEVEL, stripListMarkers, textColorOf,
  textLinesOf, titleElementOf,
  cloneElements, createDeck, createElement, createSlide, DEFAULT_STROKE,
  deleteElement, deleteSlide, duplicateSlide, findElement, isLineShape,
  moveSlide, parseClipboard, parseDeck, PASTE_OFFSET, pastePlacement,
  reorderElement, serializeClipboard, serializeDeck, setSlideNotes,
  updateElement,
} from '../model.js';
import {
  DOUBLE_CLICK_MS, DOUBLE_CLICK_SLOP_PX, MIN_SIZE, ROTATION_SNAP_DEG,
  angleToPoint, clampToSlide, cornerAt, dragRect, elementsInRect, insetPolygon,
  isDoubleClick, normalizeAngle, resizeRect, resizeRotatedRect, rotatePoint,
  snapTargets, trianglePoints,
} from '../geometry.js';
import {
  deckToHtml, deckToPrintHtml, escapeBareAmpersands, inlineDeckAssets,
  pdfExportFrameName, pptxParagraphs,
} from '../export.js';
import { fontFamilyWithMathFallback } from '../fonts.js';
import { fitRect, sizeFromBytes } from '../imageSize.js';
import { latexToOmml, mathmlToOmml, parseXml } from '../omml.js';
import { videoEmbedUrl, videoFileUrl, videoSourceOf } from '../video.js';
import {
  clampEquationFont, EQUATION_FONT_MAX, EQUATION_FONT_MIN, equationScaleFactor,
  renderEquation,
} from '../equation.js';
import {
  COALESCE_MS, canRedoText, canUndoText, createTextHistory, isBoundaryInput,
  recordText, redoText, undoText,
} from '../textHistory.js';

// ─── round-trip ──────────────────────────────────────────────────────────────

test('a freshly created deck round-trips byte for byte', () => {
  const text = serializeDeck(createDeck('Round trip'));
  assert.equal(serializeDeck(parseDeck(text)), text);
});

test('unknown keys survive a round-trip', () => {
  const deck = createDeck('Forward compat');
  deck.futureDeckField = { mode: 'unknown' };
  deck.slides[0].futureSlideField = 42;
  deck.slides[0].elements[0].futureElementField = ['a', 'b'];

  const once = serializeDeck(deck);
  const twice = serializeDeck(parseDeck(once));
  assert.equal(twice, once);

  const reparsed = parseDeck(once);
  assert.deepEqual(reparsed.futureDeckField, { mode: 'unknown' });
  assert.equal(reparsed.slides[0].futureSlideField, 42);
  assert.deepEqual(reparsed.slides[0].elements[0].futureElementField, ['a', 'b']);
});

test('known keys are serialized in a stable order', () => {
  const deck = parseDeck(serializeDeck(createDeck('Order')));
  const keys = Object.keys(JSON.parse(serializeDeck(deck)));
  assert.deepEqual(keys.slice(0, 6), ['version', 'title', 'width', 'height', 'theme', 'slides']);
});

test('a structurally broken but parseable deck is repaired, not rejected', () => {
  const deck = parseDeck(JSON.stringify({
    slides: [{ elements: [{ type: 'text', x: 'nonsense', text: 'hi' }, null, 7] }],
  }));
  assert.equal(deck.slides.length, 1);
  assert.equal(deck.slides[0].elements.length, 1);
  assert.equal(deck.slides[0].elements[0].x, 0);          // repaired
  assert.equal(deck.slides[0].elements[0].text, 'hi');    // preserved
  assert.ok(deck.slides[0].id, 'a missing slide id is generated');
});

test('malformed JSON throws rather than yielding an empty deck', () => {
  assert.throws(() => parseDeck('{ not json'), SyntaxError);
});

// ─── operations ──────────────────────────────────────────────────────────────

test('operations return a new deck and leave the original untouched', () => {
  const deck = createDeck();
  const next = addSlide(deck);
  assert.notEqual(next, deck);
  assert.equal(deck.slides.length, 1);
  assert.equal(next.slides.length, 2);
});

test('deleting the last remaining slide empties it instead of removing it', () => {
  const deck = createDeck();
  const next = deleteSlide(deck, deck.slides[0].id);
  assert.equal(next.slides.length, 1, 'a deck always has a slide to draw');
  assert.equal(next.slides[0].elements.length, 0);
});

test('deleting one of several slides removes it', () => {
  const deck = addSlide(createDeck());
  const doomed = deck.slides[1].id;
  const next = deleteSlide(deck, doomed);
  assert.equal(next.slides.length, 1);
  assert.equal(next.slides.find(s => s.id === doomed), undefined);
});

test('duplicating a slide gives every copied element a fresh id', () => {
  const deck = createDeck('Dup');
  const next = duplicateSlide(deck, deck.slides[0].id);
  const [original, copy] = next.slides;
  assert.equal(next.slides.length, 2);
  assert.notEqual(copy.id, original.id);
  assert.equal(copy.elements.length, original.elements.length);
  for (let i = 0; i < copy.elements.length; i += 1) {
    assert.notEqual(copy.elements[i].id, original.elements[i].id);
    assert.equal(copy.elements[i].text, original.elements[i].text);
  }
});

test('moveSlide reorders without losing a slide', () => {
  let deck = createDeck();
  deck = addSlide(deck, { slide: createSlide({ notes: 'second' }) });
  deck = addSlide(deck, { slide: createSlide({ notes: 'third' }) });
  const moved = moveSlide(deck, 2, 0);
  assert.equal(moved.slides.length, 3);
  assert.equal(moved.slides[0].notes, 'third');
});

test('updateElement normalizes what it is given', () => {
  const deck = createDeck();
  const slideId = deck.slides[0].id;
  const elementId = deck.slides[0].elements[0].id;
  const next = updateElement(deck, slideId, elementId, { w: -50, opacity: 4 });
  const el = findElement(next, slideId, elementId);
  assert.equal(el.w, 1, 'a non-positive width is clamped');
  assert.equal(el.opacity, 1, 'opacity is clamped into range');
});

test('an operation on a missing target returns the same deck', () => {
  const deck = createDeck();
  assert.equal(deleteElement(deck, deck.slides[0].id, 'nope'), deck);
  assert.equal(updateElement(deck, 'nope', 'nope', { x: 1 }), deck);
  assert.equal(moveSlide(deck, 5, 0), deck);
});

test('reorderElement moves an element through the z-order', () => {
  const slideId = 's1';
  let deck = { ...createDeck(), slides: [createSlide({ id: slideId })] };
  for (const name of ['a', 'b', 'c']) {
    deck = addElement(deck, slideId, createElement('text', { id: name, text: name }));
  }
  const ids = d => d.slides[0].elements.map(el => el.id);
  assert.deepEqual(ids(reorderElement(deck, slideId, 'a', 'front')), ['b', 'c', 'a']);
  assert.deepEqual(ids(reorderElement(deck, slideId, 'c', 'back')), ['c', 'a', 'b']);
  assert.deepEqual(ids(reorderElement(deck, slideId, 'a', 'forward')), ['b', 'a', 'c']);
  assert.deepEqual(ids(reorderElement(deck, slideId, 'c', 'backward')), ['a', 'c', 'b']);
  assert.equal(reorderElement(deck, slideId, 'c', 'front'), deck, 'already at the front');
});

test('notes are stored as text', () => {
  const deck = createDeck();
  const next = setSlideNotes(deck, deck.slides[0].id, 'remember the point');
  assert.equal(next.slides[0].notes, 'remember the point');
});

// ─── geometry ────────────────────────────────────────────────────────────────

const RECT = { x: 100, y: 100, w: 200, h: 100 };

test('resizing from a south-east handle grows the box and keeps its origin', () => {
  assert.deepEqual(resizeRect(RECT, 'se', 50, 25), { x: 100, y: 100, w: 250, h: 125 });
});

test('resizing from a north-west handle anchors the opposite corner', () => {
  const out = resizeRect(RECT, 'nw', 50, 25);
  assert.deepEqual(out, { x: 150, y: 125, w: 150, h: 75 });
  assert.equal(out.x + out.w, RECT.x + RECT.w, 'right edge stayed put');
  assert.equal(out.y + out.h, RECT.y + RECT.h, 'bottom edge stayed put');
});

test('an edge handle moves only its own axis', () => {
  assert.deepEqual(resizeRect(RECT, 'e', 40, 999), { x: 100, y: 100, w: 240, h: 100 });
  assert.deepEqual(resizeRect(RECT, 'n', 999, 20), { x: 100, y: 120, w: 200, h: 80 });
});

test('shrinking past the minimum clamps without walking the box across the slide', () => {
  const out = resizeRect(RECT, 'nw', 500, 500);
  assert.equal(out.w, MIN_SIZE);
  assert.equal(out.h, MIN_SIZE);
  assert.equal(out.x + out.w, RECT.x + RECT.w, 'still anchored to the right edge');
  assert.equal(out.y + out.h, RECT.y + RECT.h, 'still anchored to the bottom edge');
});

test('a corner resize with aspect lock follows the dominant axis', () => {
  const out = resizeRect(RECT, 'se', 100, 10, { aspect: true });
  assert.equal(out.w / out.h, RECT.w / RECT.h);
  assert.equal(out.w, 300);
});

test('dragging snaps to the slide centre and reports the guide', () => {
  const deck = createDeck();
  const slide = createSlide();
  const targets = snapTargets(deck, slide, null);
  // A 200-wide box whose centre lands 3 units left of the slide centre.
  const rect = { x: deck.width / 2 - 100 - 3, y: 50, w: 200, h: 100 };
  const { rect: moved, guides } = dragRect(rect, 0, 0, { targets });
  assert.equal(moved.x + moved.w / 2, deck.width / 2, 'centred exactly');
  assert.ok(guides.some(g => g.axis === 'v' && g.at === deck.width / 2));
});

test('a drag past the snap threshold is left alone', () => {
  const deck = createDeck();
  const targets = snapTargets(deck, createSlide(), null);
  const rect = { x: deck.width / 2 - 100 - 40, y: 50, w: 200, h: 100 };
  const { rect: moved, guides } = dragRect(rect, 0, 0, { targets });
  assert.equal(moved.x, rect.x);
  assert.equal(guides.length, 0);
});

test('snapping can be turned off', () => {
  const deck = createDeck();
  const targets = snapTargets(deck, createSlide(), null);
  const rect = { x: deck.width / 2 - 100 - 3, y: 50, w: 200, h: 100 };
  const { rect: moved } = dragRect(rect, 0, 0, { targets, snap: false });
  assert.equal(moved.x, rect.x);
});

test('an element is never dragged entirely off the slide', () => {
  const out = clampToSlide({ x: -5000, y: -5000, w: 200, h: 100 }, 1280, 720);
  assert.ok(out.x + out.w > 0, 'still reachable horizontally');
  assert.ok(out.y + out.h > 0, 'still reachable vertically');
});

test('marquee hit-testing returns the intersecting elements', () => {
  const slide = createSlide({
    elements: [
      createElement('shape', { id: 'near', x: 0, y: 0, w: 100, h: 100 }),
      createElement('shape', { id: 'far', x: 800, y: 600, w: 50, h: 50 }),
    ],
  });
  assert.deepEqual(elementsInRect(slide, { x: 10, y: 10, w: 50, h: 50 }), ['near']);
});

// ─── rotation ────────────────────────────────────────────────────────────────

test('rotating a point by a right angle swaps the axes', () => {
  const p = rotatePoint(10, 0, 90);
  assert.ok(Math.abs(p.x - 0) < 1e-9);
  assert.ok(Math.abs(p.y - 10) < 1e-9);
});

test('angles are normalized into a single turn', () => {
  assert.equal(normalizeAngle(370), 10);
  assert.equal(normalizeAngle(-90), 270);
  assert.equal(normalizeAngle(0), 0);
});

test('the rotation handle reads zero straight above the centre', () => {
  // The handle rests above the box, so that direction must be the zero angle
  // or every rotation starts with a jump the moment the user grabs it.
  assert.equal(Math.round(angleToPoint(RECT, 200, -500)), 0);
  assert.equal(Math.round(angleToPoint(RECT, 900, 150)), 90);
  assert.equal(Math.round(angleToPoint(RECT, 200, 900)), 180);
});

test('shift snaps rotation to fixed increments', () => {
  const free = angleToPoint(RECT, 260, 40, { snap: false });
  const snapped = angleToPoint(RECT, 260, 40, { snap: true });
  assert.equal(snapped % ROTATION_SNAP_DEG, 0);
  assert.ok(Math.abs(snapped - free) <= ROTATION_SNAP_DEG / 2 + 1e-9);
});

test('resizing an unrotated element is unchanged by the rotation-aware path', () => {
  assert.deepEqual(resizeRotatedRect(RECT, 'se', 50, 25, 0), resizeRect(RECT, 'se', 50, 25));
});

test('resizing a rotated element keeps the anchored corner where the user sees it', () => {
  // This is the whole point of the correction: the element turns about its
  // centre, so growing it moves that centre and swings the opposite corner
  // away unless the result is shifted back.
  for (const rotation of [15, 30, 45, 90, 137, 180, 271, 359]) {
    const out = resizeRotatedRect(RECT, 'se', 60, 40, rotation);
    const before = cornerAt(RECT, 'nw', rotation);
    const after = cornerAt(out, 'nw', rotation);
    const drift = Math.hypot(after.x - before.x, after.y - before.y);
    assert.ok(drift < 1e-9, `anchor drifted ${drift} at ${rotation} degrees`);
  }
});

test('a rotated resize applies the drag along the element axes, not the screen', () => {
  // At 90 degrees the element's local +x points down the screen, so a purely
  // downward drag is what widens it.
  const out = resizeRotatedRect(RECT, 'e', 0, 100, 90);
  assert.ok(Math.abs(out.w - (RECT.w + 100)) < 1e-9, `w=${out.w}`);
  assert.ok(Math.abs(out.h - RECT.h) < 1e-9, `h=${out.h}`);
});

test('a rotated resize still refuses to go below the minimum size', () => {
  const out = resizeRotatedRect(RECT, 'se', -9999, -9999, 42);
  assert.equal(out.w, MIN_SIZE);
  assert.equal(out.h, MIN_SIZE);
});

test('rotation survives a round-trip', () => {
  let deck = createDeck('Rotated');
  deck = addElement(deck, deck.slides[0].id, createElement('shape', { rotation: 33.5 }));
  const text = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(text)), text);
  assert.equal(parseDeck(text).slides[0].elements.at(-1).rotation, 33.5);
});

// ─── arrows ──────────────────────────────────────────────────────────────────

test('a new line has no arrowheads', () => {
  assert.deepEqual(arrowsOf(createElement('shape', { shape: 'line' })), { start: false, end: false });
});

test('arrowheads are independent, so a line can be double-headed', () => {
  const el = createElement('shape', { shape: 'line', arrowStart: true, arrowEnd: true });
  assert.deepEqual(arrowsOf(el), { start: true, end: true });
});

test("the legacy 'arrow' shape still means a head at the end", () => {
  // Read, never rewritten: normalizing it at parse time would change the bytes
  // of a file nobody edited and break the round-trip invariant.
  assert.deepEqual(arrowsOf({ type: 'shape', shape: 'arrow' }), { start: false, end: true });
  const text = JSON.stringify({ slides: [{ elements: [{ type: 'shape', shape: 'arrow' }] }] });
  assert.equal(parseDeck(text).slides[0].elements[0].shape, 'arrow');
});

test('an explicit arrowEnd overrides the legacy shape', () => {
  assert.deepEqual(arrowsOf({ type: 'shape', shape: 'arrow', arrowEnd: false }), { start: false, end: false });
});

// ─── borders ─────────────────────────────────────────────────────────────────

test('a new shape has no border', () => {
  assert.equal(borderOf(createElement('shape', { shape: 'rect' })), null);
  assert.equal(createElement('shape').strokeWidth, 0);
});

test('a border needs both a colour and a width', () => {
  assert.equal(borderOf(createElement('shape', { strokeWidth: 4 })), null,
    'a width with no colour is an unconfigured border, not a black one');
  assert.equal(borderOf(createElement('shape', { stroke: '#ff0000' })), null,
    'a colour with no width draws nothing');
  assert.deepEqual(
    borderOf(createElement('shape', { stroke: '#ff0000', strokeWidth: 4 })),
    { color: '#ff0000', width: 4 },
  );
});

test('every filled shape kind can carry a border', () => {
  for (const shape of ['rect', 'ellipse', 'triangle']) {
    const el = createElement('shape', { shape, stroke: DEFAULT_STROKE, strokeWidth: 3 });
    assert.deepEqual(borderOf(el), { color: DEFAULT_STROKE, width: 3 }, shape);
  }
});

test('a line has no border: its stroke is the line itself', () => {
  const line = createElement('shape', { shape: 'line', stroke: '#000000', strokeWidth: 6 });
  assert.equal(borderOf(line), null);
  assert.equal(borderOf(createElement('shape', { shape: 'arrow', stroke: '#000000', strokeWidth: 6 })), null);
  assert.equal(borderOf(createElement('text')), null);
});

test('a negative border width is repaired rather than drawn inside out', () => {
  const deck = parseDeck(JSON.stringify({
    slides: [{ elements: [{ type: 'shape', shape: 'rect', stroke: '#000000', strokeWidth: -8 }] }],
  }));
  assert.equal(deck.slides[0].elements[0].strokeWidth, 0);
  assert.equal(borderOf(deck.slides[0].elements[0]), null);
});

test('a border survives a round-trip', () => {
  let deck = createDeck('Bordered');
  deck = addElement(deck, deck.slides[0].id, createElement('shape', {
    shape: 'ellipse', stroke: '#ff0000', strokeWidth: 5,
  }));
  const text = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(text)), text);
  const reparsed = parseDeck(text).slides[0].elements[2];
  assert.deepEqual(borderOf(reparsed), { color: '#ff0000', width: 5 });
});

test('insetting a triangle pulls every edge inwards by the same distance', () => {
  const outline = trianglePoints({ w: 400, h: 300 });
  const inset = insetPolygon(outline, 10);
  assert.equal(inset.length, 3);
  // The base sits at y = h, so its inset copy sits exactly 10 units above it.
  assert.equal(Math.round(inset[1][1]), 290);
  assert.equal(Math.round(inset[2][1]), 290);
  // The apex retreats further than the offset, because it is the meeting point
  // of two edges rather than a point on one.
  assert.ok(inset[0][1] > 10, 'the apex moves down by more than the offset');
  assert.ok(Math.abs(inset[0][0] - 200) < 1e-6, 'and stays on the axis of symmetry');
  // The inset triangle is strictly inside the original.
  for (const [x, y] of inset) {
    assert.ok(x > 0 && x < 400 && y > 0 && y < 300);
  }
});

test('an inset larger than the shape collapses to a point instead of inverting', () => {
  const inset = insetPolygon(trianglePoints({ w: 40, h: 30 }), 200);
  const [first] = inset;
  for (const point of inset) assert.deepEqual(point, first);
});

test('insetting by nothing leaves the polygon alone', () => {
  const outline = trianglePoints({ w: 400, h: 300 });
  assert.equal(insetPolygon(outline, 0), outline);
});

test('isLineShape distinguishes strokes from filled areas', () => {
  assert.equal(isLineShape(createElement('shape', { shape: 'line' })), true);
  assert.equal(isLineShape(createElement('shape', { shape: 'arrow' })), true);
  assert.equal(isLineShape(createElement('shape', { shape: 'rect' })), false);
  assert.equal(isLineShape(createElement('text')), false);
});

// ─── click pairing ───────────────────────────────────────────────────────────
// Double click is computed from consecutive presses rather than read off the
// event, because `pointerdown` carries `detail = 0` in Chrome: the click count
// exists only on `mousedown`/`click`/`dblclick`, which the gesture layer does
// not listen to. A test that fabricates `detail: 2` on a synthetic pointerdown
// proves nothing — it is asserting against a field the browser never sets.

const press = (id, time, x = 100, y = 100) => ({ id, time, x, y });

test('two quick presses on the same element are a double click', () => {
  assert.equal(isDoubleClick(press('a', 1000), press('a', 1080)), true);
});

test('the first press of a session is never a double click', () => {
  assert.equal(isDoubleClick(null, press('a', 1000)), false);
});

test('presses on different elements never pair', () => {
  assert.equal(isDoubleClick(press('a', 1000), press('b', 1080)), false);
});

test('a slow second press is a separate click', () => {
  assert.equal(isDoubleClick(press('a', 1000), press('a', 1000 + DOUBLE_CLICK_MS + 1)), false);
  assert.equal(isDoubleClick(press('a', 1000), press('a', 1000 + DOUBLE_CLICK_MS)), true);
});

test('a second press that moved is a drag, not a double click', () => {
  const slop = DOUBLE_CLICK_SLOP_PX;
  assert.equal(isDoubleClick(press('a', 1000, 100, 100), press('a', 1050, 100 + slop, 100)), true);
  assert.equal(isDoubleClick(press('a', 1000, 100, 100), press('a', 1050, 100 + slop + 1, 100)), false);
  assert.equal(isDoubleClick(press('a', 1000, 100, 100), press('a', 1050, 100, 100 + slop + 1)), false);
});

test('a press older than its predecessor cannot pair', () => {
  // timeStamp is monotonic per document, but a fallback to Date.now() on one
  // press and not the other would otherwise produce a huge negative delta that
  // passes an upper-bound-only check.
  assert.equal(isDoubleClick(press('a', 5000), press('a', 1000)), false);
});

// ─── bullets ─────────────────────────────────────────────────────────────────
// A list is a `bullet` style on the box and a leading tab per nesting level in
// the text. These check the two halves of that: the field survives a file, and
// the parser reads the same lines the five surfaces will draw.

test('a text box without a list style keeps one, so old decks render unchanged', () => {
  const el = createElement('text', { text: 'Plain' });
  assert.equal(el.bullet, null);
  const lines = textLinesOf(el);
  assert.deepEqual(lines, [{ level: 0, text: 'Plain', marker: '' }]);
});

test('a list style round-trips byte for byte, beside the text it belongs to', () => {
  let deck = createDeck('Lists');
  deck = addElement(deck, deck.slides[0].id, createElement('text', {
    text: 'Point\n\tSub-point', bullet: 'disc',
  }));
  const once = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(once)), once);
  // I3: the key sorts with the rest of the text payload, not after everything.
  assert.ok(once.indexOf('"bullet"') > once.indexOf('"text"'));
  assert.ok(once.indexOf('"bullet"') < once.indexOf('"fontSize"'));
});

test('a bullet style this build does not know is repaired to none', () => {
  const deck = parseDeck(JSON.stringify({
    slides: [{ elements: [{ type: 'text', text: 'x', bullet: 'sparkles' }] }],
  }));
  assert.equal(deck.slides[0].elements[0].bullet, null);
});

test('leading tabs are the nesting level, and the level is clamped', () => {
  const el = createElement('text', {
    text: 'top\n\tone\n\t\ttwo\n\t\t\t\t\t\t\t\tmiles in',
    bullet: 'disc',
  });
  assert.deepEqual(textLinesOf(el).map(line => line.level), [0, 1, 2, MAX_BULLET_LEVEL]);
  assert.deepEqual(textLinesOf(el).map(line => line.text),
    ['top', 'one', 'two', 'miles in']);
});

test('disc markers walk the levels and a dashed list stays dashed', () => {
  const disc = createElement('text', { text: 'a\n\tb\n\t\tc\n\t\t\td', bullet: 'disc' });
  assert.deepEqual(textLinesOf(disc).map(line => line.marker),
    ['\u2022', '\u25e6', '\u25aa', '\u2022']);
  const dash = createElement('text', { text: 'a\n\tb', bullet: 'dash' });
  assert.deepEqual(textLinesOf(dash).map(line => line.marker), ['\u2013', '\u2013']);
});

test('a numbered list counts per level and restarts a sub-list', () => {
  const el = createElement('text', {
    text: 'one\n\tsub\n\tsub\ntwo\n\tsub\n\t\tdeep',
    bullet: 'number',
  });
  assert.deepEqual(textLinesOf(el).map(line => line.marker),
    ['1.', 'a.', 'b.', '2.', 'a.', 'i.']);
});

test('an empty line gets no marker and no number', () => {
  const el = createElement('text', { text: 'one\n\ntwo', bullet: 'number' });
  assert.deepEqual(textLinesOf(el).map(line => line.marker), ['1.', '', '2.']);
});

test('the marker column and the indent scale with the type', () => {
  const small = bulletMetricsOf(createElement('text', { fontSize: 20, bullet: 'disc' }));
  const large = bulletMetricsOf(createElement('text', { fontSize: 40, bullet: 'disc' }));
  assert.equal(large.indent, small.indent * 2);
  assert.equal(large.gutter, small.gutter * 2);
  // A box with no style still indents, so Tab means something in plain text.
  assert.equal(bulletMetricsOf(createElement('text', { fontSize: 20 })).gutter, 0);
  assert.ok(bulletMetricsOf(createElement('text', { fontSize: 20 })).indent > 0);
});

test('markers typed into the text are taken off, but a minus sign is not', () => {
  assert.equal(stripListMarkers('\u2022  Point\n\t\u2013 Sub\n2. Two\n-5 degrees'),
    'Point\n\tSub\nTwo\n-5 degrees');
});

// ─── indenting ───────────────────────────────────────────────────────────────

test('the indent command moves every line of a box and clamps at both ends', () => {
  assert.equal(indentText('a\n\tb', 1), '\ta\n\t\tb');
  assert.equal(indentText('a\n\tb', -1), 'a\nb', 'a flat line has nowhere to go');
  assert.equal(indentText('x', 40), '\t'.repeat(MAX_BULLET_LEVEL) + 'x');
  assert.equal(indentText('', 1), '\t', 'an empty box still takes a level');
});

test('indenting leaves the words alone, tabs included further in', () => {
  assert.equal(indentText('a\tb', 1), '\ta\tb', 'only the leading tabs are the level');
});

// ─── undo inside a text box ──────────────────────────────────────────────────
// The deck's history is one entry per finished edit; this is the one the caret
// needs, over the string being typed. See textHistory.js.

const snap = text => ({ text, caret: { line: 0, offset: text.length } });

test('a run of typing is one undo step', () => {
  let history = createTextHistory(snap(''), { now: 0 });
  'abc'.split('').forEach((char, index) => {
    history = recordText(history, snap('abc'.slice(0, index + 1)), { now: index * 50 });
  });
  const step = undoText(history);
  assert.equal(step.snapshot.text, '', 'three keystrokes, one step back');
  assert.equal(canUndoText(step.history), false);
});

test('a pause ends the run, so the step is what was typed after it', () => {
  let history = createTextHistory(snap('one'), { now: 0 });
  history = recordText(history, snap('one two'), { now: 100 });
  history = recordText(history, snap('one two three'), { now: 100 + COALESCE_MS + 1 });
  assert.equal(undoText(history).snapshot.text, 'one two');
});

test('a boundary edit is a step of its own', () => {
  let history = createTextHistory(snap('a'), { now: 0 });
  history = recordText(history, snap('a\n'), { now: 10, boundary: true });
  history = recordText(history, snap('a\nb'), { now: 20 });
  assert.equal(undoText(history).snapshot.text, 'a\n', 'the line break survives one undo');
});

test('redo walks back up, and a new edit ends the chain', () => {
  let history = createTextHistory(snap(''), { now: 0 });
  history = recordText(history, snap('one'), { now: 0, boundary: true });
  history = recordText(history, snap('one two'), { now: 10, boundary: true });

  const back = undoText(history);
  assert.equal(back.snapshot.text, 'one');
  assert.equal(canRedoText(back.history), true);
  const forward = redoText(back.history);
  assert.equal(forward.snapshot.text, 'one two');

  const again = undoText(forward.history);
  const typed = recordText(again.history, snap('one three'), { now: 20, boundary: true });
  assert.equal(canRedoText(typed), false, 'typing after an undo is a new branch');
});

test('undo runs out rather than emptying the box, so the deck can take over', () => {
  const history = createTextHistory(snap('untouched'), { now: 0 });
  assert.equal(canUndoText(history), false);
  assert.equal(undoText(history), null);
  assert.equal(redoText(history), null);
});

test('a caret move is not an edit', () => {
  let history = createTextHistory(snap('same'), { now: 0 });
  history = recordText(history, { text: 'same', caret: { line: 0, offset: 1 } }, { now: 10 });
  assert.equal(canUndoText(history), false);
  assert.deepEqual(history.current.caret, { line: 0, offset: 1 },
    'but the caret it comes back to is the current one');
});

test('what counts as a boundary is the input, not a guess', () => {
  assert.equal(isBoundaryInput('insertText', 'a'), false);
  assert.equal(isBoundaryInput('insertText', ' '), true, 'a word is a step');
  assert.equal(isBoundaryInput('insertText', '\n'), true);
  assert.equal(isBoundaryInput('deleteContentBackward', null), true);
  assert.equal(isBoundaryInput('insertFromPaste', null), true);
  assert.equal(isBoundaryInput('insertText', 'pasted words'), true);
});

// ─── export ──────────────────────────────────────────────────────────────────

test('HTML export emits one section per slide and escapes text', () => {
  let deck = createDeck('Export me');
  deck = addSlide(deck);
  deck = addElement(deck, deck.slides[1].id, createElement('text', {
    text: '<script>alert(1)</script> & "quotes"',
  }));
  const html = deckToHtml(deck);
  assert.equal((html.match(/class="slide"/g) || []).length, 2);
  assert.ok(!html.includes('<script>alert(1)</script>'), 'user text is not injected as markup');
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(html.includes('&amp;'));
  assert.ok(html.includes('Export me'), 'the deck title becomes the document title');
});

test('HTML export draws a list with its markers in a hanging indent', () => {
  let deck = createDeck('Bulleted');
  deck = addElement(deck, deck.slides[0].id, createElement('text', {
    text: 'Point\n\tSub\n\nAfter a gap', bullet: 'disc', fontSize: 20,
  }));
  const html = deckToHtml(deck);
  // The marker sits in a column of exactly the width the first line is pulled
  // back by: that is the whole hanging-indent trick, and half of it is a bug.
  assert.ok(html.includes('text-indent:-22px'));
  // The marker cancels that indent for its own content: an inline-block
  // inherits `text-indent`, and applying it twice draws the glyph outside the
  // box, where the exported slide clips it exactly as the canvas did.
  assert.ok(html.includes('width:22px;text-indent:0;'));
  assert.ok(html.includes('padding-left:22px'), 'a top-level line clears the marker');
  assert.ok(html.includes('padding-left:52px'), 'a sub-point clears one indent more');
  assert.equal((html.match(/\u2022/g) || []).length, 2, 'both top-level lines');
  assert.equal((html.match(/\u25e6/g) || []).length, 1, 'and the sub-point below them');
  assert.ok(html.includes('<br>'), 'the empty line keeps its height');
});

test('PPTX export writes real PowerPoint list paragraphs, not typed markers', () => {
  const deck = createDeck('Bulleted');
  const numbered = pptxParagraphs(deck, createElement('text', {
    text: 'one\n\tsub\n\ntwo', bullet: 'number', fontSize: 20,
  }));
  assert.deepEqual(numbered.map(p => p.text), ['one', 'sub', '', 'two']);
  assert.deepEqual(numbered.map(p => p.options.indentLevel), [0, 1, 0, 0]);
  assert.equal(numbered[0].options.bullet.type, 'number');
  assert.equal(numbered[1].options.bullet.numberType, 'alphaLcPeriod');
  assert.equal(numbered[2].options.bullet, false, 'an empty paragraph gets no marker');

  const disc = pptxParagraphs(deck, createElement('text', { text: 'a\n\tb', bullet: 'disc' }));
  assert.equal(disc[0].options.bullet.characterCode, '2022');
  assert.equal(disc[1].options.bullet.characterCode, '25E6');

  const plain = pptxParagraphs(deck, createElement('text', { text: 'a\nb' }));
  assert.deepEqual(plain.map(p => p.options.bullet), [false, false]);
});

test('HTML export draws arrowheads and carries rotation', () => {
  let deck = createDeck('Arrows');
  const slideId = deck.slides[0].id;
  deck = addElement(deck, slideId, createElement('shape', {
    shape: 'line', w: 300, h: 24, strokeWidth: 4, arrowStart: true, arrowEnd: true, rotation: 45,
  }));
  const html = deckToHtml(deck);
  assert.equal((html.match(/<polygon/g) || []).length, 2, 'a head at each end');
  assert.ok(html.includes('rotate(45deg)'));
  assert.match(html, /stroke-linecap="butt"/, 'an arrow shaft stops under its heads');
});

test('HTML export omits heads a line does not have', () => {
  let deck = createDeck('Plain');
  deck = addElement(deck, deck.slides[0].id, createElement('shape', { shape: 'line', w: 300, h: 24 }));
  const html = deckToHtml(deck);
  assert.ok(!html.includes('<polygon'));
  assert.match(html, /stroke-linecap="round"/, 'a plain line has the canvas rounded ends');
});

test('text keeps its prose face and falls back to the bundled math font for missing glyphs', () => {
  const stack = fontFamilyWithMathFallback('Inter, Segoe UI, system-ui, sans-serif');
  assert.equal(
    stack,
    "Inter, Segoe UI, system-ui, 'STIX Two Math', 'Noto Sans Math', 'Cambria Math', 'DejaVu Math TeX Gyre', sans-serif",
  );
  assert.equal(fontFamilyWithMathFallback(stack), stack, 'the resolver is idempotent');

  let deck = createDeck('Unicode math in prose');
  deck = addElement(deck, deck.slides[0].id, createElement('text', {
    text: 'n⃗ = (P − C)/r',
  }));
  const html = deckToPrintHtml(deck);
  assert.match(html, /font-family:Inter, Segoe UI, system-ui, 'STIX Two Math'/);
  assert.match(html, /fonts\.googleapis\.com\/css2\?family=/,
    'the print iframe loads the same web faces as the editor');
});

test('the Qt PDF frame protocol preserves a suggested name', () => {
  assert.equal(
    pdfExportFrameName('m1-2', 'Aula 02: vetores.pdf'),
    'gilzabots-pdf-v1:m1-2:Aula%2002%3A%20vetores.pdf',
  );
});

test('HTML export inlines the deck dimensions so it presents standalone', () => {
  const html = deckToHtml(createDeck());
  assert.ok(html.includes('width:1280px'));
  assert.ok(html.includes('height:720px'));
  assert.ok(html.includes('ArrowRight'), 'keyboard navigation is included');
});

test('HTML export draws a border on every filled shape kind', () => {
  let deck = createDeck('Borders');
  const slideId = deck.slides[0].id;
  for (const shape of ['rect', 'ellipse', 'triangle']) {
    deck = addElement(deck, slideId, createElement('shape', {
      shape, w: 200, h: 200, stroke: '#ff0000', strokeWidth: 6,
    }));
  }
  const html = deckToHtml(deck);
  assert.equal((html.match(/border:6px solid #ff0000/g) || []).length, 2,
    'the rectangle and the ellipse use a CSS border');
  assert.ok(html.includes('box-sizing:border-box'), 'which is drawn inside the box');
  assert.ok(html.includes('stroke="#ff0000" stroke-width="6"'),
    'and the triangle strokes an inset outline');
});

test('HTML export leaves a borderless shape as it was', () => {
  let deck = createDeck('Plain shapes');
  deck = addElement(deck, deck.slides[0].id, createElement('shape', { shape: 'triangle' }));
  const html = deckToHtml(deck);
  assert.ok(html.includes('clip-path:polygon'), 'no border, no SVG needed');
  assert.ok(!html.includes('border:'));
});

test('HTML export ignores a border width with no colour', () => {
  let deck = createDeck('Half configured');
  deck = addElement(deck, deck.slides[0].id, createElement('shape', { strokeWidth: 4 }));
  assert.ok(!deckToHtml(deck).includes('border:'), 'no "solid null" reaches the document');
});


// ─── clipboard ───────────────────────────────────────────────────────────────

test('elements survive a trip through the clipboard payload', () => {
  const source = [
    createElement('text', { text: 'Título', fontSize: 44, bold: true }),
    createElement('shape', { shape: 'ellipse', fill: '#c0392b', strokeWidth: 3, stroke: '#000000' }),
  ];
  const back = parseClipboard(serializeClipboard(source));
  assert.deepEqual(back, source);
});

test('an unknown key on a copied element survives the clipboard too', () => {
  const source = [{ ...createElement('shape'), futureField: { a: 1 } }];
  const back = parseClipboard(serializeClipboard(source));
  assert.deepEqual(back[0].futureField, { a: 1 });
});

test('text that is not a payload is reported as such rather than repaired', () => {
  assert.equal(parseClipboard('hello world'), null);
  assert.equal(parseClipboard(''), null);
  assert.equal(parseClipboard(undefined), null);
  // Valid JSON, but not ours: a deck file is not an element payload either.
  assert.equal(parseClipboard(serializeDeck(createDeck('Deck'))), null);
  // Ours by tag but empty: nothing to paste is not a payload.
  assert.equal(parseClipboard(JSON.stringify({ kind: CLIPBOARD_KIND, elements: [] })), null);
});

test('a truncated payload is refused instead of throwing', () => {
  const half = serializeClipboard([createElement('text')]).slice(0, 40);
  assert.equal(parseClipboard(half), null);
});

test('a pasted element is repaired like any other', () => {
  const payload = JSON.stringify({
    kind: CLIPBOARD_KIND,
    version: 1,
    elements: [{ type: 'shape', shape: 'hexagon', x: 'nope', w: -5 }],
  });
  const [el] = parseClipboard(payload);
  assert.equal(el.shape, 'rect');
  assert.equal(el.x, 0);
  assert.ok(el.w >= 1);
  assert.ok(el.id, 'a payload with no id still yields an addressable element');
});

test('cloned elements get fresh ids and keep everything else', () => {
  const source = [createElement('text', { text: 'a' }), createElement('shape')];
  const copies = cloneElements(source, { dx: 10, dy: -4 });
  assert.notEqual(copies[0].id, source[0].id);
  assert.notEqual(copies[1].id, source[1].id);
  assert.equal(new Set(copies.map(el => el.id)).size, 2, 'and are distinct from each other');
  assert.equal(copies[0].text, 'a');
  assert.equal(copies[0].x, source[0].x + 10);
  assert.equal(copies[0].y, source[0].y - 4);
});

test('cloning the same payload twice never collides', () => {
  const source = [createElement('text')];
  const first = cloneElements(source);
  const second = cloneElements(source);
  assert.notEqual(first[0].id, second[0].id);
});

test('addElements lands the whole group in one edit', () => {
  const deck = createDeck('Group');
  const slideId = deck.slides[0].id;
  const before = deck.slides[0].elements.length;
  const next = addElements(deck, slideId, [createElement('text'), createElement('shape')]);
  assert.equal(next.slides[0].elements.length, before + 2);
  assert.equal(deck.slides[0].elements.length, before, 'the original is untouched');
  assert.equal(addElements(deck, slideId, []), deck, 'an empty group is not an edit');
});

test('the bounding box of a group spans every element', () => {
  const box = boundsOf([
    createElement('shape', { x: 100, y: 50, w: 100, h: 100 }),
    createElement('shape', { x: 40, y: 200, w: 60, h: 60 }),
  ]);
  assert.deepEqual(box, { x: 40, y: 50, w: 160, h: 210 });
});

// ─── paste placement ─────────────────────────────────────────────────────────

test('a paste onto a slide that has room lands where it was copied from', () => {
  const deck = createDeck('Empty');
  const slide = createSlide();
  const copied = [createElement('shape', { x: 300, y: 200 })];
  assert.deepEqual(pastePlacement(deck, slide, copied), { dx: 0, dy: 0 });
});

test('a paste on top of the original steps aside', () => {
  const deck = createDeck('Same slide');
  const original = createElement('shape', { x: 300, y: 200 });
  const slide = createSlide({ elements: [original] });
  assert.deepEqual(pastePlacement(deck, slide, [original]),
    { dx: PASTE_OFFSET, dy: PASTE_OFFSET });
});

test('repeated pastes cascade instead of piling up invisibly', () => {
  const deck = createDeck('Cascade');
  const original = createElement('shape', { x: 300, y: 200 });
  const first = { ...original, x: 300 + PASTE_OFFSET, y: 200 + PASTE_OFFSET };
  const slide = createSlide({ elements: [original, first] });
  assert.deepEqual(pastePlacement(deck, slide, [original]),
    { dx: PASTE_OFFSET * 2, dy: PASTE_OFFSET * 2 });
});

test('a paste aimed at a point centres the group on it', () => {
  const deck = createDeck('Aimed');
  const slide = createSlide();
  const copied = [createElement('shape', { x: 0, y: 0, w: 200, h: 100 })];
  const { dx, dy } = pastePlacement(deck, slide, copied, { at: { x: 640, y: 360 } });
  assert.deepEqual({ x: dx, y: dy }, { x: 540, y: 310 });
});

test('a group keeps its arrangement when it is pasted at a point', () => {
  const deck = createDeck('Arrangement');
  const slide = createSlide();
  const copied = [
    createElement('shape', { x: 100, y: 100, w: 100, h: 100 }),
    createElement('shape', { x: 300, y: 100, w: 100, h: 100 }),
  ];
  const { dx, dy } = pastePlacement(deck, slide, copied, { at: { x: 640, y: 360 } });
  const moved = cloneElements(copied, { dx, dy });
  assert.equal(moved[1].x - moved[0].x, 200, 'the gap between them is unchanged');
  assert.equal(moved[0].y, moved[1].y);
});

test('a paste aimed off the slide is pulled back to where it can be seen', () => {
  const deck = createDeck('Off slide');
  const slide = createSlide();
  const copied = [createElement('shape', { x: 0, y: 0, w: 200, h: 100 })];
  const { dx, dy } = pastePlacement(deck, slide, copied, { at: { x: 4000, y: 4000 } });
  const [moved] = cloneElements(copied, { dx, dy });
  assert.ok(moved.x < deck.width, 'the group is not left beyond the right edge');
  assert.ok(moved.y < deck.height);
  assert.ok(moved.x + moved.w > 0);
});

test('an empty paste asks for no displacement', () => {
  assert.deepEqual(pastePlacement(createDeck('None'), createSlide(), []), { dx: 0, dy: 0 });
});

// ─── equations ───────────────────────────────────────────────────────────────
// What is stored is the LaTeX, never a rendered picture of it: the source is
// what can be corrected later, read by an agent, and re-rendered by a newer
// KaTeX. Everything below is the consequence of that one decision.

test('a new equation carries a source and a display style, and no picture', () => {
  const el = createElement('equation');
  assert.equal(el.type, 'equation');
  assert.equal(el.latex, '');
  assert.equal(el.displayMode, true);
  assert.equal(typeof el.fontSize, 'number');
  assert.ok(!('src' in el), 'an equation is not stored as an image');
});

test('an equation round-trips byte for byte', () => {
  let deck = createDeck('Math');
  deck = addElement(deck, deck.slides[0].id, createElement('equation', {
    id: 'eq1', latex: '\\int_0^1 x^2 \\, dx = \\frac{1}{3}', fontSize: 44,
  }));
  const once = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(once)), once);
  assert.equal(findElement(parseDeck(once), deck.slides[0].id, 'eq1').latex,
    '\\int_0^1 x^2 \\, dx = \\frac{1}{3}');
});

test('a deck written by hand with a missing or odd formula is repaired', () => {
  const deck = parseDeck(JSON.stringify({
    slides: [{ elements: [
      { id: 'a', type: 'equation' },
      { id: 'b', type: 'equation', latex: 42, displayMode: false, fontSize: 'huge' },
    ] }],
  }));
  const [a, b] = deck.slides[0].elements;
  assert.equal(a.latex, '');
  assert.equal(a.displayMode, true, 'display style is the default on a slide');
  assert.equal(b.latex, '42');
  assert.equal(b.displayMode, false, 'an explicit inline style is kept');
  assert.equal(b.fontSize, 40, 'a non-numeric size falls back rather than breaking layout');
});

test('a formula renders to MathML, which needs no stylesheet beside it', () => {
  const { html, error } = renderEquation('E = mc^2');
  assert.equal(error, null);
  assert.match(html, /<math/);
  assert.match(html, /display="block"/);
  assert.doesNotMatch(html, /katex-html/, 'the HTML output would need katex.min.css and its fonts');
});

test('display style and inline style are different renderings', () => {
  assert.doesNotMatch(renderEquation('x', { displayMode: false }).html, /display="block"/);
});

test('an unfinished formula still renders, and says what is wrong', () => {
  const { html, error } = renderEquation('\\frac{1}{');
  assert.ok(error && error.includes('KaTeX'), `expected a message, got ${error}`);
  assert.ok(html.length > 0, 'a formula being typed must still draw something');
});

test('the same formula is rendered once and reused', () => {
  const first = renderEquation('a^2 + b^2 = c^2');
  assert.equal(renderEquation('a^2 + b^2 = c^2'), first, 'a drag re-renders the element every frame');
});

test('resizing an equation is a change of scale, taken from the diagonal', () => {
  assert.equal(equationScaleFactor({ w: 100, h: 50 }, { w: 200, h: 100 }), 2);
  assert.equal(equationScaleFactor({ w: 100, h: 50 }, { w: 100, h: 50 }), 1);
  assert.ok(equationScaleFactor({ w: 100, h: 50 }, { w: 50, h: 25 }) < 1);
});

test('a font size is kept inside what a slide can show', () => {
  assert.equal(clampEquationFont(1e9), EQUATION_FONT_MAX);
  assert.equal(clampEquationFont(0), EQUATION_FONT_MIN);
  assert.equal(clampEquationFont(Number.NaN), 40);
  assert.equal(clampEquationFont(41.4), 41);
});

test('HTML export inlines the formula as MathML rather than as a link to one', () => {
  let deck = createDeck('Export math');
  deck = addElement(deck, deck.slides[0].id, createElement('equation', {
    latex: '\\sum_{i=1}^{n} i', x: 100, y: 200, w: 300, h: 120, fontSize: 48,
  }));
  const html = deckToHtml(deck);
  assert.match(html, /<math/);
  assert.match(html, /font-size:48px/);
  // The app loads katex.min.css, which sizes .katex at 1.21em; an export that
  // did not undo it would draw every formula a fifth larger than the editor.
  assert.match(html, /\.katex \{ font-size: 1em; \}/);
  assert.match(html, /font-family: 'STIX Two Math'/);
});

test('an equation with no formula exports nothing at all', () => {
  let deck = createDeck('Empty math');
  deck = addElement(deck, deck.slides[0].id, createElement('equation', { latex: '   ' }));
  assert.doesNotMatch(deckToHtml(deck), /<math/);
});

test('an equation survives the clipboard, formula and all', () => {
  const source = createElement('equation', { latex: 'x = \\frac{-b}{2a}', displayMode: false, fontSize: 52 });
  const [pasted] = parseClipboard(serializeClipboard([source]));
  assert.equal(pasted.latex, 'x = \\frac{-b}{2a}');
  assert.equal(pasted.displayMode, false);
  assert.equal(pasted.fontSize, 52);
  // Ids are regenerated by cloneElements when the group lands, not here.
  assert.notEqual(cloneElements([pasted])[0].id, pasted.id);
});

// ─── backgrounds ─────────────────────────────────────────────────────────────
// A colour with an optional picture over it, resolved in one place so the
// canvas, the thumbnails, presentation mode and the three exports cannot
// disagree about what is behind the content.

test('a slide with no background of its own inherits the theme', () => {
  const deck = createDeck('Inherit');
  deck.theme.background = '#101010';
  deck.theme.backgroundImage = 'theme.png';
  deck.theme.backgroundOpacity = 0.4;
  const background = backgroundOf(deck, deck.slides[0]);
  assert.equal(background.color, '#101010');
  assert.equal(background.image, 'theme.png');
  assert.equal(background.opacity, 0.4);
});

test("a slide's own picture overrides the theme's, with its own fit and opacity", () => {
  const deck = createDeck('Override');
  deck.theme.backgroundImage = 'theme.png';
  deck.theme.backgroundOpacity = 0.4;
  const slide = { ...deck.slides[0], backgroundImage: 'own.png', backgroundFit: 'contain', backgroundOpacity: 1 };
  const background = backgroundOf(deck, slide);
  assert.equal(background.image, 'own.png');
  assert.equal(background.fit, 'contain');
  assert.equal(background.opacity, 1);
});

test('null is an explicit no-picture, which is what makes one plain slide possible', () => {
  const deck = createDeck('Off');
  deck.theme.backgroundImage = 'theme.png';
  assert.equal(backgroundOf(deck, { ...deck.slides[0], backgroundImage: null }).image, '');
});

test('a deck written before backgrounds existed still resolves one', () => {
  const deck = parseDeck(JSON.stringify({
    theme: { background: '#fafafa' },
    slides: [{ id: 's1', elements: [] }],
  }));
  const background = backgroundOf(deck, deck.slides[0]);
  assert.equal(background.color, '#fafafa');
  assert.equal(background.image, '');
  assert.equal(background.fit, 'cover');
  assert.equal(background.opacity, 1);
});

test('a malformed background is repaired rather than rejected', () => {
  const deck = parseDeck(JSON.stringify({
    slides: [{ id: 's1', backgroundFit: 'stretch', backgroundOpacity: 5, elements: [] }],
  }));
  assert.equal(deck.slides[0].backgroundFit, 'cover');
  assert.equal(deck.slides[0].backgroundOpacity, 1);
});

test('background fields round-trip byte for byte', () => {
  const deck = createDeck('Round trip');
  deck.theme.backgroundImage = 'theme.png';
  deck.slides[0].backgroundImage = 'own.png';
  deck.slides[0].backgroundFit = 'contain';
  deck.slides[0].backgroundOpacity = 0.5;
  const once = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(once)), once);
  const keys = Object.keys(JSON.parse(once).slides[0]);
  assert.deepEqual(keys.slice(0, 5),
    ['id', 'background', 'backgroundImage', 'backgroundFit', 'backgroundOpacity']);
});

test('HTML export draws the picture under the elements and never on top of them', () => {
  const deck = createDeck('Export bg');
  deck.slides[0].backgroundImage = 'photo.jpg';
  deck.slides[0].backgroundOpacity = 0.4;
  deck.slides[0].backgroundFit = 'contain';
  const html = deckToHtml(deck);
  const section = html.slice(html.indexOf('<section'));
  assert.ok(section.indexOf('class="el bg"') < section.indexOf('class="el"'),
    'the background must be the first child, or it paints over the slide');
  assert.match(html, /object-fit:contain/);
  assert.match(html, /opacity:0\.4/);
  // Without this the picture swallows the click that advances the deck.
  assert.match(html, /\.bg \{ pointer-events:none; \}/);
});

test('a deck with no background picture exports no background layer', () => {
  assert.doesNotMatch(deckToHtml(createDeck('Plain')), /class="el bg"/);
});

// ─── self-contained exports ──────────────────────────────────────────────────
// A deck an agent wrote references pictures by project path, and that path
// resolves through the IDE's own server. Sent to anyone else, those pictures
// are gone — so an export inlines them first.

function stubFetch(responses) {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    const body = responses[url];
    if (!body) return { ok: false, status: 404, headers: { get: () => null } };
    return {
      ok: true,
      status: 200,
      headers: { get: () => body.type },
      arrayBuffer: async () => body.bytes.buffer,
    };
  };
  return calls;
}

const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

test('a project picture becomes a data URI, and the deck stops needing the server', async () => {
  const calls = stubFetch({ '/api/raw?figures/plot.png': { type: 'image/png', bytes: PNG_BYTES } });
  let deck = createDeck('Portable');
  deck.slides[0].backgroundImage = 'figures/plot.png';
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'figures/plot.png' }));

  const { deck: portable, inlined, failed } = await inlineDeckAssets(deck, {
    resolveSrc: src => `/api/raw?${src}`,
  });

  assert.equal(failed.length, 0);
  assert.equal(inlined, 1, 'the same source is fetched once, however often it appears');
  assert.equal(calls.length, 1);
  assert.match(portable.slides[0].backgroundImage, /^data:image\/png;base64,/);
  assert.match(portable.slides[0].elements[2].src, /^data:image\/png;base64,/);
  assert.doesNotMatch(deckToHtml(portable), /\/api\//);
});

test('the original deck is left alone', async () => {
  stubFetch({ '/api/raw?a.png': { type: 'image/png', bytes: PNG_BYTES } });
  const deck = createDeck('Untouched');
  const withImage = addElement(deck, deck.slides[0].id, createElement('image', { src: 'a.png' }));
  await inlineDeckAssets(withImage, { resolveSrc: src => `/api/raw?${src}` });
  assert.equal(withImage.slides[0].elements[2].src, 'a.png', 'exporting must not edit the document');
});

test('a data URI is left as it is and never fetched', async () => {
  const calls = stubFetch({});
  const deck = createDeck('Already');
  const withImage = addElement(deck, deck.slides[0].id,
    createElement('image', { src: 'data:image/png;base64,iVBORw0KGgo=' }));
  const { inlined, failed } = await inlineDeckAssets(withImage, { resolveSrc: s => s });
  assert.equal(calls.length, 0);
  assert.equal(inlined, 0);
  assert.equal(failed.length, 0);
});

test('one unreachable picture costs that picture, not the whole export', async () => {
  stubFetch({ '/api/raw?there.png': { type: 'image/png', bytes: PNG_BYTES } });
  let deck = createDeck('Partial');
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'there.png' }));
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'gone.png' }));

  const { deck: portable, inlined, failed } = await inlineDeckAssets(deck, {
    resolveSrc: src => `/api/raw?${src}`,
  });
  assert.equal(inlined, 1);
  assert.deepEqual(failed, ['gone.png']);
  assert.match(portable.slides[0].elements[2].src, /^data:/);
  assert.equal(portable.slides[0].elements[3].src, 'gone.png', 'the reference is kept, not blanked');
});

// ─── themes ──────────────────────────────────────────────────────────────────
// A theme is the deck's whole look: colours, type, an optional picture, and the
// bands a Beamer-style theme draws. The chrome is theme data rather than
// elements, so it cannot be dragged away or fall out of step between slides.

function titledDeck() {
  let deck = createDeck('Themed');
  return addSlide(deck, {
    slide: createSlide({
      id: 'content',
      elements: [
        createElement('text', { id: 'title', x: 80, y: 60, w: 1120, h: 110, text: 'A title', bold: true }),
        createElement('text', { id: 'body', x: 80, y: 220, w: 1120, h: 300, text: 'Body' }),
      ],
    }),
  });
}

test('a deck with no theme chrome draws none', () => {
  const deck = titledDeck();
  assert.equal(chromeOf(deck, deck.slides[1]), null);
});

test('applying a theme marks each slide title so it takes the theme colour', () => {
  const deck = applyTheme(titledDeck(), { headerHeight: 180, titleColor: '#ffffff' });
  const content = deck.slides[1];
  assert.equal(content.elements[0].role, 'title');
  assert.equal(textColorOf(content.elements[0], deck.theme), '#ffffff');
  assert.equal(textColorOf(content.elements[1], deck.theme), deck.theme.color);
});

test('the header band is drawn only where there is a title', () => {
  const deck = applyTheme(titledDeck(), { headerHeight: 180, footerHeight: 40 });
  // The editor's default cover places its title below the band on purpose.
  assert.equal(chromeOf(deck, deck.slides[0]).header, 0);
  assert.equal(chromeOf(deck, deck.slides[1]).header, 180);
  assert.equal(chromeOf(deck, deck.slides[0]).footer, 40, 'the footline shows on every slide');
});

test('an explicit colour still wins over the theme', () => {
  const deck = applyTheme(titledDeck(), { headerHeight: 180, titleColor: '#ffffff' });
  const title = { ...deck.slides[1].elements[0], color: '#ff0000' };
  assert.equal(textColorOf(title, deck.theme), '#ff0000');
});

test('a title is found by weight and position when a deck predates roles', () => {
  const deck = titledDeck();
  assert.equal(titleElementOf(deck.slides[1]).id, 'title');
  assert.equal(titleElementOf(deck.slides[0]), null, 'a cover title sits below the band');
});

test('theme fields round-trip byte for byte', () => {
  const deck = applyTheme(titledDeck(), {
    headerHeight: 180, headerColor: '#3465a4', titleColor: '#ffffff',
    footerHeight: 40, footerColor: '#3465a4', footerText: 'title',
  });
  const once = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(once)), once);
  assert.equal(parseDeck(once).slides[1].elements[0].role, 'title');
});

test('a nonsense role or band is repaired rather than rejected', () => {
  const deck = parseDeck(JSON.stringify({
    theme: { headerHeight: -20, footerText: 'everything' },
    slides: [{ id: 's1', elements: [{ id: 't', type: 'text', text: 'x', role: 'subtitle' }] }],
  }));
  assert.equal(deck.theme.headerHeight, 0);
  assert.equal(deck.theme.footerText, '');
  assert.equal(deck.slides[0].elements[0].role, null);
});

test('HTML export draws the bands behind the elements and writes the footline', () => {
  const deck = applyTheme(titledDeck(), {
    headerHeight: 180, headerColor: '#3465a4', titleColor: '#ffffff',
    footerHeight: 40, footerColor: '#3465a4', footerText: 'title',
  });
  const html = deckToHtml(deck);
  const section = html.split('<section').slice(2).join('<section');   // the content slide
  assert.ok(section.includes('height:180px;background:#3465a4'));
  assert.ok(section.indexOf('class="el bg"') < section.indexOf('A title'),
    'the band must be drawn before the title it sits behind');
  assert.match(section, /Themed<\/span><span>2<\/span>/, 'the footline carries the deck title and the slide number');
  assert.match(section, /color:#ffffff/, 'the title takes the theme colour');
});

test('a cover exports without an empty band', () => {
  const deck = applyTheme(titledDeck(), { headerHeight: 180, headerColor: '#3465a4' });
  const cover = deckToHtml(deck).split('<section')[1];
  assert.ok(!cover.includes('height:180px'));
});

// ─── packing ─────────────────────────────────────────────────────────────────
// Which pictures a deck would lose if it were moved. The editor asks this to
// offer packing them, because it is the one thing about a deck's portability
// the user cannot see: a referenced picture and an embedded one draw the same.

test('a source that travels with the file is not external', () => {
  assert.ok(isPortableSource('data:image/png;base64,iVBORw0KGgo='));
  assert.ok(isPortableSource(`jpt:assets/${'a'.repeat(64)}.png`));
  assert.ok(isPortableSource('https://example.com/a.png'));
  assert.ok(isPortableSource(''));
  assert.ok(!isPortableSource('figures/a.png'));
});

test('external sources are collected from elements and both backgrounds, once each', () => {
  let deck = createDeck('Mixed');
  deck.theme.backgroundImage = 'figures/theme.png';
  deck.slides[0].backgroundImage = 'figures/slide.png';
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'figures/theme.png' }));
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'data:image/png;base64,x' }));
  assert.deepEqual(externalSourcesOf(deck).sort(), ['figures/slide.png', 'figures/theme.png']);
});

test('a deck whose pictures are all embedded has nothing to pack', () => {
  const deck = createDeck('Portable');
  assert.deepEqual(externalSourcesOf(deck), []);
});

test('packing turns every external source into a data URI, in one pass', async () => {
  const calls = stubFetch({ '/api/raw?figures/a.png': { type: 'image/png', bytes: PNG_BYTES } });
  let deck = createDeck('Pack me');
  deck.theme.backgroundImage = 'figures/a.png';
  deck = addElement(deck, deck.slides[0].id, createElement('image', { src: 'figures/a.png' }));

  const { deck: packed, inlined } = await inlineDeckAssets(deck, { resolveSrc: s => `/api/raw?${s}` });
  assert.equal(inlined, 1);
  assert.equal(calls.length, 1);
  assert.deepEqual(externalSourcesOf(packed), [], 'the packed deck depends on nothing');
});

// ─── video ───────────────────────────────────────────────────────────────────
// A video is the first element type whose `src` means two entirely different
// things, so most of what is worth testing is that the *derivation* — provider
// link versus file — comes out the same everywhere, and that nothing plays on
// a surface that cannot be paused.

test('a video round-trips with its playback fields in the declared order', () => {
  let deck = createDeck('Video');
  deck = addElement(deck, deck.slides[0].id, createElement('video', {
    id: 'v1', src: 'https://youtu.be/abc123def', poster: 'data:image/png;base64,x', start: 12,
  }));
  const text = serializeDeck(deck);
  assert.equal(serializeDeck(parseDeck(text)), text);
  // The order in the *file*, which is what has to match the Python writer byte
  // for byte (I1) — not the order of the in-memory object, which follows the
  // defaults and is put right by `ordered()` on the way out.
  const written = JSON.parse(text).slides[0].elements.find(el => el.id === 'v1');
  const payload = ['src', 'alt', 'fit', 'poster', 'autoplay', 'loop', 'muted', 'controls', 'start'];
  assert.deepEqual(Object.keys(written).filter(k => payload.includes(k)), payload);
});

test('a video keeps controls unless the deck turns them off', () => {
  const deck = parseDeck(JSON.stringify({
    ...createDeck('V'),
    slides: [{
      id: 's', elements: [
        { id: 'a', type: 'video', x: 0, y: 0, w: 10, h: 10, src: 'a.mp4' },
        { id: 'b', type: 'video', x: 0, y: 0, w: 10, h: 10, src: 'b.mp4', controls: false },
      ],
    }],
  }));
  assert.equal(deck.slides[0].elements[0].controls, true, 'absent means on');
  assert.equal(deck.slides[0].elements[1].controls, false);
});

test('a malformed video is repaired rather than rejected', () => {
  const deck = parseDeck(JSON.stringify({
    ...createDeck('V'),
    slides: [{
      id: 's',
      elements: [{
        id: 'a', type: 'video', x: 0, y: 0, w: 10, h: 10,
        src: 'a.mp4', fit: 'sideways', start: -30, loop: 'yes', poster: 7,
      }],
    }],
  }));
  const el = deck.slides[0].elements[0];
  assert.equal(el.fit, 'contain');
  assert.equal(el.start, 0);
  // A flag that is off by default takes an explicit `true` and nothing else,
  // so a deck cannot turn one on by accident; `controls`, which is on by
  // default, is the mirror of that and takes an explicit `false`.
  assert.equal(el.loop, false, "'yes' is not true");
  assert.equal(el.controls, true);
  assert.equal(el.poster, '');
});

test('a provider link and a file are told apart, whatever URL shape was pasted', () => {
  const kinds = (src) => videoSourceOf({ src }).kind;
  for (const src of [
    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    'https://youtu.be/dQw4w9WgXcQ',
    'https://www.youtube.com/embed/dQw4w9WgXcQ',
    'https://www.youtube.com/shorts/dQw4w9WgXcQ',
    'https://www.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ',
  ]) {
    assert.equal(kinds(src), 'youtube', src);
    assert.equal(videoSourceOf({ src }).id, 'dQw4w9WgXcQ', src);
  }
  assert.equal(kinds('https://vimeo.com/76979871'), 'vimeo');
  assert.equal(kinds('media/lecture.mp4'), 'file');
  // A plain URL to a file is a file, not a provider: a <video> plays it.
  assert.equal(kinds('https://example.org/clip.mp4'), 'file');
  assert.equal(videoSourceOf({ src: '   ' }), null);
});

test('a looping YouTube video names itself as its own playlist', () => {
  // YouTube loops a playlist, not a video; without this the video plays once.
  const url = videoEmbedUrl({ src: 'https://youtu.be/abc123def', loop: true });
  assert.match(url, /playlist=abc123def/);
  assert.match(url, /loop=1/);
});

test('an autoplaying embed is muted, because a browser would refuse it otherwise', () => {
  const url = videoEmbedUrl({ src: 'https://youtu.be/abc123def', autoplay: true }, { autoplay: true });
  assert.match(url, /autoplay=1/);
  assert.match(url, /mute=1/);
});

test('a start offset reaches a file as a media fragment', () => {
  assert.equal(videoFileUrl({ src: 'a.mp4', start: 90 }), 'a.mp4#t=90');
  assert.equal(videoFileUrl({ src: 'a.mp4' }), 'a.mp4');
});

test('the HTML export plays a video and the print document does not', () => {
  let deck = createDeck('V');
  deck = addElement(deck, deck.slides[0].id, createElement('video', {
    src: 'https://youtu.be/abc123def',
  }));
  const html = deckToHtml(deck, {});
  assert.match(html, /<iframe[^>]+youtube\.com\/embed\/abc123def/);

  const print = deckToPrintHtml(deck);
  assert.doesNotMatch(print, /<iframe|<video/, 'paper cannot play');
  assert.match(print, /<a href="https:\/\/www\.youtube\.com\/watch\?v=abc123def"/,
    'but it can carry the link');
  assert.match(print, /repeating-linear-gradient/,
    'the no-poster still matches the striped canvas placeholder');
  assert.match(print, /width="34" height="34"/,
    'the printed play badge matches the canvas badge');
});

test('a video contributes its poster and its file to what a deck would lose', () => {
  let deck = createDeck('V');
  deck = addElement(deck, deck.slides[0].id, createElement('video', {
    src: 'media/talk.mp4', poster: 'figures/still.png',
  }));
  deck = addElement(deck, deck.slides[0].id, createElement('video', {
    src: 'https://youtu.be/abc123def',
  }));
  assert.deepEqual(externalSourcesOf(deck).sort(), ['figures/still.png', 'media/talk.mp4'],
    'a provider link travels on its own and is not a missing asset');
});

test('packing embeds a poster but leaves the film alone unless media is asked for', async () => {
  stubFetch({
    '/api/raw?figures/still.png': { type: 'image/png', bytes: PNG_BYTES },
    '/api/raw?media/talk.mp4': { type: 'video/mp4', bytes: PNG_BYTES },
  });
  let deck = createDeck('V');
  deck = addElement(deck, deck.slides[0].id, createElement('video', {
    src: 'media/talk.mp4', poster: 'figures/still.png',
  }));
  const options = { resolveSrc: src => `/api/raw?${src}` };

  const videoOf = (result) => result.deck.slides[0].elements.find(el => el.type === 'video');

  const packed = videoOf(await inlineDeckAssets(deck, options));
  assert.match(packed.poster, /^data:image\/png/);
  assert.equal(packed.src, 'media/talk.mp4',
    'a film in the .jpt would make every open re-parse the film');

  const portable = videoOf(await inlineDeckAssets(deck, { ...options, media: true }));
  assert.match(portable.src, /^data:video\/mp4/);
});

// ─── equations in PowerPoint ─────────────────────────────────────────────────
// The conversion is LaTeX → KaTeX's MathML → OMML. What is worth pinning down
// is the shape of the OMML for each construction, and — just as much — that an
// unconvertible formula says so instead of producing something plausible.

test('the constructions a deck actually uses become OMML, not text', () => {
  const cases = {
    '\\frac{a}{b}': /<m:f>.*<m:num>.*<m:den>/s,
    '\\sqrt{x}': /<m:rad>.*<m:degHide m:val="1"\/>/s,
    '\\sqrt[3]{x}': /<m:rad>.*<m:degHide m:val="0"\/>.*<m:deg>/s,
    'x_i': /<m:sSub>/,
    'x^2': /<m:sSup>/,
    'x_i^2': /<m:sSubSup>/,
    '\\vec{D}': /<m:acc>.*<m:chr m:val="⃗"\/>/s,
    '\\sum_{i=0}^{n} x': /<m:nary>.*<m:chr m:val="∑"\/>/s,
    '\\left(\\frac{a}{b}\\right)': /<m:d>.*<m:begChr m:val="\("\/>/s,
    '\\begin{aligned} a &= 1 \\\\ b &= 2 \\end{aligned}': /<m:eqArr>/,
    '\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}': /<m:m>.*<m:mr>/s,
    '\\lim_{x \\to 0} f': /<m:limLow>/,
  };
  for (const [latex, pattern] of Object.entries(cases)) {
    const omml = latexToOmml(latex, { sz: 2400 });
    assert.ok(omml, `${latex} produced no OMML`);
    assert.match(omml, pattern, latex);
  }
});

test('a variable is italic and a number, operator or function name is not', () => {
  const omml = latexToOmml('\\sin x = 2', { sz: 2400 });
  // `sin` is a name and upright; `x` is a variable and italic.
  assert.match(omml, /<m:sty m:val="p"\/>[\s\S]*?i="0"[\s\S]*?<m:t>sin<\/m:t>/);
  assert.match(omml, /<m:sty m:val="i"\/>[\s\S]*?i="1"[\s\S]*?<m:t>x<\/m:t>/);
  assert.match(omml, /<m:sty m:val="p"\/>[\s\S]*?<m:t>2<\/m:t>/);
});

test('the deck size and colour reach every run and every fraction bar', () => {
  const omml = latexToOmml('\\frac{a}{b}', { sz: 2700, color: '19376D' });
  assert.equal((omml.match(/sz="2700"/g) || []).length >= 3, true);
  assert.match(omml, /<m:ctrlPr><a:rPr[^>]*sz="2700"[^>]*><a:solidFill><a:srgbClr val="19376D"\/>/,
    'a blue equation with a black fraction bar is a bug, not a nuance');
});

test('LaTeX that will not compile falls back rather than producing a guess', () => {
  assert.equal(latexToOmml('\\frac{a}', { sz: 2400 }), null);
  assert.equal(latexToOmml('\\thisIsNotAMacro{x}', { sz: 2400 }), null);
  assert.equal(latexToOmml('', { sz: 2400 }), null);
  assert.equal(latexToOmml('   ', { sz: 2400 }), null);
});

test('MathML with an element nobody mapped stops instead of approximating', () => {
  const known = '<math><mrow><mi>x</mi></mrow></math>';
  assert.ok(mathmlToOmml(known));
  const unknown = '<math><mrow><mi>x</mi><mglyph src="x.png"/></mrow></math>';
  assert.equal(mathmlToOmml(unknown), null);
});

test('the tiny XML reader handles what KaTeX emits, and rejects what it does not', () => {
  const tree = parseXml('<math display="block"><mrow><mo stretchy="false">(</mo></mrow></math>');
  const math = tree.children[0];
  assert.equal(math.name, 'math');
  assert.equal(math.attrs.display, 'block');
  assert.equal(math.children[0].children[0].attrs.stretchy, 'false');
  assert.throws(() => parseXml('<math><mrow></math>'));
});

test('escaping ampersands is idempotent, so repairing correct markup is safe', () => {
  assert.equal(escapeBareAmpersands('a=1&b=2'), 'a=1&amp;b=2');
  assert.equal(escapeBareAmpersands('a=1&amp;b=2'), 'a=1&amp;b=2');
  assert.equal(escapeBareAmpersands('&#38;&lt;'), '&#38;&lt;');
});

// ─── pictures in PowerPoint ──────────────────────────────────────────────────
// PowerPoint has no `object-fit`: it stretches a picture to the frame it is
// given. The export therefore has to compute the letterbox itself, and to do
// that it has to know the pixel size of the image.

test('an image header is read without a browser to decode it', () => {
  // A 2x1 PNG: the IHDR width and height sit at fixed offsets.
  const png = new Uint8Array(24);
  png.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  png[19] = 2;
  png[23] = 1;
  assert.deepEqual(sizeFromBytes(png), { w: 2, h: 1 });

  const gif = new Uint8Array(16);
  gif.set([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x40, 0x00, 0x20, 0x00]);
  assert.deepEqual(sizeFromBytes(gif), { w: 64, h: 32 });

  assert.equal(sizeFromBytes(new Uint8Array(4)), null, 'too short to say anything');
});

test('contain letterboxes inside the box and cover crops instead', () => {
  const box = { x: 0, y: 0, w: 100, h: 100 };

  const contained = fitRect(box, { w: 200, h: 100 }, 'contain');
  assert.deepEqual(contained.rect, { x: 0, y: 25, w: 100, h: 50 });
  assert.equal(contained.crop, null);

  const covered = fitRect(box, { w: 200, h: 100 }, 'cover');
  assert.deepEqual(covered.rect, box, 'cover keeps the whole box');
  assert.equal(covered.crop.left, 0.25);
  assert.equal(covered.crop.top, 0);

  assert.deepEqual(fitRect(box, { w: 200, h: 100 }, 'fill').rect, box);
  assert.deepEqual(fitRect(box, null, 'contain').rect, box,
    'an image that could not be measured is stretched, as it was before');
});

// ─── the print document ──────────────────────────────────────────────────────

test('the print document asks for pages the size of the deck', () => {
  const deck = createDeck('Printed');
  const html = deckToPrintHtml(deck);
  assert.match(html, /@page \{ size: 1280px 720px; margin: 0; \}/,
    'without this the PDF is letterboxed onto A4');
  assert.equal((html.match(/class="slide"/g) || []).length, deck.slides.length);
});
