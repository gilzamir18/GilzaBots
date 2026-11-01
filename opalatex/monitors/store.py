"""Persistent monitor state shared by every OpalaTex process.

The window's server, the CLI, the monitor daemon and the turns the daemon
starts are separate processes. They coordinate through this one SQLite file,
``<opalatex home>/monitors.db``, rather than through a socket, so no process
has to be running for another to register, pause or cancel a monitor: the
daemon reads what is due on every tick.

Tables:

- ``monitors``: the registered monitors and their scheduling state.
- ``monitor_events``: the history of checks, firings and errors (bounded per
  monitor).
- ``wake_requests``: a chat a monitor asked to wake. Claimed atomically by the
  process that will run the turn -- the window when it has that project open,
  the daemon otherwise.
- ``pending_inputs``: a question a turn without a front-end is waiting on
  (tool permission, plan approval, ``ask_question``), answered from the window
  or the CLI.
- ``notifications``: the OS notifications to deliver. The daemon owns
  delivery, because it is the long-lived process that can receive the click.
- ``hosts``: liveness of the front-ends, so the daemon knows whether a window
  will run a wake-up itself.
- ``turn_leases``: which process is running a turn in a chat, so two processes
  never run the same chat's orchestrator at once (its saved state is per chat).
- ``open_requests``: a notification click asking the window to show a chat.
- ``settings``: monitor-wide preferences (login autostart).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .process import pid_alive

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"
#: States a monitor can leave only by being deleted or resumed explicitly.
FINAL_STATUSES = (STATUS_COMPLETED, STATUS_EXPIRED, STATUS_CANCELLED, STATUS_ERROR)
ALL_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, *FINAL_STATUSES)

WAKE_PENDING = "pending"
WAKE_CLAIMED = "claimed"
WAKE_DONE = "done"
WAKE_FAILED = "failed"
WAKE_INTERRUPTED = "interrupted"

INPUT_WAITING = "waiting"
INPUT_ANSWERED = "answered"
INPUT_CLOSED = "closed"

#: Events kept per monitor; older ones are dropped as new ones arrive.
MAX_EVENTS_PER_MONITOR = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT 'user',
    project TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    sessions_db TEXT NOT NULL DEFAULT '',
    check_json TEXT NOT NULL,
    condition_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    max_checks INTEGER,
    expires_at REAL,
    repeat INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    status_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    next_check_at REAL,
    last_check_at REAL,
    check_count INTEGER NOT NULL DEFAULT 0,
    fire_count INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    has_value INTEGER NOT NULL DEFAULT 0,
    last_value_json TEXT NOT NULL DEFAULT 'null',
    last_met INTEGER,
    reference_json TEXT NOT NULL DEFAULT 'null',
    last_fired_at REAL
);
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS monitor_events_by_monitor ON monitor_events (monitor_id, id);
CREATE TABLE IF NOT EXISTS wake_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT NOT NULL,
    monitor_name TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL,
    project_path TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL,
    sessions_db TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL,
    display TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_pid INTEGER,
    claimed_at REAL,
    finished_at REAL,
    error TEXT NOT NULL DEFAULT '',
    new_chat INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_inputs (
    id TEXT PRIMARY KEY,
    wake_request_id INTEGER,
    project TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    monitor_name TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL,
    holder_pid INTEGER,
    created_at REAL NOT NULL,
    status TEXT NOT NULL,
    answer TEXT,
    answered_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    project TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    delivered_at REAL,
    backend TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hosts (
    kind TEXT NOT NULL,
    pid INTEGER NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    seen_at REAL NOT NULL,
    PRIMARY KEY (kind, pid)
);
CREATE TABLE IF NOT EXISTS turn_leases (
    project TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    holder TEXT NOT NULL DEFAULT '',
    acquired_at REAL NOT NULL,
    PRIMARY KEY (project, chat_id)
);
CREATE TABLE IF NOT EXISTS open_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_db_path() -> str:
    from opalatex.config import get_opalatex_home

    return os.path.join(get_opalatex_home(), "monitors.db")


def _loads(text: str | None, default: Any = None) -> Any:
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class MonitorStore:
    """Access to ``monitors.db``. Every method opens its own connection.

    Connections are short-lived on purpose: several processes write this file,
    and a connection held open across a long turn would pin a WAL snapshot.
    """

    def __init__(self, db_path: str | None = None, clock=time.time):
        self.db_path = db_path or default_db_path()
        self.clock = clock
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the columns a database created by an earlier version lacks."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(wake_requests)")}
        if "new_chat" not in columns:
            # Whether the monitor opened this chat for the wake-up, so the
            # window shows it instead of running it out of sight.
            conn.execute("ALTER TABLE wake_requests ADD COLUMN new_chat INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Monitors ────────────────────────────────────────────────────────────

    def create_monitor(self, spec: dict, *, initial_value: Any = None, has_initial_value: bool = False,
                       initial_met: bool | None = None, initial_reference: Any = None) -> dict:
        """Store a normalized specification (``spec.normalize_spec``)."""
        now = self.clock()
        monitor_id = "mon_" + uuid.uuid4().hex[:12]
        first = spec.get("first_check_seconds")
        next_check = now + (spec["interval_seconds"] if first is None else first)
        expires_at = now + spec["ttl_seconds"] if spec.get("ttl_seconds") else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitors (
                    id, name, description, owner, project, project_path, chat_id, sessions_db,
                    check_json, condition_json, action_json, interval_seconds, max_checks,
                    expires_at, repeat, status, created_at, updated_at, next_check_at,
                    has_value, last_value_json, last_met, reference_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    monitor_id, spec["name"], spec.get("description", ""), spec.get("owner", "user"),
                    spec.get("project", ""), spec.get("project_path", ""), spec.get("chat_id", ""),
                    spec.get("sessions_db", ""),
                    _dumps(spec["check"]), _dumps(spec["condition"]), _dumps(spec["action"]),
                    int(spec["interval_seconds"]), spec.get("max_checks"), expires_at,
                    1 if spec.get("repeat") else 0, STATUS_ACTIVE, now, now, next_check,
                    1 if has_initial_value else 0, _dumps(initial_value),
                    None if initial_met is None else int(bool(initial_met)),
                    _dumps(initial_reference),
                ),
            )
        self.add_event(monitor_id, "registered", spec["name"], {"next_check_at": next_check})
        return self.get_monitor(monitor_id)

    @staticmethod
    def _monitor_from_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["check"] = _loads(data.pop("check_json"), {})
        data["condition"] = _loads(data.pop("condition_json"), {})
        data["action"] = _loads(data.pop("action_json"), {})
        data["last_value"] = _loads(data.pop("last_value_json"), None)
        data["reference"] = _loads(data.pop("reference_json"), None)
        data["has_value"] = bool(data["has_value"])
        data["repeat"] = bool(data["repeat"])
        data["last_met"] = None if data["last_met"] is None else bool(data["last_met"])
        return data

    def get_monitor(self, monitor_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        return self._monitor_from_row(row) if row else None

    def list_monitors(self, statuses: tuple[str, ...] | None = None, project: str | None = None) -> list[dict]:
        query = "SELECT * FROM monitors"
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._monitor_from_row(row) for row in rows]

    def due_monitors(self, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM monitors WHERE status = ? AND next_check_at <= ? ORDER BY next_check_at",
                (STATUS_ACTIVE, now),
            ).fetchall()
        return [self._monitor_from_row(row) for row in rows]

    def next_due_at(self) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(next_check_at) AS at FROM monitors WHERE status = ?", (STATUS_ACTIVE,)
            ).fetchone()
        return row["at"] if row and row["at"] is not None else None

    def count_active(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM monitors WHERE status = ?", (STATUS_ACTIVE,)
            ).fetchone()
        return int(row["n"])

    def record_check(self, monitor_id: str, *, value: Any, met: bool | None, reference: Any,
                     next_check_at: float | None, fired: bool) -> None:
        now = self.clock()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE monitors SET
                    check_count = check_count + 1,
                    fire_count = fire_count + ?,
                    consecutive_errors = 0,
                    last_error = '',
                    has_value = 1,
                    last_value_json = ?,
                    last_met = ?,
                    reference_json = ?,
                    last_check_at = ?,
                    last_fired_at = CASE WHEN ? THEN ? ELSE last_fired_at END,
                    next_check_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    1 if fired else 0, _dumps(value), None if met is None else int(bool(met)),
                    _dumps(reference), now, 1 if fired else 0, now, next_check_at, now, monitor_id,
                ),
            )

    def record_check_error(self, monitor_id: str, error: str, next_check_at: float | None) -> int:
        """Count a failed check; returns the number of consecutive failures."""
        now = self.clock()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE monitors SET
                    check_count = check_count + 1,
                    consecutive_errors = consecutive_errors + 1,
                    last_error = ?, last_check_at = ?, next_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], now, next_check_at, now, monitor_id),
            )
            row = conn.execute(
                "SELECT consecutive_errors FROM monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
        return int(row["consecutive_errors"]) if row else 0

    def set_status(self, monitor_id: str, status: str, reason: str = "") -> bool:
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown monitor status '{status}'.")
        now = self.clock()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE monitors SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                (status, reason[:500], now, monitor_id),
            )
            changed = cursor.rowcount > 0
        if changed:
            self.add_event(monitor_id, "status", status, {"reason": reason})
        return changed

    def resume_monitor(self, monitor_id: str) -> bool:
        """Reactivate a paused monitor; its next check is one interval away."""
        monitor = self.get_monitor(monitor_id)
        if monitor is None or monitor["status"] != STATUS_PAUSED:
            return False
        now = self.clock()
        with self._connect() as conn:
            conn.execute(
                "UPDATE monitors SET status = ?, status_reason = '', next_check_at = ?, updated_at = ? WHERE id = ?",
                (STATUS_ACTIVE, now + monitor["interval_seconds"], now, monitor_id),
            )
        self.add_event(monitor_id, "status", STATUS_ACTIVE, {"reason": "resumed"})
        return True

    def delete_monitor(self, monitor_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            conn.execute("DELETE FROM monitor_events WHERE monitor_id = ?", (monitor_id,))
        return cursor.rowcount > 0

    def add_event(self, monitor_id: str, kind: str, message: str = "", detail: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO monitor_events (monitor_id, at, kind, message, detail_json) VALUES (?,?,?,?,?)",
                (monitor_id, self.clock(), kind, str(message)[:2000], _dumps(detail or {})),
            )
            conn.execute(
                """
                DELETE FROM monitor_events WHERE monitor_id = ? AND id NOT IN (
                    SELECT id FROM monitor_events WHERE monitor_id = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (monitor_id, monitor_id, MAX_EVENTS_PER_MONITOR),
            )

    def list_events(self, monitor_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM monitor_events WHERE monitor_id = ? ORDER BY id DESC LIMIT ?",
                (monitor_id, int(limit)),
            ).fetchall()
        events = []
        for row in rows:
            data = dict(row)
            data["detail"] = _loads(data.pop("detail_json"), {})
            events.append(data)
        return events

    # ── Wake requests ───────────────────────────────────────────────────────

    def enqueue_wake(self, monitor: dict, prompt: str, display: str, *, chat_id: str | None = None,
                     new_chat: bool = False) -> int:
        """Queue a wake-up of *chat_id*, by default the monitor's own chat.

        Raises ValueError without a chat: a front-end given an empty chat would
        run the turn in whichever chat it has open.
        """
        chat = monitor.get("chat_id", "") if chat_id is None else chat_id
        if not str(chat or "").strip():
            raise ValueError(f"The wake-up of monitor {monitor.get('id', '?')} names no chat to run in.")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO wake_requests (
                    monitor_id, monitor_name, project, project_path, chat_id, sessions_db,
                    prompt, display, created_at, status, new_chat
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    monitor["id"], monitor.get("name", ""), monitor.get("project", ""),
                    monitor.get("project_path", ""),
                    chat,
                    monitor.get("sessions_db", ""), prompt, display, self.clock(), WAKE_PENDING,
                    1 if new_chat else 0,
                ),
            )
            return int(cursor.lastrowid)

    def get_wake(self, wake_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM wake_requests WHERE id = ?", (int(wake_id),)).fetchone()
        return dict(row) if row else None

    def list_wakes(self, statuses: tuple[str, ...] = (WAKE_PENDING,), project: str | None = None) -> list[dict]:
        query = f"SELECT * FROM wake_requests WHERE status IN ({','.join('?' for _ in statuses)})"
        params: list[Any] = list(statuses)
        if project is not None:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def claim_wake(self, wake_id: int, holder: str, pid: int) -> bool:
        """Take a pending wake-up for one process. Exactly one caller wins."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE wake_requests SET status = ?, claimed_by = ?, claimed_pid = ?, claimed_at = ?
                WHERE id = ? AND status = ?
                """,
                (WAKE_CLAIMED, holder, int(pid), self.clock(), int(wake_id), WAKE_PENDING),
            )
            return cursor.rowcount == 1

    def fail_unrunnable_wakes(self, holder: str, pid: int) -> int:
        """Fail the pending wake-ups that name no chat; returns how many.

        Only code from before ``enqueue_wake`` refused them could have queued
        one; it is closed with a diagnostic instead of being handed to a
        front-end that would run it in another chat.
        """
        failed = 0
        for wake in self.list_wakes((WAKE_PENDING,)):
            if str(wake.get("chat_id") or "").strip():
                continue
            if self.claim_wake(wake["id"], holder, pid):
                self.finish_wake(wake["id"], WAKE_FAILED, "the wake-up names no chat to run in")
                failed += 1
        return failed

    def set_wake_pid(self, wake_id: int, pid: int) -> None:
        """Hand a claimed wake-up to the process that actually runs the turn."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE wake_requests SET claimed_pid = ? WHERE id = ? AND status = ?",
                (int(pid), int(wake_id), WAKE_CLAIMED),
            )

    def release_wake(self, wake_id: int, holder: str) -> bool:
        """Give a claimed wake-up back, e.g. when the turn could not start."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE wake_requests SET status = ?, claimed_by = '', claimed_pid = NULL, claimed_at = NULL
                WHERE id = ? AND status = ? AND claimed_by = ?
                """,
                (WAKE_PENDING, int(wake_id), WAKE_CLAIMED, holder),
            )
            return cursor.rowcount == 1

    def finish_wake(self, wake_id: int, status: str, error: str = "") -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE wake_requests SET status = ?, finished_at = ?, error = ? WHERE id = ? AND status = ?",
                (status, self.clock(), error[:2000], int(wake_id), WAKE_CLAIMED),
            )
            return cursor.rowcount == 1

    # ── Pending inputs ──────────────────────────────────────────────────────

    def add_input(self, request: dict, *, wake_request_id: int | None, project: str, project_path: str,
                  chat_id: str, monitor_name: str, pid: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_inputs (
                    id, wake_request_id, project, project_path, chat_id, monitor_name,
                    request_json, holder_pid, created_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(request["id"]), wake_request_id, project, project_path, chat_id, monitor_name,
                    _dumps(request), int(pid), self.clock(), INPUT_WAITING,
                ),
            )

    @staticmethod
    def _input_from_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["request"] = _loads(data.pop("request_json"), {})
        return data

    def get_input(self, input_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pending_inputs WHERE id = ?", (str(input_id),)).fetchone()
        return self._input_from_row(row) if row else None

    def list_inputs(self, status: str = INPUT_WAITING, project: str | None = None) -> list[dict]:
        query = "SELECT * FROM pending_inputs WHERE status = ?"
        params: list[Any] = [status]
        if project is not None:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY created_at"
        with self._connect() as conn:
            return [self._input_from_row(row) for row in conn.execute(query, params).fetchall()]

    def answer_input(self, input_id: str, answer: str) -> bool:
        """Record an answer. False when nothing is waiting under that id."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE pending_inputs SET status = ?, answer = ?, answered_at = ? WHERE id = ? AND status = ?",
                (INPUT_ANSWERED, str(answer), self.clock(), str(input_id), INPUT_WAITING),
            )
            return cursor.rowcount == 1

    def close_input(self, input_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending_inputs SET status = ? WHERE id = ? AND status = ?",
                (INPUT_CLOSED, str(input_id), INPUT_WAITING),
            )

    # ── Notifications ───────────────────────────────────────────────────────

    def enqueue_notification(self, title: str, body: str = "", *, urgency: str = "normal",
                             project: str = "", project_path: str = "", chat_id: str = "") -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO notifications (title, body, urgency, project, project_path, chat_id, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (title[:300], body[:2000], urgency, project, project_path, chat_id, self.clock()),
            )
            return int(cursor.lastrowid)

    def undelivered_notifications(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE delivered_at IS NULL ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification(self, notification_id: int, backend: str, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notifications SET delivered_at = ?, backend = ?, error = ? WHERE id = ?",
                (self.clock(), backend, error[:1000], int(notification_id)),
            )
            # Keep the last few hundred for diagnostics.
            conn.execute(
                "DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 300)"
            )

    def recent_notifications(self, limit: int = 30) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Hosts ───────────────────────────────────────────────────────────────

    def touch_host(self, kind: str, pid: int, project: str = "", project_path: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hosts (kind, pid, project, project_path, seen_at) VALUES (?,?,?,?,?)
                ON CONFLICT(kind, pid) DO UPDATE SET
                    project = excluded.project, project_path = excluded.project_path, seen_at = excluded.seen_at
                """,
                (kind, int(pid), project, project_path, self.clock()),
            )

    def remove_host(self, kind: str, pid: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM hosts WHERE kind = ? AND pid = ?", (kind, int(pid)))

    def live_hosts(self, kind: str, max_age: float) -> list[dict]:
        cutoff = self.clock() - max_age
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hosts WHERE kind = ? AND seen_at >= ?", (kind, cutoff)
            ).fetchall()
        return [dict(row) for row in rows if pid_alive(int(row["pid"]))]

    # ── Turn leases ─────────────────────────────────────────────────────────

    def acquire_lease(self, project: str, chat_id: str, pid: int, holder: str) -> dict | None:
        """Take the chat for one process. Returns the other holder when refused.

        A lease whose process is gone is stale and is taken over: a crashed
        turn must not lock its chat forever.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM turn_leases WHERE project = ? AND chat_id = ?", (project, chat_id)
            ).fetchone()
            if row is not None and int(row["pid"]) != int(pid) and pid_alive(int(row["pid"])):
                return dict(row)
            conn.execute(
                "INSERT OR REPLACE INTO turn_leases (project, chat_id, pid, holder, acquired_at) VALUES (?,?,?,?,?)",
                (project, chat_id, int(pid), holder, self.clock()),
            )
        return None

    def release_lease(self, project: str, chat_id: str, pid: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM turn_leases WHERE project = ? AND chat_id = ? AND pid = ?",
                (project, chat_id, int(pid)),
            )

    def lease_holder(self, project: str, chat_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM turn_leases WHERE project = ? AND chat_id = ?", (project, chat_id)
            ).fetchone()
        if row is None or not pid_alive(int(row["pid"])):
            return None
        return dict(row)

    # ── Open requests ───────────────────────────────────────────────────────

    def add_open_request(self, project: str, project_path: str, chat_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO open_requests (project, project_path, chat_id, created_at) VALUES (?,?,?,?)",
                (project, project_path, chat_id, self.clock()),
            )
            return int(cursor.lastrowid)

    def take_open_request(self, max_age: float = 300.0) -> dict | None:
        """The newest unconsumed request, marking every older one consumed."""
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM open_requests WHERE consumed_at IS NULL AND created_at >= ? ORDER BY id DESC LIMIT 1",
                (now - max_age,),
            ).fetchone()
            conn.execute("UPDATE open_requests SET consumed_at = ? WHERE consumed_at IS NULL", (now,))
        return dict(row) if row else None

    # ── Settings ────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return _loads(row["value"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, _dumps(value))
            )
