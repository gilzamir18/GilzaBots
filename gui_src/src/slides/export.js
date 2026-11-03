// ─────────────────────────────────────────────────────────────────────────────
// export.js
//
// Getting a deck out of GilzaBots, in the three shapes people actually ask for:
//
//   • PPTX  — a real, editable PowerPoint file. Built with PptxGenJS (MIT),
//             entirely in the browser: no server round-trip and no headless
//             browser, which is what keeps this working in the packaged app.
//   • HTML  — one self-contained file that presents in any browser, with the
//             slides inlined as absolutely positioned boxes and arrow-key
//             navigation. No runtime dependency, nothing to install.
//   • PDF   — through the browser's own print-to-PDF, driving a print window
//             whose @page matches the deck aspect exactly.
//
// PptxGenJS is imported lazily. It is the single largest dependency the deck
// editor pulls in, and a user who never exports should never pay for it.
// ─────────────────────────────────────────────────────────────────────────────

import {
  arrowsOf, backgroundOf, borderOf, bulletMetricsOf, chromeOf, isLineShape,
  textColorOf, textLinesOf,
} from './model.js';
import { insetPolygon, polygonPoints, trianglePoints } from './geometry.js';
import { renderEquation } from './equation.js';
import { fitRect, naturalSize } from './imageSize.js';
import {
  isEmbeddedVideo, videoEmbedUrl, videoExtensionOf, videoFileUrl, videoLabelOf,
  videoMimeOf, videoSourceOf, videoWatchUrl,
} from './video.js';
import { latexToOmml } from './omml.js';
import { fontFamilyWithMathFallback } from './fonts.js';

// Equations travel as MathML, laid out by the reading browser itself. That is
// what makes an exported deck self-contained: no stylesheet beside it, no font
// files to lose, and a formula that stays selectable text rather than becoming
// a picture of one. The two rules below are the whole of what it needs — the
// first undoes katex.min.css's 1.21em where a viewer happens to have KaTeX
// loaded, so the deck reads the same with and without it; the second asks for a
// font with an OpenType MATH table, without which a browser draws a stretchy
// brace at single-line height (see mathFont.css).
const EXPORT_MATH_CSS = `
  .katex { font-size: 1em; }
  math { font-family: 'STIX Two Math', 'Cambria Math', 'Latin Modern Math', KaTeX_Main, serif; margin: 0; }
`;

// The editor loads these faces in index.html. An iframe is a separate document
// and does not inherit its parent's @font-face rules, so the PDF document must
// request the same stylesheet or an Inter deck reflows in a platform fallback
// immediately before it is printed.
const EDITOR_FONT_LINKS = `
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@300;400;500;600;700&amp;family=Inter:wght@300;400;500;600;700&amp;family=Outfit:wght@300;400;500;600;700;800&amp;display=swap" rel="stylesheet">`;

// PPTX measures in inches. A 16:9 deck is 10in wide by convention, so one deck
// unit is 10/width inches — the single conversion every mapping below uses.
const PPTX_WIDTH_IN = 10;

function inchesPerUnit(deck) {
  return PPTX_WIDTH_IN / deck.width;
}

// PowerPoint sizes text in points, and 1in = 72pt.
function pointsFor(deck, fontSize) {
  return Math.max(1, Math.round(fontSize * inchesPerUnit(deck) * 72 * 10) / 10);
}

function hex(color, fallback) {
  const value = color || fallback || '#000000';
  const match = /^#?([0-9a-f]{6})$/i.exec(String(value).trim());
  return match ? match[1].toUpperCase() : '000000';
}

const PPTX_SHAPES = {
  rect: 'rect',
  ellipse: 'ellipse',
  triangle: 'triangle',
  line: 'line',
  arrow: 'line',
};

// PowerPoint takes rotation in whole degrees, clockwise, like CSS.
function rotationOf(el) {
  const deg = Math.round(el.rotation || 0);
  return deg ? { rotate: deg } : {};
}

// PptxGenJS takes a picture as either `data` (a data URI it embeds verbatim)
// or `path` (a location it fetches). A data URI handed to `path` only works by
// accident — the browser's `fetch` happens to accept one — and fails outright
// under Node, which reads a `path` from the filesystem. So the two cases are
// told apart here rather than left to chance.
function pictureSource(src, resolveSrc) {
  if (String(src).startsWith('data:')) return { data: src };
  return { path: resolveSrc ? resolveSrc(src) : src };
}

// The marker the OMML pass looks for.
//
// PptxGenJS cannot write an equation, so an equation is written twice: once by
// PptxGenJS as an ordinary text box holding the LaTeX source, and once here as
// the OMML that PowerPoint's equation editor actually edits. The two are joined
// after the file is built (`injectEquations`), and this name on the shape is how
// the second pass finds the first one's work.
function equationMarker(index) {
  return `gilzabots-equation-${index}`;
}

// PowerPoint pads the inside of a text box by 0.1in left and right and 0.05in
// top and bottom, and the editor pads by nothing. Left alone, that is a fifth
// of an inch of width taken out of every text box on export — enough to rewrap
// a line and to shift every left edge visibly right of where the user put it.
const NO_MARGIN = 0;

/**
 * A text box, in the terms PowerPoint uses for the things the editor draws
 * with CSS.
 *
 * `lineSpacing` rather than `lineSpacingMultiple` is the load-bearing choice.
 * PowerPoint's percentage line spacing is a percentage of *single* spacing,
 * which already includes the font's own leading — roughly 1.2em — so a deck
 * asking for `line-height: 1.5` and exported as 150% comes out at about 1.8em
 * and overflows the box it was fitted to. Exact point spacing is the same
 * quantity CSS means, so the two agree.
 */
function textOptions(deck, { fontSize, color, align, valign, lineHeight }) {
  const size = pointsFor(deck, fontSize);
  return {
    fontSize: size,
    color,
    align,
    valign,
    lineSpacing: Math.round(size * (lineHeight ?? 1.3) * 10) / 10,
    margin: NO_MARGIN,
    // PowerPoint grows a text box to fit by default, which would move
    // everything the user carefully positioned around it.
    autoFit: false,
    shrinkText: false,
    wrap: true,
    isTextBox: true,
  };
}

