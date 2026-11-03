// The phone's row keys must equal gilzabots/cloud/chats.py's, byte for byte:
// a journal row and its copy in chats.json are recognised as one row only then.
// Expected values were produced by `_message_key` / `_activity_key` in Python.
import test from 'node:test';
import assert from 'node:assert/strict';
import { activityKey, messageKey } from '../keys.js';

const message = {
  chat_id: 'c1',
  timestamp: '2026-10-19T11:22:57.598097+00:00',
  role: 'assistant',
  content: 'Olá, ^{i\\pi}$ — ok',
  attachments: '[]',
  client_message_id: '',
};

test('a message without a client id is keyed by the Python content hash', async () => {
  assert.equal(await messageKey(message), 'h:465793b3f3d7d2733b97c8ee49e425aa9ec037b640854b487b267ccbdbec0140');
});

test('a client id is authoritative', async () => {
  assert.equal(await messageKey({ ...message, client_message_id: 'abc' }), 'cid:abc');
});

test('an activity row is keyed by the Python content hash', async () => {
  const row = {
    chat_id: 'c1',
    timestamp: '2026-10-19T11:22:58.498058+00:00',
    event: 'thought',
    agent: 'chat_orchestrator',
    content: 'The',
    payload: '{"agent": "chat_orchestrator", "thought_tokens": 27}',
  };
  assert.equal(await activityKey(row), '663676e4c3a8c13ae4785dd35d09b9464531c038b446473b667d1e322dc5f4f4');
});
