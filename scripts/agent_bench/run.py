#!/usr/bin/env python3
"""Internal agent benchmark: real tasks, a model, and a pass/fail check for each.

    python scripts/agent_bench/run.py --model ollama/glm-5.3:cloud
    python scripts/agent_bench/run.py --model <id> --tasks py_off_by_one,json_config
    python scripts/agent_bench/run.py --self-check

Each task under tasks/<id>/ holds:

    task.json   {"prompt": ..., "timeout": seconds, "requires": ["node", ...]}
    repo/       the files the agent starts from
    verify.py   `python verify.py <workdir>` exits 0 when the task is done
    solution/   files that, laid over repo/, complete the task (for --self-check)

A run copies repo/ to a fresh directory, registers it as a project in a
sessions database of its own (the user's sessions.db is never touched), sets
it to `auto` mode, sends the prompt through the command-line front-end
(`main.py -p`), and runs verify.py, which lives outside the copy so the agent
cannot read or change it. The model catalog and API keys come from the user's
normal GilzaBots home, so any registered model can be measured.

Besides pass/fail it reports what the turn did, read from that database: tool
calls and tool errors per tool. `--self-check` needs no model: it proves every
task is solvable and not already solved (verify fails on repo/, passes on
repo/ + solution/).

The agent runs in `auto` mode, with the shell, in the copy: run it only on a
machine where that is acceptable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TASKS_DIR = os.path.join(HERE, "tasks")
DEFAULT_TIMEOUT = 900


def load_tasks(selected: list[str] | None) -> list[dict]:
    tasks = []
    for task_id in sorted(os.listdir(TASKS_DIR)):
        folder = os.path.join(TASKS_DIR, task_id)
        if not os.path.isfile(os.path.join(folder, "task.json")):
            continue
        if selected and task_id not in selected:
            continue
        with open(os.path.join(folder, "task.json"), encoding="utf-8") as f:
            spec = json.load(f)
        tasks.append({"id": task_id, "folder": folder, **spec})
    if selected:
        missing = sorted(set(selected) - {t["id"] for t in tasks})
        if missing:
            sys.exit(f"Unknown task(s): {', '.join(missing)}")
    return tasks


def missing_requirements(task: dict) -> list[str]:
    return [tool for tool in task.get("requires", []) if shutil.which(tool) is None]


def verify(task: dict, workdir: str) -> tuple[bool, str]:
    run = subprocess.run(
        [sys.executable, os.path.join(task["folder"], "verify.py"), workdir],
        capture_output=True, text=True, timeout=300,
    )
    # verify.py states the reason in its first line (sys.exit), or prints "ok".
    message = (run.stderr if run.returncode else run.stdout).strip()
    return run.returncode == 0, message.splitlines()[0] if message else ""


def fresh_copy(task: dict, base: str, with_solution: bool = False) -> str:
    workdir = os.path.join(base, task["id"], "work")
    shutil.copytree(os.path.join(task["folder"], "repo"), workdir)
    solution = os.path.join(task["folder"], "solution")
    if with_solution and os.path.isdir(solution):
        shutil.copytree(solution, workdir, dirs_exist_ok=True)
    return workdir


def self_check(tasks: list[dict]) -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="gilzabots-bench-check-") as base:
        for task in tasks:
            missing = missing_requirements(task)
            if missing:
                print(f"SKIP  {task['id']}: needs {', '.join(missing)}")
                continue
            unsolved_ok, unsolved_msg = verify(task, fresh_copy(task, os.path.join(base, "unsolved")))
            solved_ok, solved_msg = verify(task, fresh_copy(task, os.path.join(base, "solved"), with_solution=True))
            if unsolved_ok:
                failures += 1
                print(f"FAIL  {task['id']}: verify passes before any change")
            elif not solved_ok:
                failures += 1
                print(f"FAIL  {task['id']}: verify rejects the reference solution: {solved_msg}")
            else:
                print(f"ok    {task['id']}: unsolved -> {unsolved_msg!r}")
    return 1 if failures else 0


_TOOL_CALL_RE = re.compile(r"^Decided to execute tool '([^']+)'")
_TOOL_ERROR_RE = re.compile(r"^Tool '([^']+)' returned an error")


def turn_metrics(db_path: str) -> dict:
    calls, errors = Counter(), Counter()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute("SELECT content FROM project_activity WHERE event = 'thought' AND agent = ''").fetchall()
        conn.close()
    except sqlite3.Error:
        rows = []
    for (content,) in rows:
        if m := _TOOL_CALL_RE.match(content or ""):
            calls[m.group(1)] += 1
        elif m := _TOOL_ERROR_RE.match(content or ""):
            errors[m.group(1)] += 1
    return {"tool_calls": dict(calls), "tool_errors": dict(errors)}


def run_task(task: dict, model: str, base: str, timeout_override: int | None) -> dict:
    sys.path.insert(0, ROOT)
    from gilzabots.project import ProjectStore

    workdir = fresh_copy(task, base)
    db_path = os.path.join(base, task["id"], "sessions.db")
    name = f"bench_{task['id']}"
    store = ProjectStore(db_path=db_path)
    store.create(name, "auto", model, project_name=task["id"], project_path=workdir)
    store.close_activity_connection()

    timeout = timeout_override or int(task.get("timeout", DEFAULT_TIMEOUT))
    log_path = os.path.join(base, task["id"], "agent.log")
    started = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            run = subprocess.run(
                [sys.executable, os.path.join(ROOT, "main.py"), "--project", name,
                 "--db", db_path, "-p", task["prompt"]],
                cwd=workdir, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            exit_code = run.returncode
        except subprocess.TimeoutExpired:
            exit_code = "timeout"
    seconds = round(time.monotonic() - started, 1)
    passed, message = verify(task, workdir)
    return {
        "task": task["id"], "passed": passed, "message": message, "exit_code": exit_code,
        "seconds": seconds, "workdir": workdir, "log": log_path, **turn_metrics(db_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="catalog id of the model to measure, e.g. ollama/glm-5.3:cloud")
    parser.add_argument("--tasks", help="comma-separated task ids (default: all)")
    parser.add_argument("--timeout", type=int, help="seconds per task, overriding task.json")
    parser.add_argument("--out", help="write the results as JSON to this file")
    parser.add_argument("--keep", action="store_true", help="keep the work directories and logs")
    parser.add_argument("--self-check", action="store_true", help="check the tasks themselves; needs no model")
    args = parser.parse_args()

    tasks = load_tasks([t.strip() for t in args.tasks.split(",")] if args.tasks else None)
    if args.self_check:
        return self_check(tasks)
    if not args.model:
        parser.error("--model is required (or pass --self-check)")

    base = tempfile.mkdtemp(prefix="gilzabots-bench-")
    results = []
    try:
        for task in tasks:
            missing = missing_requirements(task)
            if missing:
                print(f"SKIP  {task['id']}: needs {', '.join(missing)}", flush=True)
                continue
            print(f"...   {task['id']}", flush=True)
            result = run_task(task, args.model, base, args.timeout)
            results.append(result)
            errors = sum(result["tool_errors"].values())
            calls = sum(result["tool_calls"].values())
            status = "PASS" if result["passed"] else "FAIL"
            print(f"{status}  {task['id']}: {result['seconds']}s, {calls} tool calls, {errors} tool errors"
                  + ("" if result["passed"] else f" — {result['message']}"), flush=True)
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
            for result in results:
                result.pop("workdir", None)
                result.pop("log", None)

    passed = sum(r["passed"] for r in results)
    print(f"\n{args.model}: {passed}/{len(results)} passed")
    if args.keep:
        print(f"Work directories and logs: {base}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"model": args.model, "results": results}, f, indent=2, ensure_ascii=False)
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