// PowerPoint's own numbering formats, in the order the model cycles its number
// markers. Named rather than drawn: a numbered list exported as literal "1."
// text would stop renumbering the moment someone inserted a line in PowerPoint.
const PPTX_NUMBER_TYPES = ['arabicPeriod', 'alphaLcPeriod', 'romanLcPeriod'];

/**
 * A text box as PowerPoint paragraphs — the one place the deck's lists become
 * real PowerPoint lists.
 *
 * `indentLevel` and `bullet` are what PowerPoint uses for exactly this, so a
 * deck exported from here opens as something the recipient can carry on
 * editing: Tab still indents, the numbering still renumbers, and the marker is
 * an attribute of the paragraph rather than two characters someone has to
 * delete. The glyph is taken from the marker the model already resolved, so
 * neither side keeps its own table of bullet characters (I7b).
 */
export function pptxParagraphs(deck, el) {
  const { gutter } = bulletMetricsOf(el);
  const indent = Math.max(1, Math.round(pointsFor(deck, gutter)));
  return textLinesOf(el).map((line) => {
    let bullet = false;
    if (line.marker && el.bullet === 'number') {
      bullet = {
        type: 'number',
        numberType: PPTX_NUMBER_TYPES[line.level % PPTX_NUMBER_TYPES.length],
        indent,
      };
    } else if (line.marker) {
      bullet = {
        characterCode: line.marker.codePointAt(0).toString(16).toUpperCase().padStart(4, '0'),
        indent,
      };
    }
    return {
      text: line.text,
      options: { breakLine: true, indentLevel: line.level, bullet },
    };
  });
}

/**
 * The deck as `.pptx` bytes.
 *
 * Split from `exportPptx` so the file can be built where there is no browser to
 * download it to — which is what lets the export be tested against a real deck
 * rather than only clicked.
 */
export async function buildPptx(deck, { resolveSrc } = {}) {
  const { default: PptxGenJS } = await import('pptxgenjs');
  const pptx = new PptxGenJS();
  const scale = inchesPerUnit(deck);

  // A custom layout rather than one of the presets: a deck whose aspect the
  // user changed must not be silently letterboxed into 16:9 on export.
  pptx.defineLayout({
    name: 'GILZABOTS',
    width: PPTX_WIDTH_IN,
    height: deck.height * scale,
  });
  pptx.layout = 'GILZABOTS';
  if (deck.title) pptx.title = deck.title;

  // Filled in as the slides are built, and consumed by the OMML pass at the
  // end. An equation whose LaTeX KaTeX cannot parse never gets an entry, so it
  // keeps the source text box PptxGenJS wrote — the export's behaviour before
  // equations were convertible at all.
  const equations = [];
  const missing = [];
  let equationsAsSource = 0;
  // Whether any relationship in the file points outside it. Only those need the
  // escaping repair below, so a deck without one never pays for the pass.
  let linkedMedia = false;

  for (const [index, slide] of deck.slides.entries()) {
    const target = pptx.addSlide();
    const background = backgroundOf(deck, slide);
    // PowerPoint takes either a colour or a picture, not a picture over a
    // colour at an opacity — so a dimmed background exports at full strength.
    // Listed as a known gap rather than silently dropped: the picture is the
    // part the slide was designed around.
    target.background = background.image
      ? pictureSource(background.image, resolveSrc)
      : { color: hex(background.color, '#FFFFFF') };

    // The theme's bands, added before the elements so they sit behind them.
    const chrome = chromeOf(deck, slide);
    if (chrome) {
      if (chrome.header > 0) {
        target.addShape('rect', {
          x: 0, y: 0, w: PPTX_WIDTH_IN, h: chrome.header * scale,
          fill: { color: hex(chrome.headerColor, '#2F6FB3') },
        });
      }
      if (chrome.footer > 0) {
        target.addShape('rect', {
          x: 0, y: (deck.height - chrome.footer) * scale,
          w: PPTX_WIDTH_IN, h: chrome.footer * scale,
          fill: { color: hex(chrome.footerColor, '#2F6FB3') },
        });
        if (chrome.footerText === 'title') {
          // The same two boxes the editor draws: the deck title against the
          // left inset, the slide number against the right one. They are laid
          // out side by side rather than overlapping, which is what the number
          // box used to do to the end of a long title.
          const inset = deck.width * 0.0625 * scale;
          const numberWidth = inset;
          const fontSize = Math.max(12, chrome.footer * 0.42);
          const band = {
            y: (deck.height - chrome.footer) * scale,
            h: chrome.footer * scale,
            color: hex(chrome.footerTextColor, '#FFFFFF'),
            fontFace: fontFaceOf(deck.theme.fontFamily),
          };
          const common = textOptions(deck, {
            fontSize, color: band.color, align: 'left', valign: 'middle', lineHeight: 1,
          });
          target.addText(deck.title || '', {
            ...band, ...common,
            x: inset,
            w: PPTX_WIDTH_IN - inset * 2 - numberWidth,
          });
          target.addText(String(index + 1), {
            ...band, ...common,
            align: 'right',
            x: PPTX_WIDTH_IN - inset - numberWidth,
            w: numberWidth,
          });
        }
      }
    }

    for (const el of slide.elements) {
      const box = {
        x: el.x * scale,
        y: el.y * scale,
        w: el.w * scale,
        h: el.h * scale,
      };
      const opacity = el.opacity ?? 1;
      // PowerPoint states the inverse of what the model does: a percentage of
      // the object that is *not* there.
      const transparency = opacity >= 1 ? 0 : Math.round((1 - opacity) * 100);

      if (el.type === 'text') {
        if (!el.text) continue;
        target.addText(pptxParagraphs(deck, el), {
          ...box,
          ...rotationOf(el),
          ...textOptions(deck, {
            fontSize: el.fontSize,
            color: hex(textColorOf(el, deck.theme), '#1A1A1A'),
            align: el.align,
            valign: el.valign,
            lineHeight: el.lineHeight,
          }),
          fontFace: fontFaceOf(el.fontFamily || deck.theme.fontFamily),
          bold: !!el.bold,
          italic: !!el.italic,
          underline: el.underline ? { style: 'sng' } : undefined,
          transparency,
        });
      } else if (el.type === 'equation') {
        if (!el.latex || !el.latex.trim()) continue;
        const sizePt = pointsFor(deck, el.fontSize);
        const color = hex(el.color || deck.theme.color, '#1A1A1A');
        const omml = latexToOmml(el.latex, {
          displayMode: el.displayMode !== false,
          // PowerPoint sizes text in hundredths of a point.
          sz: Math.round(sizePt * 100),
          color,
        });
        const marker = equationMarker(equations.length);
        if (omml) {
          equations.push({ marker, omml, sizePt, color });
        } else {
          equationsAsSource += 1;
        }
        // Written either way. When the OMML pass finds this shape it keeps it
        // as the fallback PowerPoint shows to a reader that cannot render
        // mathematics; when there is no OMML it is the export, exactly as it
        // was before.
        target.addText(el.latex, {
          ...box,
          ...rotationOf(el),
          ...textOptions(deck, {
            fontSize: el.fontSize,
            color,
            align: 'center',
            valign: 'middle',
            lineHeight: 1.2,
          }),
          objectName: marker,
          fontFace: 'Cambria Math',
          // The source of a formula is longer than the formula, so this is the
          // one text box in the deck that is allowed to shrink to fit rather
          // than overflow the space the equation had.
          shrinkText: !omml,
          transparency,
        });
      } else if (el.type === 'image' && el.src) {
        // `object-fit` has no counterpart in PowerPoint, which stretches a
        // picture to its frame, so the frame is computed here instead: a
        // `contain` picture gets a smaller centred box, a `cover` picture keeps
        // the box and gets cropped.
        const natural = await naturalSize(resolveSrc && !el.src.startsWith('data:')
          ? resolveSrc(el.src)
          : el.src);
        if (!natural) missing.push(el.src);
        const { rect, crop } = fitRect(box, natural, el.fit || 'contain');
        target.addImage({
          ...rect,
          ...rotationOf(el),
          ...pictureSource(el.src, resolveSrc),
          altText: el.alt || '',
          transparency,
          ...(crop
            ? { sizing: { type: 'crop', x: crop.left * box.w, y: crop.top * box.h, w: box.w, h: box.h } }
            : {}),
        });
      } else if (el.type === 'video' && el.src) {
        if (isEmbeddedVideo(el)) linkedMedia = true;
        addPptxVideo(target, el, box, resolveSrc);
      } else if (el.type === 'shape') {
        const kind = PPTX_SHAPES[el.shape] || 'rect';
        const line = isLineShape(el);
        const heads = arrowsOf(el);
        // A shape's border. PowerPoint centres an outline on the shape's edge
        // where the editor keeps it inside, so a thick border sits half a width
        // further out there; the alternative — shrinking the box on export —
        // would move an element the user positioned, which is worse.
        const border = borderOf(el);
        target.addShape(kind, {
          ...box,
          ...rotationOf(el),
          fill: line ? undefined : { color: hex(el.fill, '#2F6FB3'), transparency },
          line: line
            ? {
              color: hex(el.stroke || el.fill, '#2F6FB3'),
              width: pointsFor(deck, el.strokeWidth || 4),
              transparency,
              // PowerPoint names the ends "begin" and "end"; a double-headed
              // arrow is simply both, which is why the model stores them
              // separately instead of as one arrow shape.
              beginArrowType: heads.start ? 'triangle' : undefined,
              endArrowType: heads.end ? 'triangle' : undefined,
            }
            : (border
              ? { color: hex(border.color, '#1A1A1A'), width: pointsFor(deck, border.width), transparency }
              : undefined),
          rectRadius: el.shape === 'rect' && el.radius ? el.radius / 100 : undefined,
        });
      }
    }

    if (slide.notes) target.addNotes(slide.notes);
  }

  // Built to bytes rather than written to disk, so the equations can be put in
  // before the user gets the file.
  const built = await pptx.write({ outputType: 'arraybuffer' });
  const bytes = equations.length || linkedMedia
    ? await finalizePptx(built, { equations, repairLinks: linkedMedia })
    : built;
  return { bytes, failed: missing, equations: equations.length, equationsAsSource };
}

const PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation';

export async function exportPptx(deck, { resolveSrc, fileName = 'presentation.pptx' } = {}) {
  const { bytes, ...result } = await buildPptx(deck, { resolveSrc });
  downloadBlob(new Blob([bytes], { type: PPTX_MIME }), fileName);
  return result;
}

/**
 * A video as a real PowerPoint media object.
 *
 * The two kinds stay two kinds all the way down: a provider's video becomes an
 * *online* video — a link PowerPoint plays in place, the same object its own
 * "Insert > Video > Online Video" produces — and a file becomes an embedded
 * media part. Neither is a picture of a video, so the deck arrives able to
 * play, which is the entire point of putting one on a slide.
 *
 * The still shown before playback has to be a PNG: that is the only thing
 * PptxGenJS writes a cover as, and handing it a JPEG would store one under a
 * `.png` name. A poster that is not a PNG is therefore left out and PowerPoint
 * shows its own play-button plate — a known gap, recorded in the format spec,
 * rather than a file that might not open.
 */
function addPptxVideo(target, el, box, resolveSrc) {
  const source = videoSourceOf(el);
  if (!source) return;

  const cover = /^data:image\/png;base64,/i.test(el.poster || '') ? { cover: el.poster } : {};
  const common = { ...box, ...rotationOf(el), ...cover, altText: el.alt || '' };

  if (source.kind !== 'file') {
    // PowerPoint stores an online video as its *embed* address, not the page a
    // person would visit — the player it opens is the embedded one.
    target.addMedia({ ...common, type: 'online', link: videoEmbedUrl(el) });
    return;
  }

  // The extension is taken from the deck's own source rather than from the URL
  // it resolves to: a project file arrives as `/api/file/raw?...`, whose last
  // dot-segment is a query parameter, not a format.
  const extn = videoExtensionOf(source.url) || (videoMimeOf(source.url).split('/')[1] || 'mp4');
  target.addMedia({
    ...common,
    type: 'video',
    extn,
    ...pictureSource(source.url, resolveSrc),
  });
}

