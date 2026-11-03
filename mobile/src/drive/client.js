/**
 * Google Drive REST v3, the subset the app needs.
 *
 * It addresses the same layout the desktop writes
 * (`gilzabots/cloud/providers/google_drive.py`): a container folder `GilzaBots`
 * in the Drive root and one folder per project inside it. The scope is
 * `drive.file`, granted per Google Cloud project, so this client sees what the
 * desktop client of the same project created and nothing else in the account.
 *
 * Paths are project-relative POSIX paths, resolved through an index built from
 * a walk of the project folder — Drive itself addresses content by opaque id
 * and allows two files with one name in a folder. The index applies the
 * desktop's rules for that: the first folder of a name wins, and among files
 * of one name the most recently modified wins.
 */

export const API_BASE = 'https://www.googleapis.com/drive/v3';
export const UPLOAD_BASE = 'https://www.googleapis.com/upload/drive/v3/files';
export const CONTAINER_FOLDER = 'GilzaBots';
export const FOLDER_MIME = 'application/vnd.google-apps.folder';
const SHORTCUT_MIME = 'application/vnd.google-apps.shortcut';
const FILE_FIELDS = 'id,name,mimeType,size,md5Checksum,modifiedTime,version,trashed';
const LIST_FIELDS = `nextPageToken,files(${FILE_FIELDS})`;
const RETRY_STATUSES = new Set([429, 500, 502, 503, 504]);
const MAX_ATTEMPTS = 4;

export class DriveError extends Error {
  constructor(message, { status = 0, code = '' } = {}) {
    super(message);
    this.name = 'DriveError';
    this.status = status;
    this.code = code;
  }
}

/** The file changed on Drive after this device read it. */
export class DriveConflictError extends DriveError {
  constructor(path) {
    super(`"${path}" changed in Google Drive since it was read on this device.`, { code: 'conflict' });
    this.name = 'DriveConflictError';
    this.path = path;
  }
}

