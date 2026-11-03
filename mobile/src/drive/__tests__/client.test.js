import test from 'node:test';
import assert from 'node:assert/strict';
import { createDriveClient, DriveConflictError, quote } from '../client.js';
import { createFakeDrive, goodToken } from './fakeDrive.js';

const noSleep = async () => {};

function setup() {
  const fake = createFakeDrive();
  const container = fake.folder('GilzaBots');
  const root = fake.folder('thesis', container);
  fake.folder('slides', container);
  fake.add({ name: 'not-a-project.txt', parent: container, content: 'x' });
  const getToken = goodToken();
  const drive = createDriveClient({ getToken, fetchImpl: fake.fetchImpl, sleep: noSleep });
  return { fake, root, drive, getToken };
}

test('projects are the folders of the GilzaBots container', async () => {
  const { drive } = setup();
  assert.deepEqual((await drive.listProjects()).map((p) => p.name), ['slides', 'thesis']);
});

test('an account that never synced has no projects and gets no folder', async () => {
  const fake = createFakeDrive();
  const drive = createDriveClient({ getToken: goodToken(), fetchImpl: fake.fetchImpl, sleep: noSleep });
  assert.deepEqual(await drive.listProjects(), []);
  assert.equal(fake.files.size, 1);
});

test('the index follows the desktop rules for paths', async () => {
  const { fake, root, drive } = setup();
  fake.write('main.tex', 'A', root);
  fake.write('chapters/one.tex', 'B', root);
  const sub = [...fake.files.values()].find((f) => f.name === 'chapters').id;
  // Two files with one name: the most recently modified wins.
  fake.add({ name: 'dup.tex', parent: root, content: 'old' });
  fake.add({ name: 'dup.tex', parent: root, content: 'new' });
  fake.add({ name: 'doc', parent: root, mimeType: 'application/vnd.google-apps.document' });
  const index = await drive.buildIndex(root);
  assert.deepEqual([...index.files.keys()].sort(), ['chapters/one.tex', 'dup.tex', 'main.tex']);
  assert.equal(index.folders.get('chapters'), sub);
  assert.equal(await drive.download(index.files.get('dup.tex').id), 'new');
});

test('writing creates missing folders, then updates in place', async () => {
  const { fake, root, drive } = setup();
  const index = await drive.buildIndex(root);
  await drive.writeText(index, '.gilzabots/session/devices/d1.json', '{"a":1}');
  await drive.writeText(index, '.gilzabots/session/devices/d1.json', '{"a":2}');
  assert.equal(fake.read('.gilzabots/session/devices/d1.json', root), '{"a":2}');
  const named = [...fake.files.values()].filter((f) => f.name === 'd1.json');
  assert.equal(named.length, 1);
});

test('a write against a changed checksum is refused', async () => {
  const { fake, root, drive } = setup();
  fake.write('main.tex', 'A', root);
  const index = await drive.buildIndex(root);
  const readMd5 = index.files.get('main.tex').md5;
  fake.write('main.tex', 'changed on the desktop', root);
  await assert.rejects(drive.writeText(index, 'main.tex', 'phone', { expectedMd5: readMd5 }), DriveConflictError);
  assert.equal(fake.read('main.tex', root), 'changed on the desktop');
});

test('creating a file that appeared meanwhile is refused', async () => {
  const { fake, root, drive } = setup();
  const index = await drive.buildIndex(root);
  fake.write('new.tex', 'made on the desktop', root);
  await assert.rejects(drive.writeText(index, 'new.tex', 'phone', { expectedMd5: null }), DriveConflictError);
  assert.equal(fake.read('new.tex', root), 'made on the desktop');
});

test('a file deleted since the index was built is created again, not patched', async () => {
  const { fake, root, drive } = setup();
  fake.write('gone.tex', 'x', root);
  const index = await drive.buildIndex(root);
  const id = index.files.get('gone.tex').id;
  fake.files.get(id).trashed = true;
  await drive.writeText(index, 'gone.tex', 'again', { expectedMd5: null });
  assert.equal(fake.read('gone.tex', root), 'again');
  assert.notEqual(index.files.get('gone.tex').id, id);
});

test('an expired token is refreshed once, transient errors are retried', async () => {
  const { fake, drive, getToken } = setup();
  getToken.value = 'stale';
  const original = getToken.value;
  const wrapped = async (opts) => { if (opts?.refresh) getToken.value = 'good'; return getToken(opts); };
  const client = createDriveClient({ getToken: wrapped, fetchImpl: fake.fetchImpl, sleep: noSleep });
  assert.equal(original, 'stale');
  assert.equal((await client.listProjects()).length, 2);
  fake.failures.push(503, 429);
  assert.equal((await drive.listProjects()).length, 2);
});

test('query literals are escaped', () => {
  assert.equal(quote("it's \\ here"), "'it\\'s \\\\ here'");
});
