import test from 'node:test';
import assert from 'node:assert/strict';

import {
  compactionMessage,
  compactionMessagesFromActivity,
  formatTokenCount,
} from '../chatCompaction.js';

test('a stream payload becomes a system divider row with the summary', () => {
  const row = compactionMessage({
    trigger: 'auto', pre_tokens: 81000, post_tokens: 2300, context_window: 100000,
    messages_compacted: 42, summary: '1. Primary Request', timestamp: '2026-11-08T10:00:00.000000+00:00',
  });
  assert.equal(row.role, 'system');
  assert.equal(row.content, '');
  assert.equal(row.timestamp, '2026-11-08T10:00:00.000000+00:00');
  assert.deepEqual(row._compaction, {
    trigger: 'auto', preTokens: 81000, postTokens: 2300, contextWindow: 100000,
    messagesCompacted: 42, summary: '1. Primary Request', customInstructions: '',
    restoredFiles: [], skippedFiles: [], hookFailures: [],
  });
});

test('restored files, skipped files and failed hooks are carried; successful hooks are not', () => {
  const row = compactionMessage({
    restored_files: ['a.py'],
    skipped_files: [{ path: 'big.csv', reason: 'too large' }, { reason: 'no path' }],
    hooks: [
      { event: 'PreCompact', command: 'ok.sh', outcome: 'ok' },
      { event: 'PostCompact', command: 'slow.sh', outcome: 'timeout', message: 'stopped' },
    ],
  });
  assert.deepEqual(row._compaction.restoredFiles, ['a.py']);
  assert.deepEqual(row._compaction.skippedFiles, [{ path: 'big.csv', reason: 'too large' }]);
  assert.deepEqual(row._compaction.hookFailures, [{ event: 'PostCompact', command: 'slow.sh', message: 'stopped' }]);
});

test('unknown triggers and bad numbers are normalised', () => {
  const row = compactionMessage({ trigger: 'weird', pre_tokens: 'x', post_tokens: -3 });
  assert.equal(row._compaction.trigger, 'manual');
  assert.equal(row._compaction.preTokens, 0);
  assert.equal(row._compaction.postTokens, 0);
});

test('only compaction activity rows are turned into dividers, summary from content', () => {
  const rows = compactionMessagesFromActivity([
    { event: 'error', content: 'boom', timestamp: 't0' },
    { event: 'compaction', content: 'the summary', timestamp: 't1', payload: { trigger: 'manual', pre_tokens: 10, post_tokens: 2 } },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].timestamp, 't1');
  assert.equal(rows[0]._fromActivity, true);
  assert.equal(rows[0]._compaction.summary, 'the summary');
  assert.equal(rows[0]._compaction.preTokens, 10);
});

test('token counts are shortened for the label', () => {
  assert.equal(formatTokenCount(950), '950');
  assert.equal(formatTokenCount(12345), '12.3k');
  assert.equal(formatTokenCount(2000), '2k');
  assert.equal(formatTokenCount(1500000), '1.5M');
  assert.equal(formatTokenCount(undefined), '0');
});
