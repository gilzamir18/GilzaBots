"""Unit tests for the humanize skill and its tic scanner.

The scanner is what keeps this skill from being a matter of taste: it decides
which passages get rewritten and, afterwards, whether the rewrite actually
helped. Two failure modes are tested harder than the rest, because both make the
skill actively harmful rather than merely useless:

  - a hit inside math, code, a LaTeX comment or a \\cite key, which would send the
    worker editing markup it must not touch;
  - "clean" reported for a Portuguese draft, which happens the moment the
    patterns are English-only.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

from gilzabots.assetstore import (
    asset_matches_install,
    find_assets,
    install_asset,
    list_assets,
)
from gilzabots.skills import discover_skills, parse_skill_md

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILL_DIR = os.path.join(_REPO_ROOT, "skills", "humanize")
_SCRIPT = os.path.join(_SKILL_DIR, "scripts", "humanize_scan.py")

_spec = importlib.util.spec_from_file_location("humanize_scan", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

analyze = _mod.analyze
detect_language = _mod.detect_language
flavor_for = _mod.flavor_for
strip_markup_lines = _mod.strip_markup_lines


def _run(*args):
    return subprocess.run([sys.executable, _SCRIPT, *args], capture_output=True, text=True)


def _scan(text, flavor="md", lang="auto"):
    return analyze(text.splitlines(keepends=True), flavor, lang)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _categories(report):
    return set(report["hits"])


# ---------------------------------------------------------------------------
# Skill manifest and packaging
# ---------------------------------------------------------------------------

def test_skill_manifest_is_discoverable_and_uses_the_main_model():
    meta = parse_skill_md(_SKILL_DIR)
    assert meta is not None
    assert meta["name"] == "humanize"
    # A rewrite that must change every word while changing no meaning is exactly
    # the task a smaller worker model degrades, so this skill runs on the
    # project's main model rather than the worker one.
    assert meta["model"] == "default"


def test_the_description_names_the_trigger_and_the_guarantee():
    """The description is all the orchestrator sees when routing."""
    desc = parse_skill_md(_SKILL_DIR)["description"].lower()
    assert "humanize" in desc
    # Routing trigger.
    assert any(w in desc for w in ("generated text", "generated"))
    # The preservation promise: a router that only reads "rewrites text" would
    # send it prose whose citations and numbers must not move.
    assert "meaning" in desc
    assert "citations" in desc


def test_skill_ships_a_light_profile_body():
    full = parse_skill_md(_SKILL_DIR, "full")
    light = parse_skill_md(_SKILL_DIR, "light")
    assert light["body"] and light["body"] != full["body"]
    assert len(light["body"]) < len(full["body"])
    # Frontmatter must never come from the variant file.
    assert light["name"] == full["name"] and light["model"] == full["model"]


def test_both_profiles_carry_the_preservation_contract():
    """Dropping the preservation rules from the condensed body would leave a
    worker that rewrites LaTeX commands and citation keys."""
    for profile in ("full", "light"):
        body = parse_skill_md(_SKILL_DIR, profile)["body"].lower()
        assert "cite" in body, profile
        assert "preserve" in body, profile
        # The boundary that keeps this an editing skill, not a laundering one.
        assert "detector" in body or "classifier" in body, profile


def test_skill_is_discovered_from_the_repo_skills_dir():
    assert "humanize" in {s["name"] for s in discover_skills()}


def test_skill_is_registered_in_the_asset_store():
    ids = {a["id"] for a in list_assets("skill")}
    assert "humanize" in ids
    matches = find_assets("skill", "humanize")
    assert len(matches) == 1
    assert matches[0]["_zip"].exists()


def test_the_store_asset_carries_the_scanner_not_just_the_manifest(tmp_path):
    """A skill whose body tells the worker to run a script that the packaged zip
    does not contain fails only at run time, in the user's project."""
    meta = find_assets("skill", "humanize")[0]
    install_asset(meta, str(tmp_path))
    installed = tmp_path / ".gilzabots" / "skills" / "humanize"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "SKILL.light.md").is_file()
    assert (installed / "scripts" / "humanize_scan.py").is_file()
    # An install is self-consistent by construction, so checking it against the
    # zip proves nothing. The drift that actually happens is the catalog zip
    # falling behind `skills/humanize/` after the source is edited and nobody
    # re-registers, which ships an old skill to every store user.
    assert asset_matches_install(meta, str(tmp_path))
    for rel in ("SKILL.md", "SKILL.light.md", os.path.join("scripts", "humanize_scan.py")):
        source = os.path.join(_SKILL_DIR, rel)
        packaged = installed / rel
        with open(source, "rb") as a, open(packaged, "rb") as b:
            assert a.read() == b.read(), (
                f"{rel} in the store zip differs from skills/humanize/{rel}; "
                f"re-run assetstore.register_asset after editing the skill")


