"""The authoring API: layout, linting, and the three agent tools.

What these check is not "does it produce JSON" — `test_jpt_format.py` covers
that. It is the claim the API actually makes: an agent that states intent gets
a deck a room can read. So the tests are about the properties that make a deck
good — text inside its box, one title position, nothing off the slide, type big
enough to project — and about the linter catching each of those when they are
violated, because the linter is the only reason "perfect" means anything here.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from gilzabots import jpt
from gilzabots.jpt import layout, metrics
# The package re-exports `lint` as the function, which shadows the module of
# the same name — so the module's other helpers are imported by name.
from gilzabots.jpt.lint import MIN_CONTRAST, check_latex, contrast_ratio
from gilzabots.tools import (
    check_presentation, create_presentation, edit_presentation, read_file,
    set_project_context, write_content_pos, write_file,
)


def _raw(tool):
    return getattr(tool, "_func", None) or tool


def _run(tool, tmp_path, **kwargs):
    set_project_context(SimpleNamespace(project_path=str(tmp_path), mode="auto"))
    return asyncio.run(_raw(tool)(**kwargs))


def _deck_of(path):
    return jpt.parse(jpt.read_jpt(path).text)


LONG = ("Kolmogorov complexity is the length of the shortest program that "
        "outputs a given string, which makes it uncomputable in general and "
        "yet the right definition of how much information something carries.")


# ─── layout ──────────────────────────────────────────────────────────────────

def test_every_layout_produces_a_deck_that_passes_its_own_linter():
    """The strongest single statement this API can make: the generator does not
    emit what the checker rejects."""
    deck = jpt.compile_outline({
        "title": "All layouts",
        "slide_numbers": True,
        "slides": [
            {"layout": "title", "title": "Cover", "subtitle": "A subtitle"},
            {"layout": "section", "title": "Part one"},
            {"layout": "bullets", "title": "Points", "bullets": ["one", "two", "three"]},
            {"layout": "text", "title": "Prose", "text": LONG},
            {"layout": "equation", "title": "Math", "equation": r"e^{i\pi} + 1 = 0",
             "caption": "Euler"},
            {"layout": "two_columns", "title": "Both",
             "left": {"bullets": ["a", "b"]}, "right": {"text": "Some prose."}},
            {"layout": "quote", "quote": "Simplicity is prerequisite for reliability.",
             "attribution": "Dijkstra"},
            {"layout": "blank", "title": "Nothing else"},
        ],
    })
    findings = jpt.lint(deck)
    assert not findings, jpt.format_report(findings)


def test_the_title_lands_in_the_same_place_on_every_titled_slide():
    deck = jpt.compile_outline({"title": "Consistent", "slides": [
        {"layout": "bullets", "title": "One", "bullets": ["x"]},
        {"layout": "equation", "title": "Two", "equation": "x = 1"},
        {"layout": "two_columns", "title": "Three", "left": {"text": "a"}},
    ]})
    tops = {slide["elements"][0]["y"] for slide in deck["slides"]}
    assert len(tops) == 1


def test_a_long_title_shrinks_instead_of_overflowing():
    short = jpt.compile_outline({"slides": [
        {"layout": "blank", "title": "Short"}]})["slides"][0]["elements"][0]
    long = jpt.compile_outline({"slides": [
        {"layout": "blank", "title": "A considerably longer title than that one, "
                                     "long enough to need two lines of type"}]})
    long_title = long["slides"][0]["elements"][0]
    assert long_title["fontSize"] < short["fontSize"]
    assert long_title["w"] == short["w"], "the box is the grid's; only the type moves"


def test_text_that_cannot_fit_at_a_readable_size_is_reported_not_clipped():
    deck = jpt.compile_outline({"slides": [
        {"layout": "text", "title": "Too much", "text": LONG * 12},
    ]})
    codes = {finding.code for finding in jpt.lint(deck)}
    assert "text-overflow" in codes


def test_every_element_of_every_layout_stays_inside_the_safe_area():
    deck = jpt.compile_outline({"slides": [
        {"layout": "title", "title": "Cover", "subtitle": "Sub"},
        {"layout": "image_text", "title": "Side by side", "side": "left",
         "image": "data:image/png;base64,iVBORw0KGgo=", "bullets": ["a", "b"]},
        {"layout": "quote", "quote": "Words.", "attribution": "Someone"},
    ]})
    grid = layout.Grid(deck["width"], deck["height"])
    for slide in deck["slides"]:
        for element in slide["elements"]:
            assert element["x"] >= grid.margin_x - 1
            assert element["y"] >= grid.margin_top - 1
            assert element["x"] + element["w"] <= deck["width"] - grid.margin_x + 1
            assert element["y"] + element["h"] <= deck["height"] - grid.margin_bottom + 1


def test_columns_share_the_content_area_with_one_gutter_between_them():
    grid = layout.Grid()
    left, right = grid.columns(2)
    assert right.x - (left.x + left.w) == pytest.approx(grid.gutter, abs=1)
    assert left.y == right.y == grid.content.y


def test_the_raw_element_escape_hatch_is_validated_like_everything_else():
    with pytest.raises(jpt.JptError, match="unknown element type"):
        jpt.compile_outline({"slides": [
            {"layout": "blank", "elements": [{"type": "hologram", "src": "a.mp4"}]},
        ]})


def test_an_unknown_layout_names_the_ones_that_exist():
    with pytest.raises(jpt.JptError, match="unknown layout"):
        jpt.compile_outline({"slides": [{"layout": "carousel"}]})


def test_a_block_holds_exactly_one_thing():
    with pytest.raises(jpt.JptError, match="a block holds one of"):
        jpt.compile_outline({"slides": [
            {"layout": "two_columns", "left": {"bullets": ["a"], "text": "b"}},
        ]})


def test_a_list_is_a_field_on_the_box_not_markers_typed_into_the_text():
    """What the screenshot of the old behaviour showed: every line beginning
    with "•  " because that was the only way to have a bullet. Such a list
    cannot be restyled, cannot nest, and arrives in PowerPoint as characters
    somebody has to delete by hand."""
    deck = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "A list", "bullets": ["first", "second"]},
    ]})
    body = deck["slides"][0]["elements"][1]
    assert body["bullet"] == "disc"
    assert body["text"] == "first\nsecond"


def test_a_sub_point_is_a_level_and_three_spellings_reach_the_same_one():
    """An author writes an outline, not a representation: a dict with a level,
    a string that already carries its tabs, and a plain string all land as the
    same thing in the file."""
    assert layout.bullet_text([
        "top",
        {"text": "under it", "level": 1},
        "\tunder it too",
        {"text": "deeper", "level": 2},
    ]) == "top\n\tunder it\n\tunder it too\n\t\tdeeper"


def test_a_nesting_level_is_capped_rather_than_running_off_the_box():
    assert layout.bullet_text([{"text": "far in", "level": 40}]) == (
        "\t" * jpt.MAX_BULLET_LEVEL + "far in"
    )


def test_a_level_that_is_not_a_whole_number_is_refused():
    with pytest.raises(jpt.JptError, match="bullet level"):
        layout.bullet_text([{"text": "x", "level": "deep"}])


def test_an_outline_can_ask_for_a_numbered_list():
    deck = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "Steps", "bullets": ["first", "second"],
         "bulletStyle": "number"},
    ]})
    body = deck["slides"][0]["elements"][1]
    assert body["bullet"] == "number"
    assert [line["marker"] for line in jpt.text_lines(body)] == ["1.", "2."]


def test_a_list_style_the_format_does_not_have_names_the_ones_it_does():
    with pytest.raises(jpt.JptError, match="is not one of"):
        jpt.compile_outline({"slides": [
            {"layout": "bullets", "bullets": ["a"], "bulletStyle": "sparkles"},
        ]})


def test_a_nested_list_is_fitted_against_the_width_its_words_actually_get():
    """The indent is real space taken out of the line, so a deeply nested list
    of the same words has to come out no larger than a flat one."""
    flat = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "T", "bullets": [LONG, LONG, LONG]},
    ]})["slides"][0]["elements"][1]
    nested = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "T", "bullets": [
            {"text": LONG, "level": 3}, {"text": LONG, "level": 3},
            {"text": LONG, "level": 3},
        ]},
    ]})["slides"][0]["elements"][1]
    assert nested["fontSize"] <= flat["fontSize"]


def test_caption_colour_is_derived_from_the_theme_not_hardcoded():
    dark = jpt.compile_outline({
        "theme": {"background": "#101010", "color": "#f5f5f5"},
        "slides": [{"layout": "equation", "equation": "x", "caption": "note"}],
    })
    caption = dark["slides"][0]["elements"][-1]
    ratio = contrast_ratio(caption["color"], "#101010")
    assert ratio and ratio >= MIN_CONTRAST


# ─── metrics ─────────────────────────────────────────────────────────────────

def test_wrapping_breaks_on_words_and_honours_hard_newlines():
    lines = metrics.wrap_lines("alpha beta\ngamma", 100, 20)
    assert lines[-1][0] == "gamma"
    assert len(lines) >= 2


def test_wrapping_gives_a_bulleted_line_only_the_width_it_really_has():
    """A marker column and a nesting level are width the words do not get.

    Estimating a list against the whole box is how a deck an agent wrote comes
    out overflowing with nothing wrong in the JSON to point at.
    """
    text = "alpha beta gamma delta"
    plain = metrics.wrap_lines(text, 200, 20)
    bulleted = metrics.wrap_lines(text, 200, 20, insets=[1.1])
    nested = metrics.wrap_lines(text, 200, 20, insets=[1.1 + 1.5 * 2])
    assert len(plain) <= len(bulleted) <= len(nested)
    assert len(nested) > len(plain), "a deeply nested line wraps sooner"
    # Every wrapped continuation starts where its first line does, so the inset
    # is a property of the paragraph and not only of its opening line.
    assert {inset for _, inset in bulleted} == {1.1 * 20}


def test_auto_fit_returns_the_floor_when_nothing_fits():
    size = metrics.fit_font_size(LONG * 6, 200, 60, preferred=30, minimum=18)
    assert size == 18


def test_an_equation_is_estimated_from_its_glyphs_not_its_source_length():
    """`\\alpha` is six characters and one glyph; an estimate that counted
    characters would size every formula by how verbose its LaTeX is."""
    short_source, _ = metrics.estimate_equation_box("x + y", 40)
    verbose_source, _ = metrics.estimate_equation_box(r"\alpha + \beta", 40)
    assert verbose_source < len(r"\alpha + \beta") * 0.62 * 40


# ─── lint ────────────────────────────────────────────────────────────────────

def _deck_with(element):
    deck = jpt.create_deck("Lint")
    jpt.add_slide(deck, jpt.create_slide(id="s1"))
    jpt.add_element(deck, "s1", element)
    return deck


def test_an_element_off_the_slide_is_an_error():
    deck = _deck_with(jpt.create_element("text", x=2000, y=100, text="gone"))
    findings = jpt.lint(deck)
    assert jpt.has_errors(findings)
    assert any(f.code == "off-slide" for f in findings)


def test_unreadable_type_is_an_error_and_merely_small_type_is_a_warning():
    tiny = jpt.lint(_deck_with(jpt.create_element("text", text="hi", fontSize=9)))
    assert any(f.code == "tiny-type" and f.level == "error" for f in tiny)
    small = jpt.lint(_deck_with(jpt.create_element("text", text="hi", fontSize=18)))
    assert any(f.code == "small-type" and f.level == "warning" for f in small)
    assert not jpt.has_errors(small)


def test_too_many_bullets_is_reported_for_a_list_that_declares_itself():
    """The old check looked for a marker character at the start of the text.
    A list whose markers are a field has none, and would have gone unnoticed."""
    deck = _deck_with(jpt.create_element(
        "text", id="t1", x=100, y=100, w=900, h=500, fontSize=20, bullet="disc",
        text="\n".join(f"point {n}" for n in range(12)),
    ))
    assert any(f.code == "too-many-bullets" for f in jpt.lint(deck))


def test_low_contrast_text_is_reported():
    deck = _deck_with(jpt.create_element("text", text="washed out", color="#eeeeee"))
    assert any(f.code == "low-contrast" for f in jpt.lint(deck))


def test_dollar_delimiters_in_a_formula_are_reported_because_katex_never_sees_them():
    deck = _deck_with(jpt.create_element("equation", latex="$x^2$"))
    findings = jpt.lint(deck)
    assert any(f.code == "latex-syntax" and f.level == "error" for f in findings)
    assert "delimiters" in next(f.message for f in findings if f.code == "latex-syntax")


def test_unbalanced_braces_are_reported():
    assert check_latex(r"\frac{1}{2") is not None
    assert check_latex(r"\frac{1}{2}") is None
    assert check_latex(r"\begin{cases} a \\ b") is not None


def test_a_missing_image_file_is_an_error_when_the_project_is_known(tmp_path):
    deck = _deck_with(jpt.create_element("image", src="figures/nope.png"))
    findings = jpt.lint(deck, project_root=str(tmp_path))
    assert any(f.code == "missing-image" and f.level == "error" for f in findings)
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "nope.png").write_bytes(b"\x89PNG")
    assert not any(f.code == "missing-image"
                   for f in jpt.lint(deck, project_root=str(tmp_path)))


def test_two_overlapping_content_elements_are_reported_but_a_shape_behind_text_is_not():
    deck = jpt.create_deck("Overlap")
    jpt.add_slide(deck, jpt.create_slide(id="s1"))
    jpt.add_element(deck, "s1", jpt.create_element(
        "text", id="a", x=100, y=100, w=400, h=200, text="one"))
    jpt.add_element(deck, "s1", jpt.create_element(
        "text", id="b", x=140, y=140, w=400, h=200, text="two"))
    assert any(f.code == "overlap" for f in jpt.lint(deck))

    carded = jpt.create_deck("Card")
    jpt.add_slide(carded, jpt.create_slide(id="s1"))
    jpt.add_element(carded, "s1", jpt.create_element(
        "shape", id="card", x=100, y=100, w=400, h=200, fill="#eef3fa"))
    jpt.add_element(carded, "s1", jpt.create_element(
        "text", id="label", x=120, y=120, w=360, h=160, text="on the card"))
    assert not any(f.code == "overlap" for f in jpt.lint(carded))


def test_a_title_that_moves_between_slides_is_reported():
    deck = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "One", "bullets": ["x"]},
        {"layout": "bullets", "title": "Two", "bullets": ["x"]},
        {"layout": "bullets", "title": "Three", "bullets": ["x"]},
    ]})
    deck["slides"][2]["elements"][0]["y"] += 40
    assert any(f.code == "title-drift" for f in jpt.lint(deck))


# ─── tools ───────────────────────────────────────────────────────────────────

def test_create_presentation_writes_a_deck_the_editor_can_open(tmp_path):
    out = _run(create_presentation, tmp_path, path="talk.jpt", outline_json=json.dumps({
        "title": "A talk",
        "slides": [
            {"layout": "title", "title": "A talk", "subtitle": "about things"},
            {"layout": "bullets", "title": "Agenda", "bullets": ["first", "second"]},
        ],
    }))
    assert "2 slides" in out
    deck = _deck_of(tmp_path / "talk.jpt")
    assert deck["title"] == "A talk" and len(deck["slides"]) == 2


def test_the_array_form_of_create_pptx_file_is_accepted_too(tmp_path):
    """The sibling tool takes `[{title, bullets}]`. An agent that knows one
    should not have to relearn the other."""
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps(
        [{"title": "One", "bullets": ["a", "b"]}]))
    deck = _deck_of(tmp_path / "d.jpt")
    assert len(deck["slides"]) == 1


def test_a_deck_with_errors_is_not_written_at_all(tmp_path):
    with pytest.raises(ValueError, match="NOT written"):
        _run(create_presentation, tmp_path, path="broken.jpt", outline_json=json.dumps({
            "slides": [{"layout": "image", "title": "Missing", "image": "figures/gone.png"}],
        }))
    assert not (tmp_path / "broken.jpt").exists()


def test_warnings_do_not_block_the_write_but_are_reported(tmp_path):
    out = _run(create_presentation, tmp_path, path="wordy.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "Dense",
                    "bullets": [f"point number {n} said at some length" for n in range(9)]}],
    }))
    assert (tmp_path / "wordy.jpt").exists()
    assert "too-many-bullets" in out


def test_the_path_must_be_a_jpt(tmp_path):
    with pytest.raises(ValueError, match="must end with"):
        _run(create_presentation, tmp_path, path="deck.pptx", outline_json="[]")


def test_generic_file_tools_use_the_logical_jpt_document(tmp_path):
    deck = jpt.compile_outline({
        "slides": [{"layout": "blank", "title": "Generic write"}],
    })
    out = _run(write_file, tmp_path, path="generic.jpt", content=jpt.serialize(deck))
    assert "packaged JPT" in out
    assert jpt.is_packaged_jpt(tmp_path / "generic.jpt")

    logical = _run(read_file, tmp_path, path="generic.jpt")
    assert json.loads(logical)["slides"][0]["elements"][0]["text"] == "Generic write"
    with pytest.raises(ValueError, match="cannot be edited by line position"):
        _run(
            write_content_pos, tmp_path,
            path="generic.jpt", content="not valid here", pos=1,
        )


def test_edit_presentation_applies_operations_and_re_checks(tmp_path):
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    deck = _deck_of(tmp_path / "d.jpt")
    slide_id = deck["slides"][0]["id"]

    out = _run(edit_presentation, tmp_path, path="d.jpt", operations_json=json.dumps([
        {"op": "add_slide", "slide": {"layout": "equation", "title": "Two",
                                      "equation": r"\pi r^2"}},
        {"op": "set_notes", "slide": slide_id, "notes": "remember the pause"},
    ]))
    assert "2 slides" in out
    after = _deck_of(tmp_path / "d.jpt")
    assert after["slides"][0]["notes"] == "remember the pause"
    assert after["slides"][1]["elements"][1]["type"] == "equation"


def test_a_failed_operation_leaves_the_file_untouched(tmp_path):
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    before = (tmp_path / "d.jpt").read_bytes()
    with pytest.raises(ValueError, match="operation 2"):
        _run(edit_presentation, tmp_path, path="d.jpt", operations_json=json.dumps([
            {"op": "set_notes", "slide": _deck_of(tmp_path / "d.jpt")["slides"][0]["id"], "notes": "ok"},
            {"op": "delete_element", "element": "does-not-exist"},
        ]))
    assert (tmp_path / "d.jpt").read_bytes() == before


def test_check_presentation_reads_without_writing(tmp_path):
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    before = (tmp_path / "d.jpt").read_bytes()
    out = _run(check_presentation, tmp_path, path="d.jpt")
    assert "1 slide" in out and "No problems found" in out
    assert (tmp_path / "d.jpt").read_bytes() == before


def test_check_presentation_reports_a_hand_broken_deck(tmp_path):
    (tmp_path / "hand.jpt").write_text(json.dumps({
        "slides": [{"id": "s1", "elements": [
            {"id": "t1", "type": "text", "x": 40, "y": 40, "w": 300, "h": 60,
             "text": "unreadable", "fontSize": 8},
        ]}],
    }), encoding="utf-8")
    out = _run(check_presentation, tmp_path, path="hand.jpt")
    assert "tiny-type" in out


def test_latex_survives_the_tool_boundary(tmp_path):
    """The regression that a smoke test found and the suite did not.

    `tools._decode_escape_sequences` repairs models that emit a literal `\\n`,
    and it fires on any string containing `\\n`, `\\t` or `\\r`. Valid JSON
    carrying LaTeX is full of those — `\\top`, `\\rm`, `\\nabla` — so running
    the repair *before* parsing turned `\\top` into a tab and "op". Parsing
    first, and repairing only after a real failure, is what keeps a formula the
    thing the agent typed.
    """
    formula = r"\frac{QK^{\top}}{\sqrt{d_k}} \rightarrow \nabla_\theta"
    _run(create_presentation, tmp_path, path="math.jpt", outline_json=json.dumps({
        "slides": [{"layout": "equation", "title": "Attention", "equation": formula}],
    }))
    deck = _deck_of(tmp_path / "math.jpt")
    stored = next(el for el in deck["slides"][0]["elements"] if el["type"] == "equation")
    assert stored["latex"] == formula
    assert "\t" not in stored["latex"], "a tab means the escape repair ran over valid JSON"


def test_a_double_escaped_outline_is_still_repaired(tmp_path):
    """The repair path the helper exists for still works — it just runs second."""
    outline = json.dumps({"slides": [{"layout": "bullets", "title": "Lines",
                                      "bullets": ["one"]}]})
    _run(create_presentation, tmp_path, path="ok.jpt",
         outline_json=outline.replace("\n", "\\n"))
    assert (tmp_path / "ok.jpt").exists()


def test_create_pptx_file_keeps_backslashes_in_a_bullet(tmp_path):
    """The sibling tool had the same latent defect: the escape repair ran over
    valid JSON, so a bullet holding a Windows path or a LaTeX command lost it."""
    pptx = pytest.importorskip("pptx")
    from gilzabots.tools import create_pptx_file

    _run(create_pptx_file, tmp_path, path="d.pptx", slides_json=json.dumps(
        [{"title": "Paths", "bullets": [r"C:\temp\report", r"\theta rises"]}]))

    body = pptx.Presentation(str(tmp_path / "d.pptx")).slides[0].placeholders[1]
    text = body.text_frame.text
    assert r"C:\temp\report" in text and r"\theta rises" in text
    assert "\t" not in text


def test_math_left_in_a_text_box_is_reported():
    """A text element renders `$x^2$` as five characters. It is the mistake a
    Beamer conversion makes, and it is invisible until someone reads the slide."""
    deck = _deck_with(jpt.create_element("text", text=r"Let $x^2$ be the area"))
    assert any(f.code == "math-in-text" for f in jpt.lint(deck))


def test_prices_and_shell_variables_are_not_mistaken_for_math():
    for prose in ("the licence costs $5 today", "export $PATH and $HOME", "a $5 to $9 range"):
        deck = _deck_with(jpt.create_element("text", text=prose))
        assert not any(f.code == "math-in-text" for f in jpt.lint(deck)), prose


# ─── backgrounds ─────────────────────────────────────────────────────────────

def test_a_slide_inherits_the_theme_background_and_can_override_or_refuse_it():
    deck = jpt.compile_outline({
        "theme": {"backgroundImage": "figures/theme.png", "backgroundOpacity": 0.4},
        "slides": [
            {"layout": "blank", "title": "Inherits"},
            {"layout": "blank", "title": "Its own", "backgroundImage": "figures/own.png",
             "backgroundFit": "contain", "backgroundOpacity": 1},
            {"layout": "blank", "title": "None at all", "backgroundImage": None,
             "background": "#101010"},
        ],
    })
    first, second, third = (jpt.background_of(deck, s) for s in deck["slides"])
    assert first["image"] == "figures/theme.png" and first["opacity"] == 0.4
    assert second["image"] == "figures/own.png" and second["fit"] == "contain"
    assert third["image"] == "" and third["color"] == "#101010"


def test_a_background_picture_that_does_not_exist_is_an_error(tmp_path):
    deck = jpt.compile_outline({"slides": [
        {"layout": "blank", "title": "Missing", "backgroundImage": "figures/nope.jpg"}]})
    findings = jpt.lint(deck, project_root=str(tmp_path))
    assert any(f.code == "missing-background" and f.level == "error" for f in findings)


def _picture(fill, *, dark_right=False, width=640, height=360):
    """A data URI for a solid colour, optionally dark down its right half."""
    pymupdf = pytest.importorskip("pymupdf")
    import base64
    document = pymupdf.open()
    page = document.new_page(width=width, height=height)
    page.draw_rect(pymupdf.Rect(0, 0, width, height), color=None, fill=fill)
    if dark_right:
        page.draw_rect(pymupdf.Rect(width / 2, 0, width, height), color=None, fill=(0.05, 0.05, 0.08))
    data = page.get_pixmap(dpi=72).tobytes("png")
    document.close()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _themed(image, **theme):
    deck = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "Over a picture", "bullets": ["can you read this"]}]})
    jpt.apply_theme(deck, {"backgroundImage": image, "backgroundOpacity": 1, **theme})
    return deck


def test_legibility_over_a_picture_is_measured_rather_than_assumed():
    """The linter decodes the picture and samples what is under the glyphs. A
    pale background chosen for text to sit on must raise nothing, or the warning
    is noise the reader learns to skip."""
    pale = _themed(_picture((0.94, 0.96, 0.99)))
    assert not any(f.code in ("low-contrast", "text-over-picture") for f in jpt.lint(pale))


def test_dark_ink_on_a_dark_picture_is_reported_with_the_share_it_covers():
    dark = _themed(_picture((0.08, 0.09, 0.12)))
    finding = next(f for f in jpt.lint(dark) if f.code == "low-contrast")
    assert "100%" in finding.message


def test_the_same_dark_picture_with_light_ink_raises_nothing():
    """It is the *pair* that is illegible, not the picture."""
    light_on_dark = _themed(_picture((0.08, 0.09, 0.12)), color="#ffffff")
    assert not any(f.code in ("low-contrast", "text-over-picture") for f in jpt.lint(light_on_dark))


def test_only_the_area_under_the_glyphs_counts():
    """A left-aligned title spans the whole slide width and its letters do not.
    Measuring the box instead of the ink would condemn a picture whose dark half
    is nowhere near the text."""
    deck = _themed(_picture((0.95, 0.95, 0.95), dark_right=True))
    assert not any(f.code == "low-contrast" for f in jpt.lint(deck))


def test_a_picture_that_cannot_be_read_is_reported_as_such():
    """Neither silence nor a guess: an http: background is not fetched, and the
    reader is told to check it themselves."""
    remote = _themed("https://example.com/photo.jpg")
    assert any(f.code == "text-over-picture" for f in jpt.lint(remote))


def test_an_invalid_background_is_refused_with_a_diagnostic():
    with pytest.raises(jpt.JptError, match="backgroundFit"):
        jpt.compile_outline({"slides": [{"layout": "blank", "title": "x",
                                     "backgroundImage": "a.png", "backgroundFit": "stretch"}]})
    with pytest.raises(jpt.JptError, match="backgroundOpacity"):
        jpt.compile_outline({"slides": [{"layout": "blank", "title": "x",
                                     "backgroundImage": "a.png", "backgroundOpacity": 4}]})


def test_set_background_reaches_one_slide_or_the_whole_deck(tmp_path):
    (tmp_path / "figures").mkdir()
    (tmp_path / "figures" / "bg.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "blank", "title": "One"}, {"layout": "blank", "title": "Two"}]}))
    deck = _deck_of(tmp_path / "d.jpt")
    second = deck["slides"][1]["id"]

    _run(edit_presentation, tmp_path, path="d.jpt", operations_json=json.dumps([
        {"op": "set_background", "all": True,
         "backgroundImage": "figures/bg.png", "backgroundOpacity": 0.4},
        {"op": "set_background", "slide": second, "backgroundImage": None,
         "background": "#101010"},
    ]))
    after = _deck_of(tmp_path / "d.jpt")
    # Packaged on the way in, like every other local asset an agent references.
    assert jpt.background_of(after, after["slides"][0])["image"].startswith("jpt:assets/")
    assert jpt.background_of(after, after["slides"][1])["image"] == ""
    assert after["theme"]["backgroundOpacity"] == 0.4


# ─── portability ─────────────────────────────────────────────────────────────
# Every local asset becomes a package member, so a `.jpt` survives being moved.
# A deck authored by an agent and one built in the editor follow one contract.

def _figure(tmp_path, name="figures/plot.png", size=(120, 80)):
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    page = document.new_page(width=size[0], height=size[1])
    page.draw_circle(pymupdf.Point(size[0] / 2, size[1] / 2), 20, fill=(0.2, 0.4, 0.8))
    page.get_pixmap(dpi=96).save(str(path))
    document.close()
    return name


def _images_of(path):
    deck = _deck_of(path)
    return [el["src"] for slide in deck["slides"] for el in slide["elements"]
            if el["type"] == "image"]


def test_a_created_deck_embeds_the_pictures_it_references(tmp_path):
    figure = _figure(tmp_path)
    out = _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "theme": {"backgroundImage": figure},
        "slides": [{"layout": "image", "title": "A figure", "image": figure}],
    }))
    assert "Packaged 1 internal asset" in out, out
    deck = _deck_of(tmp_path / "d.jpt")
    assert deck["theme"]["backgroundImage"].startswith("jpt:assets/")
    assert _images_of(tmp_path / "d.jpt")[0].startswith("jpt:assets/")
    # The file it came from is left where it is: it is the source the user
    # re-renders or re-edits, not a temporary.
    assert (tmp_path / figure).exists()


def test_the_same_picture_used_twice_is_embedded_once(tmp_path):
    figure = _figure(tmp_path)
    out = _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "image", "title": "One", "image": figure},
                   {"layout": "image", "title": "Two", "image": figure}],
    }))
    assert "Packaged 1 internal asset" in out


def test_self_containment_cannot_be_declined(tmp_path):
    figure = _figure(tmp_path)
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "embed_images": False,
        "slides": [{"layout": "image", "title": "One", "image": figure}],
    }))
    assert _images_of(tmp_path / "d.jpt")[0].startswith("jpt:assets/")


def test_an_edit_keeps_the_deck_self_contained(tmp_path):
    figure = _figure(tmp_path)
    for embed in (True, False):
        name = f"{embed}.jpt"
        _run(create_presentation, tmp_path, path=name, outline_json=json.dumps({
            "embed_images": embed,
            "slides": [{"layout": "image", "title": "One", "image": figure}]}))
        _run(edit_presentation, tmp_path, path=name, operations_json=json.dumps([
            {"op": "add_slide", "slide": {"layout": "image", "title": "Two", "image": figure}}]))
        sources = _images_of(tmp_path / name)
        assert len(sources) == 2
        assert all(src.startswith("jpt:assets/") for src in sources), sources


def test_a_picture_over_the_size_limit_keeps_its_reference_and_says_so(tmp_path):
    from gilzabots.jpt import assets
    figure = _figure(tmp_path)
    deck = jpt.compile_outline({"slides": [
        {"layout": "image", "title": "Big", "image": figure}]})
    report = assets.embed_images(deck, str(tmp_path), max_bytes=10)
    assert report["embedded"] == 0
    assert report["skipped"] and "embed limit" in report["skipped"][0][1]
    assert "left as a reference" in assets.describe(report)
    assert deck["slides"][0]["elements"][1]["src"] == figure


def test_a_missing_picture_is_left_for_the_linter_to_name(tmp_path):
    from gilzabots.jpt import assets
    deck = jpt.compile_outline({"slides": [
        {"layout": "image", "title": "Gone", "image": "figures/gone.png"}]})
    report = assets.embed_images(deck, str(tmp_path))
    assert report["embedded"] == 0
    assert any(f.code == "missing-image"
               for f in jpt.lint(deck, project_root=str(tmp_path)))


def test_data_uris_and_urls_are_left_alone(tmp_path):
    from gilzabots.jpt import assets
    assert assets.is_portable("data:image/png;base64,iVBORw0KGgo=")
    assert assets.is_portable("https://example.com/a.png")
    assert not assets.is_portable("figures/a.png")


# ─── themes ──────────────────────────────────────────────────────────────────

def test_a_theme_marks_each_slide_title_so_it_takes_the_theme_colour():
    """The half of `apply_theme` that is not a dict update: a deck written
    before roles existed has none, and a white title colour would otherwise
    leave every title in dark ink on its own header band."""
    deck = jpt.compile_outline({"slides": [
        {"layout": "title", "title": "Cover"},
        {"layout": "bullets", "title": "Content", "bullets": ["a"]}]})
    jpt.apply_theme(deck, {"headerHeight": 180, "titleColor": "#ffffff"})

    cover, content = deck["slides"]
    assert jpt.title_element_of(content)["role"] == "title"
    assert jpt.text_color_of(content["elements"][0], deck["theme"]) == "#ffffff"
    # A cover places its title below the band on purpose, so it is not one.
    assert jpt.title_element_of(cover) is None
    assert jpt.text_color_of(cover["elements"][0], deck["theme"]) == deck["theme"]["color"]


def test_the_header_band_is_drawn_only_where_there_is_a_title():
    """The band is the title's background. An empty coloured bar across a cover
    is a theme applied to a slide it was not meant for."""
    deck = jpt.compile_outline({"slides": [
        {"layout": "title", "title": "Cover"},
        {"layout": "bullets", "title": "Content", "bullets": ["a"]}]})
    jpt.apply_theme(deck, {"headerHeight": 180, "footerHeight": 40, "footerText": "title"})
    cover, content = (jpt.chrome_of(deck, s) for s in deck["slides"])
    assert cover["header"] == 0 and content["header"] == 180
    # The footline shows on every slide, as Beamer's does.
    assert cover["footer"] == 40 and content["footer"] == 40


def test_no_theme_means_no_chrome_at_all():
    deck = jpt.compile_outline({"slides": [{"layout": "bullets", "title": "x", "bullets": ["a"]}]})
    assert jpt.chrome_of(deck, deck["slides"][0]) is None


def test_an_explicit_element_colour_still_wins_over_the_theme():
    deck = jpt.compile_outline({"slides": [
        {"layout": "bullets", "title": "Content", "bullets": ["a"]}]})
    jpt.apply_theme(deck, {"headerHeight": 180, "titleColor": "#ffffff"})
    title = jpt.title_element_of(deck["slides"][0])
    title["color"] = "#ff0000"
    assert jpt.text_color_of(title, deck["theme"]) == "#ff0000"


def test_a_role_that_is_not_a_role_is_refused():
    with pytest.raises(jpt.JptError, match="role"):
        jpt.create_element("text", text="x", role="subtitle")


def test_set_presentation_theme_applies_a_store_theme(tmp_path):
    from gilzabots.tools import set_presentation_theme

    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "title": "Talk", "slides": [
            {"layout": "title", "title": "Talk"},
            {"layout": "bullets", "title": "Agenda", "bullets": ["one"]}]}))
    out = _run(set_presentation_theme, tmp_path, path="d.jpt", theme="madrid")
    assert "Madrid" in out

    deck = _deck_of(tmp_path / "d.jpt")
    assert deck["theme"]["headerHeight"] == 132
    assert deck["theme"]["footerHeight"] == 24
    assert deck["theme"]["fontFamily"].startswith("Latin Modern Sans")
    assert deck["theme"]["footerText"] == "title"
    assert jpt.title_element_of(deck["slides"][1])["role"] == "title"


def test_a_named_theme_replaces_the_look_rather_than_blending_into_it(tmp_path):
    """Applying Madrid and then a theme with no header must not leave Madrid's
    band standing: the deck would look like neither."""
    from gilzabots.tools import set_presentation_theme

    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    _run(set_presentation_theme, tmp_path, path="d.jpt", theme="madrid")
    _run(set_presentation_theme, tmp_path, path="d.jpt", theme="blue-arcs")

    theme = _deck_of(tmp_path / "d.jpt")["theme"]
    assert theme["headerHeight"] == 0 and theme["titleColor"] is None
    assert theme["backgroundImage"].startswith("jpt:assets/")


def test_explicit_fields_tweak_the_theme_instead_of_replacing_it(tmp_path):
    from gilzabots.tools import set_presentation_theme

    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    _run(set_presentation_theme, tmp_path, path="d.jpt", theme="madrid")
    _run(set_presentation_theme, tmp_path, path="d.jpt", fields_json='{"headerColor": "#aa3355"}')

    theme = _deck_of(tmp_path / "d.jpt")["theme"]
    assert theme["headerColor"] == "#aa3355"
    assert theme["headerHeight"] == 132, "a tweak must not undo the theme"


def test_an_unknown_theme_answers_with_the_catalogue(tmp_path):
    """The discovery path: no second tool to list them."""
    from gilzabots.tools import set_presentation_theme

    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    with pytest.raises(ValueError, match="madrid"):
        _run(set_presentation_theme, tmp_path, path="d.jpt", theme="warsaw")


def test_a_misspelled_theme_field_is_refused_with_the_known_ones(tmp_path):
    from gilzabots.tools import set_presentation_theme

    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    with pytest.raises(ValueError, match="headerHight"):
        _run(set_presentation_theme, tmp_path, path="d.jpt", fields_json='{"headerHight": 10}')


def test_embed_images_is_an_idempotent_compatibility_operation(tmp_path):
    figure = _figure(tmp_path)
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "embed_images": False,
        "slides": [{"layout": "image", "title": "One", "image": figure}]}))
    before = _images_of(tmp_path / "d.jpt")[0]
    assert before.startswith("jpt:assets/")

    out = _run(edit_presentation, tmp_path, path="d.jpt",
               operations_json=json.dumps([{"op": "embed_images"}]))
    assert "Packaged 1 internal asset" in out
    assert _images_of(tmp_path / "d.jpt")[0] == before


def test_packing_covers_backgrounds_as_well_as_elements(tmp_path):
    figure = _figure(tmp_path)
    _run(create_presentation, tmp_path, path="d.jpt", outline_json=json.dumps({
        "embed_images": False,
        "theme": {"backgroundImage": figure},
        "slides": [{"layout": "bullets", "title": "One", "bullets": ["a"]}]}))
    _run(edit_presentation, tmp_path, path="d.jpt",
         operations_json=json.dumps([{"op": "embed_images"}]))
    deck = _deck_of(tmp_path / "d.jpt")
    assert deck["theme"]["backgroundImage"].startswith("jpt:assets/")
    assert not [src for src in jpt.used_sources(deck) if not jpt.is_portable(src)]
