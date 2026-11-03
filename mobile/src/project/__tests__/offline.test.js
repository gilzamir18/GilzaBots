import test from 'node:test';
import assert from 'node:assert/strict';
import { createDriveClient } from '../../drive/client.js';
import { createFakeDrive, goodToken } from '../../drive/__tests__/fakeDrive.js';
import { createMemoryKv } from '../../platform/kv.js';
import { createWorkspace, offlineKindOf, MAX_TEXT_BYTES } from '../workspace.js';

function setup() {
  const fake = createFakeDrive();
  const container = fake.folder('GilzaBots');
  const root = fake.folder('thesis', container);
  fake.write('main.tex', '\\section{A}', root);
  fake.write('chapters/one.tex', 'one', root);
  fake.write('figures/plot.png', 'PNGDATA', root);
  fake.write('build/main.pdf', '%PDF-1.7', root);
  fake.write('data/archive.zip', 'zip', root);
  fake.write('.gilzabots/session/chats.json', '{}', root);
  const drive = createDriveClient({ getToken: goodToken(), fetchImpl: fake.fetchImpl, sleep: async () => {} });
  const kv = createMemoryKv();
  return { fake, root, drive, kv, workspace: createWorkspace({ drive, kv, root }) };
}

const downloads = (fake) => fake.calls.filter((call) => call.params.alt === 'media').length;

test('what counts as available offline', () => {
  assert.equal(offlineKindOf('a.tex', { size: 10 }), 'text');
  assert.equal(offlineKindOf('a.tex', { size: MAX_TEXT_BYTES + 1 }), null);
  assert.equal(offlineKindOf('out.pdf', { size: 10 }), 'blob');
  assert.equal(offlineKindOf('pic.jpeg', { size: 10 }), 'blob');
  assert.equal(offlineKindOf('pic.jpeg', { size: 51 * 1024 * 1024 }), null);
  assert.equal(offlineKindOf('a.zip', { size: 10 }), null);
  assert.equal(offlineKindOf('.gilzabots/session/chats.json', { size: 10 }), null);
});

test('prefetch saves every viewable file and reports what stays online', async () => {
  const { workspace } = setup();
  await workspace.refresh();
  assert.deepEqual(await workspace.offlineStatus(), { total: 4, saved: 0, bytes: 0, skipped: 1 });
  const progress = [];
  const result = await workspace.prefetch({ onProgress: (p) => progress.push(p) });
  assert.deepEqual(result, { done: 4, failed: 0, total: 4 });
  assert.deepEqual(progress.at(-1), { done: 4, total: 4 });
  const status = await workspace.offlineStatus();
  assert.equal(status.saved, 4);
  assert.ok(status.bytes > 0);
});

test('a later prefetch downloads only what changed, and drops removed files', async () => {
  const { fake, root, workspace, kv } = setup();
  await workspace.refresh();
  await workspace.prefetch();
  const before = downloads(fake);

  fake.write('main.tex', '\\section{B}', root);
  const removed = [...fake.files.values()].find((file) => file.name === 'one.tex');
  removed.trashed = true;
  fake.files.delete(removed.id);
  await workspace.refresh();
  const result = await workspace.prefetch();

  assert.equal(result.total, 1);
  assert.equal(downloads(fake) - before, 1);
  assert.equal((await kv.keys(`file:${root}:chapters/`)).length, 0);
  assert.deepEqual(await workspace.offlineStatus(), { total: 3, saved: 3, bytes: (await workspace.offlineStatus()).bytes, skipped: 1 });
});

test('saved files open with Drive unreachable', async () => {
  const { fake, workspace } = setup();
  await workspace.refresh();
  await workspace.prefetch();
  fake.failures.push(...Array(40).fill(503));
  await workspace.refresh();
  assert.equal(workspace.offline, true);
  assert.equal((await workspace.readText('chapters/one.tex')).content, 'one');
  const pdf = await workspace.readBlob('build/main.pdf');
  assert.equal(await pdf.content.text(), '%PDF-1.7');
});

test('losing the network stops the prefetch instead of failing every file', async () => {
  const { fake, workspace } = setup();
  await workspace.refresh();
  fake.failures.push(...Array(40).fill(503));
  await assert.rejects(workspace.prefetch());
  assert.ok(fake.failures.length > 30, 'it gave up after the first file');
});

test('a prefetch stops when asked', async () => {
  const { workspace } = setup();
  await workspace.refresh();
  const controller = new AbortController();
  const result = await workspace.prefetch({ signal: controller.signal, onProgress: ({ done }) => { if (done === 1) controller.abort(); } });
  assert.equal(result.done, 1);
});

test('turning it off frees the space but keeps the conversations', async () => {
  const { root, workspace, kv } = setup();
  await workspace.refresh();
  await workspace.readInternalText('.gilzabots/session/chats.json');
  await workspace.prefetch();
  await workspace.purgeCache();
  assert.deepEqual(await kv.keys(`file:${root}:`), [`file:${root}:.gilzabots/session/chats.json`]);
  assert.equal((await workspace.offlineStatus()).saved, 0);
});