# ---------------------------------------------------------------------------
# Language coverage
# ---------------------------------------------------------------------------

def test_portuguese_tics_are_detected():
    report = _scan(
        "Nos dias de hoje, é importante ressaltar que a tecnologia não é apenas "
        "uma ferramenta, mas também um divisor de águas.\n\n"
        "Além disso, estudos mostram que as possibilidades são infinitas.\n"
    )
    assert report["language"] == "pt"
    assert {"filler-opener", "antithesis", "vague-authority",
            "grand-closer", "hollow-connective"} <= _categories(report)
    assert report["verdict"] == "heavy"


def test_english_tics_are_detected():
    report = _scan(
        "In today's fast-paced world, it's important to note that this is not "
        "just a trend, but also a paradigm shift.\n\n"
        "Moreover, studies show that the possibilities are endless.\n"
    )
    assert report["language"] == "en"
    assert {"filler-opener", "antithesis", "vague-authority",
            "grand-closer", "hollow-connective"} <= _categories(report)


def test_a_portuguese_draft_is_not_reported_clean_by_english_patterns():
    """The regression that motivates carrying two pattern sets."""
    pt = ("Vale destacar que a solução é abrangente e robusta. Por fim, "
          "as possibilidades são infinitas.\n")
    assert _scan(pt, lang="pt")["hits"]
    assert _scan(pt)["language"] == "pt"


def test_english_patterns_still_apply_inside_a_portuguese_document():
    """Mixed-language drafts are the common case; an English tic in a Portuguese
    paper is still a tic."""
    report = _scan(
        "O sistema é robusto e a arquitetura é sólida. Nos dias de hoje isso "
        "importa muito para todos os times de engenharia.\n\n"
        "This section will delve into the intricate tapestry of the results.\n",
        lang="pt",
    )
    assert "inflated-vocabulary" in report["hits"]
    matched = {h["match"].lower() for h in report["hits"]["inflated-vocabulary"]["examples"]}
    assert any("delve" in m or "tapestry" in m for m in matched)


# ---------------------------------------------------------------------------
# Markup must never be scanned
# ---------------------------------------------------------------------------

def test_latex_comments_math_and_verbatim_are_not_scanned():
    tex = (
        "\\section{Resultados}\n"
        "% delve into the tapestry of crucial synergy\n"
        "O resultado foi obtido com $E = mc^2$ e \\(x \\ge 0\\).\n"
        "\\begin{lstlisting}\n"
        "leverage the robust seamless pipeline\n"
        "\\end{lstlisting}\n"
        "\\begin{equation}\n"
        "  \\text{delve} = \\text{tapestry}\n"
        "\\end{equation}\n"
    )
    report = analyze(tex.splitlines(keepends=True), "tex", "en")
    assert report["hits"] == {}


def test_citation_and_label_keys_are_not_scanned():
    """`\\label{sec:robust-design}` is a key, not the word "robust"."""
    tex = ("O método é descrito em \\cite{robusto2024} e \\ref{sec:holistico}.\n"
           "\\label{fig:seamless-pipeline}\n")
    report = analyze(tex.splitlines(keepends=True), "tex", "pt")
    assert report["hits"] == {}


def test_markdown_code_fences_and_inline_code_are_not_scanned():
    md = ("O trecho abaixo mostra a chamada.\n\n"
          "```python\n"
          "leverage_the_robust_pipeline()  # delve into the tapestry\n"
          "```\n\n"
          "Use `harness_seamless()` para iniciar.\n")
    assert _scan(md)["hits"] == {}


def test_a_percent_sign_only_starts_a_comment_in_latex():
    """Cutting a Markdown line at `%` would silently hide half the prose."""
    text = "Cerca de 50% dos casos exigem que se aprofunde no problema.\n"
    md = strip_markup_lines(text.splitlines(keepends=True), "md")
    assert "dos casos" in md[0][1]
    tex = strip_markup_lines(text.splitlines(keepends=True), "tex")
    assert "dos casos" not in tex[0][1]


def test_hits_report_the_line_number_of_the_original_file():
    """Stripping happens line by line precisely so this stays true."""
    tex = (
        "\\section{Introdução}\n"
        "% comentário\n"
        "\\begin{lstlisting}\n"
        "noise\n"
        "\\end{lstlisting}\n"
        "Nos dias de hoje, tudo mudou.\n"
    )
    report = analyze(tex.splitlines(keepends=True), "tex", "pt")
    lines = [ex["line"] for ex in report["hits"]["filler-opener"]["examples"]]
    assert lines == [6]


