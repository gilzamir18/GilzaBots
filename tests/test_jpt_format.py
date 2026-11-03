"""The `.jpt` format, from the Python side.

The format has two implementations — `gui_src/src/slides/model.js`, which the
editor runs, and `gilzabots/jpt/model.py`, which agents author with — so the
tests that matter most here are the ones that keep them from drifting:

  * every deck this side writes validates against `docs/specs/jpt.schema.json`,
    the writer contract both obey;
  * a deck built here round-trips **byte for byte** through the editor's own
    serializer, which is invariant I1 of the specification. That check needs
    Node and is skipped without it, so it is not the only line of defence — but
    when it runs, it is the one that actually proves the two agree.
"""

import json
import os
import shutil
import subprocess

import pytest

from gilzabots import jpt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(REPO, "docs", "specs", "jpt.schema.json")
JS_MODEL = os.path.join(REPO, "gui_src", "src", "slides", "model.js")


def sample_deck():
    """One deck holding every element type, so a single fixture exercises the
    whole key order and every payload."""
    return jpt.compile_outline({
        "title": "Every element",
        "theme": {"backgroundImage": "figures/theme.png", "backgroundOpacity": 0.4},
        "slides": [
            {"layout": "title", "title": "Cover", "subtitle": "with a subtitle"},
            {"layout": "bullets", "title": "A list",
             "bullets": ["first point", "second point"], "notes": "say this"},
            {"layout": "equation", "title": "A formula",
             "equation": r"\int_0^1 x^2\,dx = \frac{1}{3}", "caption": "with a caption"},
            {"layout": "blank", "title": "Raw", "background": "#101010",
             "backgroundImage": None, "elements": [
                {"type": "shape", "shape": "ellipse", "x": 100, "y": 300,
                 "w": 200, "h": 120, "fill": "#2f6fb3"},
                {"type": "image", "x": 400, "y": 300, "w": 200, "h": 120,
                 "src": "data:image/png;base64,iVBORw0KGgo=", "alt": "a dot"},
                {"type": "video", "x": 700, "y": 300, "w": 400, "h": 225,
                 "src": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 "poster": "data:image/png;base64,iVBORw0KGgo=",
                 "alt": "a talk", "start": 30, "muted": True},
            ]},
        ],
    })


# ─── the writer contract ─────────────────────────────────────────────────────

def test_a_written_deck_validates_against_the_published_schema():
    jsonschema = pytest.importorskip("jsonschema")
    with open(SCHEMA, encoding="utf-8") as handle:
        schema = json.load(handle)
    deck = json.loads(jpt.serialize(sample_deck()))
    jsonschema.Draft202012Validator(schema).validate(deck)


def test_serialization_is_two_space_json_with_one_trailing_newline():
    text = jpt.serialize(sample_deck())
    assert text.endswith("}\n")
    assert not text.endswith("}\n\n")
    assert '\n  "version": 1,' in text


def test_integral_numbers_are_written_as_integers():
    """JavaScript writes 1 where Python would write 1.0, and a deck full of
    `1.0` would break the byte-exact round-trip on the editor's first save."""
    deck = jpt.create_deck("Numbers")
    jpt.add_slide(deck, jpt.create_slide(id="s1"))
    jpt.add_element(deck, "s1", jpt.create_element(
        "text", id="t1", x=100.0, y=200.0, w=400.0, h=120.0, text="x", fontSize=28.0,
    ))
    text = jpt.serialize(deck)
    assert '"x": 100,' in text
    assert '"fontSize": 28,' in text
    assert "100.0" not in text


def test_known_keys_are_written_in_the_order_the_editor_uses():
    deck = json.loads(jpt.serialize(sample_deck()))
    assert list(deck) == ["version", "title", "width", "height", "theme", "slides"]
    assert list(deck["slides"][0]) == [
        "id", "background", "backgroundImage", "backgroundFit", "backgroundOpacity",
        "notes", "elements",
    ]
    first = list(deck["slides"][0]["elements"][0])
    assert first[:8] == ["id", "type", "x", "y", "w", "h", "rotation", "opacity"]


