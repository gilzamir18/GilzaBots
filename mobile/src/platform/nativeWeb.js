/**
 * Browser stand-in for the native plugin, for `npm run dev` only.
 *
 * Google authorization has no browser implementation here: it is an Android
 * API. During development an access token can be pasted on the sign-in screen
 * (for example from the OAuth 2.0 Playground with the drive.file scope); it is
 * kept for this browser session only and expires after an hour like any other.
 * Secrets go to sessionStorage — this is a development surface, not a secure
 * one, and the sign-in screen says so.
 */

import { WebPlugin } from '@capacitor/core';

const TOKEN_KEY = 'gilzabots.dev.accessToken';
const SECRET_PREFIX = 'gilzabots.dev.secret.';

export class GilzaNativeWeb extends WebPlugin {
  async authorize() {
    const token = safe(() => sessionStorage.getItem(TOKEN_KEY));
    if (!token) throw this.unavailable('Google authorization needs the Android app; paste a development token instead.');
    return { accessToken: token, email: '' };
  }

  async setDevelopmentToken({ token }) {
    safe(() => sessionStorage.setItem(TOKEN_KEY, token));
  }

  async clearToken() {
    safe(() => sessionStorage.removeItem(TOKEN_KEY));
  }

  async revoke() {
    safe(() => sessionStorage.removeItem(TOKEN_KEY));
  }

  async appIdentity() {
    throw this.unavailable('The app identity exists only in the Android app.');
  }

  async secureGet({ key }) {
    return { value: safe(() => sessionStorage.getItem(SECRET_PREFIX + key)) };
  }

  async secureSet({ key, value }) {
    safe(() => sessionStorage.setItem(SECRET_PREFIX + key, value));
  }

  async secureRemove({ key }) {
    safe(() => sessionStorage.removeItem(SECRET_PREFIX + key));
  }

  async streamRequest() {
    throw this.unimplemented('Streaming requests use fetch in the browser.');
  }

  async cancelRequest() {}
}

function safe(fn) {
  try {
    return fn();
  } catch {
    return null;
  }
}
