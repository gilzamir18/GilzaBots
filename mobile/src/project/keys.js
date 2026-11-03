/**
 * Row identities, computed exactly as `gilzabots/cloud/chats.py` computes them.
 *
 * A message or activity row the phone writes into its journal comes back to it
 * inside `chats.json` once a desktop has folded it in. Both copies must be
 * recognised as one row, so the key has to be the desktop's key, byte for byte:
 *
 *   message:  `cid:<client_message_id>` when present, otherwise
 *             `h:` + sha256 over chat_id, timestamp, role, content, attachments
 *   activity: sha256 over chat_id, timestamp, event, agent, content, payload
 *
 * Fields are joined with U+001F after each one, UTF-8 encoded.
 */

const SEPARATOR = '\x1f';

const MESSAGE_FIELDS = ['chat_id', 'timestamp', 'role', 'content', 'attachments'];
const ACTIVITY_FIELDS = ['chat_id', 'timestamp', 'event', 'agent', 'content', 'payload'];

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function joined(row, fields) {
  return fields.map((name) => `${row?.[name] ?? ''}${SEPARATOR}`).join('');
}

export async function messageKey(row) {
  const clientId = String(row?.client_message_id ?? '');
  if (clientId) return `cid:${clientId}`;
  return `h:${await sha256Hex(joined(row, MESSAGE_FIELDS))}`;
}

export async function activityKey(row) {
  return sha256Hex(joined(row, ACTIVITY_FIELDS));
}

export function newId() {
  return globalThis.crypto.randomUUID();
}
