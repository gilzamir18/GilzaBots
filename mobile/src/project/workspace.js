/**
 * A project's files, read from Drive and cached for offline use.
 *
 * File content is cached by Drive's md5: a cached copy is used as long as the
 * index still names the same checksum, and refreshed when it does not. When
 * Drive cannot be reached, the last index and the cached content are used and
 * `offline` says so — reading a stale copy is better than reading nothing, as
 * long as the UI tells the user which one they are looking at.
 */

const TEXT_EXTENSIONS = new Set([
  'tex', 'bib', 'sty', 'cls', 'bst', 'md', 'markdown', 'txt', 'json', 'yaml', 'yml', 'csv', 'tsv',
  'py', 'js', 'jsx', 'ts', 'html', 'css', 'xml', 'svg', 'sh', 'r', 'm', 'ipynb', 'toml', 'ini', 'cfg', 'log',
]);
export const MAX_TEXT_BYTES = 2 * 1024 * 1024;

// What "available offline" downloads besides text: what the file viewer can
// show. A file above the cap is left online and counted as skipped.
const OFFLINE_BLOB_EXTENSIONS = new Set(['pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp']);
export const MAX_OFFLINE_BLOB_BYTES = 50 * 1024 * 1024;

const isInternal = (path) => path === '.gilzabots' || path.startsWith('.gilzabots/');

/** How a file is kept for offline use, or null when it is not. */
export function offlineKindOf(path, node) {
  if (isInternal(path)) return null;
  if (isTextPath(path)) return node.size <= MAX_TEXT_BYTES ? 'text' : null;
  if (OFFLINE_BLOB_EXTENSIONS.has(extensionOf(path))) return node.size <= MAX_OFFLINE_BLOB_BYTES ? 'blob' : null;
  return null;
}

export function extensionOf(path) {
  const name = path.split('/').pop() || '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export function isTextPath(path) {
  return TEXT_EXTENSIONS.has(extensionOf(path));
}

export class FileUnavailableError extends Error {
  constructor(path, reason) {
    super(reason === 'binary'
      ? `${path} is not a text file.`
      : reason === 'too-large'
        ? `${path} is too large to read on the phone.`
        : reason === 'offline'
          ? `${path} is not cached on this device and Google Drive cannot be reached.`
          : `${path} does not exist in the project.`);
    this.name = 'FileUnavailableError';
    this.path = path;
    this.reason = reason;
  }
}

