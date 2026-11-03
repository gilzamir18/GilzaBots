/**
 * This device's conversation journal: `.gilzabots/session/devices/<id>.json`.
 *
 * The phone never rewrites `chats.json`. That file belongs to the desktops,
 * which merge into it, and a phone uploading its own copy would race them and
 * lose whichever write came second. Instead the phone appends to a file only it
 * writes; desktops read it, fold it into their database and publish the union
 * in `chats.json` (see `gilzabots/cloud/chats.py: merge_device_journal`).
 *
 * The shape is the export's, restricted to what a device may add: chats
 * (identity only), messages and activity. The format is specified in
 * docs/specs/chats_export.md.
 */

import { activityKey, messageKey } from './keys.js';

export const DEVICE_JOURNAL_KIND = 'gilzabots.device-journal';
export const DEVICE_JOURNAL_VERSION = 1;
export const DEVICE_JOURNALS_DIR = '.gilzabots/session/devices';
export const CHATS_EXPORT_PATH = '.gilzabots/session/chats.json';

export function journalPath(deviceId) {
  return `${DEVICE_JOURNALS_DIR}/${deviceId}.json`;
}

export function isDeviceJournalPath(path) {
  const prefix = `${DEVICE_JOURNALS_DIR}/`;
  if (!path.startsWith(prefix)) return false;
  const name = path.slice(prefix.length);
  return Boolean(name) && !name.includes('/') && name.endsWith('.json') && !name.startsWith('.');
}

export function createJournal(device, projectName) {
  return {
    kind: DEVICE_JOURNAL_KIND,
    version: DEVICE_JOURNAL_VERSION,
    device: { id: device.id, name: device.name || '' },
    project: projectName,
    chats: [],
    messages: [],
    activity: [],
  };
}

/** Accept a parsed journal, or report why it cannot be used. */
export function validateJournal(payload) {
  if (!payload || typeof payload !== 'object' || payload.kind !== DEVICE_JOURNAL_KIND) {
    return { ok: false, reason: 'not-a-journal' };
  }
  if (Number(payload.version || 0) > DEVICE_JOURNAL_VERSION) {
    return { ok: false, reason: 'newer-format' };
  }
  return { ok: true };
}

export function addChat(journal, chat) {
  if (journal.chats.some((existing) => existing.id === chat.id)) return journal;
  return { ...journal, chats: [...journal.chats, { id: chat.id, name: chat.name, created_at: chat.created_at }] };
}

export function addMessage(journal, message) {
  return {
    ...journal,
    messages: [
      ...journal.messages,
      {
        chat_id: message.chat_id,
        timestamp: message.timestamp,
        role: message.role,
        content: message.content,
        client_message_id: message.client_message_id,
        attachments: message.attachments ?? '[]',
      },
    ],
  };
}

export function addActivity(journal, row) {
  return {
    ...journal,
    activity: [
      ...journal.activity,
      {
        chat_id: row.chat_id,
        timestamp: row.timestamp,
        event: row.event,
        agent: row.agent || '',
        content: row.content || '',
        payload: typeof row.payload === 'string' ? row.payload : JSON.stringify(row.payload || {}),
      },
    ],
  };
}

/**
 * Drop what a desktop has already folded into `chats.json`.
 *
 * Once a row is in the export it is durable without the journal: every
 * computer gets it from there. Keeping it would grow the journal forever and
 * make every desktop re-check it on every pass. A chat is kept while any of
 * its messages still live only here, since a desktop drops a message whose
 * chat it does not know.
 */
export async function compactJournal(journal, exportDoc) {
  if (!exportDoc) return journal;
  const exportedMessages = new Set(await Promise.all((exportDoc.messages || []).map(messageKey)));
  const exportedActivity = new Set(await Promise.all((exportDoc.activity || []).map(activityKey)));
  const exportedChats = new Set((exportDoc.chats || []).map((chat) => chat.id));

  const messages = [];
  for (const message of journal.messages) {
    if (!exportedMessages.has(await messageKey(message))) messages.push(message);
  }
  const activity = [];
  for (const row of journal.activity) {
    if (!exportedActivity.has(await activityKey(row))) activity.push(row);
  }
  const chats = journal.chats.filter((chat) => !exportedChats.has(chat.id));
  return { ...journal, chats, messages, activity };
}

/**
 * Deterministic text, so an unchanged journal is byte-identical and is not
 * uploaded again.
 */
export function serializeJournal(journal) {
  const byTime = (a, b) => (
    String(a.chat_id).localeCompare(String(b.chat_id)) || String(a.timestamp).localeCompare(String(b.timestamp))
  );
  const ordered = {
    activity: [...journal.activity].sort(byTime).map(sortKeys),
    chats: [...journal.chats].sort((a, b) => String(a.id).localeCompare(String(b.id))).map(sortKeys),
    device: sortKeys(journal.device),
    kind: journal.kind,
    messages: [...journal.messages].sort(byTime).map(sortKeys),
    project: journal.project,
    version: journal.version,
  };
  return `${JSON.stringify(ordered, null, 2)}\n`;
}

function sortKeys(object) {
  return Object.fromEntries(Object.keys(object).sort().map((key) => [key, object[key]]));
}
