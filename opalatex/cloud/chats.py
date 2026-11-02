"""Make a project's conversations part of the synced project.

A project's files live in its directory, but its chats, message history, agent
activity and per-chat memory live in ``~/.opalatex/sessions.db`` — a single
SQLite database shared by *every* project on the machine. That database cannot
be mirrored as-is: it would copy unrelated projects' conversations into this
project's cloud folder, and reconciling two machines' copies of a WAL-mode
SQLite file is not something a file-level sync can do safely.

So the rows belonging to one project are exported to a portable JSON document
inside the project (``.opalatex/session/chats.json``), which syncs like any
other file, and merged back on the way in.

Merge policy
------------
Conversation data is append-mostly, so the merge is a union keyed by content:
messages and activity present on either side survive. Deletions therefore do not
propagate — a chat cleared on one machine comes back from the other. That is the
deliberate trade: for a history the user cannot reconstruct, resurrecting a
message is a nuisance while dropping one is data loss.

Project-level settings use row-level last-writer-wins on ``updated_at``. Fields
that only make sense on one machine — the project's path, its git root — are
never exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from .atomic import discard_file, replace_file
from .state import CHATS_EXPORT_REL_PATH, DEVICE_JOURNALS_REL_DIR, is_device_journal

# Columns of `projects` that describe the work rather than the machine. The
# path columns are excluded on purpose: importing them would point a project at
# a directory that does not exist here.
PORTABLE_PROJECT_FIELDS = (
    "description",
    "core_memory",
    "main_file",
    "mode",
    "model",
    "worker_model",
    "model_params",
    "worker_model_params",
    "use_shared_memory",
    "compile_on_save_partial",
    "compile_on_save_full",
)

EXPORT_VERSION = 1

# Columns of `project_chats` that describe this machine's working state and must
# never be written from an incoming document.
_MACHINE_LOCAL_CHAT_FIELDS = frozenset({"project", "external_turns"})

# A device journal names itself, so a stray JSON file dropped into the devices
# folder is not mistaken for conversation history.
DEVICE_JOURNAL_KIND = "opalatex.device-journal"
DEVICE_JOURNAL_VERSION = 1

# What a device journal may say about a chat. Core memory and agent state are
# the desktop orchestrator's working state; a journal only creates chats.
_JOURNAL_CHAT_FIELDS = ("id", "name", "created_at")

_EXTERNAL_TURN_ROLES = ("user", "assistant")


@dataclass
class MergeStats:
    chats_added: int = 0
    messages_added: int = 0
    activity_added: int = 0
    settings_applied: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chats_added": self.chats_added,
            "messages_added": self.messages_added,
            "activity_added": self.activity_added,
            "settings_applied": self.settings_applied,
            "notes": self.notes,
        }

    @property
    def changed(self) -> bool:
        return bool(
            self.chats_added or self.messages_added or self.activity_added or self.settings_applied
        )

    def absorb(self, other: "MergeStats") -> None:
        """Add another merge's counts to this one (several documents, one pass)."""
        self.chats_added += other.chats_added
        self.messages_added += other.messages_added
        self.activity_added += other.activity_added
        self.settings_applied = self.settings_applied or other.settings_applied
        self.notes.extend(other.notes)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _default_db_path() -> str:
    from ..config import DEFAULT_DB_PATH

    return DEFAULT_DB_PATH


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Read a table's actual columns.

    The schema grows by additive migrations, and a database written by an older
    build will be missing the newest ones. Selecting explicitly-named columns
    that may not exist yet would make the export fail on exactly the machine
    most likely to be syncing for the first time.
    """
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _message_key(row: dict[str, Any]) -> str:
    """Stable identity for one message.

    ``client_message_id`` is authoritative when present — the front-end already
    uses it to deduplicate. Otherwise the content itself is the key, so replaying
    the same export twice cannot double a conversation.
    """
    client_id = str(row.get("client_message_id", "") or "")
    if client_id:
        return f"cid:{client_id}"
    digest = hashlib.sha256()
    for field_name in ("chat_id", "timestamp", "role", "content", "attachments"):
        digest.update(str(row.get(field_name, "")).encode("utf-8", "replace"))
        digest.update(b"\x1f")
    return f"h:{digest.hexdigest()}"


def _activity_key(row: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for field_name in ("chat_id", "timestamp", "event", "agent", "content", "payload"):
        digest.update(str(row.get(field_name, "")).encode("utf-8", "replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()


# ─── Export ───────────────────────────────────────────────────────────────────

def export_project(project_name: str, db_path: Optional[str] = None) -> dict[str, Any]:
    """Build the portable document for one project.

    The output is deterministic: rows are sorted and the machine-local
    autoincrement ids are dropped. An unchanged conversation therefore produces
    byte-identical JSON, so the sync engine sees no change and uploads nothing.
    """
    path = db_path or _default_db_path()
    if not os.path.isfile(path):
        return {"version": EXPORT_VERSION, "project": project_name, "chats": [], "messages": [], "activity": []}

    with _connect(path) as conn:
        project_columns = _columns(conn, "projects")
        settings: dict[str, Any] = {}
        updated_at = ""
        if project_columns:
            row = conn.execute(
                "SELECT * FROM projects WHERE name = ?", (project_name,)
            ).fetchone()
            if row is not None:
                updated_at = str(row["updated_at"] if "updated_at" in row.keys() else "")
                settings = {
                    field_name: row[field_name]
                    for field_name in PORTABLE_PROJECT_FIELDS
                    if field_name in project_columns
                }

        chat_columns = _columns(conn, "project_chats")
        chats = []
        if chat_columns:
            wanted = [c for c in ("id", "name", "created_at", "core_memory", "agent_state", "context_usage") if c in chat_columns]
            for row in conn.execute(
                f"SELECT {', '.join(wanted)} FROM project_chats WHERE project = ? ORDER BY id",
                (project_name,),
            ):
                chats.append({key: row[key] for key in wanted})

        history_columns = _columns(conn, "project_history")
        messages = []
        if history_columns:
            wanted = [
                c
                for c in (
                    "chat_id", "timestamp", "role", "content", "client_message_id",
                    "attachments", "deleted_at", "superseded_by",
                )
                if c in history_columns
            ]
            for row in conn.execute(
                f"SELECT {', '.join(wanted)} FROM project_history WHERE project = ? ORDER BY id",
                (project_name,),
            ):
                messages.append({key: row[key] for key in wanted})

        activity_columns = _columns(conn, "project_activity")
        activity = []
        if activity_columns:
            wanted = [
                c
                for c in ("chat_id", "timestamp", "event", "agent", "content", "payload", "deleted_at")
                if c in activity_columns
            ]
            for row in conn.execute(
                f"SELECT {', '.join(wanted)} FROM project_activity WHERE project = ? ORDER BY id",
                (project_name,),
            ):
                activity.append({key: row[key] for key in wanted})

    messages.sort(key=lambda row: (str(row.get("chat_id", "")), str(row.get("timestamp", "")), _message_key(row)))
    activity.sort(key=lambda row: (str(row.get("chat_id", "")), str(row.get("timestamp", "")), _activity_key(row)))
    chats.sort(key=lambda row: str(row.get("id", "")))

    return {
        "version": EXPORT_VERSION,
        "project": project_name,
        "updated_at": updated_at,
        "settings": settings,
        "chats": chats,
        "messages": messages,
        "activity": activity,
    }


def export_path(project_path: str) -> str:
    return os.path.join(os.path.abspath(project_path), *CHATS_EXPORT_REL_PATH.split("/"))


# Characters handed to the file per write while the export is serialized.
_EXPORT_WRITE_CHARS = 1 << 16


def _serialized_chunks(payload: dict[str, Any]):
    """The export's JSON text, in pieces of about `_EXPORT_WRITE_CHARS`.

    The text is exactly what ``json.dumps(payload, indent=2, ensure_ascii=False,
    sort_keys=True)`` returns. It is produced incrementally because a single
    ``dumps`` call holds the interpreter until it returns: since Python 3.14 the
    C encoder also handles ``indent``, and on a 309 MB export it froze every
    thread of the app — the HTTP server, the terminal, the window — for about
    four seconds per sync pass. ``iterencode`` (not one-shot) runs the Python
    encoder, which lets other threads run between its pieces.
    """
    encoder = json.JSONEncoder(indent=2, ensure_ascii=False, sort_keys=True)
    pending: list[str] = []
    size = 0
    for piece in encoder.iterencode(payload):
        pending.append(piece)
        size += len(piece)
        if size >= _EXPORT_WRITE_CHARS:
            yield "".join(pending)
            pending.clear()
            size = 0
    if pending:
        yield "".join(pending)


def write_export(project_path: str, payload: dict[str, Any]) -> bool:
    """Write the export, returning True only when the bytes actually changed.

    Rewriting an identical file would bump its mtime, and the scanner would then
    re-hash and re-upload a document that says exactly what the remote already
    holds — on every single sync pass.

    The new text is compared with the current file piece by piece while it is
    written to a temporary file, so neither document is ever held whole in
    memory; when they turn out identical the temporary file is discarded and the
    current one is left untouched.
    """
    target = export_path(project_path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    try:
        current = open(target, "r", encoding="utf-8")
    except OSError:
        current = None
    identical = current is not None
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".chats-", suffix=".tmp", delete=False
    )
    try:
        try:
            with handle:
                for chunk in _serialized_chunks(payload):
                    if identical:
                        try:
                            identical = current.read(len(chunk)) == chunk
                        except (OSError, UnicodeDecodeError):
                            identical = False
                    handle.write(chunk)
                if identical:
                    try:
                        identical = current.read(1) == ""
                    except (OSError, UnicodeDecodeError):
                        identical = False
                if not identical:
                    handle.flush()
                    os.fsync(handle.fileno())
        finally:
            if current is not None:
                current.close()
        if not identical:
            replace_file(handle.name, target)
    except BaseException:
        try:
            discard_file(handle.name)
        except OSError:
            pass
        raise
    if identical:
        # Same handling as a failed write above: a temp file that cannot be
        # removed stays behind, and its name pattern is excluded from the scan.
        try:
            discard_file(handle.name)
        except OSError:
            pass
        return False
    return True


def read_export(project_path: str) -> Optional[dict[str, Any]]:
    return read_export_from(export_path(project_path))


def read_export_from(path: str) -> Optional[dict[str, Any]]:
    """Read an export document from an explicit path.

    Used for the copy a conflict parks beside the canonical file, which holds
    the other machine's history and has to be merged from where it landed.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


