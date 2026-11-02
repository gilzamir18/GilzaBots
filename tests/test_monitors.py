"""Monitor agents: specification, scheduling, wake-ups, notifications, services."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from opalatex.monitors import agent_tools, api, daemon as daemon_mod, engine as engine_mod
from opalatex.monitors import notify, service, spec, store as st, wake_runner
from opalatex.monitors.checks import CheckError
from opalatex.monitors.spec import MonitorSpecError, normalize_spec


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _spec(**overrides) -> dict:
    raw = {
        "name": "price",
        "check": {"type": "http", "url": "https://example.invalid/price"},
        "condition": {"type": "gt", "value": 5},
        "action": {"type": "notify", "message": "{name} is {value}"},
        "interval_seconds": 60,
        "project_path": os.getcwd(),
    }
    raw.update(overrides)
    return normalize_spec(raw)


def _wake_spec(**overrides) -> dict:
    raw = {
        "name": "build",
        "check": {"type": "timer"},
        "action": {"type": "resume_chat", "prompt": "check the build log"},
        "interval_seconds": 120,
        "project": "proj",
        "project_path": os.getcwd(),
        "chat_id": "chat-1",
    }
    raw.update(overrides)
    return normalize_spec(raw)


class FakeEngine(engine_mod.MonitorEngine):
    """An engine whose observations come from a list instead of a real check."""

    def __init__(self, store, values, clock):
        super().__init__(store, clock=clock)
        self.values = list(values)

    async def observe(self, monitor):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mstore(clock):
    return st.MonitorStore(clock=clock)


# ── Specification ───────────────────────────────────────────────────────────

class TestSpec:
    def test_unknown_check_type_is_refused_with_choices(self):
        with pytest.raises(MonitorSpecError, match="Unknown check type 'ftp'.*timer, http"):
            _spec(check={"type": "ftp"})

    def test_condition_must_fit_the_check(self):
        with pytest.raises(MonitorSpecError, match="does not apply to a 'file' check"):
            _spec(check={"type": "file", "path": "x"}, condition={"type": "gt", "value": 1})

    def test_interval_is_required_and_bounded(self):
        with pytest.raises(MonitorSpecError, match="interval_seconds"):
            _spec(interval_seconds=None)
        with pytest.raises(MonitorSpecError, match="at least 10"):
            _spec(interval_seconds=1)

    def test_llm_checks_have_a_higher_minimum_interval(self):
        with pytest.raises(MonitorSpecError, match="at least 300"):
            _spec(check={"type": "llm", "question": "q"}, condition={}, interval_seconds=60)
        assert _spec(check={"type": "llm", "question": "q"}, condition={}, interval_seconds=300)["condition"] == {"type": "llm"}

    def test_waking_a_chat_needs_project_and_chat(self):
        with pytest.raises(MonitorSpecError, match="no chat was given"):
            _wake_spec(chat_id="")
        with pytest.raises(MonitorSpecError, match="needs the project"):
            _wake_spec(project="")

    def test_a_new_chat_wakeup_needs_only_the_project(self):
        normalized = _wake_spec(chat_id="", action={"type": "resume_chat", "prompt": "p", "chat": "new"})
        assert normalized["action"]["chat"] == "new"
        assert _wake_spec()["action"]["chat"] == "origin"
        with pytest.raises(MonitorSpecError, match="'chat' must be one of origin, new"):
            _wake_spec(action={"type": "resume_chat", "prompt": "p", "chat": "elsewhere"})

    def test_timer_defaults_to_always_and_wakeups_do_not_repeat(self):
        normalized = _wake_spec()
        assert normalized["condition"] == {"type": "always"}
        assert normalized["repeat"] is False
        assert normalized["owner"] == "agent"
        assert _spec()["repeat"] is True
        assert _spec()["owner"] == "user"

    def test_repeat_follows_the_choice_and_defaults_by_kind(self):
        new_chat = {"type": "resume_chat", "prompt": "p", "chat": "new"}
        # Unset: a wake-up of this chat is a task wait; a new chat each time recurs.
        assert _wake_spec()["repeat"] is False
        assert _wake_spec(action=new_chat)["repeat"] is True
        assert _wake_spec(action={**new_chat, "type": "notify_and_resume"})["repeat"] is True
        # A choice always holds, whichever the kind.
        assert _wake_spec(action=new_chat, repeat=False)["repeat"] is False
        assert _wake_spec(repeat=True)["repeat"] is True
        assert _spec(repeat=False)["repeat"] is False

    def test_invalid_regex_and_json_path_are_refused(self):
        with pytest.raises(MonitorSpecError, match="not a valid regular expression"):
            _spec(check={"type": "http", "url": "https://x", "regex": "("}, condition={"type": "changed"})
        with pytest.raises(MonitorSpecError, match="json_path"):
            _spec(check={"type": "http", "url": "https://x", "json_path": "a..[x"}, condition={"type": "changed"})

    def test_http_url_must_be_http(self):
        with pytest.raises(MonitorSpecError, match="http:// or https://"):
            _spec(check={"type": "http", "url": "file:///etc/passwd"}, condition={"type": "changed"})

    def test_delta_threshold_must_be_positive(self):
        with pytest.raises(MonitorSpecError, match="greater than zero"):
            _spec(condition={"type": "delta", "threshold": 0})

    def test_command_check_needs_a_folder(self):
        with pytest.raises(MonitorSpecError, match="project folder"):
            _spec(check={"type": "command", "command": "true"}, project_path="")

    def test_json_path_extraction(self):
        doc = {"USDBRL": {"bid": "5.42"}, "items": [{"p": 1}, {"p": 2}]}
        assert spec.extract_json_path(doc, "USDBRL.bid") == "5.42"
        assert spec.extract_json_path(doc, "$.items[1].p") == 2
        with pytest.raises(LookupError, match="no key 'ask'"):
            spec.extract_json_path(doc, "USDBRL.ask")

    @pytest.mark.parametrize("raw,expected", [("5,42", 5.42), (" 7.5 ", 7.5), (3, 3.0), ("1.234,56", None), (True, None), ("nan", None)])
    def test_to_number(self, raw, expected):
        assert spec.to_number(raw) == expected

    def test_template_does_not_reach_attributes(self):
        rendered = spec.render_template("{name} {value.__class__} {delta} {unknown}", {"name": "n", "value": 1.5, "delta": 0.1})
        assert rendered == "n {value.__class__} +0.1 {unknown}"


class TestEvaluate:
    check = {"type": "http"}

    def test_delta_sets_a_baseline_then_fires_and_moves_the_reference(self):
        condition = {"type": "delta", "threshold": 0.1, "direction": "any"}
        first = spec.evaluate(self.check, condition, "5.40")
        assert (first.fire, first.reference) == (False, 5.40)
        small = spec.evaluate(self.check, condition, "5.45", reference=5.40)
        assert small.fire is False and small.reference == 5.40
        moved = spec.evaluate(self.check, condition, "5.51", reference=5.40)
        assert moved.fire is True and moved.reference == 5.51
        assert moved.details["delta"] == pytest.approx(0.11)

    def test_delta_direction(self):
        condition = {"type": "delta", "threshold": 0.1, "direction": "up"}
        assert spec.evaluate(self.check, condition, 5.2, reference=5.4).fire is False
        assert spec.evaluate(self.check, condition, 5.6, reference=5.4).fire is True

    def test_level_conditions_fire_on_the_rising_edge_only(self):
        condition = {"type": "gt", "value": 5}
        assert spec.evaluate(self.check, condition, 6, previous_met=None).fire is True
        assert spec.evaluate(self.check, condition, 7, previous_met=True).fire is False
        assert spec.evaluate(self.check, condition, 4, previous_met=True).met is False
        assert spec.evaluate(self.check, condition, 6, previous_met=False).fire is True

    def test_numeric_condition_on_text_is_a_condition_error(self):
        with pytest.raises(spec.ConditionError, match="not a number"):
            spec.evaluate(self.check, {"type": "gt", "value": 1}, "n/a")

    def test_changed_needs_a_previous_value(self):
        condition = {"type": "changed"}
        assert spec.evaluate(self.check, condition, "a").fire is False
        assert spec.evaluate(self.check, condition, "a", previous="a", has_previous=True).fire is False
        assert spec.evaluate(self.check, condition, "b", previous="a", has_previous=True).fire is True

    def test_command_exit_code_and_output(self):
        check = {"type": "command"}
        value = {"exit_code": 0, "output": "BUILD SUCCESSFUL"}
        assert spec.evaluate(check, {"type": "exit_code", "value": 0}, value).met is True
        assert spec.evaluate(check, {"type": "contains", "value": "SUCCESS"}, value).met is True

    def test_file_existence(self):
        check = {"type": "file", "watch": "mtime"}
        assert spec.evaluate(check, {"type": "exists"}, {"exists": True}).met is True
        assert spec.evaluate(check, {"type": "missing"}, {"exists": True}).met is False


# ── Store ───────────────────────────────────────────────────────────────────

class TestStore:
    def test_claim_is_exclusive_and_release_returns_it(self, mstore):
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "prompt", "display")
        assert mstore.claim_wake(wake_id, "gui", os.getpid()) is True
        assert mstore.claim_wake(wake_id, "daemon", os.getpid()) is False
        assert mstore.release_wake(wake_id, "daemon") is False
        assert mstore.release_wake(wake_id, "gui") is True
        assert mstore.get_wake(wake_id)["status"] == st.WAKE_PENDING

    def test_lease_is_reentrant_refuses_live_holders_and_takes_over_dead_ones(self, mstore):
        assert mstore.acquire_lease("p", "c", os.getpid(), "turn") is None
        assert mstore.acquire_lease("p", "c", os.getpid(), "turn") is None
        live = os.getppid()
        assert mstore.acquire_lease("p", "c2", live, "monitor") is None
        holder = mstore.acquire_lease("p", "c2", os.getpid(), "turn")
        assert holder is not None and holder["pid"] == live
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        assert mstore.acquire_lease("p", "c3", dead.pid, "monitor") is None
        assert mstore.acquire_lease("p", "c3", os.getpid(), "turn") is None

    def test_open_requests_are_consumed(self, mstore):
        mstore.add_open_request("p", "/p", "c1")
        mstore.add_open_request("p", "/p", "c2")
        assert mstore.take_open_request()["chat_id"] == "c2"
        assert mstore.take_open_request() is None

    def test_a_database_from_an_earlier_version_gains_the_new_chat_column(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "old.db")
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE wake_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id TEXT NOT NULL,"
                         " monitor_name TEXT NOT NULL DEFAULT '', project TEXT NOT NULL, project_path TEXT NOT NULL DEFAULT '',"
                         " chat_id TEXT NOT NULL, sessions_db TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL,"
                         " display TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, status TEXT NOT NULL,"
                         " claimed_by TEXT NOT NULL DEFAULT '', claimed_pid INTEGER, claimed_at REAL, finished_at REAL,"
                         " error TEXT NOT NULL DEFAULT '')")
        upgraded = st.MonitorStore(db_path=path)
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(wake_requests)")}
        assert {"new_chat", "result", "seen"} <= columns
        monitor = upgraded.create_monitor(_wake_spec())
        wake_id = upgraded.enqueue_wake(monitor, "p", "d", new_chat=True)
        assert upgraded.get_wake(wake_id)["new_chat"] == 1
        assert upgraded.get_wake(upgraded.enqueue_wake(monitor, "p", "d"))["new_chat"] == 0

    def test_events_are_bounded(self, mstore):
        monitor = mstore.create_monitor(_spec())
        for index in range(st.MAX_EVENTS_PER_MONITOR + 20):
            mstore.add_event(monitor["id"], "check", str(index))
        events = mstore.list_events(monitor["id"], limit=1000)
        assert len(events) == st.MAX_EVENTS_PER_MONITOR
        assert events[0]["message"] == str(st.MAX_EVENTS_PER_MONITOR + 19)


# ── Engine ──────────────────────────────────────────────────────────────────

class TestEngine:
    def _due(self, mstore, clock, spec_dict):
        monitor = mstore.create_monitor(spec_dict)
        clock.advance(spec_dict["interval_seconds"])
        return monitor

    def test_a_watchdog_notifies_on_each_rising_edge_and_keeps_going(self, mstore, clock):
        monitor = self._due(mstore, clock, _spec())
        eng = FakeEngine(mstore, ["6", "7", "4", "8"], clock)
        for _ in range(4):
            asyncio.run(eng.tick())
            clock.advance(60)
        bodies = [n["body"] for n in mstore.undelivered_notifications()]
        assert bodies == ["price is 6", "price is 8"]
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_ACTIVE

    def test_once_completes_after_the_first_firing(self, mstore, clock):
        monitor = self._due(mstore, clock, _spec(repeat=False))
        asyncio.run(FakeEngine(mstore, ["6"], clock).tick())
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_COMPLETED

    def test_a_timer_wakeup_wakes_its_chat_once(self, mstore, clock):
        monitor = self._due(mstore, clock, _wake_spec())
        asyncio.run(FakeEngine(mstore, [None], clock).tick())
        wakes = mstore.list_wakes()
        assert len(wakes) == 1
        assert wakes[0]["chat_id"] == "chat-1"
        assert "[Monitor event: condition_met]" in wakes[0]["prompt"]
        assert "check the build log" in wakes[0]["prompt"]
        assert "build" in wakes[0]["display"]
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_COMPLETED

    def test_a_new_chat_is_opened_for_each_wakeup(self, mstore, clock, tmp_path):
        from opalatex.project import ProjectStore

        sessions = ProjectStore(db_path=str(tmp_path / "sessions.db"))
        sessions.create("proj", "auto", "", project_path=str(tmp_path / "proj"))
        sessions.create_chat("proj", "chat-1", "Origin")
        watch = _wake_spec(check={"type": "http", "url": "https://x"}, condition={"type": "gt", "value": 5},
                           action={"type": "notify_and_resume", "prompt": "write the report", "chat": "new"},
                           repeat=True, sessions_db=str(tmp_path / "sessions.db"))
        monitor = self._due(mstore, clock, watch)
        eng = FakeEngine(mstore, ["6", "4", "7"], clock)
        eng.chat_creator = engine_mod.create_wake_chat
        for _ in range(3):
            asyncio.run(eng.tick())
            clock.advance(120)
        wakes = mstore.list_wakes()
        assert len(wakes) == 2
        chat_ids = {wake["chat_id"] for wake in wakes}
        assert "chat-1" not in chat_ids and len(chat_ids) == 2
        stored_chats = {c["id"]: c["name"] for c in sessions.load("proj").chats}
        assert chat_ids <= set(stored_chats)
        assert all(stored_chats[c].startswith("⏰ build") for c in chat_ids)
        assert "This is a new chat the monitor opened" in wakes[0]["prompt"]
        assert all(wake["new_chat"] == 1 for wake in wakes)
        assert "write the report" in wakes[0]["prompt"]
        assert sessions.load("proj", chat_id="chat-1").history == []
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_ACTIVE

    def test_a_new_chat_that_cannot_be_opened_is_reported_not_redirected(self, mstore, clock):
        monitor = self._due(mstore, clock, _wake_spec(action={"type": "resume_chat", "prompt": "p", "chat": "new"}))
        eng = FakeEngine(mstore, [None], clock)

        def refuse(_monitor):
            raise RuntimeError("the project 'proj' no longer exists")

        eng.chat_creator = refuse
        asyncio.run(eng.tick())
        assert mstore.list_wakes() == []
        note = mstore.undelivered_notifications()[0]
        assert note["urgency"] == "critical" and "no longer exists" in note["body"]
        assert any(e["kind"] == "error" and "new chat" in e["message"] for e in mstore.list_events(monitor["id"]))

    def test_running_out_of_checks_wakes_the_chat_with_the_reason(self, mstore, clock):
        wait = _wake_spec(check={"type": "command", "command": "x"}, condition={"type": "exit_code"}, max_checks=2)
        monitor = self._due(mstore, clock, wait)
        eng = FakeEngine(mstore, [{"exit_code": 1, "output": ""}, {"exit_code": 1, "output": ""}], clock)
        asyncio.run(eng.tick())
        assert mstore.list_wakes() == []
        clock.advance(120)
        asyncio.run(eng.tick())
        wakes = mstore.list_wakes()
        assert len(wakes) == 1 and "[Monitor event: exhausted]" in wakes[0]["prompt"]
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_EXPIRED

    def test_repeated_errors_stop_the_monitor_and_tell_the_user(self, mstore, clock):
        monitor = self._due(mstore, clock, _spec())
        eng = FakeEngine(mstore, [CheckError("unreachable")] * engine_mod.MAX_CONSECUTIVE_ERRORS, clock)
        for _ in range(engine_mod.MAX_CONSECUTIVE_ERRORS):
            asyncio.run(eng.tick())
            clock.advance(60)
        stored = mstore.get_monitor(monitor["id"])
        assert stored["status"] == st.STATUS_ERROR
        assert stored["last_error"] == "unreachable"
        assert [n["urgency"] for n in mstore.undelivered_notifications()] == ["critical"]

    def test_a_success_resets_the_error_count(self, mstore, clock):
        monitor = self._due(mstore, clock, _spec())
        eng = FakeEngine(mstore, [CheckError("x"), "1", CheckError("x")], clock)
        for _ in range(3):
            asyncio.run(eng.tick())
            clock.advance(60)
        assert mstore.get_monitor(monitor["id"])["consecutive_errors"] == 1

    def test_ttl_expires_without_checking(self, mstore, clock):
        monitor = self._due(mstore, clock, _wake_spec(ttl_seconds=60, check={"type": "command", "command": "x"},
                                                     condition={"type": "exit_code"}))
        eng = FakeEngine(mstore, [], clock)
        asyncio.run(eng.tick())
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_EXPIRED
        assert "[Monitor event: expired]" in mstore.list_wakes()[0]["prompt"]

    def test_paused_monitors_are_not_checked(self, mstore, clock):
        monitor = self._due(mstore, clock, _spec())
        mstore.set_status(monitor["id"], st.STATUS_PAUSED)
        assert asyncio.run(FakeEngine(mstore, [], clock).tick()) == 0

    def test_dry_run_changes_nothing(self, mstore, clock):
        monitor = mstore.create_monitor(_spec())
        result = asyncio.run(FakeEngine(mstore, ["9"], clock).dry_run(monitor))
        assert result["ok"] and result["would_fire"] is True
        stored = mstore.get_monitor(monitor["id"])
        assert stored["check_count"] == 0 and mstore.undelivered_notifications() == []

    def test_real_command_and_http_checks(self, mstore, clock):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"USDBRL": {"bid": "5,42"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/json"
            http = mstore.create_monitor(_spec(check={"type": "http", "url": url, "json_path": "USDBRL.bid"},
                                               condition={"type": "gt", "value": 5}))
            bad = mstore.create_monitor(_spec(check={"type": "http", "url": url, "json_path": "USDBRL.ask"},
                                              condition={"type": "gt", "value": 5}))
            eng = engine_mod.MonitorEngine(mstore, clock=clock)
            assert asyncio.run(eng.dry_run(http))["would_fire"] is True
            assert "no key 'ask'" in asyncio.run(eng.dry_run(bad))["error"]
        finally:
            server.shutdown()
        command = mstore.create_monitor(_spec(check={"type": "command", "command": "echo hello; exit 3"},
                                              condition={"type": "exit_code", "value": 3}))
        result = asyncio.run(engine_mod.MonitorEngine(mstore, clock=clock).dry_run(command))
        assert result["met"] is True and "hello" in result["value"]


# ── Agent tools ─────────────────────────────────────────────────────────────

@pytest.fixture
def session(monkeypatch, tmp_path):
    from opalatex import tools

    project = types.SimpleNamespace(name="proj", project_path=str(tmp_path), current_chat_id="chat-1",
                                    mode="auto", results={})
    project_store = types.SimpleNamespace(db_path=str(tmp_path / "sessions.db"))
    monkeypatch.setattr(tools, "_PROJECT_SESSION", project)
    monkeypatch.setattr(tools, "_PROJECT_STORE", project_store)
    return project


def _tools():
    return {tool.name: tool._func for tool in agent_tools.build_monitor_tools()}


class TestAgentTools:
    def test_register_a_wakeup_in_this_chat(self, session, _isolated_monitors):
        result = asyncio.run(_tools()["register_monitor"](
            name="wait build", interval_seconds=120, resume_prompt="continue the deploy",
        ))
        assert result.startswith("Registered monitor mon_")
        assert "End your turn now" in result
        monitor = st.MonitorStore().list_monitors()[0]
        assert (monitor["project"], monitor["chat_id"]) == ("proj", "chat-1")
        assert monitor["sessions_db"].endswith("sessions.db")
        assert _isolated_monitors and _isolated_monitors[-1][-2:] == ["monitors", "daemon"]

    def test_a_check_that_fails_first_is_not_registered(self, session):
        result = asyncio.run(_tools()["register_monitor"](
            name="w", interval_seconds=60, check_type="file", path="missing.txt", watch="content",
            condition="changed", action="notify",
        ))
        # A missing file is an observation, not an error: it registers.
        assert result.startswith("Registered")
        result = asyncio.run(_tools()["register_monitor"](
            name="w", interval_seconds=60, check_type="command", command="exit 0",
            condition="gt", value="3", action="notify",
        ))
        assert result.startswith("register_monitor failed: The first check failed")
        assert "not a number" in result
        assert len(st.MonitorStore().list_monitors()) == 1

    def test_the_tool_never_overrides_the_choice_to_repeat(self, session):
        register = _tools()["register_monitor"]
        recurring = asyncio.run(register(name="news", interval_seconds=3600, resume_prompt="list the news",
                                         wake_in="new_chat", action="notify_and_resume"))
        assert "It fires every time the condition holds." in recurring
        single = asyncio.run(register(name="news once", interval_seconds=3600, resume_prompt="p",
                                      wake_in="new_chat", once=True))
        assert "It fires once, then ends." in single
        wait = asyncio.run(register(name="build", interval_seconds=120, resume_prompt="continue"))
        assert "It fires once, then ends." in wait
        again = asyncio.run(register(name="poll", interval_seconds=120, resume_prompt="continue", once=False))
        assert "It fires every time" in again
        by_name = {m["name"]: m["repeat"] for m in st.MonitorStore().list_monitors()}
        assert by_name == {"news": True, "news once": False, "build": False, "poll": True}

    def test_a_recurring_new_chat_monitor_keeps_firing(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec(action={"type": "resume_chat", "prompt": "p", "chat": "new"}))
        clock.advance(120)
        eng = FakeEngine(mstore, [None, None, None], clock)
        eng.chat_creator = lambda _monitor: f"chat-{len(mstore.list_wakes())}"
        for _ in range(3):
            asyncio.run(eng.tick())
            clock.advance(120)
        assert len(mstore.list_wakes()) == 3
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_ACTIVE

    def test_wake_in_a_new_chat(self, session):
        tools_ = _tools()
        result = asyncio.run(tools_["register_monitor"](
            name="report", interval_seconds=120, resume_prompt="write the report", wake_in="new_chat",
        ))
        assert "opens a new chat" in result and "End your turn now" not in result
        monitor = st.MonitorStore().list_monitors()[0]
        assert monitor["action"]["chat"] == "new"
        assert "(wakes a new chat)" in agent_tools.describe(monitor)
        bad = asyncio.run(tools_["register_monitor"](name="x", interval_seconds=120, resume_prompt="p", wake_in="other"))
        assert "'wake_in' must be 'same_chat' or 'new_chat'" in bad

    def test_spec_errors_are_reported_not_guessed(self, session):
        result = asyncio.run(_tools()["register_monitor"](
            name="w", interval_seconds=60, check_type="http", url="https://example.invalid", action="notify",
        ))
        assert "condition.type' is required" in result

    def test_list_pause_resume_cancel(self, session):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec(project="proj"))
        tools_ = _tools()

        def call(name, *args, **kwargs):
            return asyncio.run(tools_[name](*args, **kwargs))

        assert monitor["id"] in call("list_monitors")
        assert "Paused" in call("pause_monitor", monitor["id"])
        assert "No monitors with status 'active'" in call("list_monitors")
        assert "Resumed" in call("pause_monitor", monitor["id"], paused=False)
        assert "Cancelled" in call("cancel_monitor", monitor["id"])
        assert "already cancelled" in call("cancel_monitor", monitor["id"])
        assert "no monitor 'nope'" in call("cancel_monitor", "nope")
        assert "unknown status" in call("list_monitors", "weird")

    def test_register_is_gated_like_execution(self, session):
        session.mode = "plan"
        result = asyncio.run(_tools()["register_monitor"](name="w", interval_seconds=60, resume_prompt="x"))
        assert "Execution blocked" in result
        assert st.MonitorStore().list_monitors() == []

    def test_the_orchestrator_holds_the_tools_and_the_prompt_block(self):
        from opalatex.memgpt_runtime import chat_orchestrator_system_prompt  # noqa: F401
        assert "register_monitor" in agent_tools.MONITORS_PROMPT_BLOCK


# ── Notifications ───────────────────────────────────────────────────────────

class TestNotify:
    def test_open_uri_round_trip_and_refusals(self):
        uri = notify.open_uri("my proj", "c&1", "/a b")
        assert notify.parse_open_uri(uri) == {"project": "my proj", "chat_id": "c&1", "project_path": "/a b"}
        with pytest.raises(ValueError):
            notify.parse_open_uri("https://evil/open?project=x")
        with pytest.raises(ValueError):
            notify.parse_open_uri("opalatex-monitor:run?cmd=rm")

    def test_freedesktop_call_escapes_and_maps_clicks(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "(uint32 42,)\n", "")

        monkeypatch.setattr(notify.subprocess, "run", fake_run)
        notifier = notify.FreedesktopNotifier("gdbus")
        uri = notify.open_uri("p", "c")
        assert notifier.send("It's <done>", "a & b\nline", uri=uri, action_label="Open chat") == "42"
        argv = calls[0]
        assert "'It\\'s <done>'" in argv
        assert "'a &amp; b\\nline'" in argv
        assert "['default', 'Open chat']" in argv
        # Under Snap confinement, AppArmor denies org.freedesktop.DBus.Introspectable.Introspect
        # on /org/freedesktop/Notifications, so gdbus parses each argument without introspection;
        # an untyped "0" defaults to int32 ("i") instead of uint32 ("u") and fails with InvalidArgs.
        method_idx = argv.index("org.freedesktop.Notifications.Notify")
        notify_args = argv[method_idx + 1:]
        assert len(notify_args) == 8
        assert notify_args[1] == "uint32 0"
        assert notify_args[7] == "int32 -1"
        # No positional argument may look like an option to gdbus.
        assert not any(arg.startswith("-") and not arg.startswith("--") for arg in argv[1:])
        opened = []
        notifier.handle_signal_line(
            "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 42, 'default')",
            opened.append,
        )
        notifier.handle_signal_line(
            "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 7, 'default')",
            opened.append,
        )
        assert opened == [uri]

    def test_freedesktop_refusal_is_reported(self, monkeypatch):
        monkeypatch.setattr(notify.subprocess, "run",
                            lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no server"))
        with pytest.raises(notify.NotifyError, match="no server"):
            notify.FreedesktopNotifier("gdbus").send("t", "b")

    def test_windows_toast_payload_is_escaped_and_base64_wrapped(self):
        uri = notify.open_uri("p", "c")
        xml = notify.toast_xml('A "quote" <tag>', "x & y", uri, "Open chat")
        assert "&quot;quote&quot; &lt;tag&gt;" in xml and "x &amp; y" in xml
        assert 'activationType="protocol"' in xml and "opalatex-monitor:open?project=p&amp;chat=c" in xml
        script = notify.toast_script(xml)
        payload = script.split("FromBase64String('")[1].split("'")[0]
        assert base64.b64decode(payload).decode("utf-8") == xml
        assert "CreateToastNotifier('com.opalacoder.OpalaTex')" in script

    def test_windows_toast_registers_aumid_and_settings_even_for_notify_test(self, monkeypatch):
        registry: dict[tuple[str, str], dict[str, tuple[int, object]]] = {}
        deleted: list[tuple[str, str]] = []

        class FakeKey:
            def __init__(self, hive, path):
                self.slot = registry.setdefault((hive, path), {})

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake_winreg = types.SimpleNamespace(
            HKEY_CURRENT_USER="HKCU",
            REG_SZ=1,
            REG_DWORD=4,
            CreateKey=lambda hive, path: FakeKey(hive, path),
            SetValueEx=lambda key, name, _reserved, kind, value: key.slot.__setitem__(name, (kind, value)),
            DeleteKey=lambda hive, path: (deleted.append((hive, path)), registry.pop((hive, path), None)),
        )
        monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
        monkeypatch.setattr(notify.sys, "platform", "win32")
        monkeypatch.setattr(notify, "_powershell", lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

        runs = []

        def fake_run(argv, **kwargs):
            runs.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(notify.subprocess, "run", fake_run)

        # default_notifier() with no arguments (used by notify-test in GUI/CLI)
        # must still register the AppUserModelId, Notification Settings and URI scheme.
        notifier = notify.default_notifier()
        assert isinstance(notifier, notify.WindowsToastNotifier)
        assert notifier.open_command and notifier.open_command[-2:] == ["monitors", "open"]
        notifier.send("OpalaTex", "Monitor notifications work.")

        aumid_key = ("HKCU", rf"Software\Classes\AppUserModelId\{notify.WINDOWS_APP_ID}")
        settings_key = ("HKCU", rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{notify.WINDOWS_APP_ID}")
        command_key = ("HKCU", rf"Software\Classes\{notify.URI_SCHEME}\shell\open\command")
        assert registry[aumid_key]["DisplayName"] == (1, "OpalaTex")
        assert registry[settings_key]["Enabled"] == (4, 1)
        assert registry[settings_key]["ShowInActionCenter"] == (4, 1)
        assert command_key in registry
        assert runs[0][1]["stdin"] == subprocess.DEVNULL
        assert runs[0][1]["creationflags"] == 0x08000000

        # Even if constructed with open_command=None directly, AppUserModelId and
        # Notification Settings are still registered before showing the toast.
        registry.clear()
        direct = notify.WindowsToastNotifier("powershell.exe", open_command=None)
        direct.send("OpalaTex", "Direct toast")
        assert aumid_key in registry and settings_key in registry
        assert command_key not in registry

        notify.unregister_windows_integration()
        assert settings_key not in registry and aumid_key not in registry

    def test_platform_selection(self, monkeypatch):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/gdbus" if name == "gdbus" else None)
        assert isinstance(notify.default_notifier(), notify.FreedesktopNotifier)
        monkeypatch.setattr(notify.shutil, "which", lambda name: None)
        with pytest.raises(notify.NotifyError):
            notify.default_notifier().send("t", "b")


# ── Services ────────────────────────────────────────────────────────────────

class TestService:
    def test_systemd_unit_carries_the_data_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPALATEX_HOME", "/data/opala home")
        unit = service.SystemdUserAutostart(config_home=str(tmp_path)).unit_text()
        assert "ExecStart=" in unit and "monitors daemon" in unit
        assert 'Environment="OPALATEX_HOME=/data/opala home"' in unit
        assert "Restart=on-failure" in unit

    def test_snap_autostart_entry_names_the_snap_app(self, tmp_path):
        entry = service.XdgAutostart(config_home=str(tmp_path), snap=True)
        entry.install()
        assert entry.installed()
        assert "Exec=opalatex.monitors" in open(entry.entry_path).read()
        entry.uninstall()
        assert not entry.installed()

    def test_a_service_is_never_removed_or_replaced_for_another_data_folder(self, tmp_path, monkeypatch):
        """Turning autostart off from one data folder removed the service of another."""
        monkeypatch.setattr(service, "_run", lambda argv, timeout=20: subprocess.CompletedProcess(argv, 0, "", ""))
        unit = service.SystemdUserAutostart(config_home=str(tmp_path))
        monkeypatch.setattr(service, "current_home", lambda: "/data/main")
        unit.install()
        assert unit.installed() and "# OpalaTex data folder: /data/main" in open(unit.unit_path).read()

        monkeypatch.setattr(service, "current_home", lambda: "/tmp/scratch-home")
        assert unit.installed() is False and unit.foreign_owner() == "/data/main"
        with pytest.raises(RuntimeError, match=r"another OpalaTex data folder \(/data/main\); it was not removed"):
            unit.uninstall()
        with pytest.raises(RuntimeError, match="it was not replaced"):
            unit.install()
        assert "/data/main" in open(unit.unit_path).read()

        monkeypatch.setattr(service, "current_home", lambda: "/data/main")
        unit.uninstall()
        assert not unit.present()

    def test_a_service_written_before_ownership_belongs_to_the_folder_it_resolves(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "_home_without_env", lambda: "/data/default")
        unit = service.SystemdUserAutostart(config_home=str(tmp_path))
        os.makedirs(os.path.dirname(unit.unit_path))
        with open(unit.unit_path, "w") as handle:
            handle.write("[Service]\nExecStart=/x/python main.py monitors daemon\n")
        assert unit.owner() == "/data/default"
        with open(unit.unit_path, "w") as handle:
            handle.write('[Service]\nEnvironment="OPALATEX_HOME=/data/other place"\n')
        assert unit.owner() == "/data/other place"
        entry = service.XdgAutostart(config_home=str(tmp_path), snap=False)
        os.makedirs(os.path.dirname(entry.entry_path))
        with open(entry.entry_path, "w") as handle:
            handle.write("Exec=env OPALATEX_HOME=/data/x /py main.py monitors daemon\n")
        assert entry.owner() == "/data/x"

    def test_set_autostart_reports_a_foreign_service(self, mstore, tmp_path, monkeypatch):
        unit = service.XdgAutostart(config_home=str(tmp_path), snap=False)
        monkeypatch.setattr(service, "current_home", lambda: "/data/main")
        unit.install()
        monkeypatch.setattr(service, "current_home", lambda: "/data/scratch")
        monkeypatch.setattr(service, "autostart_backend", lambda: unit)
        result = service.set_autostart(mstore, False)
        assert result["ok"] is False and "/data/main" in result["error"]
        assert unit.present()

    def test_lock_is_exclusive(self, tmp_path):
        first = service.DaemonLock(str(tmp_path / "d.lock"))
        second = service.DaemonLock(str(tmp_path / "d.lock"))
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()

    def test_ensure_starts_a_detached_daemon(self, mstore, _isolated_monitors):
        result = service.ensure_daemon_running(mstore)
        assert result["started"] is True
        assert _isolated_monitors[-1][-2:] == ["monitors", "daemon"]

    def test_ensure_reports_error_when_daemon_exits_before_wait_finishes(self, mstore, _isolated_monitors):
        result = service.ensure_daemon_running(mstore, wait=0.05)
        assert result["started"] is True
        assert result["running"] is False
        assert "did not stay running" in result["error"]

    def test_open_chat_uses_the_running_window_or_starts_one(self, mstore, monkeypatch, _isolated_monitors):
        saved = {}
        monkeypatch.setattr("opalatex.ui_settings.save_ui_settings", lambda data: saved.update(data))
        assert service.open_chat(mstore, "proj", "c1") == {"launched": True}
        assert saved == {"last_active_project": "proj"}
        assert len(_isolated_monitors) == 1
        mstore.touch_host("gui", os.getpid(), "proj", "/p")
        assert service.open_chat(mstore, "proj", "c2") == {"launched": False}
        assert len(_isolated_monitors) == 1
        assert mstore.take_open_request()["chat_id"] == "c2"

    def test_opalatex_command_runs_main_py_from_a_checkout(self):
        argv = service.opalatex_command() if hasattr(service, "opalatex_command") else None
        assert argv[-1].endswith("main.py")

    def test_spawn_detached_on_windows_avoids_conflicting_creation_flags(self, monkeypatch):
        from opalatex.monitors import process as proc_mod

        captured = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs

        monkeypatch.setattr(proc_mod.sys, "platform", "win32")
        monkeypatch.setattr(proc_mod.subprocess, "Popen", FakePopen)
        proc_mod.spawn_detached(["pythonw.exe", "main.py", "monitors", "daemon"])
        flags = captured["kwargs"]["creationflags"]
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        # Combining DETACHED_PROCESS and CREATE_NO_WINDOW fails with WinError 87
        # in Windows CreateProcessW.
        assert flags & CREATE_NO_WINDOW
        assert not (flags & DETACHED_PROCESS)


# ── Daemon ──────────────────────────────────────────────────────────────────

class RecordingNotifier(notify.Notifier):
    backend = "recording"

    def __init__(self):
        self.sent = []

    def send(self, title, body, *, urgency="normal", uri="", action_label="Open"):
        self.sent.append((title, body, urgency, uri))
        return "1"


class TestDaemon:
    def _daemon(self, mstore, clock, argv=None):
        return daemon_mod.MonitorDaemon(
            mstore, notifier=RecordingNotifier(), engine=FakeEngine(mstore, [], clock), clock=clock,
            child_argv=argv or (lambda wake_id: [sys.executable, "-c", "pass"]),
        )

    def test_notifications_are_delivered_with_an_open_uri(self, mstore, clock):
        mstore.enqueue_notification("t", "b", project="p", chat_id="c")
        daemon = self._daemon(mstore, clock)
        assert daemon.deliver_notifications() == 1
        assert daemon.notifier.sent[0][3] == notify.open_uri("p", "c")
        assert mstore.undelivered_notifications() == []

    def test_wakeups_of_the_open_project_are_left_to_the_window(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec())
        mstore.enqueue_wake(monitor, "p", "d")
        mstore.touch_host("gui", os.getpid(), "proj", "/elsewhere")
        daemon = self._daemon(mstore, clock)
        assert daemon.dispatch_wakes() == 0
        assert mstore.list_wakes()[0]["status"] == st.WAKE_PENDING

    def test_other_wakeups_run_in_a_child_and_a_dead_child_fails_its_request(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        daemon = self._daemon(mstore, clock)
        assert daemon.dispatch_wakes() == 1
        daemon.children[wake_id][0].wait()
        daemon.reap_children()
        wake = mstore.get_wake(wake_id)
        assert wake["status"] == st.WAKE_FAILED and "exited with code 0" in wake["error"]

    def test_a_new_chat_wakeup_runs_here_even_with_the_window_open(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d", chat_id="fresh", new_chat=True)
        mstore.touch_host("gui", os.getpid(), "proj", "")
        daemon = self._daemon(mstore, clock)
        assert daemon.dispatch_wakes() == 1
        assert mstore.get_wake(wake_id)["claimed_by"] == "daemon"
        daemon.children[wake_id][0].wait()

    def test_a_busy_chat_waits(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec())
        mstore.enqueue_wake(monitor, "p", "d")
        mstore.acquire_lease("proj", "chat-1", os.getppid(), "turn")
        assert self._daemon(mstore, clock).dispatch_wakes() == 0

    def test_reconcile_closes_what_dead_processes_left(self, mstore, clock):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        mstore.claim_wake(wake_id, "gui", dead.pid)
        self._daemon(mstore, clock).reconcile()
        assert mstore.get_wake(wake_id)["status"] == st.WAKE_INTERRUPTED

    def test_idle_daemon_exits(self, mstore, clock):
        daemon = self._daemon(mstore, clock)
        asyncio.run(daemon.step())
        assert daemon.should_exit() is False
        clock.advance(daemon_mod.IDLE_EXIT_SECONDS)
        assert daemon.should_exit() is True
        mstore.create_monitor(_spec())
        asyncio.run(daemon.step())
        assert daemon.should_exit() is False

    def test_the_daemon_restarts_when_the_installed_code_changes(self, mstore, clock, monkeypatch):
        versions = iter(["v1", "v1", "v2"])
        daemon = daemon_mod.MonitorDaemon(
            mstore, notifier=RecordingNotifier(), engine=FakeEngine(mstore, [], clock), clock=clock,
            fingerprint=lambda: next(versions),
        )
        monitor = mstore.create_monitor(_spec())
        clock.advance(daemon_mod.CODE_CHECK_SECONDS)
        asyncio.run(daemon.step())
        assert daemon.restart_requested is False
        clock.advance(daemon_mod.CODE_CHECK_SECONDS)
        asyncio.run(daemon.step())
        assert daemon.restart_requested is True and daemon.should_exit()
        # Stale code did no work in the step that found the change.
        assert mstore.get_monitor(monitor["id"])["check_count"] == 0

    def test_restart_execs_the_installation_not_sys_argv(self, monkeypatch):
        calls = []
        monkeypatch.setattr(daemon_mod.os, "execv", lambda path, argv: calls.append((path, argv)))
        monkeypatch.setattr(daemon_mod.sys, "platform", "linux")
        daemon_mod._restart_with_new_code()
        assert calls == [(service.daemon_argv()[0], service.daemon_argv())]
        assert calls[0][1][-2:] == ["monitors", "daemon"]

    def test_code_fingerprint_is_stable_and_versioned(self):
        from opalatex import __version__

        assert service.code_fingerprint() == service.code_fingerprint()
        assert service.code_fingerprint().startswith(__version__ + "|")

    def test_a_click_opens_the_chat(self, mstore, clock, monkeypatch):
        opened = []
        monkeypatch.setattr(service, "open_chat", lambda store, *args: opened.append(args))
        daemon = self._daemon(mstore, clock)
        daemon.open_from_notification(notify.open_uri("p", "c", "/p"))
        daemon.open_from_notification("https://not-ours")
        assert opened == [("p", "c", "/p")]


# ── Background turns ────────────────────────────────────────────────────────

class TestNoChatNeverRunsElsewhere:
    """A wake-up without a chat once ran in whatever chat the window had open."""

    def _legacy_wake(self, mstore):
        monitor = mstore.create_monitor(_spec(project="proj"))
        with mstore._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO wake_requests (monitor_id, monitor_name, project, chat_id, prompt, created_at, status)"
                " VALUES (?, 'm', 'proj', '', 'p', 0, 'pending')", (monitor["id"],))
            return cursor.lastrowid

    def test_enqueueing_without_a_chat_is_refused(self, mstore):
        monitor = mstore.create_monitor(_spec(project="proj"))
        with pytest.raises(ValueError, match="names no chat"):
            mstore.enqueue_wake(monitor, "p", "d")

    def test_the_window_never_receives_one(self, mstore):
        wake_id = self._legacy_wake(mstore)
        body = asyncio.run(api.handle("/api/monitors/poll", "POST", {"project_name": "proj", "idle": True}, {},
                                      turn_active=False))[1]
        assert body["wake"] is None
        wake = st.MonitorStore().get_wake(wake_id)
        assert wake["status"] == st.WAKE_FAILED and "names no chat" in wake["error"]

    def test_the_daemon_fails_it(self, mstore, clock):
        wake_id = self._legacy_wake(mstore)
        daemon = daemon_mod.MonitorDaemon(mstore, notifier=RecordingNotifier(),
                                          engine=FakeEngine(mstore, [], clock), clock=clock)
        assert daemon.dispatch_wakes() == 0
        assert mstore.get_wake(wake_id)["status"] == st.WAKE_FAILED

    def test_a_stored_monitor_without_its_chat_reports_instead_of_waking(self, mstore, clock):
        monitor = mstore.create_monitor(_wake_spec())
        with mstore._connect() as conn:  # as stored before chats were required
            conn.execute("UPDATE monitors SET chat_id = '' WHERE id = ?", (monitor["id"],))
        clock.advance(120)
        asyncio.run(FakeEngine(mstore, [None], clock).tick())
        assert mstore.list_wakes() == []
        assert mstore.undelivered_notifications()[0]["urgency"] == "critical"


class TestWakeRunner:
    def _wake(self, mstore):
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        return mstore.get_wake(wake_id)

    def test_an_unclaimed_request_is_not_run(self, mstore):
        wake = self._wake(mstore)
        assert asyncio.run(wake_runner.run_wake(wake["id"], store=mstore)) == 2

    def test_a_missing_project_fails_the_request_and_notifies(self, mstore, tmp_path):
        monitor = mstore.create_monitor(_wake_spec(sessions_db=str(tmp_path / "s.db")))
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        mstore.claim_wake(wake_id, "daemon", os.getpid())
        assert asyncio.run(wake_runner.run_wake(wake_id, store=mstore)) == 1
        wake = mstore.get_wake(wake_id)
        assert wake["status"] == st.WAKE_FAILED and "no longer exists" in wake["error"]
        assert mstore.lease_holder("proj", "chat-1") is None
        assert mstore.undelivered_notifications()[-1]["urgency"] == "critical"


class TestUnattendedTurn:
    """A monitor's turn asks nothing outside its chat; it reports in its reply."""

    def _run_unattended(self, monkeypatch, body):
        from opalatex import agent_stdin

        seen = {}

        async def fake_body(data):
            seen["during"] = agent_stdin.unattended_turn()
            seen["result"] = await body()

        monkeypatch.setattr(agent_stdin, "_handle_run", fake_body)
        asyncio.run(agent_stdin.handle_run({"project_name": "proj", "chat_id": "c-un", "monitor_wake": "News"}))
        seen["after"] = agent_stdin.unattended_turn()
        return seen

    def test_the_turn_is_marked_only_while_it_runs(self, monkeypatch):
        async def nothing():
            return None

        seen = self._run_unattended(monkeypatch, nothing)
        assert seen["during"] == "News" and seen["after"] is None

    def test_no_question_reaches_a_front_end(self, monkeypatch):
        from opalatex import agent_stdin, tools

        events = []
        monkeypatch.setattr(agent_stdin, "print_event", lambda event, data: events.append(event))
        project = types.SimpleNamespace(name="proj", project_path=os.getcwd(), current_chat_id="c-un",
                                        mode="edit", results={})
        monkeypatch.setattr(tools, "_PROJECT_SESSION", project)

        async def body():
            ask = await tools.ask_question._func("Which topic?", options=["a", "b"])
            write = await tools.write_file._func("x.txt", "data")
            interactive = await tools.run_interactive_command._func("ssh host")
            project.mode = "plan"
            plan = await tools.create_plan._func("1. do it")
            project.mode = "edit"
            try:
                await agent_stdin.request_user_input("anything?")
                direct = "asked"
            except agent_stdin.UnattendedTurnError as exc:
                direct = str(exc)
            return ask, write, interactive, plan, direct

        ask, write, interactive, plan, direct = self._run_unattended(monkeypatch, body)["result"]
        assert ask.startswith("Not done: the question 'Which topic?' was not asked")
        assert "say so in your reply in this chat" in ask and '"News"' in ask
        assert write.startswith("Execution blocked: Not done: 'write_file' needs the user's approval")
        assert not os.path.exists("x.txt")
        assert interactive.startswith("Execution blocked: Not done: 'run_interactive_command'")
        assert "write it in your reply" in plan
        assert direct.startswith("Not done: the question 'anything?'")
        assert "input_request" not in events

    def test_auto_mode_tools_still_run(self, monkeypatch, tmp_path):
        from opalatex import tools

        project = types.SimpleNamespace(name="proj", project_path=str(tmp_path), current_chat_id="c-un",
                                        mode="auto", results={})
        monkeypatch.setattr(tools, "_PROJECT_SESSION", project)
        tools.set_project_context(project, None)

        async def body():
            written = await tools.write_file._func("note.txt", "ok")
            interactive = await tools.run_interactive_command._func("ssh host")
            return written, interactive

        written, interactive = self._run_unattended(monkeypatch, body)["result"]
        assert "Execution blocked" not in str(written)
        assert (tmp_path / "note.txt").read_text() == "ok"
        # Auto mode needs no approval, but an interactive command still needs a person.
        assert "needs a person at a terminal" in interactive

    def test_the_wake_message_says_nobody_is_watching(self):
        prompt = engine_mod.wake_prompt({"id": "m", "name": "News", "action": {"prompt": "list today's AI news"}},
                                        engine_mod.EVENT_CONDITION_MET, None, {"checks": 1}, new_chat=True)
        assert "nobody is watching this turn" in prompt and "do not ask questions" in prompt
        assert "with ask_question" not in prompt and "list today's AI news" in prompt

    def test_wake_message_distinguishes_a_continuing_monitor_from_an_ended_one(self):
        recurring = engine_mod.wake_prompt(
            {"id": "mon_1", "name": "News", "repeat": True, "interval_seconds": 3600,
             "action": {"prompt": "summarize news"}},
            engine_mod.EVENT_CONDITION_MET, None, {"checks": 1}, new_chat=True,
        )
        assert "is recurring and remains active" in recurring
        assert "Do NOT call register_monitor" in recurring
        assert "To keep waiting, register a new monitor" not in recurring

        one_shot_wait = engine_mod.wake_prompt(
            {"id": "mon_2", "name": "Build", "repeat": False, "interval_seconds": 60,
             "action": {"prompt": "check build"}},
            engine_mod.EVENT_CONDITION_MET, None, {"checks": 1}, new_chat=False,
        )
        assert 'The monitor "Build" (mon_2) has ended.' in one_shot_wait
        assert "To keep waiting, register a new monitor with register_monitor" in one_shot_wait

        one_shot_new_chat = engine_mod.wake_prompt(
            {"id": "mon_3", "name": "Once", "repeat": False, "interval_seconds": 60,
             "action": {"prompt": "report once"}},
            engine_mod.EVENT_CONDITION_MET, None, {"checks": 1}, new_chat=True,
        )
        assert "was set to fire once and has now ended; do not re-register it" in one_shot_new_chat
        assert "To keep waiting, register a new monitor" not in one_shot_new_chat

    def test_a_woken_turn_cannot_re_register_while_its_monitor_continues(self, session, mstore, monkeypatch):
        from opalatex import agent_stdin

        recurring = mstore.create_monitor(_wake_spec(
            name="Notícias de IA",
            action={"type": "resume_chat", "prompt": "summarize news", "chat": "new"},
            repeat=True,
        ))
        wake_id = mstore.enqueue_wake(recurring, "p", "d", chat_id="chat-wake", new_chat=True)
        mstore.claim_wake(wake_id, "daemon", os.getpid())
        session.current_chat_id = "chat-wake"

        async def try_register():
            # Even if the woken agent tweaks the name, description or action type,
            # the turn's waking monitor is still active and registration is refused.
            return await _tools()["register_monitor"](
                name="Notícias de IA (próximo ciclo)",
                interval_seconds=11400,
                resume_prompt="summarize news again",
                wake_in="new_chat",
            )

        captured = {}

        async def fake_body(_data):
            captured["id"] = agent_stdin.unattended_monitor_id()
            captured["result"] = await try_register()

        monkeypatch.setattr(agent_stdin, "_handle_run", fake_body)
        asyncio.run(agent_stdin.handle_run({
            "project_name": "proj",
            "chat_id": "chat-wake",
            "monitor_wake": recurring["name"],
            "monitor_wake_id": recurring["id"],
        }))
        assert captured["id"] == recurring["id"]
        assert agent_stdin.unattended_monitor_id() is None
        assert f"woken by monitor {recurring['id']}" in captured["result"]
        assert "remains active" in captured["result"]
        assert [m["id"] for m in mstore.list_monitors()] == [recurring["id"]]

        # Fallback when monitor_wake_id was not passed: still resolves from the claimed wake / name.
        asyncio.run(agent_stdin.handle_run({
            "project_name": "proj",
            "chat_id": "chat-wake",
            "monitor_wake": recurring["name"],
        }))
        assert f"woken by monitor {recurring['id']}" in captured["result"]
        assert [m["id"] for m in mstore.list_monitors()] == [recurring["id"]]

    def test_a_woken_turn_can_register_a_followup_once_its_task_wait_ended(self, session, mstore, clock, monkeypatch):
        from opalatex import agent_stdin

        wait = mstore.create_monitor(_wake_spec(name="step 1", repeat=False))
        clock.advance(120)
        asyncio.run(FakeEngine(mstore, [None], clock).tick())
        assert mstore.get_monitor(wait["id"])["status"] == st.STATUS_COMPLETED

        async def fake_body(_data):
            return await _tools()["register_monitor"](
                name="step 2", interval_seconds=120, resume_prompt="finish deploy",
            )

        monkeypatch.setattr(agent_stdin, "_handle_run", fake_body)
        result = asyncio.run(agent_stdin.handle_run({
            "project_name": "proj",
            "chat_id": "chat-1",
            "monitor_wake": wait["name"],
            "monitor_wake_id": wait["id"],
        }))
        assert result.startswith("Registered monitor mon_")
        active = mstore.list_monitors((st.STATUS_ACTIVE,))
        assert len(active) == 1 and active[0]["name"] == "step 2"

    def test_duplicate_active_monitor_is_refused(self, session):
        register = _tools()["register_monitor"]
        first = asyncio.run(register(
            name="Notícias de IA", interval_seconds=3600, resume_prompt="list news", wake_in="new_chat",
        ))
        assert first.startswith("Registered monitor mon_")
        second = asyncio.run(register(
            name="notícias de ia", interval_seconds=3600, resume_prompt="list news again", wake_in="new_chat",
        ))
        assert "already active" in second
        assert len(st.MonitorStore().list_monitors((st.STATUS_ACTIVE,))) == 1

    def test_the_headless_runner_marks_its_turn(self, mstore, tmp_path, monkeypatch):
        from opalatex import agent_stdin
        from opalatex.project import ProjectStore

        sessions = ProjectStore(db_path=str(tmp_path / "s.db"))
        sessions.create("proj", "auto", "", project_path=str(tmp_path / "p"))
        sessions.create_chat("proj", "chat-1", "c")
        monitor = mstore.create_monitor(_wake_spec(sessions_db=str(tmp_path / "s.db")))
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        mstore.claim_wake(wake_id, "daemon", os.getpid())
        captured = {}

        async def fake_run(data):
            captured.update(data)
            agent_stdin.event_hook({"event": "agent_response", "response": "Three new **AI** stories today."})

        monkeypatch.setattr(agent_stdin, "handle_run", fake_run)
        asyncio.run(wake_runner.run_wake(wake_id, store=mstore))
        assert captured["monitor_wake"] == "build" and captured["monitor_wake_id"] == monitor["id"]
        assert captured["chat_id"] == "chat-1"
        assert agent_stdin._input_transport is None
        wake = mstore.get_wake(wake_id)
        assert wake["status"] == st.WAKE_DONE and wake["result"] == "Three new **AI** stories today."
        note = mstore.undelivered_notifications()[-1]
        assert note["title"] == "New event from monitor \u201cbuild\u201d"
        assert note["body"] == "Three new AI stories today." and note["chat_id"] == "chat-1"


