/**
 * The app's own native plugin (android/app/src/main/java/.../GilzaNativePlugin.java).
 *
 * - Google authorization for the `drive.file` scope through Google Identity
 *   Services' AuthorizationClient: the Android OAuth client is matched by
 *   package name and signing certificate, so no client id is in the code.
 * - Secret storage encrypted with a key held in the Android Keystore, for the
 *   model providers' API keys.
 * - Streaming HTTP for model providers. A web view's `fetch` is bound by CORS,
 *   and not every provider allows browser origins; native HTTP is not.
 */

import { Capacitor, registerPlugin } from '@capacitor/core';

export const GilzaNative = registerPlugin('GilzaNative', {
  web: () => import('./nativeWeb.js').then((module) => new module.GilzaNativeWeb()),
});

export const isNative = () => Capacitor.isNativePlatform();

export const DRIVE_FILE_SCOPE = 'https://www.googleapis.com/auth/drive.file';
