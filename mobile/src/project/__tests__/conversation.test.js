import test from 'node:test';
import assert from 'node:assert/strict';
import { attachThoughts, buildConversation, stripProgressFence } from '../conversation.js';

const exportDoc = {
  version: 1,
  project: 'alpha',
  settings: { model: 'ollama/x' },
  chats: [
    { id: 'c1', name: 'Main Chat', created_at: '2026-10-19T10:00:00.000000+00:00', agent_state: '{}', core_memory: '' },
  ],
  messages: [
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:01.000000+00:00', role: 'user', content: 'q1', client_message_id: 'u1', attachments: '[]', deleted_at: '', superseded_by: '' },
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:05.000000+00:00', role: 'assistant', content: '```gilzabots-progress\nstep\n```\nanswer 1', client_message_id: '', attachments: '[]', deleted_at: '', superseded_by: '' },
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:06.000000+00:00', role: 'user', content: 'removed', client_message_id: 'u2', attachments: '[]', deleted_at: 'x', superseded_by: '' },
  ],
  activity: [
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:02.000000+00:00', event: 'thought', agent: 'a', content: 'Starting…', payload: '{"auxiliary": true}' },
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:03.000000+00:00', event: 'thought', agent: 'a', content: 'Think', payload: '{}' },
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:04.000000+00:00', event: 'thought', agent: 'a', content: 'ing', payload: '{}' },
    { chat_id: 'c1', timestamp: '2026-10-19T10:00:04.500000+00:00', event: 'stream_chunk', agent: 'a', content: 'x', payload: '{}' },
  ],
};

test('the export, this device and other devices form one union', async () => {
  const own = {
    chats: [{ id: 'c2', name: 'Phone chat', created_at: '2026-10-19T11:00:00.000000+00:00' }],
    messages: [
      { chat_id: 'c1', timestamp: '2026-10-19T10:00:07.000000+00:00', role: 'user', content: 'from phone', client_message_id: 'p1', attachments: '[]' },
      // Folded into the export already: same client id, must not double.
      { chat_id: 'c1', timestamp: '2026-10-19T10:00:01.000000+00:00', role: 'user', content: 'q1', client_message_id: 'u1', attachments: '[]' },
      { chat_id: 'c2', timestamp: '2026-10-19T11:00:01.000000+00:00', role: 'user', content: 'new chat', client_message_id: 'p2', attachments: '[]' },
    ],
    activity: [],
  };
  const { chats, settings } = await buildConversation({ exportDoc, journals: [{ journal: own, own: true }] });
  assert.equal(settings.model, 'ollama/x');
  assert.deepEqual(chats.map((c) => c.id), ['c2', 'c1']);
  const main = chats.find((c) => c.id === 'c1');
  assert.deepEqual(main.messages.map((m) => m.content), ['q1', 'answer 1', 'from phone']);
  assert.equal(main.messages[1].thoughts, 'Starting…\nThinking');
  assert.equal(main.messages[2].origin, 'this-device');
});

test('the desktop progress fence is not part of the answer', () => {
  assert.equal(stripProgressFence('```gilzabots-progress\na\nb\n```\n\nHello'), 'Hello');
  assert.equal(stripProgressFence('Hello'), 'Hello');
});

test('leftover reasoning goes to the last assistant message', () => {
  const out = attachThoughts(
    [{ role: 'user', timestamp: '1' }, { role: 'assistant', timestamp: '2' }],
    [{ timestamp: '3', content: 'late', payload: '{}' }],
  );
  assert.equal(out[1].thoughts, 'late');
});