/** The first family of a CSS font stack, which is all PowerPoint takes. */
function fontFaceOf(stack) {
  return String(stack || '').split(',')[0].trim().replace(/^['"]|['"]$/g, '') || undefined;
}

// ─── equations ───────────────────────────────────────────────────────────────
//
// PowerPoint stores an equation as OMML inside the shape's text body, in an
// element (`a14:m`) from an extension namespace that predates nothing else in
// the file. The way Office itself writes that — and the reason this pass
// bothers to imitate it rather than just dropping the OMML in — is
// `mc:AlternateContent`: the shape appears twice, once for a reader that
// understands the extension and once for a reader that does not.
//
// That is exactly the behaviour worth having here. PowerPoint, Word and
// LibreOffice show a real equation object the user can click into and edit;
// anything older shows the LaTeX source, which is what this export produced
// before and is still the honest answer when nothing better can be displayed.

const A14_NS = 'http://schemas.microsoft.com/office/drawing/2010/main';
const MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006';
const MATH_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math';

/**
 * The `<p:sp>…</p:sp>` element containing `at`, as a `[start, end)` range.
 *
 * Found by scanning rather than by a regular expression: a shape's XML contains
 * other elements and a non-greedy match would end at the first `</p:sp>` that
 * follows the marker, which for a nested structure is the wrong one.
 */
function shapeRangeAt(xml, at) {
  const start = xml.lastIndexOf('<p:sp>', at);
  if (start === -1) return null;
  let depth = 0;
  let cursor = start;
  while (cursor < xml.length) {
    const open = xml.indexOf('<p:sp>', cursor + 1);
    const close = xml.indexOf('</p:sp>', cursor + 1);
    if (close === -1) return null;
    if (open !== -1 && open < close) {
      depth += 1;
      cursor = open;
      continue;
    }
    if (depth === 0) return [start, close + '</p:sp>'.length];
    depth -= 1;
    cursor = close;
  }
  return null;
}

/** One equation shape, rewritten as a Choice/Fallback pair. */
function withEquation(shape, omml, sizePt, color) {
  // The choice is the same shape with its paragraphs replaced by the formula.
  // Everything outside `<p:txBody>` — the position, the size, the rotation —
  // is kept exactly, so the equation lands where the LaTeX box was.
  const bodyStart = shape.indexOf('<a:p>');
  const bodyEnd = shape.lastIndexOf('</a:p>');
  if (bodyStart === -1 || bodyEnd === -1) return null;

  const endRPr = `<a:endParaRPr lang="en-US" sz="${Math.round(sizePt * 100)}" dirty="0">`
    + `<a:solidFill><a:srgbClr val="${color}"/></a:solidFill></a:endParaRPr>`;
  const paragraph = '<a:p><a:pPr algn="ctr"/>'
    + `<a14:m xmlns:a14="${A14_NS}">`
    + `<m:oMathPara xmlns:m="${MATH_NS}">`
    + '<m:oMathParaPr><m:jc m:val="centerGroup"/></m:oMathParaPr>'
    + `<m:oMath>${omml}</m:oMath>`
    + '</m:oMathPara></a14:m>'
    + `${endRPr}</a:p>`;

  const choice = shape.slice(0, bodyStart) + paragraph + shape.slice(bodyEnd + '</a:p>'.length);
  return `<mc:AlternateContent xmlns:mc="${MC_NS}">`
    + `<mc:Choice xmlns:a14="${A14_NS}" Requires="a14">${choice}</mc:Choice>`
    + `<mc:Fallback>${shape}</mc:Fallback>`
    + '</mc:AlternateContent>';
}

/**
 * The built `.pptx`, with every marked equation shape turned into a real
 * PowerPoint equation.
 *
 * Works on the finished archive rather than during generation because
 * PptxGenJS has no seam for it: it writes the slide XML from its own object
 * model and offers no way to contribute an element. Rewriting the parts
 * afterwards keeps the whole of that generator intact — every position, font
 * and relationship it computed is exactly what ships.
 */
async function injectEquations(zip, equations) {
  const byMarker = new Map(equations.map(entry => [entry.marker, entry]));

  const slides = Object.keys(zip.files).filter(name => /^ppt\/slides\/slide\d+\.xml$/.test(name));
  for (const name of slides) {
    let xml = await zip.file(name).async('string');
    let changed = false;
    // Rewritten back to front: every replacement changes the length of the
    // document, and working forwards would invalidate the offsets of the
    // shapes not yet reached.
    const hits = [];
    for (const [marker, entry] of byMarker) {
      const at = xml.indexOf(`name="${marker}"`);
      if (at !== -1) hits.push({ at, entry });
    }
    for (const { at, entry } of hits.sort((a, b) => b.at - a.at)) {
      const range = shapeRangeAt(xml, at);
      if (!range) continue;
      const replacement = withEquation(
        xml.slice(range[0], range[1]), entry.omml, entry.sizePt, entry.color,
      );
      if (!replacement) continue;
      xml = xml.slice(0, range[0]) + replacement + xml.slice(range[1]);
      changed = true;
      byMarker.delete(entry.marker);
    }
    if (changed) zip.file(name, xml);
  }
}

// ─── relationship escaping ───────────────────────────────────────────────────

/**
 * Every bare `&` in `xml` written as `&amp;`.
 *
 * Idempotent: an ampersand that already begins a character reference is left
 * alone, so running this over markup that is already correct changes nothing.
 * That is what makes it safe to apply to a generator's output without having to
 * know whether this particular version escaped or not.
 */
export function escapeBareAmpersands(xml) {
  return xml.replace(/&(?!#\d+;|#x[0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]*;)/g, '&amp;');
}

/**
 * Repairs the relationship parts, whose external targets PptxGenJS writes into
 * the XML without escaping them.
 *
 * A YouTube embed URL carries its playback options as query parameters, so it
 * contains `&` — and an unescaped `&` in an attribute is not well-formed XML.
 * PowerPoint does not open the file at all: not a video that fails to play, the
 * whole deck refused. The library is a third-party dependency and is not
 * patched; the file it produced is repaired here instead, in the pass that is
 * already open over the archive.
 */
async function repairRelationshipTargets(zip) {
  const parts = Object.keys(zip.files).filter(name => name.endsWith('.rels'));
  for (const name of parts) {
    const xml = await zip.file(name).async('string');
    const fixed = escapeBareAmpersands(xml);
    if (fixed !== xml) zip.file(name, fixed);
  }
}

/** The built archive, with everything PptxGenJS could not write put right. */
async function finalizePptx(buffer, { equations, repairLinks }) {
  const { default: JSZip } = await import('jszip');
  const zip = await JSZip.loadAsync(buffer);
  if (equations.length) await injectEquations(zip, equations);
  if (repairLinks) await repairRelationshipTargets(zip);
  return zip.generateAsync({ type: 'arraybuffer', compression: 'DEFLATE' });
}

// ─── self-containment ────────────────────────────────────────────────────────
// A picture the user picked or pasted is already a data URI, but one an agent
// referenced by project path — `figures/plot.png` — resolves through the IDE's
// own server, and that URL means nothing on the machine the deck is sent to.
// So every remaining project reference is fetched and inlined before the deck
// leaves the app. The `.html` export is the case that visibly breaks without
// it; the PDF path benefits too, because a data URI removes the race between
// the print dialog and an image that has not finished loading.

const DATA_URI_CHUNK = 0x8000;

const MIME_BY_EXTENSION = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', svg: 'image/svg+xml', avif: 'image/avif', bmp: 'image/bmp',
};

function mimeFor(source, headerType) {
  if (headerType && headerType !== 'application/octet-stream') return headerType.split(';')[0];
  const extension = String(source).split('?')[0].split('.').pop().toLowerCase();
  return MIME_BY_EXTENSION[extension] || 'application/octet-stream';
}

/** Bytes as a data URI. Chunked because spreading a megabyte-long array into
 *  `String.fromCharCode` overflows the call stack. */
function toDataUri(buffer, mime) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let index = 0; index < bytes.length; index += DATA_URI_CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + DATA_URI_CHUNK));
  }
  return `data:${mime};base64,${btoa(binary)}`;
}