export function createWorkspace({ drive, kv, root }) {
  let index = null;
  let offline = false;
  const indexKey = `index:${root}`;
  const filePrefix = `file:${root}:`;
  const fileKey = (path) => `${filePrefix}${path}`;
  const manifestKey = `cached:${root}`;
  // path -> {md5, kind, size} for every cached file: answers "what is saved"
  // without reading each cached body back out of IndexedDB.
  let manifest = null;

  async function loadManifest() {
    if (!manifest) manifest = (await kv.get(manifestKey)) || {};
    return manifest;
  }

  let saving = null;
  function saveManifestSoon() {
    // Coalesced: a prefetch caches hundreds of files, and the manifest is
    // rewritten once per burst rather than once per file.
    if (!saving) {
      saving = new Promise((resolve) => setTimeout(resolve, 300))
        .then(() => kv.set(manifestKey, manifest))
        .finally(() => { saving = null; });
    }
    return saving;
  }

  async function noteCached(path, entry) {
    const current = await loadManifest();
    const previous = current[path];
    if (previous && previous.md5 === entry.md5 && previous.kind === entry.kind) return;
    current[path] = entry;
    saveManifestSoon();
  }

  async function refresh() {
    try {
      index = await drive.buildIndex(root);
      offline = false;
      await kv.set(indexKey, {
        files: [...index.files.entries()],
        folders: [...index.folders.entries()],
      });
    } catch (error) {
      const cached = await kv.get(indexKey);
      if (!cached) throw error;
      index = { root, files: new Map(cached.files), folders: new Map(cached.folders) };
      offline = true;
    }
    return index;
  }

  async function ensureIndex() {
    return index || refresh();
  }

  /**
   * Adopt the last index saved on this device, without the network. True when
   * there was one. Used to show a project at once, before `refresh()` returns.
   */
  async function loadCachedIndex() {
    if (index) return true;
    const cached = await kv.get(indexKey);
    if (!cached) return false;
    index = { root, files: new Map(cached.files), folders: new Map(cached.folders) };
    return true;
  }

  /** A cached copy whose checksum matches the current index, or null. Never downloads. */
  async function readCachedText(path) {
    const node = index?.files.get(path);
    if (!node) return null;
    const cached = await kv.get(fileKey(path));
    return cached && cached.kind === 'text' && cached.md5 === node.md5 ? cached : null;
  }

  async function listPaths({ includeInternal = false } = {}) {
    const current = await ensureIndex();
    return [...current.files.keys()]
      .filter((path) => includeInternal || !isInternal(path))
      .sort();
  }

  async function listTextPaths() {
    const current = await ensureIndex();
    return (await listPaths()).filter((path) => isTextPath(path) && current.files.get(path).size <= MAX_TEXT_BYTES);
  }

  async function read(path, kind) {
    const current = await ensureIndex();
    const node = current.files.get(path);
    if (!node) throw new FileUnavailableError(path, 'missing');
    const cached = await kv.get(fileKey(path));
    if (cached && cached.md5 === node.md5 && cached.kind === kind) {
      await noteCached(path, { md5: node.md5, kind, size: node.size });
      return { ...cached, node, stale: false };
    }
    try {
      const content = await drive.download(node.id, kind === 'text' ? 'text' : 'blob');
      const entry = { md5: node.md5, kind, content };
      await kv.set(fileKey(path), entry);
      await noteCached(path, { md5: node.md5, kind, size: node.size });
      return { ...entry, node, stale: false };
    } catch (error) {
      if (cached && cached.kind === kind) return { ...cached, node, stale: true };
      if (offline) throw new FileUnavailableError(path, 'offline');
      throw error;
    }
  }

  async function readText(path) {
    const current = await ensureIndex();
    const node = current.files.get(path);
    if (!node) throw new FileUnavailableError(path, 'missing');
    if (!isTextPath(path)) throw new FileUnavailableError(path, 'binary');
    if (node.size > MAX_TEXT_BYTES) throw new FileUnavailableError(path, 'too-large');
    return read(path, 'text');
  }

  async function readBlob(path) {
    return read(path, 'blob');
  }

  /**
   * GilzaBots's own documents (`chats.json`, device journals). No size cap: a
   * long project's export is tens of megabytes, and it is the conversation.
   */
  async function readInternalText(path) {
    return read(path, 'text');
  }

  async function writeText(path, content, expectedMd5) {
    const current = await ensureIndex();
    const node = await drive.writeText(current, path, content, { expectedMd5 });
    await kv.set(fileKey(path), { md5: node.md5, kind: 'text', content });
    await noteCached(path, { md5: node.md5, kind: 'text', size: node.size });
    return node;
  }

  // ── Available offline ─────────────────────────────────────────────────────

  /** Files the offline copy covers, with whether the current version is saved. */
  async function offlinePlan() {
    const current = await ensureIndex();
    const saved = await loadManifest();
    const eligible = [];
    let skipped = 0;
    for (const [path, node] of current.files) {
      if (isInternal(path)) continue;
      const kind = offlineKindOf(path, node);
      if (!kind) { skipped += 1; continue; }
      const entry = saved[path];
      eligible.push({ path, node, kind, saved: Boolean(entry && entry.md5 === node.md5 && entry.kind === kind) });
    }
    eligible.sort((a, b) => (a.kind === b.kind ? a.node.size - b.node.size : a.kind === 'text' ? -1 : 1));
    return { eligible, skipped };
  }

  async function offlineStatus() {
    const { eligible, skipped } = await offlinePlan();
    const saved = eligible.filter((item) => item.saved);
    return {
      total: eligible.length,
      saved: saved.length,
      bytes: saved.reduce((sum, item) => sum + item.node.size, 0),
      skipped,
    };
  }

  /**
   * Download every covered file whose current version is not saved yet, then
   * drop saved copies of files that left the project. Stops at `signal`, and at
   * the first failure that is not about one file (the network went away).
   */
  async function prefetch({ signal, onProgress = () => {} } = {}) {
    const { eligible } = await offlinePlan();
    const missing = eligible.filter((item) => !item.saved);
    let done = 0;
    let failed = 0;
    onProgress({ done, total: missing.length });
    for (const item of missing) {
      if (signal?.aborted) break;
      try {
        await read(item.path, item.kind);
      } catch (error) {
        // A file deleted since the index was built is that file's problem;
        // anything else (no network, quota) would fail for every file after it.
        if (error?.status === 404 || error?.reason === 'missing') failed += 1;
        else throw error;
      }
      done += 1;
      onProgress({ done, total: missing.length });
    }
    if (!signal?.aborted) await pruneCache();
    await saving;
    return { done, failed, total: missing.length };
  }

  /** Remove saved copies of files that are no longer in the project. */
  async function pruneCache() {
    const current = await ensureIndex();
    const saved = await loadManifest();
    for (const key of await kv.keys(filePrefix)) {
      const path = key.slice(filePrefix.length);
      if (!current.files.has(path)) {
        await kv.delete(key);
        delete saved[path];
      }
    }
    saveManifestSoon();
  }

  /**
   * Free the space the offline copy uses. GilzaBots's own documents stay: they
   * are the conversations, shown even without "available offline".
   */
  async function purgeCache() {
    const saved = await loadManifest();
    for (const key of await kv.keys(filePrefix)) {
      const path = key.slice(filePrefix.length);
      if (isInternal(path)) continue;
      await kv.delete(key);
      delete saved[path];
    }
    await kv.set(manifestKey, saved);
  }

  return {
    refresh,
    loadCachedIndex,
    readCachedText,
    listPaths,
    listTextPaths,
    readText,
    readBlob,
    readInternalText,
    writeText,
    offlineStatus,
    prefetch,
    purgeCache,
    node: async (path) => (await ensureIndex()).files.get(path) || null,
    get offline() { return offline; },
    get index() { return index; },
  };
}
