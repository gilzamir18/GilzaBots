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

    def test_an_input_is_answered_once(self, mstore):
        mstore.add_input({"id": "q1", "prompt": "Allow?"}, wake_request_id=None, project="p",
                         project_path="", chat_id="c", monitor_name="m", pid=os.getpid())
        assert mstore.answer_input("q1", "yes") is True
        assert mstore.answer_input("q1", "no") is False
        assert mstore.get_input("q1")["answer"] == "yes"

    def test_open_requests_are_consumed(self, mstore):
        mstore.add_open_request("p", "/p", "c1")
        mstore.add_open_request("p", "/p", "c2")
        assert mstore.take_open_request()["chat_id"] == "c2"
        assert mstore.take_open_request() is None

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
        mstore.add_input({"id": "q"}, wake_request_id=wake_id, project="proj", project_path="",
                         chat_id="chat-1", monitor_name="m", pid=dead.pid)
        self._daemon(mstore, clock).reconcile()
        assert mstore.get_wake(wake_id)["status"] == st.WAKE_INTERRUPTED
        assert mstore.get_input("q")["status"] == st.INPUT_CLOSED

    def test_idle_daemon_exits(self, mstore, clock):
        daemon = self._daemon(mstore, clock)
        asyncio.run(daemon.step())
        assert daemon.should_exit() is False
        clock.advance(daemon_mod.IDLE_EXIT_SECONDS)
        assert daemon.should_exit() is True
        mstore.create_monitor(_spec())
        asyncio.run(daemon.step())
        assert daemon.should_exit() is False

    def test_a_click_opens_the_chat(self, mstore, clock, monkeypatch):
        opened = []
        monkeypatch.setattr(service, "open_chat", lambda store, *args: opened.append(args))
        daemon = self._daemon(mstore, clock)
        daemon.open_from_notification(notify.open_uri("p", "c", "/p"))
        daemon.open_from_notification("https://not-ours")
        assert opened == [("p", "c", "/p")]


# ── Background turns ────────────────────────────────────────────────────────

class TestWakeRunner:
    def _wake(self, mstore):
        monitor = mstore.create_monitor(_wake_spec())
        wake_id = mstore.enqueue_wake(monitor, "p", "d")
        return mstore.get_wake(wake_id)

    def test_questions_wait_for_an_answer_from_elsewhere(self, mstore):
        wake = self._wake(mstore)
        transport = wake_runner.make_input_transport(mstore, wake, poll=0.01)

        async def scenario():
            task = asyncio.ensure_future(transport({"id": "q1", "prompt": "Run **rm**?", "type": "confirm"}))
            await asyncio.sleep(0.05)
            assert mstore.get_input("q1")["status"] == st.INPUT_WAITING
            assert mstore.answer_input("q1", "yes")
            return await asyncio.wait_for(task, 2)

        assert asyncio.run(scenario()) == "yes"
        note = mstore.undelivered_notifications()[0]
        assert note["urgency"] == "critical" and note["body"] == "Run rm?"
        assert note["chat_id"] == "chat-1"

    def test_a_cancelled_question_is_closed(self, mstore):
        wake = self._wake(mstore)
        transport = wake_runner.make_input_transport(mstore, wake, poll=0.01)

        async def scenario():
            task = asyncio.ensure_future(transport({"id": "q2", "prompt": "?", "type": "ask"}))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert mstore.get_input("q2")["status"] == st.INPUT_CLOSED

    def test_interactive_commands_are_declined(self, mstore, monkeypatch):
        events = []
        monkeypatch.setattr("opalatex.agent_stdin.print_event", lambda event, data: events.append((event, data)))
        transport = wake_runner.make_input_transport(mstore, self._wake(mstore))
        assert asyncio.run(transport({"id": "t", "type": "interactive_terminal"})) == "cancel"
        assert events[0][0] == "problem" and "no terminal" in events[0][1]["message"]

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

    def test_poll_delivers_a_click_and_raises_the_window(self):
        mstore = st.MonitorStore()
        mstore.add_open_request("proj", "/p", "c9")
        focused = []
        body = self._call("/api/monitors/poll", "POST", {"project_name": "x"}, focus=lambda: focused.append(1))[1]
        assert body["open"]["chat_id"] == "c9" and focused == [1]

    def test_actions_and_remote_answers(self):
        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec())
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "pause"})[0] == 200
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "pause"})[0] == 409
        assert self._call("/api/monitors/action", "POST", {"id": monitor["id"], "action": "explode"})[0] == 400
        mstore.add_input({"id": "q"}, wake_request_id=None, project="p", project_path="", chat_id="c",
                         monitor_name="m", pid=os.getpid())
        assert api.answer_remote_input("q", "no") is True
        assert api.answer_remote_input("q", "yes") is False
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

    def test_list_pause_and_answer(self, capsys):
        from opalatex.monitors.cli import main

        mstore = st.MonitorStore()
        monitor = mstore.create_monitor(_spec())
        assert main(["list"]) == 0
        assert monitor["id"] in capsys.readouterr().out
        assert main(["pause", monitor["id"]]) == 0
        assert mstore.get_monitor(monitor["id"])["status"] == st.STATUS_PAUSED
        assert main(["answer", "nothing", "yes"]) == 1
        assert main(["open", "https://x"]) == 2

    def test_llm_verdict_parsing(self):
        from opalatex.monitors.llm_check import parse_answer

        assert parse_answer('```json\n{"met": true, "value": "5.6", "summary": "up"}\n```')["met"] is True
        assert parse_answer('<think>hm</think>{"met": false}')["met"] is False
        with pytest.raises(CheckError, match="boolean 'met'"):
            parse_answer('{"met": "yes"}')
        with pytest.raises(CheckError, match="JSON verdict"):
            parse_answer("The dollar went up.")