/** True for a source that already travels with the file. */
function isSelfContained(src) {
  return !src || src.startsWith('data:');
}

async function fetchAsDataUri(src, resolveSrc) {
  const url = resolveSrc ? resolveSrc(src) : src;
  if (!url) return null;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const buffer = await response.arrayBuffer();
  return toDataUri(buffer, mimeFor(src, response.headers.get('content-type')));
}

/**
 * A copy of `deck` in which every picture is a data URI.
 *
 * Returns what it could not inline rather than throwing: one unreachable image
 * must not cost the user the whole export, and the deck still renders — with
 * that one picture broken, exactly as it is broken in the editor, which is
 * where they have already seen it.
 *
 * `media` extends the same treatment to video files, and is off by default
 * because most exports cannot use the bytes and would only pay for them: a PDF
 * shows a video's still whatever happens. The JPT package stores films as
 * binary members, outside this JSON conversion. HTML turns `media` on because
 * being one self-contained exported file is the whole point of that format.
 */
export async function inlineDeckAssets(deck, { resolveSrc, media = false } = {}) {
  const cache = new Map();
  const failed = [];
  let inlined = 0;

  const resolveOne = async (src) => {
    if (isSelfContained(src)) return src;
    if (cache.has(src)) return cache.get(src);
    let result = src;
    try {
      result = (await fetchAsDataUri(src, resolveSrc)) || src;
      if (result !== src) inlined += 1;
    } catch {
      failed.push(src);
    }
    cache.set(src, result);
    return result;
  };

  const theme = { ...deck.theme };
  theme.backgroundImage = await resolveOne(theme.backgroundImage || '');

  const slides = [];
  for (const slide of deck.slides) {
    const elements = [];
    for (const el of slide.elements) {
      if (el.type === 'image' && el.src) {
        elements.push({ ...el, src: await resolveOne(el.src) });
      } else if (el.type === 'video') {
        // A video's still is a picture and travels like every other picture.
        // The film itself only travels when the caller asked for media, and
        // only when it is a file — there is nothing to inline about a link to
        // YouTube, and the player fetches it anyway.
        const next = { ...el };
        if (next.poster) next.poster = await resolveOne(next.poster);
        if (media && next.src && !isEmbeddedVideo(next)) {
          next.src = await resolveOne(next.src);
        }
        elements.push(next);
      } else {
        elements.push(el);
      }
    }
    slides.push({
      ...slide,
      backgroundImage: slide.backgroundImage
        ? await resolveOne(slide.backgroundImage)
        : slide.backgroundImage,
      elements,
    });
  }

  return { deck: { ...deck, theme, slides }, inlined, failed };
}

// ─── HTML ────────────────────────────────────────────────────────────────────

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// The picture behind a slide, as the first child of the section so everything
// else paints over it. Mirrors SlideBackground in the editor exactly; the two
// cannot be one function because an export ships no React.
function backgroundToHtml(background, resolveSrc) {
  if (!background.image) return '';
  const src = resolveSrc ? resolveSrc(background.image) : background.image;
  if (!src) return '';
  return `<img class="el bg" alt="" aria-hidden="true" style="left:0;top:0;width:100%;height:100%;`
    + `object-fit:${background.fit};opacity:${background.opacity};" src="${escapeHtml(src)}">`;
}

// The theme's bands as markup. Mirrors SlideChrome; the two cannot be one
// function because an export ships no React.
function chromeToHtml(deck, slide, index) {
  const chrome = chromeOf(deck, slide);
  if (!chrome) return '';
  const inset = Math.round(deck.width * 0.0625);
  const parts = [];
  if (chrome.header > 0) {
    parts.push(`<div class="el bg" style="left:0;top:0;width:${deck.width}px;`
      + `height:${chrome.header}px;background:${chrome.headerColor};"></div>`);
  }
  if (chrome.footer > 0) {
    const font = Math.max(12, Math.round(chrome.footer * 0.42));
    const body = chrome.footerText === 'title'
      ? `<span style="overflow:hidden;text-overflow:ellipsis;">${escapeHtml(deck.title)}</span>`
        + `<span>${index + 1}</span>`
      : '';
    parts.push(`<div class="el bg" style="left:0;top:${deck.height - chrome.footer}px;`
      + `width:${deck.width}px;height:${chrome.footer}px;background:${chrome.footerColor};`
      + `color:${chrome.footerTextColor};display:flex;align-items:center;`
      + `justify-content:space-between;padding:0 ${inset}px;box-sizing:border-box;`
      + `font-size:${font}px;white-space:nowrap;overflow:hidden;">${body}</div>`);
  }
  return parts.join('');
}

