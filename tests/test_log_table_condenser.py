"""Unit tests for the log-table-condenser skill and its streaming log processor."""

import csv
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest

from gilzabots.assetstore import list_assets
from gilzabots.skills import discover_skills, parse_skill_md

_script_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "log-table-condenser",
    "scripts",
    "log_processor.py",
)
_spec = importlib.util.spec_from_file_location("log_processor", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

condense_logs = _mod.condense_logs
sample_schema = _mod.sample_schema
iter_log_records = _mod.iter_log_records


def _create_sample_jsonl(path: str, count: int = 20) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(count):
            record = {
                "timestamp": f"2026-08-16T08:{i:02d}:00Z",
                "level": "ERROR" if i % 3 == 0 else "INFO",
                "service": "auth-service" if i % 2 == 0 else "payment-service",
                "status_code": 500 if i % 3 == 0 else 200,
                "user_id": f"usr_{1000 + i}",
                "message": f"Operation log message for event {i}",
                "payload": {"headers": {"host": "api.local"}, "attempt": i},
            }
            f.write(json.dumps(record) + "\n")


def test_sample_schema_jsonl(tmp_path):
    log_file = str(tmp_path / "test_logs.jsonl")
    _create_sample_jsonl(log_file, count=15)

    schema = sample_schema(log_file, sample_size=10)
    assert schema["file"] == "test_logs.jsonl"
    assert schema["sample_lines"] == 10

    field_names = {f["name"] for f in schema["fields"]}
    assert "timestamp" in field_names
    assert "level" in field_names
    assert "service" in field_names
    assert "status_code" in field_names
    assert "payload" in field_names


def test_sample_schema_csv_and_tsv(tmp_path):
    csv_file = str(tmp_path / "data.csv")
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "role", "salary"])
        writer.writerow(["1", "Alice", "Engineer", "90000"])
        writer.writerow(["2", "Bob", "Designer", "80000"])

    schema_csv = sample_schema(csv_file, sample_size=5)
    csv_fields = [f["name"] for f in schema_csv["fields"]]
    assert csv_fields == ["id", "name", "role", "salary"]

    tsv_file = str(tmp_path / "data.tsv")
    with open(tsv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["host", "latency_ms", "status"])
        writer.writerow(["node-1", "12.4", "OK"])

    schema_tsv = sample_schema(tsv_file, sample_size=5)
    tsv_fields = [f["name"] for f in schema_tsv["fields"]]
    assert tsv_fields == ["host", "latency_ms", "status"]


