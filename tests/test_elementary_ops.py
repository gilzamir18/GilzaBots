"""The elementary-ops skill and its plan runner.

The skill exists so a small model never compares text by eye: it writes a plan,
`ops.py` executes it and `check` lines state the answer. Three things would make
it harmful rather than useless, and are tested hardest:

  - an example in either skill body that the runner refuses — a small model
    copies the example, not the rule;
  - an invalid plan that runs anyway, repaired or reinterpreted silently;
  - a check that passes on evidence the script never computed (a citation
    inside a LaTeX comment, a section that ran into the next one).
"""

import importlib.util
import os
import re
import subprocess
import sys

import pytest

from gilzabots.assetstore import asset_matches_install, find_assets, install_asset, list_assets
from gilzabots.skills import discover_skills, parse_skill_md

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILL_DIR = os.path.join(_REPO_ROOT, "skills", "elementary-ops")
_SCRIPT = os.path.join(_SKILL_DIR, "scripts", "ops.py")

_spec = importlib.util.spec_from_file_location("elementary_ops_under_test", _SCRIPT)
ops = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ops
_spec.loader.exec_module(ops)


PAPER = r"""\documentclass{article}
\begin{document}
\section{Introdução}
Texto inicial \cite{knuth84}.
\section{Método}\label{sec:metodo}
Usamos 12 amostras e 3.5\% de erro \citep[p.~3]{lamport94, knuth84}. % \cite{hidden}
Ver Tabela~\ref{tab:a} e a Seção~\ref{sec:missing}.
\begin{table}
\begin{table}
inner
\end{table}
\label{tab:a}
\end{table}
\subsection{Detalhes}
Mais texto.
\section{Resultados}
Fim.
\end{document}
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "v1.tex").write_text(PAPER, encoding="utf-8")
    (tmp_path / "v2.tex").write_text(
        PAPER.replace("12 amostras", "15 amostras").replace("lamport94, ", ""), encoding="utf-8"
    )
    return tmp_path


def _run(root, plan_text, *extra):
    plan = root / "q.plan"
    plan.write_text(plan_text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, _SCRIPT, *extra[:1], str(plan), *extra[1:]] if extra
        else [sys.executable, _SCRIPT, "run", str(plan)],
        capture_output=True, text=True, cwd=root,
    )
    return proc.returncode, proc.stdout


def _values(root, plan_text):
    plan = ops.parse_plan(plan_text)
    return plan, ops.run_plan(plan, str(root))


def _check(root, plan_text):
    plan, values = _values(root, plan_text)
    return [ops.evaluate(c.expr, values) for c in plan.checks]


# ---------------------------------------------------------------------------
# Manifest, profiles, packaging
# ---------------------------------------------------------------------------

def test_manifest_is_discoverable_and_runs_on_the_worker_model():
    meta = parse_skill_md(_SKILL_DIR)
    assert meta["name"] == "elementary-ops"
    # The point of the skill is that a small model can drive it.
    assert meta["model"] == "worker"
    assert "elementary-ops" in {s["name"] for s in discover_skills()}


def test_the_description_names_the_routing_triggers():
    desc = parse_skill_md(_SKILL_DIR)["description"].lower()
    for trigger in ("compare two files", "passages", "citations", "labels", "diff"):
        assert trigger in desc, trigger


def test_skill_ships_a_shorter_light_body_with_the_same_contract():
    full = parse_skill_md(_SKILL_DIR, "full")
    light = parse_skill_md(_SKILL_DIR, "light")
    assert light["body"] != full["body"] and len(light["body"]) < len(full["body"])
    assert light["name"] == full["name"] and light["model"] == full["model"]
    for profile in ("full", "light"):
        body = parse_skill_md(_SKILL_DIR, profile)["body"]
        for rule in ("NOT APPLICABLE", "BLOCKED", "at most 2", "diff_files", "ELEMENTARY OPS REPORT",
                     "run_python_script", ".gilzabots/ops/"):
            assert rule in body, (profile, rule)


def test_every_operation_and_condition_is_documented_in_both_bodies():
    conditions = ("empty", "nonempty", "equal", "sameset", "subset", "contains", "all(", "any(", "not(")
    for profile in ("full", "light"):
        body = parse_skill_md(_SKILL_DIR, profile)["body"]
        for name in [*ops.CATALOG, *conditions]:
            assert f"`{name}" in body or f"{name}`" in body or f" {name}" in body, (profile, name)


def _example_plans():
    for profile in ("full", "light"):
        body = parse_skill_md(_SKILL_DIR, profile)["body"]
        for block in re.findall(r"```plan\n(.*?)```", body, re.S):
            yield profile, block


def test_every_example_plan_in_the_bodies_is_valid():
    """A small model copies the example; an example the runner refuses teaches failure."""
    examples = list(_example_plans())
    assert len(examples) >= 5
    for profile, block in examples:
        ops.parse_plan(block)  # raises PlanError on any invalid example


def test_the_catalog_command_lists_every_operation():
    proc = subprocess.run([sys.executable, _SCRIPT, "catalog"], capture_output=True, text=True)
    assert proc.returncode == 0
    for name in ops.CATALOG:
        assert f"  {name} " in proc.stdout


def test_skill_is_registered_in_the_asset_store_and_matches_the_source(tmp_path):
    assert "elementary-ops" in {a["id"] for a in list_assets("skill")}
    meta = find_assets("skill", "elementary-ops")[0]
    install_asset(meta, str(tmp_path))
    installed = tmp_path / ".gilzabots" / "skills" / "elementary-ops"
    assert asset_matches_install(meta, str(tmp_path))
    for rel in ("SKILL.md", "SKILL.light.md", os.path.join("scripts", "ops.py")):
        with open(os.path.join(_SKILL_DIR, rel), "rb") as a, open(installed / rel, "rb") as b:
            assert a.read() == b.read(), (
                f"{rel} in the store zip differs from skills/elementary-ops/{rel}; "
                "re-run assetstore.register_asset after editing the skill")


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------

def test_section_ends_at_the_next_heading_of_the_same_level_and_keeps_subsections(root):
    _, values = _values(root, 's1 = section v1.tex "Método"\ncheck nonempty s1\n')
    seg = values["s1"]
    assert (seg.start, seg.end) == (5, 15)
    assert seg.lines[0].startswith(r"\section{Método}")
    assert not any("Resultados" in line for line in seg.lines)


def test_section_title_ignores_case_and_accents_but_not_ambiguity(root):
    _, values = _values(root, "s1 = section v1.tex metodo\ncheck nonempty s1\n")
    assert values["s1"].start == 5
    (root / "twice.md").write_text("# Method A\ntext\n# Method B\nmore\n", encoding="utf-8")
    with pytest.raises(ops.PlanError, match="matches 2 headings"):
        _values(root, "s1 = section twice.md Method\ncheck nonempty s1\n")


def test_an_unknown_section_lists_the_headings_that_exist(root):
    with pytest.raises(ops.PlanError) as error:
        _values(root, "s1 = section v1.tex Conclusão\ncheck nonempty s1\n")
    assert "L3 Introdução" in str(error.value) and "L16 Resultados" in str(error.value)


def test_markdown_sections_skip_headings_inside_code_fences(root):
    (root / "doc.md").write_text(
        "# Intro\nx\n## Setup\n```\n# not a heading\n```\ny\n## Next\nz\n", encoding="utf-8"
    )
    _, values = _values(root, "s1 = section doc.md Setup\ncheck nonempty s1\n")
    assert (values["s1"].start, values["s1"].end) == (3, 7)


def test_env_matches_nested_environments_of_the_same_name(root):
    _, values = _values(root, "s1 = env v1.tex table\ncheck nonempty s1\n")
    assert (values["s1"].start, values["s1"].end) == (8, 13)
    with pytest.raises(ops.PlanError, match="#2 does not exist"):
        _values(root, "s1 = env v1.tex table 2\ncheck nonempty s1\n")


def test_lines_and_between(root):
    _, values = _values(
        root,
        "s1 = lines v1.tex 3-4\ns2 = lines v1.tex 16-end\n"
        "s3 = between v1.tex '^\\\\begin\\{table\\}' '^\\\\label'\ncheck nonempty s1\n",
    )
    assert values["s1"].lines == [r"\section{Introdução}", r"Texto inicial \cite{knuth84}."]
    assert values["s2"].end == 18
    assert (values["s3"].start, values["s3"].end) == (8, 12)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extractors_read_keys_and_skip_latex_comments(root):
    _, values = _values(
        root,
        's1 = section v1.tex "Método"\ns2 = cites s1\ns3 = refs s1\ns4 = labels s1\n'
        "s5 = numbers s1\ns6 = headings s1\ns7 = words s1\ncheck nonempty s2\n",
    )
    assert values["s2"].items == ["lamport94", "knuth84"]  # not "hidden"
    assert values["s3"].items == ["tab:a", "sec:missing"]
    assert values["s4"].items == ["sec:metodo", "tab:a"]
    assert values["s5"].items == ["12", "3.5"]  # "3" of p.~3 is inside \citep's option
    assert values["s6"].items == ["Método", "Detalhes"]
    assert values["s7"].value > 10


def test_grep_nonblank_count_and_unique(root):
    _, values = _values(
        root,
        "s1 = file v1.tex\ns2 = grep s1 '\\\\section'\ns3 = nonblank s1\n"
        "s4 = count s3\ns5 = cites s1\ns6 = unique s5\ncheck s4 == 18\n",
    )
    assert len(values["s2"].items) == 3
    assert values["s4"].value == 18
    assert values["s6"].items == ["knuth84", "lamport94"]


# ---------------------------------------------------------------------------
# Comparison and checks
# ---------------------------------------------------------------------------

def test_diff_and_set_operations_between_versions(root):
    _, values = _values(
        root,
        's1 = section v1.tex "Método"\ns2 = section v2.tex "Método"\ns3 = diff s1 s2\n'
        "s4 = cites s1\ns5 = cites s2\ns6 = only-a s4 s5\ns7 = common s4 s5\ns8 = only-b s4 s5\n"
        "check empty s6\n",
    )
    assert values["s3"].blocks == [(6, 6, 6, 6)]
    assert values["s6"].items == ["lamport94"]
    assert values["s7"].items == ["knuth84"]
    assert values["s8"].items == []


def test_checks_report_the_evidence_of_a_failure(root):
    results = _check(
        root,
        's1 = section v1.tex "Método"\ns2 = section v2.tex "Método"\ns3 = cites s1\ns4 = cites s2\n'
        "s5 = numbers s1\ns6 = numbers s2\ns7 = diff s1 s2\n"
        "check subset s3 s4\ncheck sameset s5 s6\ncheck s7 <= 1\ncheck equal s1 s2\n"
        "check contains s2 'Usamos 15'\n",
    )
    assert results[0] == (False, "missing from s4 (1): lamport94")
    assert results[1] == (False, "only in s5: 12; only in s6: 15")
    assert results[2] == (False, "s7=2 <= 1")
    assert results[3][0] is False and "position 2" in results[3][1]
    assert results[4] == (True, "s2 line 6 contains 'Usamos 15'")


def test_logical_combinators(root):
    results = _check(
        root,
        "s1 = file v1.tex\ns2 = cites s1\ns3 = labels s1\ns4 = refs s1\ns5 = only-a s4 s3\n"
        "check all(nonempty s2, s2 >= 3)\ncheck any(empty s5, s3 > s2)\n"
        "check not(empty s5)\ncheck all(nonempty s2, empty s5)\n",
    )
    assert [ok for ok, _ in results] == [True, False, True, False]
    assert "s5 has 1: sec:missing" in results[3][1]


# ---------------------------------------------------------------------------
# Plan validation: refused with a line number, never repaired
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "plan, message",
    [
        ("s1 = difff a b\ncheck empty s1\n", r"unknown operation 'difff'. Did you mean 'diff'\?"),
        ("s1 = cites s0\ncheck empty s1\n", r"'s0' is not a result defined above"),
        ("s1 = file v1.tex\ns2 = diff s1\ncheck empty s2\n", r"diff takes 2 argument"),
        ("s1 = file v1.tex\ns2 = cites s1\ns3 = diff s1 s2\ncheck empty s3\n", r"'s2' is items"),
        ("s1 = lines v1.tex 9-3\ncheck empty s1\n", r"ends before it starts"),
        ("s1 = file v1.tex\n", r"no `check` line"),
        ("s1 = file v1.tex\ns1 = file v2.tex\ncheck empty s1\n", r"already defined"),
        ("s1 = file v1.tex\ncheck emtpy s1\n", r"Did you mean 'empty'\?"),
        ("s1 = file v1.tex\ncheck all(empty s1\n", r"expected"),
        ("s1 = file v1.tex\ncheck contains s1 TODO\n", r"quoted text"),
        ("file = file v1.tex\ncheck empty file\n", r"reserved word"),
        ('s1 = section v1.tex "Método\ncheck empty s1\n', r"close every quote"),
        ("just some prose\n", r"expected `NAME = OPERATION ARGS`"),
        ("s1 = file v1.tex\ns2 = grep s1 '(unclosed'\ncheck empty s2\n", r"line 2: invalid regular expression"),
    ],
)
def test_invalid_plans_are_refused_with_a_diagnostic(plan, message):
    with pytest.raises(ops.PlanError, match=message):
        ops.parse_plan(plan)


def test_the_step_limit_is_enforced():
    plan = "".join(f"s{i} = file v1.tex\n" for i in range(13)) + "check empty s0\n"
    with pytest.raises(ops.PlanError, match="limit is 12"):
        ops.parse_plan(plan)


def test_a_step_that_cannot_run_names_its_line(root):
    with pytest.raises(ops.PlanError, match=r"line 2 \(s2 = file\): file not found: 'nope.tex'"):
        _values(root, "s1 = file v1.tex\ns2 = file nope.tex\ncheck empty s2\n")


# ---------------------------------------------------------------------------
# Command line: exit codes, output budget, paging
# ---------------------------------------------------------------------------

def test_run_exit_codes_distinguish_answers_from_errors(root):
    code, out = _run(root, "s1 = file v1.tex\ns2 = file v1.tex\ns3 = diff s1 s2\ncheck empty s3\n")
    assert code == 0 and "VERDICT: PASS" in out
    code, out = _run(root, "s1 = file v1.tex\ns2 = file v2.tex\ns3 = diff s1 s2\ncheck empty s3\n")
    assert code == 1 and "VERDICT: FAIL (1 of 1 checks failed)" in out
    assert "DETAIL:" in out and "diff_files('v1.tex', 'v2.tex', start_a=1, end_a=18" in out
    code, out = _run(root, "s1 = fiel v1.tex\ncheck empty s1\n")
    assert code == 2 and out.startswith("PLAN ERROR: line 1") and "at most 2" in out


def test_run_output_stays_under_the_tool_output_cut(root):
    """run_python_script keeps only the head and tail of stdout past 2000 chars."""
    plan = "".join(f"s{i} = file v1.tex\n" for i in range(12)) + "".join(
        f"check contains s{i} 'a string that is long enough to make every check line wide'\n"
        for i in range(12)
    )
    code, out = _run(root, plan)
    assert len(out) <= 1801
    assert "show" in out.splitlines()[-1]


def test_show_pages_a_result_with_its_original_line_numbers(root):
    plan = "s1 = file v1.tex\ns2 = lines v1.tex 5-7\ncheck nonempty s1\n"
    code, out = _run(root, plan, "show", "s2")
    assert code == 0
    assert out.splitlines()[1].startswith(r"L5: \section{Método}")
    code, out = _run(root, plan, "show", "s1", "--max-chars", "300")
    assert "next page:" in out
    offset = int(out.rsplit("--offset ", 1)[1].split("]")[0])
    code, second = _run(root, plan, "show", "s1", "--offset", str(offset))
    assert second.splitlines()[1].startswith(f"L{offset + 1}:")


def test_validate_runs_nothing(root):
    code, out = _run(root, "s1 = file does-not-exist.tex\ncheck empty s1\n", "validate")
    assert code == 0 and out.startswith("PLAN OK: 1 steps, 1 checks.")