# ─── cross-language agreement ────────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the editor's model")
def test_a_python_deck_round_trips_byte_for_byte_through_the_editor():
    text = jpt.serialize(sample_deck())
    script = (
        "import fs from 'node:fs';"
        f"const {{ parseDeck, serializeDeck }} = await import({json.dumps(JS_MODEL)});"
        "const input = fs.readFileSync(0, 'utf8');"
        "process.stdout.write(serializeDeck(parseDeck(input)));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=text, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == text, "the editor would rewrite a deck Python wrote"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the editor's model")
def test_python_and_the_editor_read_the_same_lines_out_of_a_list():
    """The other place the two implementations could drift apart silently.

    This side estimates whether a list fits its box; the editor is what draws
    it. If they disagreed about which line is a sub-point, or about what stands
    in front of it, the fit would be computed for one list and the projector
    would show another — and nothing in the file would look wrong.
    """
    cases = [
        {"text": "one\n\ttwo\n\t\tthree", "bullet": "disc"},
        {"text": "one\n\tsub\n\tsub\ntwo\n\tsub\n\t\tdeep", "bullet": "number"},
        {"text": "one\n\n\ttwo", "bullet": "dash"},
        {"text": "\t" * 9 + "far in", "bullet": "disc"},
        {"text": "plain\n\tindented", "bullet": None},
    ]
    script = (
        f"const {{ textLinesOf }} = await import({json.dumps(JS_MODEL)});"
        f"const cases = {json.dumps(cases)};"
        "process.stdout.write(JSON.stringify(cases.map(el => textLinesOf(el))));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [jpt.text_lines(case) for case in cases]


def test_a_list_style_is_written_beside_the_text_it_belongs_to():
    deck = json.loads(jpt.serialize(sample_deck()))
    body = deck["slides"][1]["elements"][1]
    assert body["bullet"] == "disc"
    assert "\u2022" not in body["text"], "markers are a field, not characters"
    keys = list(body)
    assert keys.index("bullet") == keys.index("role") + 1


def test_a_text_box_has_no_list_unless_it_asks_for_one():
    element = jpt.create_element("text", id="t1", x=0, y=0, w=100, h=40, text="x")
    assert element["bullet"] is None


def test_a_list_style_the_format_does_not_have_is_refused():
    with pytest.raises(jpt.JptError, match="is not a list style"):
        jpt.create_element("text", id="t1", x=0, y=0, w=100, h=40,
                           text="x", bullet="sparkles")


# ─── strictness ──────────────────────────────────────────────────────────────
# The editor repairs a malformed deck because a user must never be locked out of
# their own file. An author gets an exception instead: a silently repaired deck
# is a defect the agent never learns about.

def test_an_unknown_element_type_is_refused_rather_than_degraded():
    # "hologram" stands for any type the format does not have. It used to be
    # "video", which the format now does have — a placeholder is only useful
    # while it stays unimplemented.
    with pytest.raises(jpt.JptError, match="unknown element type"):
        jpt.create_element("hologram", src="a.mp4")


@pytest.mark.parametrize("patch, message", [
    ({"w": 0}, "at least 1 deck unit"),
    ({"opacity": 4}, "opacity"),
    ({"align": "middle"}, "align"),
    ({"fontSize": 0}, "fontSize"),
    ({"x": "left"}, "must be a number"),
])
def test_a_malformed_element_is_refused_with_a_diagnostic(patch, message):
    with pytest.raises(jpt.JptError, match=message):
        jpt.create_element("text", id="t1", text="x", **patch)


def test_a_deck_needs_at_least_one_slide():
    with pytest.raises(jpt.JptError, match="at least one slide"):
        jpt.serialize(jpt.create_deck("Empty"))


def test_duplicate_element_ids_are_refused():
    deck = jpt.create_deck("Dupes")
    jpt.add_slide(deck, jpt.create_slide(id="s1"))
    jpt.add_element(deck, "s1", jpt.create_element("text", id="same", text="a"))
    jpt.add_element(deck, "s1", jpt.create_element("text", id="same", text="b"))
    with pytest.raises(jpt.JptError, match="duplicate element id"):
        jpt.serialize(deck)


def test_parsing_a_damaged_file_reports_it_instead_of_repairing_it():
    with pytest.raises(jpt.JptError, match="not valid JSON"):
        jpt.parse("{oops")
    with pytest.raises(jpt.JptError, match="JSON object"):
        jpt.parse("[]")


def test_parsing_fills_in_the_defaults_the_editor_would():
    deck = jpt.parse(json.dumps({
        "slides": [{"id": "s1", "elements": [
            {"id": "t1", "type": "text", "x": 0, "y": 0, "w": 100, "h": 40, "text": "hi"},
        ]}],
    }))
    element = deck["slides"][0]["elements"][0]
    assert element["fontSize"] == 28 and element["align"] == "left"
    assert deck["theme"]["background"] == "#ffffff"
    assert deck["width"] == 1280


# ─── operations ──────────────────────────────────────────────────────────────

def test_deleting_the_last_slide_empties_it_instead_of_removing_it():
    deck = jpt.create_deck("One")
    jpt.add_slide(deck, jpt.create_slide(id="only"))
    jpt.add_element(deck, "only", jpt.create_element("text", text="x"))
    jpt.delete_slide(deck, "only")
    assert len(deck["slides"]) == 1 and deck["slides"][0]["elements"] == []


def test_an_element_cannot_change_type():
    deck = sample_deck()
    element = deck["slides"][1]["elements"][1]
    with pytest.raises(jpt.JptError, match="cannot change type"):
        jpt.update_element(deck, element["id"], {"type": "image"})


def test_reorder_moves_an_element_through_the_paint_order():
    deck = sample_deck()
    slide = deck["slides"][3]
    first = slide["elements"][0]["id"]
    jpt.reorder_element(deck, first, "front")
    assert slide["elements"][-1]["id"] == first


# ─── video ───────────────────────────────────────────────────────────────────
# A video's `src` is the one field in the format that means two different
# things, and both implementations have to read it the same way — the editor
# decides from it whether to draw a player, and the linter decides from it
# whether the source is a path that has to exist.

@pytest.mark.parametrize("src, kind, ident", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"),
    ("https://vimeo.com/76979871", "vimeo", "76979871"),
    ("media/lecture.mp4", "file", ""),
    ("https://example.org/clip.mp4", "file", ""),
])
def test_a_provider_link_and_a_file_are_told_apart(src, kind, ident):
    source = jpt.video_source_of({"src": src})
    assert source["kind"] == kind
    assert source["id"] == ident


