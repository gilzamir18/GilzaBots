# GilzaBots Mobile (Android)

A companion app for GilzaBots: open the projects your desktop mirrors to Google
Drive, read their files, and continue their conversations — with an agent that
can read, search and, after you approve each change, edit the project's files.
What you do on the phone reaches the desktop through the same Drive folder.

Design and decisions: [docs/specs/mobile_app.md](../docs/specs/mobile_app.md) ·
file contract with the desktop: [docs/specs/chats_export.md](../docs/specs/chats_export.md) ·
architecture: §2.21 of [PROJECT_DESIGN.md](../PROJECT_DESIGN.md).

## Before the first run: the Android OAuth client

The app has no client id in its code. Google recognises it by package name and
signing certificate, so register an **Android** OAuth client **in the same
Google Cloud project as the desktop's bundled client** (the one whose id the
release pipeline injects as `GILZABOTS_GDRIVE_CLIENT_ID`). `drive.file` access
belongs to the Cloud project: a client in any other project sees no GilzaBots
folders at all.

1. Google Cloud console → the desktop's project → APIs & Services → Credentials
   → Create credentials → OAuth client ID → **Android**.
2. Package name: `io.github.gilzamir18.gilzabots`.
3. SHA-1 of the signing certificate. One client per certificate:
   - debug builds: `keytool -list -v -keystore ~/.android/debug.keystore -alias androiddebugkey -storepass android`
   - releases: the SHA-1 of the release keystore held in the CI secrets.
4. The consent screen and the `drive.file` scope are the ones the desktop
   already uses; nothing else changes.

## Build

Requirements: Node 22+, JDK 21 (a full JDK — `javac` — not a JRE), Android SDK
with platform 36.

```bash
cd mobile
npm ci
npm test                 # unit tests (node --test)
npm run build            # web layer → dist/
npm run test:browser     # end-to-end in headless Chrome (CHROME=/path if not on PATH)
npm run android:debug    # → android/app/build/outputs/apk/debug/app-debug.apk
```

Install the debug APK with `adb install -r android/app/build/outputs/apk/debug/app-debug.apk`,
or copy it to the phone and open it (allow installs from that source).

A release (`npm run android:release`) is signed only when
`GILZABOTS_ANDROID_KEYSTORE`, `GILZABOTS_ANDROID_KEYSTORE_PASSWORD`,
`GILZABOTS_ANDROID_KEY_ALIAS` and `GILZABOTS_ANDROID_KEY_PASSWORD` are set;
otherwise Gradle produces `app-release-unsigned.apk`. CI
(`.github/workflows/android.yml`) builds every change under `mobile/` and
publishes a signed APK and AAB for tags `mobile-v*`.

## Using it

1. **Connect Google Drive** and pick the account the desktop mirrors to.
2. On the desktop, a project appears here once Cloud sync has finished a pass
   with *Sync conversations* on.
3. **Settings → Model connections**: add each provider you use with its API
   key, then its models (**Load models from the provider**, or type a name).
   Keys stay on the phone, encrypted with the Android Keystore.
4. In a chat, tap the model name at the top to pick any of those models — or
   the one the desktop uses for the project, when you have a connection for its
   provider. Each chat remembers its own model.
5. Chat. Edits are shown before they are written; the desktop picks them up on
   its next sync, and the conversation continues there with the orchestrator
   aware of what was said on the phone.
6. When a turn fails, **Try again** answers the same question. The ⋯ on any
   message copies it, tries an answered turn again, edits a question, or
   branches; those three continue in a new chat, since an answer already shared
   with the desktop is never replaced. On a tablet the chat or file opens beside
   the list.

Ollama: `https://ollama.com/v1` with an Ollama API key works. A LAN server over
plain `http://` is refused by Android's cleartext policy, which the app keeps.

## Development in a browser

`npm run dev` runs the web layer in a browser. Google authorization is an
Android API, so the sign-in screen asks for an access token instead (for
example from the OAuth 2.0 Playground with the `drive.file` scope — note that it
only sees files created by the Playground's own client). Secrets are kept in
`sessionStorage` in that mode; it is a development surface, not a secure one.

## Layout

```
src/agent/      provider adapters, SSE, tool loop, tools, context
src/drive/      Drive REST client, token handling
src/project/    journal + export formats, row keys, sessions, turns, workspace cache
src/platform/   native plugin bridge, transport, IndexedDB cache
src/ui/         screens
android/        Capacitor project; GilzaNativePlugin.java is the app's own plugin
test/browser/   end-to-end check
```
