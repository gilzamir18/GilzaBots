/** The app's long-lived services, built once. */

import { createAuth } from './drive/auth.js';
import { createDriveClient } from './drive/client.js';
import { createIndexedDbKv } from './platform/kv.js';
import { GilzaNative, isNative } from './platform/native.js';
import { fetchTransport, nativeTransport } from './platform/transport.js';

export function createServices() {
  const kv = createIndexedDbKv();
  const auth = createAuth(GilzaNative);
  const drive = createDriveClient({ getToken: auth.getToken });
  const transport = isNative() ? nativeTransport(GilzaNative) : fetchTransport();
  return { kv, auth, drive, transport, plugin: GilzaNative, native: isNative() };
}