def test_flavor_is_taken_from_the_extension_but_can_be_forced():
    assert flavor_for("/tmp/paper.tex") == "tex"
    assert flavor_for("/tmp/notes.md") == "md"
    assert flavor_for("/tmp/paper.tex", "md") == "md"


# ---------------------------------------------------------------------------
# Calibration: human prose must not be called dirty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relpath", [
    "README.md",
    "CLAUDE.md",
    "docs/specs/cloud_sync.md",
    "skills/staged-reader/SKILL.md",
    "skills/log-table-condenser/SKILL.md",
])
def test_hand_written_repository_prose_scans_clean(relpath):
    """The thresholds were set from this corpus; a change that makes the repo's
    own documentation look generated is a false-positive regression."""
    path = os.path.join(_REPO_ROOT, relpath)
    with open(path, "r", encoding="utf-8") as f:
        report = analyze(f.readlines(), flavor_for(path))
    assert report["verdict"] == "clean", f"{relpath}: density {report['density']}"


def test_em_dashes_are_reported_but_not_flagged_at_human_levels():
    """Measured on the real corpus, not assumed: this repo's hand-written docs
    use 10-13 em dashes per 1000 words. A threshold of 4, which the usual advice
    suggests, would have taught the worker to strip a construction this author
    uses on purpose -- so the flag sits above anything a human file here reaches
    and the raw count is always reported instead."""
    path = os.path.join(_REPO_ROOT, "skills", "staged-reader", "SKILL.md")
    with open(path, "r", encoding="utf-8") as f:
        report = analyze(f.readlines(), "md")
    assert report["structure"]["em_dash_per_1000"] > 4.0
    assert not any("em-dash" in f for f in report["flags"])


def test_repeated_openers_ignore_articles_and_prepositions():
    """"4 paragraphs start with 'the'" is true of any well-written document."""
    text = "".join(f"The point number {i} is made here plainly.\n\n" for i in range(5))
    assert _scan(text)["structure"]["repeated_openers"] == {}


def test_a_repeated_content_word_opener_is_still_reported():
    text = "".join(f"Moreover the point number {i} is made here.\n\n" for i in range(4))
    assert _scan(text)["structure"]["repeated_openers"].get("moreover") == 4


def test_a_document_that_quotes_tics_scores_high_and_says_so():
    """Use versus mention: the scanner cannot separate them, so the report has
    to warn instead of pretending. This skill's own manifest is the case in
    point -- it names every tic it hunts."""
    path = os.path.join(_SKILL_DIR, "SKILL.md")
    with open(path, "r", encoding="utf-8") as f:
        report = analyze(f.readlines(), "md")
    assert report["verdict"] == "heavy"
    proc = _run("scan", path)
    assert "cannot tell use from mention" in proc.stdout
    # And both manifest profiles must warn the worker before it "fixes" a
    # glossary's examples.
    for profile in ("full", "light"):
        assert "mention" in parse_skill_md(_SKILL_DIR, profile)["body"].lower()


def test_an_inline_mention_of_an_environment_does_not_swallow_the_file():
    """Regression: `\\begin{lstlisting}` quoted inside a Markdown sentence used
    to switch the scanner into verbatim mode with no matching `\\end`, so every
    line after it went unscanned and the file reported clean."""
    md = (
        "Prose that mentions `\\begin{lstlisting}` in passing.\n"
        "\n"
        "In today's fast-paced world, the rest of the file still has to be read.\n"
    )
    report = _scan(md)
    assert report["words"] > 15
    assert "filler-opener" in report["hits"]


def test_a_real_latex_environment_still_hides_its_body():
    tex = ("Texto normal antes.\n"
           "\\begin{verbatim}\n"
           "Nos dias de hoje, é importante ressaltar isso.\n"
           "\\end{verbatim}\n"
           "Texto normal depois.\n")
    assert analyze(tex.splitlines(keepends=True), "tex", "pt")["hits"] == {}


# ---------------------------------------------------------------------------
# Rhythm and structure
# ---------------------------------------------------------------------------

def test_uniform_sentence_rhythm_is_flagged():
    sentence = "The system reads the file and then writes the result to disk.\n"
    report = _scan(sentence * 10)
    assert report["rhythm"]["sentence_cv"] < 0.35
    assert any("uniform sentence rhythm" in f for f in report["flags"])


def test_varied_rhythm_is_not_flagged():
    text = (
        "It failed.\n"
        "The parser read the whole file into memory, matched every rule against "
        "every line, and then discarded the result because the encoding guess had "
        "been wrong from the first byte.\n"
        "We fixed it.\n"
        "The fix was to detect the encoding once, before any rule ran, which "
        "turned a class of silent corruption into a single loud error at startup.\n"
        "That was enough.\n"
    )
    report = _scan(text)
    assert not any("uniform sentence rhythm" in f for f in report["flags"])


