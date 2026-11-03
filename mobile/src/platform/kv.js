/**
 * The offline cache: a small key-value store.
 *
 * IndexedDB in the app (it keeps Blobs, so a PDF is cached as bytes), an
 * in-memory map in tests. Everything here can be rebuilt from Drive except one
 * thing: this device's journal, which holds turns not uploaded yet. That is why
 * the journal is written here *before* any upload is attempted.
 */

const DB_NAME = 'gilzabots-mobile';
const STORE = 'kv';

export function createMemoryKv() {
  const map = new Map();
  return {
    async get(key) { return map.has(key) ? structuredClone(map.get(key)) : undefined; },
    async set(key, value) { map.set(key, structuredClone(value)); },
    async delete(key) { map.delete(key); },
    async keys(prefix = '') { return [...map.keys()].filter((key) => key.startsWith(prefix)); },
  };
}

export function createIndexedDbKv(indexedDB = globalThis.indexedDB) {
  let opening = null;
  const open = () => {
    if (!opening) {
      opening = new Promise((resolve, reject) => {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => request.result.createObjectStore(STORE);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    }
    return opening;
  };
  const run = async (mode, operation) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, mode);
      const request = operation(transaction.objectStore(STORE));
      transaction.oncomplete = () => resolve(request?.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  };
  return {
    get: (key) => run('readonly', (store) => store.get(key)),
    set: (key, value) => run('readwrite', (store) => store.put(value, key)),
    delete: (key) => run('readwrite', (store) => store.delete(key)),
    async keys(prefix = '') {
      const all = await run('readonly', (store) => store.getAllKeys());
      return (all || []).filter((key) => String(key).startsWith(prefix));
    },
  };
}