class TestTurnLease:
    def test_a_turn_refuses_a_chat_another_process_holds(self, monkeypatch):
        from opalatex import agent_stdin

        st.MonitorStore().acquire_lease("proj", "chat-1", os.getppid(), "monitor")
        events = []
        monkeypatch.setattr(agent_stdin, "print_event", lambda event, data: events.append((event, data)))
        ran = []

        async def fake_body(data):
            ran.append(data)

        monkeypatch.setattr(agent_stdin, "_handle_run", fake_body)
        asyncio.run(agent_stdin.handle_run({"project_name": "proj", "chat_id": "chat-1", "prompt": "hi"}))
        assert ran == []
        assert events[0][0] == "error" and "monitor" in events[0][1]["message"]

    def test_a_turn_holds_and_releases_its_chat(self, monkeypatch):
        from opalatex import agent_stdin

        seen = []

        async def fake_body(data):
            seen.append(st.MonitorStore().lease_holder("proj", "chat-2"))

        monkeypatch.setattr(agent_stdin, "_handle_run", fake_body)
        asyncio.run(agent_stdin.handle_run({"project_name": "proj", "chat_id": "chat-2"}))
        assert seen[0]["pid"] == os.getpid()
        assert st.MonitorStore().lease_holder("proj", "chat-2") is None


