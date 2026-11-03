# GilzaBots Mobile (Android) — decisions and current state

Status: implemented as a first version (0.1.0) in [`mobile/`](../../mobile/).
Verified by unit tests, a browser end-to-end check and a debug/release Gradle
build. **Not yet verified on a device against a real Google account** — see
*Open items*.

The architecture is §2.21 of [PROJECT_DESIGN.md](../../PROJECT_DESIGN.md); the
file contract with the desktop is [chats_export.md](chats_export.md).

## Scope decisions

| Question | Decision |
| --- | --- |
| What the app is | A standalone companion: chat about a project and read its files. No backend of its own. |
| Where its data comes from | The project folder the desktop mirrors to Google Drive (`GilzaBots/<project>/`). |
| How its conversations reach the desktop | A per-device journal (`.gilzabots/session/devices/<id>.json`), never `chats.json`. |
| Agent | A small JavaScript loop in the app, not the desktop's MemGPT orchestrator. |
| Tools | `list_files`, `read_file`, `search_files`, `edit_file`, `create_file`. Writes need approval, as on the desktop. |
| What it cannot do | Compile LaTeX, run commands, edit `.jpt` decks, checkpoints, git. The system prompt says so. |
| Model credentials | Typed on the phone, stored encrypted with an Android Keystore key. Never uploaded, never read from the project (`.env` is not mirrored). |
| Which model a chat uses | Chosen per chat from the model chip at the top of the chat: any model of any connection, or the desktop project's model when a connection can serve it. A new chat starts with the project's last pick; a chat keeps its own. Connections hold a list of models, typed or loaded from the provider (`/models`, Anthropic `/v1/models`). |
| Navigation | Settings, a chat and the file viewer open over the screen they came from, which stays mounted. A project's session lives for the life of the app, and a project re-entered (or reopened after a restart) is shown from the device's cache at once while it syncs. |
| Trying again | A question left without an answer (the turn failed, was stopped, or the app closed) is answered again in place: only the new answer is written, as the desktop's *Try again* reuses the stored question. |
| Retrying an answered turn, editing a question | Continue in a **branch**: a new chat holding a copy of the conversation up to that point, then the turn. A journal cannot supersede a row (chats_export.md §4.2), so the answer already shared with the desktop stays; the action says so. The desktop's own *Branch from here* and *edit an earlier message* work the same way. |
| Message options | Each message's ⋯ menu: Copy, Try again in a new chat, Edit and send (questions), Branch from here. |
| Tablets | At 720 px and wider, a project shows its chat list or file list beside the open chat or file; below that, screens stack as on a phone. The list is locked while a turn runs, as the full-screen chat is on a phone. Sheets become centred dialogs from 600 px, and conversations, settings and the project list keep a readable column width. Ctrl/⌘+Enter sends from a hardware keyboard. |
| Stack | React 18 + Vite + Capacitor 8, JavaScript like `gui_src/`, the same Markdown/KaTeX libraries and versions. |
| Location | `mobile/` in this repository, with its own CI workflow and `mobile-v*` tags. |

## Why a JavaScript agent and not agenticblocks

agenticblocks is Python. Embedding a Python runtime in an APK (Chaquopy) for
litellm and pydantic's native dependencies is heavy and fragile, for a loop the
phone can express in a few hundred lines. The app therefore has its own
provider adapters — OpenAI-compatible (OpenAI, OpenRouter, Gemini's OpenAI
endpoint, Ollama, DeepSeek, Groq, Mistral, xAI, custom) and Anthropic — and its
own bounded tool loop. The CLAUDE.md rule "go through agenticblocks" governs the
Python code; it does not reach into this app. The one change to agenticblocks
is general: `MemGPTAgentBlock.append_external_messages`.

The loop keeps the desktop's contracts: an unknown tool or bad arguments are
reported to the model, never rewritten into another call; a step limit (16) and
a repeated-failure breaker (3) end runaway turns with a diagnostic.

## Why model traffic goes through native HTTP

A web view's `fetch` is bound by CORS, and not every provider allows browser
origins. The plugin's `streamRequest` performs the request natively and streams
the body back through a Capacitor callback, decoding UTF-8 across buffer
boundaries. Drive calls stay on `fetch`: Google's APIs allow cross-origin
requests with a bearer token.

