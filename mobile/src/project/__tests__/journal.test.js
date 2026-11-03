import test from 'node:test';
import assert from 'node:assert/strict';
import {
  addActivity, addChat, addMessage, compactJournal, createJournal, isDeviceJournalPath,
  journalPath, serializeJournal, validateJournal,
} from '../journal.js';

const device = { id: 'dev-1', name: 'Pixel' };
const msg = (cid, content, chat = 'c1', ts = '2026-10-19T12:00:00.000000+00:00') => ({
  chat_id: chat, timestamp: ts, role: 'user', content, client_message_id: cid,
});

test('journal paths follow the desktop rule', () => {
  assert.equal(journalPath('dev-1'), '.gilzabots/session/devices/dev-1.json');
  assert.ok(isDeviceJournalPath('.gilzabots/session/devices/dev-1.json'));
  assert.ok(!isDeviceJournalPath('.gilzabots/session/devices/nested/x.json'));
  assert.ok(!isDeviceJournalPath('.gilzabots/session/devices/.tmp.json'));
  assert.ok(!isDeviceJournalPath('.gilzabots/session/chats.json'));
});

test('a journal names itself and its version', () => {
  const journal = createJournal(device, 'alpha');
  assert.deepEqual(validateJournal(journal), { ok: true });
  assert.equal(validateJournal({ ...journal, kind: 'x' }).ok, false);
  assert.equal(validateJournal({ ...journal, version: 2 }).reason, 'newer-format');
});

test('a chat carries identity only', () => {
  const journal = addChat(createJournal(device, 'alpha'), {
    id: 'c1', name: 'Train', created_at: 't', agent_state: '{"x":1}', core_memory: 'm',
  });
  assert.deepEqual(journal.chats, [{ id: 'c1', name: 'Train', created_at: 't' }]);
  assert.equal(addChat(journal, { id: 'c1', name: 'again' }).chats.length, 1);
});

test('rows the export already holds are compacted away', async () => {
  let journal = createJournal(device, 'alpha');
  journal = addChat(journal, { id: 'c1', name: 'n', created_at: 't' });
  journal = addMessage(journal, msg('a', 'folded'));
  journal = addMessage(journal, msg('b', 'not yet'));
  journal = addActivity(journal, { chat_id: 'c1', timestamp: 't1', event: 'thought', content: 'r', payload: '{}' });
  const exportDoc = {
    chats: [{ id: 'c1' }],
    messages: [{ ...msg('a', 'folded'), attachments: '[]' }],
    activity: [{ chat_id: 'c1', timestamp: 't1', event: 'thought', agent: '', content: 'r', payload: '{}' }],
  };
  const compacted = await compactJournal(journal, exportDoc);
  assert.deepEqual(compacted.messages.map((m) => m.content), ['not yet']);
  assert.deepEqual(compacted.activity, []);
  assert.deepEqual(compacted.chats, []);
});

test('serialization is deterministic', () => {
  let a = createJournal(device, 'alpha');
  a = addMessage(a, msg('2', 'second', 'c1', '2026-10-19T12:00:02.000000+00:00'));
  a = addMessage(a, msg('1', 'first', 'c1', '2026-10-19T12:00:01.000000+00:00'));
  let b = createJournal(device, 'alpha');
  b = addMessage(b, msg('1', 'first', 'c1', '2026-10-19T12:00:01.000000+00:00'));
  b = addMessage(b, msg('2', 'second', 'c1', '2026-10-19T12:00:02.000000+00:00'));
  assert.equal(serializeJournal(a), serializeJournal(b));
  assert.ok(serializeJournal(a).endsWith('\n'));
});