# ─── Import ───────────────────────────────────────────────────────────────────

def merge_into_database(
    project_name: str,
    payload: dict[str, Any],
    db_path: Optional[str] = None,
    *,
    adopt_settings: bool = False,
) -> MergeStats:
    """Merge a document produced by :func:`export_project` into the local store.

    `adopt_settings` takes the incoming project settings unconditionally instead
    of by last-writer-wins. It exists for the one case where the timestamp
    comparison gives the wrong answer: a project being cloned onto a second
    machine has a row created seconds ago, so it is always "newer" than the
    export, and the model, mode and main file the user actually chose would
    never be adopted. It is only ever correct on a project with no history of
    its own — there is nothing local to lose.
    """
    stats = MergeStats()
    if not isinstance(payload, dict):
        return stats
    version = int(payload.get("version", 0) or 0)
    if version > EXPORT_VERSION:
        stats.notes.append(
            f"The synced conversation file was written by a newer OpalaTex "
            f"(format {version}); it was left untouched."
        )
        return stats

    path = db_path or _default_db_path()
    if not os.path.isfile(path):
        return stats

    with _connect(path) as conn:
        exists = conn.execute(
            "SELECT updated_at FROM projects WHERE name = ?", (project_name,)
        ).fetchone()
        if exists is None:
            # The project row is created by ProjectStore when the project is
            # opened. Writing history for a project that does not exist locally
            # would violate the foreign key and orphan the rows.
            stats.notes.append("The project does not exist in this installation yet.")
            return stats

        # Read before the chats are merged: a chat this document creates arrives
        # with the history it describes, so only chats that already had a saved
        # working state here can fall behind their own visible history.
        stateful_chats = _chats_with_saved_state(conn, project_name)
        stats.chats_added = _merge_chats(conn, project_name, payload.get("chats") or [])
        stats.messages_added = _merge_messages(
            conn, project_name, payload.get("messages") or [], queue_for=stateful_chats
        )
        stats.activity_added = _merge_activity(conn, project_name, payload.get("activity") or [])
        stats.settings_applied = _merge_settings(
            conn,
            project_name,
            payload.get("settings") or {},
            str(payload.get("updated_at", "") or ""),
            "" if adopt_settings else str(exists["updated_at"] or ""),
        )
        conn.commit()
    return stats