// A video, as either a player or a still.
//
// `live` is the same distinction the editor draws: a `.html` deck is opened in
// a browser and plays, a print document is paper and cannot. The still is the
// poster the author chose, and where there is none a dark plate carrying the
// video's name — which is what the editing canvas shows too, so the author has
// already seen exactly what the PDF will contain.
//
// The still is wrapped in a link to the video. A slide deck printed to PDF
// routinely outlives the presentation it was shown at, and a reader who cannot
// play the video in the page can at least reach it.
function videoToHtml(deck, el, resolveSrc, live) {
  const source = videoSourceOf(el);
  const box = `left:${el.x}px;top:${el.y}px;width:${el.w}px;height:${el.h}px;`
    + (el.rotation ? `transform:rotate(${el.rotation}deg);` : '')
    + (el.opacity != null && el.opacity !== 1 ? `opacity:${el.opacity};` : '');
  if (!source) return '';

  if (live) {
    if (isEmbeddedVideo(el)) {
      return `<iframe class="el" style="${box}border:0;" src="${escapeHtml(videoEmbedUrl(el, { autoplay: false }))}"`
        + ` title="${escapeHtml(el.alt || videoLabelOf(el))}" allowfullscreen`
        + ' allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe>';
    }
    const url = videoFileUrl(el, resolveSrc);
    const poster = el.poster ? (resolveSrc ? resolveSrc(el.poster) : el.poster) : '';
    const type = videoMimeOf(source.url);
    return `<video class="el" style="${box}object-fit:${el.fit || 'contain'};background:#000;"`
      + ` src="${escapeHtml(url)}"${type ? ` data-type="${escapeHtml(type)}"` : ''}`
      + (poster ? ` poster="${escapeHtml(poster)}"` : '')
      + (el.controls !== false ? ' controls' : '')
      + (el.loop ? ' loop' : '')
      + (el.muted ? ' muted' : '')
      + ' playsinline></video>';
  }

  const poster = el.poster ? (resolveSrc ? resolveSrc(el.poster) : el.poster) : '';
  const inner = poster
    ? `<img style="width:100%;height:100%;object-fit:${el.fit || 'contain'};display:block;"`
      + ` src="${escapeHtml(poster)}" alt="${escapeHtml(el.alt)}">`
    : '<div style="width:100%;height:100%;background:'
      + 'repeating-linear-gradient(-45deg,rgba(255,255,255,.035) 0 12px,'
      + 'rgba(255,255,255,0) 12px 24px),#101216;"></div>';
  const label = `<div style="position:absolute;left:0;right:0;bottom:0;padding:4px 8px;`
    + `background:rgba(0,0,0,.55);color:#e8eaed;font:12px system-ui,sans-serif;`
    + `white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">`
    + `${escapeHtml(videoLabelOf(el))}</div>`;
  const badge = '<svg viewBox="0 0 24 24" width="34" height="34" style="position:absolute;'
    + 'left:50%;top:50%;transform:translate(-50%,-50%);">'
    + '<circle cx="12" cy="12" r="11" fill="rgba(0,0,0,.55)"/>'
    + '<polygon points="9.5,7 17,12 9.5,17" fill="#ffffff"/></svg>';
  const href = videoWatchUrl(el);
  const plate = `<div class="el" style="${box}overflow:hidden;background:#101216;">`
    + `${inner}${badge}${label}</div>`;
  return href
    ? `<a href="${escapeHtml(href)}" style="text-decoration:none;">${plate}</a>`
    : plate;
}

/**
 * The lines of a text box, markers and all.
 *
 * One `<div>` per line rather than the raw string, because a list is a layout:
 * the marker sits in its own column and a wrapped line has to land under the
 * first word instead of under the bullet. The lengths come from the model's
 * own resolvers, so an exported slide indents exactly as far as the editor
 * drew it (I7b).
 */
function textToHtml(el) {
  const { indent, gutter } = bulletMetricsOf(el);
  return textLinesOf(el).map((line) => {
    const style = `padding-left:${indent * line.level + gutter}px;`
      + `text-indent:${-gutter}px;white-space:pre-wrap;`;
    const marker = line.marker
      // `text-indent:0` because an inline-block inherits the line's negative
      // indent and would draw the glyph a marker-column left of the box.
      ? `<span style="display:inline-block;width:${gutter}px;text-indent:0;">`
        + `${escapeHtml(line.marker)}</span>`
      : '';
    // An empty line is a gap the author left, and an empty `<div>` has no
    // height: the break is what keeps the gap the size of a line.
    return `<div style="${style}">${marker}${escapeHtml(line.text) || '<br>'}</div>`;
  }).join('');
}

