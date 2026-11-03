/**
 * The project's conversations as the phone shows them.
 *
 * Three sources hold pieces of the same history: `chats.json` (the desktops'
 * union), this device's journal, and the journals of other devices. Rows the
 * journal shares with the export are the same row (see keys.js), so the view is
 * a union by key — the same rule the desktop merge applies.
 */

import { activityKey, messageKey } from './keys.js';

const PROGRESS_FENCE = /^\s*```gilzabots-progress\n[\s\S]*?\n```\s*/;

/** The desktop's progress block is render markup, not part of the answer. */
export function stripProgressFence(content) {
  return String(content ?? '').replace(PROGRESS_FENCE, '');
}

function isVisible(message) {
  return !message.deleted_at && !message.superseded_by && (message.role === 'user' || message.role === 'assistant');
}

/**
 * @param {{exportDoc?: object, journals?: Array<{journal: object, own?: boolean}>}} sources
 */
export async function buildConversation({ exportDoc = null, journals = [] }) {
  const chats = new Map();
  const messages = new Map();
  const activity = new Map();

  const addChat = (chat, origin) => {
    if (!chat?.id || chats.has(chat.id)) return;
    chats.set(chat.id, {
      id: chat.id,
      name: chat.name || chat.id,
      created_at: chat.created_at || '',
      agent_state: chat.agent_state || '{}',
      core_memory: chat.core_memory || '',
      origin,
    });
  };
  const addRows = async (rows, target, keyOf, origin) => {
    for (const row of rows || []) {
      const key = await keyOf(row);
      if (!target.has(key)) target.set(key, { ...row, key, origin });
    }
  };

  if (exportDoc) {
    for (const chat of exportDoc.chats || []) addChat(chat, 'export');
    await addRows(exportDoc.messages, messages, messageKey, 'export');
    await addRows(exportDoc.activity, activity, activityKey, 'export');
  }
  for (const { journal, own } of journals) {
    const origin = own ? 'this-device' : 'other-device';
    for (const chat of journal.chats || []) addChat(chat, origin);
    await addRows(journal.messages, messages, messageKey, origin);
    await addRows(journal.activity, activity, activityKey, origin);
  }

  const byChat = new Map();
  for (const message of messages.values()) {
    if (!isVisible(message)) continue;
    if (!chats.has(message.chat_id)) continue;
    if (!byChat.has(message.chat_id)) byChat.set(message.chat_id, []);
    byChat.get(message.chat_id).push(message);
  }
  const thoughtsByChat = new Map();
  for (const row of activity.values()) {
    if (row.event !== 'thought' || row.deleted_at) continue;
    if (!thoughtsByChat.has(row.chat_id)) thoughtsByChat.set(row.chat_id, []);
    thoughtsByChat.get(row.chat_id).push(row);
  }

  const result = [];
  for (const chat of chats.values()) {
    const list = (byChat.get(chat.id) || []).sort(compareRows);
    const thoughts = (thoughtsByChat.get(chat.id) || []).sort(compareRows);
    const withThoughts = attachThoughts(list, thoughts).map((message) => ({
      ...message,
      content: message.role === 'assistant' ? stripProgressFence(message.content) : message.content,
    }));
    const last = withThoughts[withThoughts.length - 1];
    // A branch is newer than the messages it copied: it sorts by its creation.
    const updated = [last?.timestamp || '', chat.created_at].reduce((a, b) => (String(b) > String(a) ? b : a));
    result.push({ ...chat, messages: withThoughts, updated_at: updated });
  }
  result.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  return { chats: result, settings: exportDoc?.settings || {}, projectName: exportDoc?.project || '' };
}

function compareRows(a, b) {
  return String(a.timestamp).localeCompare(String(b.timestamp)) || String(a.key).localeCompare(String(b.key));
}

/**
 * Pair reasoning rows with the assistant message they preceded.
 *
 * The rule is the desktop's (`ProjectStore.thought_windows`): every thought up
 * to a message's timestamp belongs to the next assistant message, and whatever
 * is left belongs to the last one.
 */
export function attachThoughts(messages, thoughts) {
  const out = messages.map((message) => ({ ...message, thoughts: '' }));
  let position = 0;
  let pending = [];
  for (const message of out) {
    while (position < thoughts.length && (!message.timestamp || thoughts[position].timestamp <= message.timestamp)) {
      pending.push(thoughts[position]);
      position += 1;
    }
    if (message.role === 'assistant') {
      message.thoughts = joinThoughts(pending);
      pending = [];
    }
  }
  pending.push(...thoughts.slice(position));
  if (pending.length) {
    const lastAssistant = [...out].reverse().find((message) => message.role === 'assistant');
    if (lastAssistant) lastAssistant.thoughts = [lastAssistant.thoughts, joinThoughts(pending)].filter(Boolean).join('\n');
  }
  return out;
}

/**
 * The desktop persists reasoning token by token; auxiliary rows (a status line
 * such as "Starting execution of agent…") stand on their own line.
 */
function joinThoughts(rows) {
  let text = '';
  for (const row of rows) {
    let auxiliary = false;
    try {
      auxiliary = Boolean(JSON.parse(row.payload || '{}').auxiliary);
    } catch {
      auxiliary = false;
    }
    if (auxiliary) {
      text += `${text && !text.endsWith('\n') ? '\n' : ''}${row.content}\n`;
    } else {
      text += row.content;
    }
  }
  return text.trim();
}