def _merge_chats(conn: sqlite3.Connection, project_name: str, chats: list) -> int:
    columns = _columns(conn, "project_chats")
    if not columns:
        return 0
    known = {
        str(row["id"])
        for row in conn.execute("SELECT id FROM project_chats WHERE project = ?", (project_name,))
    }
    added = 0
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        chat_id = str(chat.get("id", "") or "")
        if not chat_id or chat_id in known:
            # An existing chat keeps this machine's row. Its messages still
            # merge below; only the chat's own metadata is left alone, because
            # core memory and agent state are working state that a blind
            # overwrite could roll back mid-conversation.
            continue
        fields = {
            key: chat.get(key)
            for key in chat
            if key in columns and key not in _MACHINE_LOCAL_CHAT_FIELDS
        }
        fields["id"] = chat_id
        fields["project"] = project_name
        fields.setdefault("name", chat_id)
        fields.setdefault("created_at", "")
        names = sorted(fields)
        conn.execute(
            f"INSERT OR IGNORE INTO project_chats ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [fields[name] for name in names],
        )
        known.add(chat_id)
        added += 1
    return added


def _chats_with_saved_state(conn: sqlite3.Connection, project_name: str) -> set[str]:
    """Chats whose orchestrator will restore `agent_state` instead of seeding.

    Empty when this database has no `external_turns` column to queue into.
    """
    columns = _columns(conn, "project_chats")
    if "external_turns" not in columns or "agent_state" not in columns:
        return set()
    stateful: set[str] = set()
    for row in conn.execute(
        "SELECT id, agent_state FROM project_chats WHERE project = ?", (project_name,)
    ):
        try:
            state = json.loads(row["agent_state"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(state, dict) and state.get("internal_history"):
            stateful.add(str(row["id"]))
    return stateful


def _queue_external_turns(conn: sqlite3.Connection, project_name: str, arrivals: dict) -> None:
    """Append merged turns to the queue the orchestrator drains on restore.

    The visible history is what the user reads; the orchestrator, however,
    restores its context from `agent_state`. Without the queue a turn made on
    another device shows up in the chat and is unknown to the agent that
    answers the next message there.
    """
    for chat_id, turns in arrivals.items():
        if not turns:
            continue
        row = conn.execute(
            "SELECT external_turns FROM project_chats WHERE project = ? AND id = ?",
            (project_name, chat_id),
        ).fetchone()
        if row is None:
            continue
        try:
            queued = json.loads(row["external_turns"] or "[]")
        except (TypeError, ValueError):
            queued = []
        if not isinstance(queued, list):
            queued = []
        turns.sort(key=lambda turn: turn["timestamp"])
        queued.extend(turns)
        conn.execute(
            "UPDATE project_chats SET external_turns = ? WHERE project = ? AND id = ?",
            (json.dumps(queued, ensure_ascii=False), project_name, chat_id),
        )


def _merge_messages(
    conn: sqlite3.Connection,
    project_name: str,
    messages: list,
    queue_for: frozenset[str] | set[str] = frozenset(),
) -> int:
    columns = _columns(conn, "project_history")
    if not columns:
        return 0
    existing_keys = set()
    wanted = [c for c in ("chat_id", "timestamp", "role", "content", "client_message_id", "attachments") if c in columns]
    for row in conn.execute(
        f"SELECT {', '.join(wanted)} FROM project_history WHERE project = ?", (project_name,)
    ):
        existing_keys.add(_message_key({key: row[key] for key in wanted}))

    valid_chats = {
        str(row["id"])
        for row in conn.execute("SELECT id FROM project_chats WHERE project = ?", (project_name,))
    }

    added = 0
    arrivals: dict[str, list[dict[str, str]]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        key = _message_key(message)
        if key in existing_keys:
            continue
        chat_id = str(message.get("chat_id", "") or "")
        if chat_id and valid_chats and chat_id not in valid_chats:
            # The message belongs to a chat this machine does not have and the
            # export did not carry. Inserting it would hide it from every chat.
            continue
        fields = {name: message.get(name) for name in message if name in columns}
        fields["project"] = project_name
        names = sorted(fields)
        conn.execute(
            f"INSERT OR IGNORE INTO project_history ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [fields[name] for name in names],
        )
        existing_keys.add(key)
        added += 1
        if (
            chat_id in queue_for
            and message.get("role") in _EXTERNAL_TURN_ROLES
            and isinstance(message.get("content"), str)
            and message.get("content")
            and not message.get("deleted_at")
            and not message.get("superseded_by")
        ):
            arrivals.setdefault(chat_id, []).append({
                "role": message["role"],
                "content": message["content"],
                "timestamp": str(message.get("timestamp", "") or ""),
            })
    if arrivals:
        _queue_external_turns(conn, project_name, arrivals)
    return added


def _merge_activity(conn: sqlite3.Connection, project_name: str, activity: list) -> int:
    columns = _columns(conn, "project_activity")
    if not columns:
        return 0
    wanted = [c for c in ("chat_id", "timestamp", "event", "agent", "content", "payload") if c in columns]
    existing_keys = {
        _activity_key({key: row[key] for key in wanted})
        for row in conn.execute(
            f"SELECT {', '.join(wanted)} FROM project_activity WHERE project = ?", (project_name,)
        )
    }
    added = 0
    for item in activity:
        if not isinstance(item, dict):
            continue
        key = _activity_key(item)
        if key in existing_keys:
            continue
        fields = {name: item.get(name) for name in item if name in columns}
        fields["project"] = project_name
        names = sorted(fields)
        conn.execute(
            f"INSERT OR IGNORE INTO project_activity ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [fields[name] for name in names],
        )
        existing_keys.add(key)
        added += 1
    return added


def _merge_settings(
    conn: sqlite3.Connection,
    project_name: str,
    settings: dict,
    incoming_updated_at: str,
    local_updated_at: str,
) -> bool:
    """Apply portable project settings when the incoming row is the newer one."""
    if not settings or not incoming_updated_at:
        return False
    # String comparison is correct here: every writer stores ISO-8601 UTC
    # timestamps, which sort chronologically as text.
    if local_updated_at and incoming_updated_at <= local_updated_at:
        return False
    columns = _columns(conn, "projects")
    applicable = {
        name: value
        for name, value in settings.items()
        if name in PORTABLE_PROJECT_FIELDS and name in columns
    }
    if not applicable:
        return False
    names = sorted(applicable)
    conn.execute(
        f"UPDATE projects SET {', '.join(f'{name} = ?' for name in names)} WHERE name = ?",
        [applicable[name] for name in names] + [project_name],
    )
    return True


# ─── Convenience wrappers used by the sync service ────────────────────────────

def _file_signature(path: str) -> Optional[str]:
    """Identity of the file on disk, or None when there is none.

    A download that replaces the export, or an edit by hand, changes its inode,
    size or modification time even when the history in the database did not
    move. The format version is part of it, so a build that writes another
    format never trusts a mark left by this one.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return f"v{EXPORT_VERSION}:{info.st_size}:{info.st_mtime_ns}:{info.st_ino}"


def history_revision(project_name: str, db_path: Optional[str] = None) -> Optional[int]:
    """The project's history counter (`project._install_history_revisions`).

    None when the database does not keep one, which only happens before
    `ProjectStore` has initialized it; the export is then always rebuilt.
    """
    path = db_path or _default_db_path()
    if not os.path.isfile(path):
        return None
    with _connect(path) as conn:
        try:
            row = conn.execute(
                "SELECT revision FROM project_revisions WHERE project = ?", (project_name,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return int(row["revision"]) if row is not None else 0


def _export_mark(conn: sqlite3.Connection, project_name: str, target: str) -> Optional[tuple[int, str]]:
    try:
        row = conn.execute(
            "SELECT revision, signature FROM project_export_marks WHERE project = ? AND export_path = ?",
            (project_name, target),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return (int(row["revision"]), str(row["signature"])) if row is not None else None


def _record_export_mark(db_path: str, project_name: str, target: str, revision: int) -> None:
    signature = _file_signature(target)
    with _connect(db_path) as conn:
        if signature is None:
            conn.execute(
                "DELETE FROM project_export_marks WHERE project = ? AND export_path = ?",
                (project_name, target),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO project_export_marks (project, export_path, revision, signature) "
                "VALUES (?, ?, ?, ?)",
                (project_name, target, revision, signature),
            )
        conn.commit()


def refresh_export(project_name: str, project_path: str, db_path: Optional[str] = None) -> bool:
    """Regenerate the on-disk export. True when the file changed.

    Skipped outright when the history has not moved since the file on disk was
    built from it: rebuilding a large export only to find it byte-identical cost
    a measured 8.5 s of reading and 4.5 s of serialization per pass. The counter
    is read *before* the rows, so a write landing during the export leaves the
    mark behind the data and the next pass rebuilds — never the reverse.
    """
    path = db_path or _default_db_path()
    target = export_path(project_path)
    revision = history_revision(project_name, path)
    if revision is not None:
        with _connect(path) as conn:
            mark = _export_mark(conn, project_name, target)
        signature = _file_signature(target)
        if mark is not None and signature is not None and mark == (revision, signature):
            return False
    changed = write_export(project_path, export_project(project_name, path))
    if revision is not None:
        _record_export_mark(path, project_name, target, revision)
    return changed


def apply_export(
    project_name: str,
    project_path: str,
    db_path: Optional[str] = None,
    *,
    adopt_settings: bool = False,
) -> MergeStats:
    """Merge whatever the on-disk export holds back into the local database."""
    payload = read_export(project_path)
    if payload is None:
        return MergeStats()
    return merge_into_database(project_name, payload, db_path, adopt_settings=adopt_settings)


# ─── Device journals ──────────────────────────────────────────────────────────

def device_journals_dir(project_path: str) -> str:
    return os.path.join(os.path.abspath(project_path), *DEVICE_JOURNALS_REL_DIR.split("/"))


def merge_device_journal(
    project_name: str, payload: Any, db_path: Optional[str] = None
) -> MergeStats:
    """Fold one device's journal into the local database.

    A journal is the export's shape restricted to what a device may add: new
    chats (identity only), messages and activity. It never carries project
    settings or a chat's working state, and anything of that kind in it is
    ignored rather than applied — a phone must not be able to change the model
    or roll back an orchestrator's memory on this machine.
    """
    stats = MergeStats()
    if not isinstance(payload, dict) or payload.get("kind") != DEVICE_JOURNAL_KIND:
        stats.notes.append("A file in the devices folder is not a device journal; it was ignored.")
        return stats
    version = int(payload.get("version", 0) or 0)
    if version > DEVICE_JOURNAL_VERSION:
        stats.notes.append(
            f"A device journal was written in a newer format ({version}); it was left untouched."
        )
        return stats
    chats_in = [
        {key: chat.get(key) for key in _JOURNAL_CHAT_FIELDS if key in chat}
        for chat in payload.get("chats") or []
        if isinstance(chat, dict)
    ]
    restricted = {
        "version": EXPORT_VERSION,
        "chats": chats_in,
        "messages": payload.get("messages") or [],
        "activity": payload.get("activity") or [],
    }
    return merge_into_database(project_name, restricted, db_path)


def read_device_journal(path: str) -> Optional[dict[str, Any]]:
    return read_export_from(path)


def apply_device_journals(
    project_name: str,
    project_path: str,
    db_path: Optional[str] = None,
    rel_paths: Optional[list[str]] = None,
) -> MergeStats:
    """Merge device journals from disk: the listed ones, or every one present."""
    total = MergeStats()
    if rel_paths is None:
        directory = device_journals_dir(project_path)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return total
        rel_paths = [f"{DEVICE_JOURNALS_REL_DIR}/{name}" for name in names]
    for rel_path in rel_paths:
        if not is_device_journal(rel_path):
            continue
        payload = read_device_journal(os.path.join(os.path.abspath(project_path), *rel_path.split("/")))
        if payload is None:
            continue
        total.absorb(merge_device_journal(project_name, payload, db_path))
    return total