function elementToHtml(deck, el, resolveSrc, live = false) {
  const box = `left:${el.x}px;top:${el.y}px;width:${el.w}px;height:${el.h}px;`
    + (el.rotation ? `transform:rotate(${el.rotation}deg);` : '')
    + (el.opacity != null && el.opacity !== 1 ? `opacity:${el.opacity};` : '');

  if (el.type === 'text') {
    const align = { top: 'flex-start', middle: 'center', bottom: 'flex-end' }[el.valign] ?? 'flex-start';
    const style = `${box}display:flex;flex-direction:column;justify-content:${align};`
      + `font-family:${fontFamilyWithMathFallback(el.fontFamily || deck.theme.fontFamily)};font-size:${el.fontSize}px;`
      + `line-height:${el.lineHeight ?? 1.3};color:${textColorOf(el, deck.theme)};`
      + `font-weight:${el.bold ? 700 : 400};font-style:${el.italic ? 'italic' : 'normal'};`
      + `text-decoration:${el.underline ? 'underline' : 'none'};text-align:${el.align};`
      + 'white-space:pre-wrap;word-break:break-word;overflow:hidden;';
    return `<div class="el" style="${style}">${textToHtml(el)}</div>`;
  }
  if (el.type === 'equation') {
    if (!el.latex || !el.latex.trim()) return '';
    const { html } = renderEquation(el.latex, { displayMode: el.displayMode !== false });
    const style = `${box}display:flex;align-items:center;justify-content:center;`
      + `font-size:${el.fontSize}px;color:${el.color || deck.theme.color};`;
    return `<div class="el eq" style="${style}">${html}</div>`;
  }
  if (el.type === 'image' && el.src) {
    const src = resolveSrc ? resolveSrc(el.src) : el.src;
    return `<img class="el" style="${box}object-fit:${el.fit || 'contain'};" src="${escapeHtml(src)}" alt="${escapeHtml(el.alt)}">`;
  }
  if (el.type === 'video') return videoToHtml(deck, el, resolveSrc, live);
  if (el.type === 'shape') {
    // Borders are drawn inside the box, exactly as the editor draws them: CSS
    // does that natively with `border-box`, and the triangle gets there by
    // insetting its outline half a stroke width.
    const border = borderOf(el);
    const css = border ? `border:${border.width}px solid ${border.color};box-sizing:border-box;` : '';
    if (el.shape === 'ellipse') {
      return `<div class="el" style="${box}background:${el.fill};border-radius:50%;${css}"></div>`;
    }
    if (el.shape === 'line' || el.shape === 'arrow') {
      const { start, end } = arrowsOf(el);
      const color = el.stroke || el.fill;
      const width = Math.max(1, el.strokeWidth || 4);
      const head = Math.min(width * 3.2, el.w / 2.5);
      const y = el.h / 2;
      const x1 = start ? head : 0;
      const x2 = end ? el.w - head : el.w;
      const heads = [
        start ? `<polygon points="0,${y} ${head},${y - head * 0.55} ${head},${y + head * 0.55}" fill="${color}"/>` : '',
        end ? `<polygon points="${el.w},${y} ${el.w - head},${y - head * 0.55} ${el.w - head},${y + head * 0.55}" fill="${color}"/>` : '',
      ].join('');
      return `<svg class="el" style="${box}" viewBox="0 0 ${el.w} ${el.h}" preserveAspectRatio="none">`
        + `<line x1="${x1}" y1="${y}" x2="${Math.max(x1, x2)}" y2="${y}" stroke="${color}" stroke-width="${width}"`
        + ` stroke-linecap="${start || end ? 'butt' : 'round'}"/>`
        + `${heads}</svg>`;
    }
    if (el.shape === 'triangle') {
      if (!border) {
        return `<div class="el" style="${box}background:${el.fill};clip-path:polygon(50% 0,100% 100%,0 100%);"></div>`;
      }
      const outline = trianglePoints(el);
      return `<svg class="el" style="${box}" viewBox="0 0 ${el.w} ${el.h}" preserveAspectRatio="none">`
        + `<polygon points="${polygonPoints(outline)}" fill="${el.fill}"/>`
        + `<polygon points="${polygonPoints(insetPolygon(outline, border.width / 2))}" fill="none"`
        + ` stroke="${border.color}" stroke-width="${border.width}" stroke-linejoin="miter"/>`
        + '</svg>';
    }
    return `<div class="el" style="${box}background:${el.fill};border-radius:${el.radius ?? 0}px;${css}"></div>`;
  }
  return '';
}

// Produces a standalone deck. The slides are scaled with a CSS variable set
// once on resize, so the presentation fits any screen without re-layout.
export function deckToHtml(deck, { resolveSrc } = {}) {
  const slides = deck.slides.map((slide, index) => {
    const background = backgroundOf(deck, slide);
    const body = backgroundToHtml(background, resolveSrc)
      + chromeToHtml(deck, slide, index)
      + slide.elements.map(el => elementToHtml(deck, el, resolveSrc, true)).join('');
    return `<section class="slide" data-index="${index}" style="background:${background.color};">${body}</section>`;
  }).join('\n');

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(deck.title)}</title>
<style>
  :root { color-scheme: light; }
  html,body { margin:0; height:100%; background:#111; overflow:hidden; font-family:${fontFamilyWithMathFallback(deck.theme.fontFamily)}; }
  #stage { position:absolute; inset:0; display:grid; place-items:center; }
  .slide {
    position:relative; width:${deck.width}px; height:${deck.height}px;
    overflow:hidden; display:none; transform-origin:center center;
    box-shadow:0 0 40px rgba(0,0,0,.5);
  }
  .slide.active { display:block; }
  .el { position:absolute; }
  .bg { pointer-events:none; }
${EXPORT_MATH_CSS}
  #hud { position:fixed; right:14px; bottom:10px; color:#888; font:12px system-ui; }
</style>
</head>
<body>
<div id="stage">
${slides}
</div>
<div id="hud"><span id="pos"></span> — ← → / space</div>
<script>
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var index = 0;
  function fit() {
    var scale = Math.min(innerWidth / ${deck.width}, innerHeight / ${deck.height});
    slides.forEach(function (s) { s.style.transform = 'scale(' + scale + ')'; });
  }
  function show(next) {
    index = Math.max(0, Math.min(slides.length - 1, next));
    slides.forEach(function (s, i) { s.classList.toggle('active', i === index); });
    document.getElementById('pos').textContent = (index + 1) + ' / ' + slides.length;
  }
  addEventListener('resize', fit);
  addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); show(index + 1); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); show(index - 1); }
    else if (e.key === 'Home') show(0);
    else if (e.key === 'End') show(slides.length - 1);
  });
  addEventListener('click', function () { show(index + 1); });
  fit(); show(0);
