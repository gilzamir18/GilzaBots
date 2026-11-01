import test from 'node:test';
import assert from 'node:assert/strict';

import { settleAtHandoff } from '../editHandoff.js';

const NOT_SENT = 'The edited message was not sent.';

test('an edit is accepted at handoff, without waiting for its turn to end', async () => {
  let finishTurn;
  const result = await settleAtHandoff(({ onHandoff }) => {
    onHandoff();
    return new Promise(resolve => { finishTurn = resolve; });   // a turn still running
  }, NOT_SENT);

  assert.deepEqual(result, { accepted: true });
  finishTurn();
});

test('a refusal carries its reason', async () => {
  const result = await settleAtHandoff(async ({ onRefused }) => {
    onRefused('Failed to prepare edit: message_id is outside the source chat history');
  }, NOT_SENT);

  assert.deepEqual(result, {
    accepted: false,
    error: 'Failed to prepare edit: message_id is outside the source chat history',
  });
});

test('a send that ends without handing off is a refusal, never a silent success', async () => {
  // The defect this exists for: an early return deep in the send path ended
  // the edit with nothing sent and nothing said, and the editor closed anyway.
  const result = await settleAtHandoff(async () => undefined, NOT_SENT);

  assert.deepEqual(result, { accepted: false, error: NOT_SENT });
});

test('a send that throws before handing off reports the error', async () => {
  const rejected = await settleAtHandoff(async () => { throw new Error('network down'); }, NOT_SENT);
  const thrown = await settleAtHandoff(() => { throw new Error('bad state'); }, NOT_SENT);

  assert.deepEqual(rejected, { accepted: false, error: 'network down' });
  assert.deepEqual(thrown, { accepted: false, error: 'bad state' });
});

test('what happens after the handoff does not change the answer', async () => {
  const result = await settleAtHandoff(async ({ onHandoff, onRefused }) => {
    onHandoff();
    onRefused('too late');
    throw new Error('the turn failed later');
  }, NOT_SENT);

  assert.deepEqual(result, { accepted: true });
});