def test_a_list_heavy_document_is_flagged():
    text = "Intro paragraph.\n\n" + "".join(f"- item {i}\n" for i in range(20))
    assert any("list-heavy" in f for f in _scan(text)["flags"])


def test_label_bullets_are_counted_in_both_markup_flavors():
    md = _scan("- **Performance**: it is fast.\n- **Safety**: it is checked.\n")
    assert md["structure"]["label_bullets"] == 2
    tex = analyze(
        ["\\item \\textbf{Desempenho}: é rápido.\n",
         "\\item \\textbf{Segurança}: é verificado.\n"], "tex", "pt")
    assert tex["structure"]["label_bullets"] == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_scan_reports_the_headline_fields(tmp_path):
    path = _write(tmp_path, "draft.md",
                  "In today's fast-paced world, it's worth noting that we must delve deeper.\n")
    proc = _run("scan", path)
    assert proc.returncode == 0
    for key in ("FILE:", "LANGUAGE:", "WORDS:", "DENSITY:", "VERDICT:", "HITS:"):
        assert key in proc.stdout
    # The report must say out loud that a hit is not an order to delete.
    assert "candidates, not verdicts" in proc.stdout


def test_scan_emits_json_on_request(tmp_path):
    import json
    path = _write(tmp_path, "draft.md", "Vale destacar que o sistema é robusto.\n")
    proc = _run("scan", path, "--json")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["language"] == "pt"
    assert "filler-opener" in data["hits"]


def test_scan_fails_loudly_on_a_missing_file(tmp_path):
    proc = _run("scan", str(tmp_path / "nope.md"))
    assert proc.returncode == 1
    assert "file not found" in proc.stderr


def test_scan_refuses_binary_input(tmp_path):
    path = tmp_path / "blob.md"
    path.write_bytes(b"prose\x00\x01\x02more prose")
    proc = _run("scan", str(path))
    assert proc.returncode == 1
    assert "binary" in proc.stderr


def test_diff_reports_the_categories_that_improved(tmp_path):
    before = _write(tmp_path, "before.md",
                    "In today's fast-paced world, it's important to note that caching "
                    "is not just useful, but also pivotal. Moreover, studies show it "
                    "helps a great deal in production systems under real load.\n")
    after = _write(tmp_path, "after.md",
                   "Caching matters because it removes repeated work. In our own "
                   "benchmarks it cut median latency by about half under production "
                   "load, which is why the service uses it on every read path.\n")
    proc = _run("diff", before, after)
    assert proc.returncode == 0
    assert "DENSITY:" in proc.stdout and "->" in proc.stdout
    assert "[ok]" in proc.stdout
    assert "WARNING" not in proc.stdout


def test_diff_warns_when_the_rewrite_deleted_the_content(tmp_path):
    """Removing tics by removing text is the failure this guard exists for."""
    body = ("It's important to note that the measurement was repeated ten times "
            "on three machines, and the variance stayed under two percent. ") * 4
    before = _write(tmp_path, "before.md", body)
    after = _write(tmp_path, "after.md", "The measurement was repeated ten times.\n")
    proc = _run("diff", before, after)
    assert proc.returncode == 0
    assert "WARNING" in proc.stdout
    assert "lost" in proc.stdout


def test_diff_warns_when_the_rewrite_made_the_text_worse(tmp_path):
    before = _write(tmp_path, "before.md",
                    "The cache stores results so repeated requests skip the work, "
                    "which is why the read path is fast enough to serve every user.\n")
    after = _write(tmp_path, "after.md",
                   "It's important to note that the cache is not just storage, but "
                   "also a pivotal, robust and seamless way to harness performance.\n")
    proc = _run("diff", before, after)
    assert proc.returncode == 0
    assert "tic density went up" in proc.stdout.lower()
    assert "[WORSE]" in proc.stdout


def test_diff_scans_both_sides_with_the_same_language(tmp_path):
    """A short rewrite can flip auto-detection and make a Portuguese pair look
    like an improvement only because the patterns changed under it."""
    before = _write(tmp_path, "before.md",
                    "Vale destacar que a solução não é apenas robusta, mas também "
                    "abrangente para todos os casos de uso considerados aqui.\n")
    after = _write(tmp_path, "after.md", "A solução cobre todos os casos.\n")
    proc = _run("diff", before, after)
    assert proc.returncode == 0
    assert "inflated-vocabulary" in proc.stdout or "filler-opener" in proc.stdout