</script>
</body>
</html>
`;
}

export function downloadBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoked on the next tick: revoking synchronously races the download in
  // Chromium and produces a zero-byte file.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function exportHtml(deck, options = {}) {
  const { deck: portable, inlined, failed } = await inlineDeckAssets(deck, { ...options, media: true });
  // The pictures are inlined, so the file needs nothing beside it — which is
  // the whole point of the HTML export. `resolveSrc` is dropped afterwards:
  // anything it could still resolve is a URL that already travels.
  const html = deckToHtml(portable, {});
  const name = options.fileName || `${(deck.title || 'presentation').replace(/[^\w.-]+/g, '_')}.html`;
  downloadBlob(new Blob([html], { type: 'text/html;charset=utf-8' }), name);
  return { inlined, failed };
}

// ─── PDF ─────────────────────────────────────────────────────────────────────

// Prints through a hidden iframe rather than a popup: the packaged app blocks
// window.open, and an iframe keeps the print dialog attached to the IDE window.
export async function exportPdf(deck, options = {}) {
  // Inlined here for a different reason than in the HTML export: the print
  // dialog opens on the iframe's `load`, and an image still in flight prints
  // as a blank box. A data URI has nothing left to wait for.
  const { deck: printable, inlined, failed } = await inlineDeckAssets(deck, options);
  const printResult = await printDeck(printable, options.fileName);
  return { inlined, failed, ...printResult };
}

export const PDF_EXPORT_RESULT_EVENT = 'gilzabots:pdf-export-result';
export const PDF_EXPORT_FRAME_PREFIX = 'gilzabots-pdf-v1:';

let pdfRequestSequence = 0;
let browserPrintFrame = null;
let pdfResultListenerInstalled = false;
const pendingHostPrints = new Map();

function nextPdfRequestId() {
  pdfRequestSequence += 1;
  return `${Date.now().toString(36)}-${pdfRequestSequence.toString(36)}`;
}

/** The versioned frame name consumed by the Qt print bridge. */
export function pdfExportFrameName(requestId, fileName) {
  return `${PDF_EXPORT_FRAME_PREFIX}${requestId}:${encodeURIComponent(fileName)}`;
}

function installPdfResultListener() {
  if (pdfResultListenerInstalled) return;
  window.addEventListener(PDF_EXPORT_RESULT_EVENT, (event) => {
    const detail = event?.detail || {};
    const pending = pendingHostPrints.get(detail.requestId);
    if (!pending) return;

    pendingHostPrints.delete(detail.requestId);
    pending.frame.remove();
    if (detail.status === 'success') {
      pending.resolve({ saved: true });
    } else if (detail.status === 'cancelled') {
      pending.resolve({ cancelled: true });
    } else {
      const error = new Error(detail.message || 'The PDF file could not be written.');
      error.code = 'PDF_WRITE_FAILED';
      pending.reject(error);
    }
  });
  pdfResultListenerInstalled = true;
}

/**
 * The deck as a print document: one page per slide, at the deck's own size.
 *
 * Separate from `printDeck` so the markup can be checked without a browser to
 * print it — the `@page` rule is what decides whether the PDF comes out at the
 * deck's aspect or letterboxed onto A4, and that is worth a test.
 */
export function deckToPrintHtml(deck) {
  const pageW = deck.width;
  const pageH = deck.height;
  const slides = deck.slides.map((slide, index) => {
    const background = backgroundOf(deck, slide);
    const body = backgroundToHtml(background)
      + chromeToHtml(deck, slide, index)
      + slide.elements.map(el => elementToHtml(deck, el)).join('');
    return `<section class="slide" style="background:${background.color};">${body}</section>`;
  }).join('\n');

  return `<!doctype html>
<html><head><meta charset="utf-8"><title>${escapeHtml(deck.title)}</title>
${EDITOR_FONT_LINKS}
<style>
  @page { size: ${pageW}px ${pageH}px; margin: 0; }
  html,body { margin:0; padding:0; background:#fff; font-family:${fontFamilyWithMathFallback(deck.theme.fontFamily)}; }
  .slide {
    position:relative; width:${pageW}px; height:${pageH}px; overflow:hidden;
    page-break-after:always; break-after:page;
  }
  .slide:last-child { page-break-after:auto; break-after:auto; }
  .el { position:absolute; }
  .bg { pointer-events:none; }
${EXPORT_MATH_CSS}
  /* Printing happens in an iframe, which the app's own @font-face rules do not
     reach; the math font is same-origin, so the print document asks for it
     itself rather than falling back mid-formula. */
  @font-face {
    font-family: 'STIX Two Math';
    src: url('/fonts/STIXTwoMath-Regular.woff2') format('woff2');
    font-display: block;
  }
  * { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
</style></head>
<body>${slides}</body></html>`;
}

async function printDeck(deck, fileName) {
  const html = deckToPrintHtml(deck);
  const host = window.__gilzabotsPdfPrintHost;
  if (host?.supported === false) {
    const error = new Error('PDF_FRAME_PRINT_UNSUPPORTED');
    error.code = 'PDF_FRAME_PRINT_UNSUPPORTED';
    throw error;
  }
  const hostManaged = host?.supported === true;
  const suggestedName = fileName
    || `${(deck.title || 'presentation').replace(/[^\w.-]+/g, '_')}.pdf`;
  const requestId = nextPdfRequestId();

  // A regular browser owns the complete, blocking print-dialog lifecycle, so
  // the previous frame is safe to discard on the next request. The Qt host
  // writes asynchronously and acknowledges completion; those frames are kept
  // independently until their own result arrives.
  if (!hostManaged && browserPrintFrame) browserPrintFrame.remove();
  const frame = document.createElement('iframe');
  frame.setAttribute('aria-hidden', 'true');
  // A subframe print request carries no custom payload. Its versioned `name`
  // gives the Qt host both the suggested file name and a request id it can use
  // to report save, cancellation, or an asynchronous write failure.
  frame.setAttribute('name', hostManaged
    ? pdfExportFrameName(requestId, suggestedName)
    : suggestedName);
  frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;';
  document.body.appendChild(frame);
  if (!hostManaged) browserPrintFrame = frame;

  await new Promise((resolve) => {
    frame.onload = () => resolve();
    frame.srcdoc = html;
  });

  // `load` fires before the web fonts the document asked for have arrived, and
  // the math font is declared `font-display: block` — printing now would render
  // every formula as blank space while the font was still in flight. The
  // formulas are the reason the font is there, so the print waits for it.
  try {
    await frame.contentDocument?.fonts?.ready;
  } catch {
    // A document that cannot report on its fonts still prints; it just may
    // print a formula in the fallback face.
  }

  let hostResult;
  if (hostManaged) {
    installPdfResultListener();
    hostResult = new Promise((resolve, reject) => {
      pendingHostPrints.set(requestId, { frame, resolve, reject });
    });
  }

  try {
    frame.contentWindow.focus();
    frame.contentWindow.print();
  } catch (error) {
    pendingHostPrints.delete(requestId);
    frame.remove();
    if (browserPrintFrame === frame) browserPrintFrame = null;
    throw error;
  }

  // Browser print() returning means only that its dialog closed; the browser
  // does not disclose whether the user printed or cancelled. Do not call that
  // a successful export. The desktop bridge, on the other hand, resolves only
  // after QWebEnginePage.pdfPrintingFinished reports the actual file result.
  if (!hostManaged) return { printDialogClosed: true };
  return hostResult;
}