# ── Window API ──────────────────────────────────────────────────────────────

class TestApi:
    def _call(self, path, method="GET", data=None, query=None, turn_active=False, focus=None):
        return asyncio.run(api.handle(path, method, data or {}, query or {}, turn_active=turn_active,
                                      focus_window=focus))

    def test_poll_claims_a_wakeup_of_the_open_project_when_idle(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        busy = self._call("/api/monitors/poll", "POST", {"project_name": "proj", "idle": True}, turn_active=True)
        assert busy[1]["wake"] is None
        status, body = self._call("/api/monitors/poll", "POST", {"project_name": "proj", "idle": True})
        assert status == 200 and body["wake"]["id"] == wake_id
        assert mstore.live_hosts("gui", 20)[0]["project"] == "proj"
        assert self._call("/api/monitors/wake/finish", "POST", {"id": wake_id, "status": "released"})[1]["ok"]
        assert mstore.get_wake(wake_id)["status"] == st.WAKE_PENDING

    def test_poll_leaves_new_chat_wakeups_to_the_daemon_and_reports_events(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_wake_spec())
        fresh = mstore.enqueue_wake(monitor, "p", "d", chat_id="fresh", new_chat=True)
        body = self._call("/api/monitors/poll", "POST", {"project_name": "proj", "idle": True})[1]
        assert body["wake"] is None and mstore.get_wake(fresh)["status"] == st.WAKE_PENDING
        assert body["unseen_events"] == 0 and body["events"] == []
        mstore.claim_wake(fresh, "daemon", os.getpid())
        mstore.finish_wake(fresh, st.WAKE_DONE, result="Summary of the news")
        body = self._call("/api/monitors/poll", "POST", {"project_name": "proj"})[1]
        assert body["unseen_events"] == 1
        event = body["events"][0]
        assert (event["monitor_name"], event["chat_id"], event["result"], event["new_chat"]) == ("build", "fresh", "Summary of the news", 1)
        assert self._call("/api/monitors")[1]["events"][0]["id"] == fresh
        self._call("/api/monitors/events/seen", "POST")
        assert self._call("/api/monitors/poll", "POST", {"project_name": "proj"})[1]["unseen_events"] == 0

    def test_poll_delivers_a_click_and_raises_the_window(self):
        mstore = st.MonitorStore()
        mstore.add_open_request("proj", "/p", "c9")
        focused = []
        body = self._call("/api/monitors/poll", "POST", {"project_name": "x"}, focus=lambda: focused.append(1))[1]
        assert body["open"]["chat_id"] == "c9" and focused == [1]

    def test_actions(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec())
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "pause"})[0] == 200
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "pause"})[0] == 409
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "explode"})[0] == 400
        listing = self._call("/api/monitors")[1]
        assert listing["monitors"][0]["status"] == st.STATUS_PAUSED

    def test_create_refuses_an_invalid_spec(self):
        status, body = self._call("/api/monitors/create", "POST", {"spec": {"name": "x"}})
        assert status == 400 and "type" in body["error"]