Native requests identify themselves as `GilzaBots-Mobile/<version> (Android
<release>)`. `HttpURLConnection` would otherwise send the platform default,
`Dalvik/2.1.0 (…)`, and ollama.com's edge refuses that with a 403 page before
the API key is checked (measured: every Dalvik agent → 403; any other agent,
including this one → the API's own 401 for a bad key). A refusal page in place
of an API error is reported as such rather than as its HTML.

Cleartext HTTP is not enabled. A LAN Ollama at `http://192.168…` is refused by
Android's default network security policy, with that error surfaced in the
chat. Enabling it is a security trade-off left to an explicit decision;
`https://ollama.com/v1` with an API key works.

## Why the agent borrows rather than shares the desktop's context

The phone reads, per chat, the desktop orchestrator's `recursive_summary`, the
project's or chat's core memory (per `use_shared_memory`), the project
description and main file, and the newest visible turns under a 40,000-character
budget. It does not replay `internal_history`: those tool calls name tools the
phone does not have. It never writes `agent_state`.

The other direction is the desktop's job: turns made on the phone are queued
for the orchestrator (`external_turns`, [chats_export.md §5](chats_export.md)).

## Google authorization

| Question | Decision |
| --- | --- |
| API | Google Identity Services `AuthorizationClient` (`play-services-auth` 22.0.0), scope `drive.file` only. |
| Client id in code | None. Android OAuth clients are matched by package name (`io.github.gilzamir18.gilzabots`) and signing-certificate SHA-1. |
| Which Cloud project | **The same one as the desktop's bundled client.** `drive.file` grants access per application, and Google identifies the application by Cloud project number (the Picker's `setAppId` is "the Cloud project number"). A client in another project would see an empty `GilzaBots` folder. |
| Tokens | One hour; kept in memory for 50 minutes; a 401 clears the cached token and authorizes again silently. |
| Refresh tokens | None on the device, as Google recommends. |
| Sign out | `revokeAccess` for the account of the last authorization; the app's local session is dropped either way. |

## Offline behaviour

The project list, each project's file index, the files that were opened (by
md5), and the conversation documents are cached in IndexedDB. A file never
opened on the phone is not cached; the agent's own reads (`read_file`,
`search_files`) cache what they read.

**Available offline** (a per-project switch at the top of the Files tab)
downloads every file the phone can show: text files up to 2 MB, PDFs and
images up to 50 MB each. Internal GilzaBots files are cached separately, and
other formats or larger files stay online and are counted on the card. It runs
when switched on and after every successful sync, downloading only files whose
md5 changed and dropping saved copies of files that left the project. It stops
at the first failure that is not about one file (the network went away) rather
than failing file after file, and continues on the next sync. Switching it off
frees the space, waiting for the file in flight first; the conversations stay.
A manifest of saved files (`cached:<root>`) answers "what is saved" without
reading bodies back out of IndexedDB. The app asks the web view for persistent
storage when it is switched on, where that is supported. Without Drive the last index and cached files are shown,
labelled offline. The journal is saved locally before every upload; sync runs on
open, on return to the foreground, when the network returns, every minute while
visible, and after each turn.

## Edits from the phone

`edit_file` replaces one exact, unique passage; `create_file` refuses an existing
path. The approval sheet shows the exact before/after. Before writing, the app
re-reads the file's md5 from Drive and refuses when it changed since it was read
(and, for a create, when a file with that name appeared). Drive v3 has no
conditional write, so this narrows the window to one round trip; a desktop that
edits in that window still loses nothing, because its sync engine sees both
sides changed and keeps a conflict copy. Nothing under `.gilzabots/` is writable.

## Open items

1. **Verify on a device**: register the Android OAuth client in the desktop's
   Cloud project (debug SHA-1 for development, release SHA-1 for releases) and
   confirm the project list shows folders the desktop created. Everything else
   depends on this.
2. Background sync while the app is closed (WorkManager) is not implemented;
   the offline copy is refreshed while the app is open.
3. Attachments (images, PDFs) in chat messages are not sent.
4. The browser check runs with web security off; CORS of each provider from the
   web view is not tested because the device does not use it.
