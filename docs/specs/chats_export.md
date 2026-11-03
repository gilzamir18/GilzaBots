# Conversation files in a mirrored project — normative specification

Status: implemented. Export format version **1**, device journal format
version **1**.

A mirrored project carries its conversations as files, because the database
that holds them (`~/.gilzabots/sessions.db`) is shared by every project on a
machine and cannot be mirrored (see [cloud_sync.md](cloud_sync.md)). Two kinds
of file exist, and they have different writers:

| File | Written by | Read by |
| --- | --- | --- |
| `.gilzabots/session/chats.json` — the **export** | every desktop, after merging | desktops, the mobile app |
| `.gilzabots/session/devices/<device id>.json` — a **device journal** | exactly one device (the mobile app) | desktops, other devices |

This document is the contract between those writers. The normative sources are
[`gilzabots/cloud/chats.py`](../../gilzabots/cloud/chats.py) for the desktop and
[`mobile/src/project/journal.js`](../../mobile/src/project/journal.js) and
[`keys.js`](../../mobile/src/project/keys.js) for the app. Where this document
and the code disagree, the code is right and this document is a bug.

**MUST**, **MUST NOT**, **SHOULD** and **MAY** are used in the RFC 2119 sense.

## 1. Why two files, and why one writer each

The export is a union that desktops rewrite: each merges what arrived, then
publishes the result. A phone uploading its own copy of the same file would
race them, and Drive keeps whichever write lands second. So a device never
writes the export. It appends to a journal that only it writes, and desktops
fold journals into their database; the rows then reach every other computer
through the export. A single writer per file means nothing has to reconcile
concurrent writes.

## 2. Shared conventions

### 2.1 Timestamps