def test_an_empty_video_source_is_nothing_rather_than_a_file():
    assert jpt.video_source_of({"src": "   "}) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the editor's model")
def test_python_and_the_editor_agree_about_what_a_video_source_is():
    """The one place the two implementations could drift apart silently: a link
    the editor treats as YouTube and the author treats as a file plays in the
    app and lints as a missing file, or the reverse."""
    sources = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "https://vimeo.com/video/76979871",
        "media/lecture.mp4",
        "https://example.org/clip.mp4",
        "data:video/mp4;base64,AAAA",
    ]
    video_js = os.path.join(REPO, "gui_src", "src", "slides", "video.js")
    script = (
        f"const {{ videoSourceOf }} = await import({json.dumps(video_js)});"
        f"const srcs = {json.dumps(sources)};"
        "process.stdout.write(JSON.stringify("
        "srcs.map(src => { const s = videoSourceOf({ src }); "
        "return s && { kind: s.kind, id: s.id }; })));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    from_js = json.loads(result.stdout)
    from_python = [
        {"kind": s["kind"], "id": s["id"]} if s else None
        for s in (jpt.video_source_of({"src": src}) for src in sources)
    ]
    assert from_js == from_python


def test_a_video_without_a_source_is_refused_rather_than_written_empty():
    with pytest.raises(jpt.JptError, match="a video needs a src"):
        jpt.create_element("video", id="v1", x=0, y=0, w=640, h=360, src="")


@pytest.mark.parametrize("patch, message", [
    ({"fit": "sideways"}, "fit"),
    ({"start": -5}, "start cannot be negative"),
    ({"loop": "yes"}, "loop must be true or false"),
    ({"poster": 7}, "poster must be a string"),
])
def test_a_malformed_video_is_refused_with_a_diagnostic(patch, message):
    with pytest.raises(jpt.JptError, match=message):
        jpt.create_element("video", id="v1", x=0, y=0, w=640, h=360,
                           src="a.mp4", **patch)