def test_condense_to_csv(tmp_path):
    log_file = str(tmp_path / "input.jsonl")
    out_csv = str(tmp_path / "output.csv")
    _create_sample_jsonl(log_file, count=25)

    fields = ["timestamp", "level", "service", "status_code"]
    res = condense_logs(
        input_path=log_file,
        output_path=out_csv,
        fields=fields,
        output_format="csv",
    )

    assert res["lines_read"] == 25
    assert res["lines_written"] == 25
    assert res["reduction_percentage"] > 0
    assert os.path.exists(out_csv)

    with open(out_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
        assert rows[0] == fields
        assert len(rows) == 26  # header + 25 rows
        assert rows[1][1] in ("ERROR", "INFO")


def test_condense_to_tsv(tmp_path):
    log_file = str(tmp_path / "input.jsonl")
    out_tsv = str(tmp_path / "output.tsv")
    _create_sample_jsonl(log_file, count=10)

    res = condense_logs(
        input_path=log_file,
        output_path=out_tsv,
        fields=["timestamp", "service"],
        output_format="tsv",
    )

    assert res["lines_written"] == 10
    with open(out_tsv, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        rows = list(reader)
        assert rows[0] == ["timestamp", "service"]
        assert len(rows) == 11


def test_condense_to_sqlite(tmp_path):
    log_file = str(tmp_path / "input.jsonl")
    out_db = str(tmp_path / "logs.sqlite")
    _create_sample_jsonl(log_file, count=30)

    fields = ["timestamp", "level", "service", "status_code"]
    res = condense_logs(
        input_path=log_file,
        output_path=out_db,
        fields=fields,
        output_format="sqlite",
        sqlite_table="server_events",
    )

    assert res["lines_written"] == 30
    assert os.path.exists(out_db)

    conn = sqlite3.connect(out_db)
    cur = conn.cursor()
    cur.execute("SELECT level, service, status_code FROM server_events")
    rows = cur.fetchall()
    assert len(rows) == 30
    assert rows[0][0] in ("ERROR", "INFO")
    conn.close()


def test_condense_to_latex(tmp_path):
    log_file = str(tmp_path / "input.jsonl")
    out_tex = str(tmp_path / "table.tex")
    _create_sample_jsonl(log_file, count=5)

    fields = ["service", "level", "message"]
    res = condense_logs(
        input_path=log_file,
        output_path=out_tex,
        fields=fields,
        output_format="latex",
    )

    assert res["lines_written"] == 5
    with open(out_tex, "r", encoding="utf-8") as f:
        tex_content = f.read()

    assert "\\begin{tabular}{lll}" in tex_content
    assert "\\toprule" in tex_content
    assert "\\bottomrule" in tex_content
    assert "\\end{tabular}" in tex_content
    # Check escaping: underscores like auth_service or auth-service
    assert "\\textbf{service}" in tex_content


def test_condense_to_markdown(tmp_path):
    log_file = str(tmp_path / "input.jsonl")
    out_md = str(tmp_path / "table.md")
    _create_sample_jsonl(log_file, count=5)

    res = condense_logs(
        input_path=log_file,
        output_path=out_md,
        fields=["timestamp", "level"],
        output_format="markdown",
    )

    assert res["lines_written"] == 5
    with open(out_md, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    assert lines[0] == "| timestamp | level |"
    assert lines[1] == "| --- | --- |"
    assert len(lines) == 7


def test_condense_with_deduplication(tmp_path):
    log_file = str(tmp_path / "repetitive.jsonl")
    out_csv = str(tmp_path / "condensed.csv")

    with open(log_file, "w", encoding="utf-8") as f:
        # 5 identical errors
        for _ in range(5):
            f.write(json.dumps({"service": "api", "level": "ERROR", "msg": "timeout"}) + "\n")
        # 3 identical infos
        for _ in range(3):
            f.write(json.dumps({"service": "api", "level": "INFO", "msg": "healthy"}) + "\n")

    res = condense_logs(
        input_path=log_file,
        output_path=out_csv,
        fields=["service", "level", "msg"],
        output_format="csv",
        dedup=True,
    )

    assert res["lines_read"] == 8
    assert res["lines_written"] == 2  # collapsed to 2 rows

    with open(out_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
        assert rows[0] == ["service", "level", "msg", "occurrence_count"]
        assert rows[1] == ["api", "ERROR", "timeout", "5"]
        assert rows[2] == ["api", "INFO", "healthy", "3"]


def test_condense_with_max_len_truncation(tmp_path):
    log_file = str(tmp_path / "verbose.jsonl")
    out_csv = str(tmp_path / "truncated.csv")

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": 1, "stack_trace": "Traceback (most recent call last):\n  File 'a.py', line 123 in foo\nValueError: bad"}) + "\n")

    condense_logs(
        input_path=log_file,
        output_path=out_csv,
        fields=["id", "stack_trace"],
        output_format="csv",
        max_len=20,
    )

    with open(out_csv, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
        val = rows[1][1]
        assert len(val) <= 20
        assert val.endswith("...")


def test_streaming_large_log_performance(tmp_path):
    log_file = str(tmp_path / "large_log.jsonl")
    out_csv = str(tmp_path / "compact.csv")

    # Generate 5,000 log records
    _create_sample_jsonl(log_file, count=5000)
    orig_size = os.path.getsize(log_file)

    res = condense_logs(
        input_path=log_file,
        output_path=out_csv,
        fields=["timestamp", "level", "status_code"],
        output_format="csv",
    )

    assert res["lines_read"] == 5000
    assert res["lines_written"] == 5000
    assert res["condensed_size_bytes"] < orig_size
    assert res["reduction_percentage"] > 50.0
    assert res["elapsed_seconds"] < 5.0  # streaming is very fast


def test_cli_execution(tmp_path):
    log_file = str(tmp_path / "cli_test.jsonl")
    out_csv = str(tmp_path / "cli_out.csv")
    _create_sample_jsonl(log_file, count=10)

    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
        "log-table-condenser",
        "scripts",
        "log_processor.py",
    )

    # 1. Test CLI sample command
    p1 = subprocess.run(
        [sys.executable, script_path, "sample", log_file, "--lines", "5"],
        capture_output=True,
        text=True,
    )
    assert p1.returncode == 0
    assert "Discovered Fields:" in p1.stdout

    # 2. Test CLI condense command
    p2 = subprocess.run(
        [
            sys.executable,
            script_path,
            "condense",
            log_file,
            "--fields",
            "timestamp,level,service",
            "--output",
            out_csv,
            "--format",
            "csv",
        ],
        capture_output=True,
        text=True,
    )
    assert p2.returncode == 0
    assert "CONDENSATION COMPLETE" in p2.stdout
    assert os.path.exists(out_csv)


def test_skill_manifest_and_store_asset():
    skills_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
        "log-table-condenser",
    )
    meta = parse_skill_md(skills_dir)
    assert meta is not None
    assert meta["name"] == "log-table-condenser"
    assert "condense" in meta["description"].lower()
    assert meta["model"] == "worker"

    # Verify discovery
    discovered = discover_skills("")
    assert any(s["name"] == "log-table-condenser" for s in discovered)

    # Verify AssetStore catalog
    assets = list_assets("skill")
    asset_match = next((a for a in assets if a.get("id") == "log-table-condenser"), None)
    assert asset_match is not None
    assert asset_match["name"] == "log-table-condenser"
    assert asset_match["_zip"].exists()


def test_compare_logs(tmp_path):
    log1 = str(tmp_path / "exp1.jsonl")
    log2 = str(tmp_path / "exp2.jsonl")

    # log1: paired seeds (1, 1, 2, 2)
    with open(log1, "w", encoding="utf-8") as f:
        f.write(json.dumps({"seed": 1, "accuracy": 0.85, "loss": 0.2}) + "\n")
        f.write(json.dumps({"seed": 1, "accuracy": 0.86, "loss": 0.19}) + "\n")
        f.write(json.dumps({"seed": 2, "accuracy": 0.90, "loss": 0.15}) + "\n")
        f.write(json.dumps({"seed": 2, "accuracy": 0.91, "loss": 0.14}) + "\n")

    # log2: unpaired seeds and missing loss field in some rows
    with open(log2, "w", encoding="utf-8") as f:
        f.write(json.dumps({"seed": 1, "accuracy": 0.80}) + "\n")
        f.write(json.dumps({"seed": 3, "accuracy": 0.82, "loss": 0.25}) + "\n")

    comp_fn = _mod.compare_logs
    res = comp_fn([log1, log2])

    assert res["files_compared"] == 2
    assert "accuracy" in res["all_fields"]
    assert "loss" in res["all_fields"]
    assert "seed" in res["all_fields"]

    det1 = next(d for d in res["details"] if d["file"] == "exp1.jsonl")
    det2 = next(d for d in res["details"] if d["file"] == "exp2.jsonl")

    assert det1["total_rows"] == 4
    assert det1["seed_stats"]["is_seed_paired"] is True
    assert det1["avg_field_presence_pct"] == 100.0

    assert det2["total_rows"] == 2
    assert det2["seed_stats"]["is_seed_paired"] is False

    assert res["most_consistent_file"] == "exp1.jsonl"


def test_cli_compare(tmp_path):
    log1 = str(tmp_path / "run_a.jsonl")
    log2 = str(tmp_path / "run_b.jsonl")
    _create_sample_jsonl(log1, count=5)
    _create_sample_jsonl(log2, count=8)

    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
        "log-table-condenser",
        "scripts",
        "log_processor.py",
    )

    p = subprocess.run(
        [sys.executable, script_path, "compare", log1, log2],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 0
    assert "LOG COMPARISON SUMMARY" in p.stdout
    assert "Files Compared: 2" in p.stdout