Every timestamp is the text Python's `datetime.now(timezone.utc).isoformat()`
writes: `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, six fractional digits, `+00:00`.

Writers MUST use exactly this form. Rows are ordered by comparing these strings
(reasoning is paired with answers by it, settings use last-writer-wins on it),
and JavaScript's `toISOString()` — three digits and `Z` — sorts wrongly against
them. `mobile/src/lib/timestamps.js` is the app's writer.

### 2.2 Row identity

Both files hold rows of the same tables. A row is the same row wherever it
appears, identified by a key both sides compute identically:

| Row | Key |
| --- | --- |
| message | `cid:<client_message_id>` when that is non-empty; otherwise `h:` + SHA-256 over `chat_id`, `timestamp`, `role`, `content`, `attachments` |
| activity | SHA-256 over `chat_id`, `timestamp`, `event`, `agent`, `content`, `payload` |

Each field is the value as text followed by U+001F; the concatenation is UTF-8
encoded and hashed; the digest is lowercase hex. A missing field is the empty
string. `mobile/src/project/__tests__/keys.test.js` pins three keys produced by
the Python implementation, and the two implementations were checked against
each other on a real 3,713-row export with no mismatch.

A device MUST set `client_message_id` (a random UUID) on every message it
writes, assistant messages included. Merging is a union by key, so the key is
also what keeps a row folded into the export from appearing twice on the phone.

## 3. The export — `chats.json`

```json
{
  "version": 1,
  "project": "<internal project name on the desktop>",
  "updated_at": "<projects.updated_at>",
  "settings": { "model": "openai/gpt-4o", "description": "…", "core_memory": "…", "…": "…" },
  "chats": [{ "id": "…", "name": "…", "created_at": "…", "core_memory": "…", "agent_state": "<json text>", "context_usage": "<json text>" }],
  "messages": [{ "chat_id": "…", "timestamp": "…", "role": "user|assistant|…", "content": "…", "client_message_id": "…", "attachments": "<json text>", "deleted_at": "", "superseded_by": "" }],
  "activity": [{ "chat_id": "…", "timestamp": "…", "event": "thought|reflection|stream_chunk|…", "agent": "…", "content": "…", "payload": "<json text>", "deleted_at": "" }]
}
```

- Serialized with sorted keys and two-space indentation, rows sorted, so an
  unchanged history is byte-identical and is not uploaded again.
- `settings` holds `PORTABLE_PROJECT_FIELDS` only. Machine-local columns (paths,
  the external-turns queue of §5) are never exported.
- `agent_state` is the desktop orchestrator's MemGPT working state
  (`internal_history`, `recursive_summary`) as JSON text. Readers MAY use the
  summary; they MUST NOT write it back.
- A reader that meets `version` > 1 MUST leave the file alone and say so.
- A message with a non-empty `deleted_at` or `superseded_by` is not part of the
  visible conversation.
- Reasoning is persisted as `thought` rows. A row holds a single token (rows
  written before segments existed) or a segment of consecutive reasoning of one
  agent (up to about 2,000 characters); a reader concatenates a message's rows in
  order and MUST NOT assume either size. A row whose payload has
  `"auxiliary": true` is a status line. A `thought` belongs to the first
  assistant message whose timestamp is not earlier than its own, and leftovers to
  the last assistant message (`ProjectStore.thought_windows`).

## 4. Device journals — `devices/<device id>.json`

```json
{
  "kind": "gilzabots.device-journal",
  "version": 1,
  "device": { "id": "<uuid>", "name": "Pixel 8" },
  "project": "<the export's project field, when known>",
  "chats":    [{ "id": "<uuid>", "name": "…", "created_at": "…" }],
  "messages": [{ "chat_id": "…", "timestamp": "…", "role": "user|assistant", "content": "…", "client_message_id": "<uuid>", "attachments": "[]" }],
  "activity": [{ "chat_id": "…", "timestamp": "…", "event": "thought", "agent": "mobile_agent", "content": "…", "payload": "<json text>" }]
}
```

### 4.1 Path

`.gilzabots/session/devices/` plus a file name that ends in `.json`, does not
start with `.`, and contains no `/` (`state.is_device_journal`). Anything else in
that folder is not a journal.

### 4.2 What a journal may say

| May | Must not |
| --- | --- |
| create a chat: `id`, `name`, `created_at` | carry project `settings` |
| add messages to any chat it created or that the export lists | carry a chat's `agent_state`, `core_memory` or `external_turns` |
| add activity rows | rename, delete or edit an existing row |

A desktop enforces the right-hand column by ignoring it, not by rejecting the
file (`chats.merge_device_journal`): chat fields beyond identity are dropped,
`settings` is never applied, and `kind` other than `gilzabots.device-journal` or
`version` > 1 leaves the file untouched with a note. A phone must not be able to
change the model a project runs, or roll back an orchestrator's memory.

### 4.3 Writing order within a turn

1. the user's message, before the model is called — a turn that fails still
   leaves the question in the history;
2. one `thought` row with the turn's reasoning and steps, timestamped after the
   question and before the answer, so the desktop pairs it with that answer;
3. the assistant's answer.

A failed turn stores no assistant message. Answering its question again
writes only steps 2 and 3: the question is already in the history, and a
second copy would be a second question.

A device that branches (retries an answered turn, edits a question) creates a
new chat and copies the messages up to that point into it, each with its own
`client_message_id` and its original timestamp, and each copied answer's
reasoning as one `thought` row at the answer's timestamp. That is §4.2's
"create a chat" and "add messages", nothing more.

### 4.4 Compaction

A device SHOULD drop from its journal every row whose key is in the export it
last read, and every chat the export lists. Those rows are durable without the
journal. Rows are only ever appended by the device, so compaction is safe
against a concurrent append as long as rows past the compacted snapshot are kept
(`mobile/src/project/session.js: pull`).

### 4.5 Uploading

Journal text is deterministic (sorted keys and rows, trailing newline), so an
unchanged journal is not uploaded. Uploads from one device are serialized:
Drive keeps two files with one name, so two concurrent first uploads would
create two journals. The journal is saved on the device before every upload;
a turn made offline is uploaded by the next sync.

## 5. The external-turns queue (desktop only)

The desktop orchestrator restores its context from `agent_state`, not from the
visible history. A turn merged from another device lands in the history — the
user sees it — but would be unknown to the agent answering the next message.

So when a merge adds a `user` or `assistant` message (not deleted, not
superseded) to a chat that **already had** a saved `internal_history` here, the
message is appended to `project_chats.external_turns`. A chat created by the
same document arrives with the history it describes, and a chat with no saved
state seeds from the visible history, so neither is queued.

On restore (`memgpt_runtime.restore_chat_orchestrator_state`) the queue is
appended after the restored history through
`MemGPTAgentBlock.append_external_messages`, after the usual repair of a tool
call left unanswered. The queue is consumed **when that state is saved**, by
count, so a turn that fails before saving loses nothing and turns that arrive
while it runs survive it. A restore that seeds instead discards the queue.
Clearing or superseding a chat resets it with `agent_state`. The queue lives in
its own column because a sync pass merges in a worker thread while a turn may be
saving `agent_state`.

This applies equally to a second computer's export: the gap existed between two
desktops before there was a phone.

## 6. Not in the format

| Not here | Why |
| --- | --- |
| Deleting or editing a row from a device | The merge is a union; a deletion does not propagate between desktops either ([cloud_sync.md](cloud_sync.md)). |
| A device writing `chats.json` | §1. |
| Attachments from a device | The app sends text only; `attachments` is always `"[]"`. Adding them needs the attachment cache (`attachment_cache`) to travel, which it does not. |
| Renaming a chat from a device | An existing chat keeps each machine's row; a rename would need a last-writer-wins field. |