# ── Command line ────────────────────────────────────────────────────────────

class TestCli:
    def test_monitors_subcommand_is_found_after_the_snap_gui_flag(self):
        from opalatex.cli import _monitors_argv

        assert _monitors_argv(["--gui", "monitors", "list"]) == ["list"]
        assert _monitors_argv(["monitors"]) == []
        assert _monitors_argv(["--cli"]) is None

    def test_list_and_pause(self, capsys):
        from opalatex.monitors.cli import main

        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec())
        assert main(["list"]) == 0
        assert monitor["id"] in capsys.readouterr().out
        assert main(["pause", monitor["id"]]) == 0
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_PAUSED
        assert main(["open", "https://x"]) == 2

    def test_llm_verdict_parsing(self):
        from opalatex.monitors.llm_check import parse_answer

        assert parse_answer('```json\n{"met": true, "value": "5.6", "summary": "up"}\n```')["met"] is True
        assert parse_answer('<think>hm</think>{"met": false}')["met"] is False
        with pytest.raises(CheckError, match="boolean 'met'"):
            parse_answer('{"met": "yes"}')
        with pytest.raises(CheckError, match="JSON verdict"):
            parse_answer("The dollar went up.")


class TestConsistencyReview:
    """Defects found reviewing the whole feature; each is pinned here."""

    def test_the_type_follows_the_action(self):
        assert _wake_spec()["owner"] == "agent"  # wakes its own chat: a task wait
        assert _wake_spec(action={"type": "resume_chat", "prompt": "p", "chat": "new"})["owner"] == "user"
        assert _spec()["owner"] == "user"

    def test_a_watch_that_ends_notifies_instead_of_opening_a_chat(self, mstore, clock):
        watch = _wake_spec(action={"type": "resume_chat", "prompt": "p", "chat": "new"},
                           check={"type": "http", "url": "https://x"}, condition={"type": "gt", "value": 5},
                           max_checks=1)
        monitor = mstore.create_monitor(watch)
        clock.advance(120)
        eng = FakeEngine(mstore, ["1"], clock)
        eng.chat_creator = lambda _m: pytest.fail("no chat may be opened to report an ending")
        asyncio.run(eng.tick())
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_EXPIRED
        assert mstore.list_wakes() == []
        note = mstore.undelivered_notifications()[0]
        assert note["urgency"] == "low" and "checks used up" in note["body"]

    def test_a_notify_only_watch_that_expires_says_so(self, mstore, clock):
        monitor = mstore.create_monitor(_spec(ttl_seconds=60))
        clock.advance(120)
        asyncio.run(FakeEngine(mstore, [], clock).tick())
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_EXPIRED
        assert "time limit" in mstore.undelivered_notifications()[0]["body"]

    def test_cancelling_a_finished_monitor_is_refused_everywhere(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec())
        mstore.set_status(monitor["id"], st.STATUS_COMPLETED, "fired")
        status, body = asyncio.run(api.handle("/api/monitors/action", "POST",
                                              {"id": monitor["id"], "action": "cancel"}, {}, turn_active=False))
        assert status == 409 and "already completed" in body["error"]
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_COMPLETED

    def test_a_wakeup_the_user_watched_is_not_an_unseen_event(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        mstore.claim_wake(wake_id, "gui", os.getpid())
        asyncio.run(api.handle("/api/monitors/wake/finish", "POST", {"id": wake_id, "status": "done"}, {},
                               turn_active=False))
        assert mstore.get_wake(wake_id)["seen"] == 1 and mstore.unseen_events() == 0

    def test_the_status_probes_the_service_manager_once_per_period(self, monkeypatch):
        calls = []
        monkeypatch.setattr(service.SystemdUserAutostart, "_probe", None)
        monkeypatch.setattr(service.SystemdUserAutostart, "_probe_systemd",
                            staticmethod(lambda: calls.append(1) or (True, "")))
        backend = service.SystemdUserAutostart(config_home="/nonexistent")
        for _ in range(5):
            backend.available()
        assert calls == [1]

    def test_the_panel_status_runs_off_the_event_loop(self, monkeypatch):
        import threading as _threading

        main = _threading.get_ident()
        seen = []
        monkeypatch.setattr(service, "daemon_status", lambda: seen.append(_threading.get_ident()) or {})
        asyncio.run(api.handle("/api/monitors", "GET", {}, {}, turn_active=False))
        assert seen and seen[0] != main

    def test_a_background_turn_gets_no_window_tools_and_no_window_prompt(self, monkeypatch):
        from opalatex import agent_stdin
        from opalatex.prompt_surface import GUI_ONLY_TOOLS, body_for_surface, tools_for_surface

        tools = [types.SimpleNamespace(name=name) for name in ("read_file", *GUI_ONLY_TOOLS)]
        body = "- `open_file` opens a file\n- `/compile` builds the PDF\n- `read_file` reads"
        monkeypatch.setattr(agent_stdin, "_reply_surface", agent_stdin.REPLY_SURFACE_BACKGROUND)
        assert [tool.name for tool in tools_for_surface(tools)] == ["read_file"]
        filtered = body_for_surface(body)
        assert "open_file" not in filtered and "/compile" not in filtered and "read_file" in filtered
        monkeypatch.setattr(agent_stdin, "_reply_surface", None)
        assert len(tools_for_surface(tools)) == len(tools)

    def test_the_background_prompt_block_replaces_the_window_block(self):
        from opalatex import memgpt_runtime

        block = memgpt_runtime._BACKGROUND_REPLY_BLOCK
        assert "nobody is watching" in block and "renders Markdown" in block
        assert "The IDE you work in" not in block


def test_clearing_events_keeps_what_has_not_finished():
    mstore = st.MonitorStore()
    monitor = mstore.create_monitor(_wake_spec())
    done = mstore.enqueue_wake(monitor, "p", "d")
    mstore.claim_wake(done, "daemon", os.getpid())
    mstore.finish_wake(done, st.WAKE_DONE, result="r")
    running = mstore.enqueue_wake(monitor, "p", "d")
    mstore.claim_wake(running, "daemon", os.getpid())
    pending = mstore.enqueue_wake(monitor, "p", "d")
    status, body = asyncio.run(api.handle("/api/monitors/events/clear", "POST", {}, {}, turn_active=False))
    assert status == 200 and body["cleared"] == 1
    assert mstore.recent_events() == [] and mstore.unseen_events() == 0
    assert mstore.get_wake(running)["status"] == st.WAKE_CLAIMED
    assert mstore.get_wake(pending)["status"] == st.WAKE_PENDING