export function quote(value) {
  return `'${String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
}

/**
 * @param {{getToken: (opts?: {refresh?: boolean}) => Promise<string>, fetchImpl?: typeof fetch, sleep?: (ms: number) => Promise<void>}} options
 */
export function createDriveClient({ getToken, fetchImpl = globalThis.fetch.bind(globalThis), sleep = defaultSleep }) {
  async function request(method, url, { params, body, headers = {}, expect = 'json' } = {}) {
    const target = params ? `${url}?${new URLSearchParams(params)}` : url;
    let refreshed = false;
    for (let attempt = 1; ; attempt += 1) {
      const token = await getToken({ refresh: refreshed });
      const response = await fetchImpl(target, {
        method,
        headers: { Authorization: `Bearer ${token}`, ...headers },
        body,
      });
      if (response.status === 401 && !refreshed) {
        // Access tokens live an hour; one refresh is the whole recovery.
        refreshed = true;
        continue;
      }
      if (RETRY_STATUSES.has(response.status) && attempt < MAX_ATTEMPTS) {
        await sleep(500 * 2 ** (attempt - 1));
        continue;
      }
      if (!response.ok) {
        throw new DriveError(await describeError(response), { status: response.status });
      }
      if (expect === 'json') return response.json();
      if (expect === 'text') return response.text();
      if (expect === 'blob') return response.blob();
      return response;
    }
  }

  async function* listChildren(folderId) {
    let pageToken = '';
    do {
      const params = {
        q: `${quote(folderId)} in parents and trashed = false`,
        fields: LIST_FIELDS,
        pageSize: '1000',
        spaces: 'drive',
        supportsAllDrives: 'true',
        includeItemsFromAllDrives: 'true',
      };
      if (pageToken) params.pageToken = pageToken;
      const payload = await request('GET', `${API_BASE}/files`, { params });
      for (const item of payload.files || []) yield item;
      pageToken = payload.nextPageToken || '';
    } while (pageToken);
  }

  async function findFolder(name, parent) {
    const payload = await request('GET', `${API_BASE}/files`, {
      params: {
        q: `name = ${quote(name)} and ${quote(parent)} in parents and mimeType = ${quote(FOLDER_MIME)} and trashed = false`,
        fields: LIST_FIELDS,
        spaces: 'drive',
      },
    });
    return payload.files?.[0]?.id || '';
  }

  /** Project folders in the GilzaBots container. Never creates the container. */
  async function listProjects() {
    const container = await findFolder(CONTAINER_FOLDER, 'root');
    if (!container) return [];
    const projects = [];
    for await (const item of listChildren(container)) {
      if (item.mimeType !== FOLDER_MIME || !item.name || !item.id) continue;
      projects.push({ name: item.name, root: item.id, modifiedTime: item.modifiedTime || '' });
    }
    projects.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    return projects;
  }

  /** Walk a project folder into `{files: Map<path, node>, folders: Map<path, id>}`. */
  async function buildIndex(root) {
    const files = new Map();
    const folders = new Map([['', root]]);
    const pending = [['', root]];
    while (pending.length) {
      const [prefix, folderId] = pending.pop();
      for await (const item of listChildren(folderId)) {
        const name = String(item.name || '');
        if (!name || name === '.' || name === '..' || name.includes('/')) continue;
        const path = prefix ? `${prefix}/${name}` : name;
        if (item.mimeType === FOLDER_MIME) {
          if (folders.has(path)) continue;
          folders.set(path, item.id);
          pending.push([path, item.id]);
          continue;
        }
        if (item.mimeType === SHORTCUT_MIME || String(item.mimeType || '').startsWith('application/vnd.google-apps.')) continue;
        const node = nodeFrom(item);
        const previous = files.get(path);
        if (previous && previous.modifiedTime >= node.modifiedTime) continue;
        files.set(path, node);
      }
    }
    return { root, files, folders };
  }

  async function getFile(id) {
    try {
      const item = await request('GET', `${API_BASE}/files/${encodeURIComponent(id)}`, { params: { fields: FILE_FIELDS } });
      return item.trashed ? null : nodeFrom(item);
    } catch (error) {
      if (error instanceof DriveError && error.status === 404) return null;
      throw error;
    }
  }

  function download(id, expect = 'text') {
    return request('GET', `${API_BASE}/files/${encodeURIComponent(id)}`, { params: { alt: 'media' }, expect });
  }

  async function ensureFolder(name, parent) {
    const existing = await findFolder(name, parent);
    if (existing) return existing;
    const item = await request('POST', `${API_BASE}/files`, {
      params: { fields: FILE_FIELDS },
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, mimeType: FOLDER_MIME, parents: [parent] }),
    });
    return item.id;
  }

  /** Make every folder of `dir` exist under the index's root, updating the index. */
  async function ensureFolderPath(index, dir) {
    if (!dir) return index.root;
    let parent = index.root;
    let prefix = '';
    for (const part of dir.split('/')) {
      prefix = prefix ? `${prefix}/${part}` : part;
      let id = index.folders.get(prefix);
      if (!id) {
        id = await ensureFolder(part, parent);
        index.folders.set(prefix, id);
      }
      parent = id;
    }
    return parent;
  }

  async function createFile(parent, name, content, mime) {
    const boundary = `gilzabots-${globalThis.crypto.randomUUID()}`;
    const metadata = JSON.stringify({ name, parents: [parent] });
    const body =
      `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${metadata}\r\n` +
      `--${boundary}\r\nContent-Type: ${mime}\r\n\r\n${content}\r\n--${boundary}--`;
    const item = await request('POST', UPLOAD_BASE, {
      params: { uploadType: 'multipart', fields: FILE_FIELDS },
      headers: { 'Content-Type': `multipart/related; boundary=${boundary}` },
      body,
    });
    return nodeFrom(item);
  }

  async function updateContent(id, content, mime) {
    const item = await request('PATCH', `${UPLOAD_BASE}/${encodeURIComponent(id)}`, {
      params: { uploadType: 'media', fields: FILE_FIELDS },
      headers: { 'Content-Type': mime },
      body: content,
    });
    return nodeFrom(item);
  }

  /**
   * Write a text file at `path`.
   *
   * `expectedMd5` is the checksum the caller read. Drive v3 has no conditional
   * write, so the check is a read immediately before the write: it narrows the
   * window to one round trip rather than closing it. A desktop that edits the
   * same file in that window still loses nothing — its sync engine sees both
   * sides changed and keeps a conflict copy.
   */
  async function writeText(index, path, content, { expectedMd5 = undefined, mime = guessMime(path) } = {}) {
    const slash = path.lastIndexOf('/');
    const dir = slash >= 0 ? path.slice(0, slash) : '';
    const name = slash >= 0 ? path.slice(slash + 1) : path;
    let existing = index.files.get(path);
    if (expectedMd5 !== undefined) {
      let current = existing ? await getFile(existing.id) : null;
      if (!current) {
        // Gone since the index was built, or never indexed: what counts is
        // what Drive holds at that path now. Without this lookup a file the
        // desktop created meanwhile would get a same-named twin.
        existing = undefined;
        const parent = index.folders.get(dir);
        current = parent ? await findFile(name, parent) : null;
      }
      if ((expectedMd5 ?? null) !== (current?.md5 ?? null)) throw new DriveConflictError(path);
    }
    let node;
    if (existing) {
      node = await updateContent(existing.id, content, mime);
    } else {
      const parent = await ensureFolderPath(index, dir);
      node = await createFile(parent, name, content, mime);
    }
    index.files.set(path, node);
    return node;
  }

  async function findFile(name, parent) {
    const payload = await request('GET', `${API_BASE}/files`, {
      params: {
        q: `name = ${quote(name)} and ${quote(parent)} in parents and trashed = false`,
        fields: LIST_FIELDS,
        spaces: 'drive',
      },
    });
    const item = (payload.files || []).find((file) => file.mimeType !== FOLDER_MIME);
    return item ? nodeFrom(item) : null;
  }

  async function aboutUser() {
    const payload = await request('GET', `${API_BASE}/about`, { params: { fields: 'user(emailAddress,displayName)' } });
    return payload.user || {};
  }

  return { request, listChildren, findFolder, listProjects, buildIndex, getFile, download, writeText, aboutUser };
}

function nodeFrom(item) {
  return {
    id: item.id,
    name: item.name,
    mimeType: item.mimeType || '',
    size: Number(item.size || 0),
    md5: item.md5Checksum || null,
    modifiedTime: item.modifiedTime || '',
  };
}

async function describeError(response) {
  try {
    const payload = await response.json();
    return payload?.error?.message || `Google Drive answered HTTP ${response.status}.`;
  } catch {
    return `Google Drive answered HTTP ${response.status}.`;
  }
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const MIME_BY_EXTENSION = {
  tex: 'text/x-tex', bib: 'text/x-bibtex', md: 'text/markdown', txt: 'text/plain',
  json: 'application/json', yaml: 'application/yaml', yml: 'application/yaml',
  py: 'text/x-python', js: 'text/javascript', csv: 'text/csv', html: 'text/html',
  sty: 'text/x-tex', cls: 'text/x-tex',
};

export function guessMime(path) {
  const extension = path.split('.').pop()?.toLowerCase() || '';
  return MIME_BY_EXTENSION[extension] || 'text/plain';
}
