# Cloud project mirroring — decisions and current state

Status: implemented. Google Drive plus a local-folder backend, behind a
provider-neutral facade in `gilzabots/cloud/`.

This file records the decisions that are already settled, so they do not get
re-litigated. The architecture itself is described in §2.16 of
[PROJECT_DESIGN.md](../../PROJECT_DESIGN.md).

## Scope decisions

| Question | Decision |
| --- | --- |
| What is mirrored | The whole project: every file in the directory **and** the project's conversations, chats, activity and per-chat memory. |
| Conversations | Exported from the global `sessions.db` to `.gilzabots/session/chats.json`, which syncs as a file and is merged back on arrival. The database itself is never mirrored. |
| Build artifacts (`.aux`, `.log`, `.synctex.gz`, the compiled PDF) | Included by default; a per-project toggle turns them off. |
| `.env` | **Excluded by default.** It holds provider API keys. Including it requires an explicit opt-in behind a confirmation. |
| OAuth scope | `drive.file` only. |
| OAuth client | Shipped with the build (injected at package time). A user-registered client is an optional override. |
| Direction | Two-way, with conflict copies. Push-only and pull-only exist in the engine and are reachable through the API; pull-only is what a download uses. |
| Getting a project onto a second machine | Explorer header → the cloud icon: list the account, pick a project, pick an empty folder, download. The clone is registered locally and keeps syncing. |
| Conflict resolution | The user chooses per file: keep mine, keep the cloud version, or keep both. |
| Progress feedback | Per-file badges in the explorer, plus the file in flight and a counter in the status bar. |
| Default state | Off. A project transfers nothing until the user enables it. |
| Other devices (the mobile app) | Write their own journal under `.gilzabots/session/devices/`, never `chats.json`; a pass merges the journals it downloads. Turns merged into a chat with saved orchestrator state are queued for that orchestrator. See [chats_export.md](chats_export.md) and [mobile_app.md](mobile_app.md). |

## Why `.env` is the one exception to "sync everything"

Every other exclusion is about noise or breakage — the shadow git repository,
this machine's own sync baseline, `node_modules`. `.env` is different in kind:
uploading it copies live API credentials into cloud storage, where they are
subject to that provider's sharing, indexing and retention. That is a security
decision the user has to make deliberately, so it is a toggle with a
confirmation rather than a default.

## Why `drive.file` and not `drive`

Google classifies scopes into three tiers, and the tier decides what the project
has to pay for:

- `drive.file` — per-file access to what the app creates or the user explicitly
  opens with it. **Non-sensitive**: no verification review, no annual security
  assessment.
- `drive` — full access to the user's Drive. **Restricted**: requires Google
  verification *and* an annual CASA assessment by an authorized third party,
  which is paid and recurring.

`drive.file` is sufficient, because the app only ever touches its own folder.
Requesting `drive` would attach a recurring paid audit to an MIT project with no
revenue, in exchange for access the feature does not use.

## Why the OAuth client ships with the build

**Superseded decision.** The first cut required each user to register their own
"Desktop app" client and paste it into Settings. That is a developer chore in
front of a consumer feature: the Account tab opened on a client id field, and a
user who is not already a Google Cloud user simply could not connect. The
expected flow — the one Insync, rclone and every desktop Drive client provide —
is: press Connect, authorize in the browser, done.

So a release now carries its own client, and the user never sees a credential.

### What that costs, and why it is accepted

- **The shipped secret is not confidential.** A desktop application hands its
  credentials to every user, so Google classifies these as *public* clients;
  the flow is bound by PKCE, not by the secret. This is what every desktop
  client that talks to Drive does.
- **All users share one project's API quota.** Drive's per-project quotas are
  large relative to a mirroring workload, and the alternative was a feature
  nobody could turn on.
- **The consent screen names the maintainer**, who must publish it. With
  `drive.file` only — a non-sensitive scope — publishing does not require the
  verification review or the CASA assessment. Left unpublished (in "Testing"),
  only added test users can authorize and refresh tokens expire in seven days,
  so publishing is not optional for a release.

### How the client reaches the build

`scripts/embed_google_client.py` writes
`gilzabots/cloud/providers/bundled_google_client.json`, which PyInstaller picks
up as package data. The file is git-ignored: the credentials come from the
release pipeline's secrets (`GILZABOTS_GDRIVE_CLIENT_ID` /
`GILZABOTS_GDRIVE_CLIENT_SECRET` in the build workflow), not from the
repository. A build without those secrets still works — it simply ships no
client, and the Account tab asks for one.

### The release that shipped without a client

Verified against the published `v0.2.10` asset that `install.ps1` installs
(`GilzaBots-windows-x64.zip`, 561 MB, 11478 entries, whole central directory
read): **`bundled_google_client.json` is not in it.** Every install from that
release therefore had Cloud sync unable to open the Google sign-in at all — the
Connect button had no client to build an authorization URL from, so it could
only report the missing-client message. Nothing in the app could have fixed
that; OAuth has no flow without a client id.

The cause is a chain of optional steps, each individually reasonable:
`build_exe.ps1` injects the client only `if ($env:GILZABOTS_GDRIVE_CLIENT_ID)`
and otherwise prints a note and carries on; the workflow feeds that env var from
a repository secret; an unset secret therefore produces a complete, publishable,
silently crippled release.

Two guards in `.github/workflows/build.yml` close it, both scoped to a published
build so forks and branch builds stay permissive: the secrets are required
*before* the build runs, and the packaged `dist/` is checked for the file
*after* it. A release now fails instead of shipping with the feature dead.

### The packaging trap this fell into

`bundled_google_client.json` is `.gitignore`'d, and hatchling drops VCS-ignored
files from the wheel by default. The snap installs with `pip install .`, so
**every installed build shipped without a client** while the source tree had
one — and the only symptom was Cloud sync answering "this build of GilzaBots
carries no Google OAuth client", sending the user to the Google Cloud console
for a chore the feature exists to avoid. A working dev checkout hides it
completely, because there `__file__` resolves to the source tree.

Two things hold it shut now: `artifacts` in `[tool.hatch.build.targets.wheel]`
(hatchling's exception list for generated files), and a check in the snap's
`override-build` that the installed package still has the file whenever the
source tree did. A checkout with no credentials still builds — it just says so.

### Resolution order

`google_drive.describe_client_config()` picks the first source that carries a
client id:

1. `GILZABOTS_GDRIVE_CLIENT_ID` / `_SECRET` in the environment — development, or
   a distribution injecting a client at run time.
2. The user's own client, saved through Cloud sync → Account → *Use my own
   Google OAuth client*. It **overrides** the bundled one, so a user who wants
   their own quota and their own consent screen gets them; *Use the GilzaBots
   client* deletes the override.
3. The client bundled with the build.

A source with an empty client id falls through to the next, so a half-written
override cannot disable a working connection.

### What a user has to do once (only when the build ships no client)

1. Google Cloud console → create (or pick) a project.
2. Enable the **Google Drive API** for it.
3. OAuth consent screen → External → add themselves as a test user. With
   `drive.file` there is nothing to submit for review.
4. Credentials → Create credentials → OAuth client ID → **Desktop app**.
5. Paste the client ID and secret into GilzaBots → Cloud sync → Account.

## Why downloading a project is its own flow

Mirroring only covers the machine that already has the project. The second
machine has nothing to enable sync *for*, and the sync panel needs an open
project to configure — so the download lives in its own modal, reached from the
Explorer header beside New and Import.

| Question | Decision |
| --- | --- |
| Where the list comes from | `CloudStorageProvider.list_projects`, gated by `Capabilities.project_listing`. A backend pointed at a single folder declines instead of answering "no projects". |
| Which folder is downloaded | The provider's own root handle from the listing, not the name. On Drive, resolving a name a second time can create a second folder with that name. |
| Where it lands | A folder that is new or empty. Enforced, not preferred — see below. |
| Direction | Pull-only. The remote is the only copy that means anything yet, and nothing local should be published before the user has opened it. |
| What comes with it | Everything the mirror holds: files, build output, and the conversations, which are merged into this machine's `sessions.db` after the project is registered. |
| What does not | `.env`, because it is not mirrored in the first place. The API keys stay on the machine they were typed on, and the panel says so. |
| Already-downloaded projects | Listed but not selectable, annotated with the local path. Two working copies of one remote folder would fight over it. |

### Why the destination has to be empty

A clone is a pull, and a pull into a directory that already has files
*reconciles* them against the remote instead of downloading a copy of the
project. That silently merges two unrelated things, which is exactly the class
of outcome the three-way baseline exists to avoid. Refusing loudly is the only
answer that cannot lose work, so a non-empty folder is an error with the reason
in it, not a prompt.

The same rule decides what happens when a download fails halfway: whatever
arrived is left on disk to be inspected, nothing is registered, and the error
names the folder to remove before retrying. Resuming would mean reconciling a
half-copy.

### Ordering: pull, then register, then merge the conversations

Three constraints fix this order, and each of them bites if the order changes.

1. **Registration scaffolds.** `ProjectStore.create` writes a starter
   `skills.yaml`. Registering first would overwrite the one that just arrived,
   and the next pass would publish that loss back to the other machine. Hence
   `create(preserve_existing_skills=True)`, used only here.
2. **The conversation merge needs a project row.** `merge_into_database`
   refuses to write history for a project that does not exist locally, so the
   merge has to come last.
3. **Last-writer-wins gives the wrong answer exactly once.** The row was created
   seconds ago, so it is always "newer" than the export, and the model, mode,
   description and main file chosen on the other machine would never be
   adopted. `adopt_settings=True` bypasses the timestamp comparison, and is only
   correct on a project with no history of its own.

The clone also reuses the internal project name from the incoming export when it
is free on this machine: reserved chat ids are derived from it
(`tutorial_<name>`), so under a fresh key the tutorial button would open a
second tutorial chat beside the imported one. (New projects are created without
any chat, so the clone no longer gains an empty "Main Chat" either way.)

### The pull contract had a hole

`SyncEngine._resolve_conflict` uploaded the working copy regardless of
direction, so a pull could overwrite the very version it was asked to fetch.
Nothing exercised it — pull-only was API-only until the clone used it. In PULL
the engine now parks the remote version beside the local one, reports the
conflict and leaves the baseline untouched for a two-way pass to settle.

## Cancelling a sync or a download

| Question | Decision |
| --- | --- |
| What can be cancelled | Any running pass: a manual or background sync, a preview, a download. All register their progress under the path they write to, and `POST /api/cloud/cancel {projectPath}` flags that path. |
| When it takes effect | Between files. The file in flight finishes; interrupting inside one transfer would need a cancel token in the provider facade. |
| A cancelled sync | Keeps everything it already moved in the baseline, skips the conversation merge, and reports `cancelled`. The next pass continues from there — it is a partial pass, not a failure to undo. |
| A cancelled download | Removes the partial copy, and the destination folder too when the download created it. Nothing is registered. |

### Why a cancelled download is deleted when a failed one is kept

A failed download is left on disk because something went wrong and what arrived
may help explain it. A cancelled one has nothing to explain: the user said they
do not want it. Leaving it would also leave a non-empty folder that the next
download into the same place refuses. Deleting it cannot lose anything: the
destination was verified empty before the first byte, so every file in it came
from the clone and is still in the cloud.

## Deleting a project's cloud copy

| Question | Decision |
| --- | --- |
| Offered when | The project has a cloud folder recorded by a pass. The delete dialog asks `/api/cloud/link`, which reads local state only. |
| Default | Off. Removing a project from this computer is not a reason to remove it everywhere. |
| Google Drive | The folder goes to the Drive trash (recoverable for a limited time), like every other Drive delete. |
| Local folder | Removed permanently; it has no trash. The dialog says so before the user confirms. |
| Order | Cloud first, and it can veto: if the cloud copy cannot be deleted, nothing local is deleted either. |
| Other computers | Their next pass finds the folder gone and stops with their files intact (`remote_missing`). The sync panel offers to upload a new copy or turn sync off. |

### Why a vanished folder is no longer re-created

Both providers used to answer "the recorded folder is gone" by creating a new,
empty one. Against the baseline, an empty folder reads as "every file was
deleted in the cloud", so the pass deleted the working copy. The bulk-delete
guard only intervenes at ten or more files; a smaller project on another
computer was wiped outright. That was already reachable by trashing the folder
by hand in Drive. Offering deletion from the app would have made it routine.
Now the engine stops and the user decides.

A network failure while checking the folder is not a deletion. Only a definite
answer counts (Drive: 404 or trashed). Anything else propagates as the error it
is.

## Why a conflict asks instead of deciding

The engine never merges, and it never picks a winner: it keeps the working copy,
uploads it, and writes the other machine's version beside it as a conflict copy.
That loses nothing, which is the point — but on its own it leaves the user
staring at two files.

`service.resolve_conflict` is the answer being applied:

| Choice | What happens |
| --- | --- |
| Keep mine | The working copy is re-sent and the copy is dropped. |
| Keep the cloud version | The conflict copy is promoted over the working copy and uploaded. |
| Keep both | Nothing — that is already the state, and the copy syncs as an ordinary file. |

**Where the cloud's version lives matters here.** By the time the user chooses,
the remote already holds the *local* version, because the conflicted pass
uploaded it. So "keep the cloud version" reads the conflict copy; downloading
would hand back the very version the user just rejected. When that copy is
missing, the call fails with that explanation instead of guessing.

Either choice rewrites the baseline to the resolved content, so the next pass
sees one agreed version rather than reporting the same conflict again.

## Why progress is polled, not streamed

The server's SSE channel (`active_queues`) exists only while an agent run is
streaming; it is not a general event bus, and building one for sync would be a
larger change than the feature needs. `/api/cloud/status` already carried the
sync state and the front-end already polled it, so progress rides along there:
a slow 20 s tick normally, 1 s while a pass is running. Passes are short and the
snapshot is cheap to read.

The per-file states in `/api/cloud/file-states` are the one part that is not
cheap — they come from a tree scan — so that scan is memoized for a few seconds
and dropped at the end of a pass. The parts that change by the second (the file
in flight, the conflicts) are overlaid on every call.

## Why the HTTP calls do not go through plain `urllib`

Staying stdlib-only (see *The Drive backend is stdlib-only*) quietly imported
one of `urllib`'s weaknesses: `socket.create_connection` walks the addresses a
host resolves to **in order**, spending the entire socket timeout on each one
that does not answer before trying the next.

That is harmless until a host's addresses are not uniformly reachable. Observed
on a user's connection: `www.googleapis.com` resolved to 8 AAAA and 8 A records,
**six of the AAAA blackholed** while all 8 A records answered in ~40 ms.
Resolvers return every IPv6 address first, and the Drive backend's socket
timeout is 120 s because it streams large files — so a listing that needs three
requests could wait tens of minutes. The symptom was a spinner that never
resolved and no error anywhere, because nothing had failed *yet*.

The same machine's browser and its other Drive sync client worked normally,
which is exactly why this looked like an application bug and not a network
fault: both implement RFC 8305 (Happy Eyeballs), racing the families rather than
exhausting one. `cloud/transport.py` now does the same — interleave families,
start an attempt every 250 ms, first socket to complete wins, losers closed.

It also splits a timeout that `create_connection` conflates. The **connect**
phase is capped at 10 s, so a silent address costs seconds rather than minutes;
the **read** phase keeps whatever the caller asked for, so a large upload or
download is not truncated by a short bound. Both the Drive provider and
`oauth.py` route through it; a stalled token refresh hangs a sync just as a
stalled API call does.

`tests/test_cloud_transport.py` pins the behaviour with a fake resolver, so the
dead-IPv6 case is reproducible without depending on the network under test.

## Why change detection is not the shadow git repo

Reusing `.gilzabots/.shadowgit` looks obvious — it already tracks content hashes
and can answer "what changed since X". It was rejected for two reasons:

1. It deliberately ignores LaTeX build output, which is part of what the feature
   is asked to mirror. It cannot report changes to files it does not track.
2. The VCS strategy is user-configurable and `NoGitStrategy` is supported. A
   git-only design would leave those projects unable to sync at all.

The manifest in `.gilzabots/cloud/state.json` is used instead, with an
exact size+mtime fast path so unchanged files are not re-hashed.

## What is deliberately not solved

- **Real-time collaboration.** Two people editing the same file at the same
  time is a different problem (CRDT/OT, presence, a state server) and does not
  come out of a storage facade. The backlog item for 0.1.3 mixed the two; this
  work covers mirroring only.
- **Deletion of conversations does not propagate.** The chat merge is a union,
  so a chat cleared on one machine returns from the other. For a history the
  user cannot reconstruct, resurrecting a message is a nuisance while dropping
  one is data loss.
- **Merging conflicted file content.** Conflicts produce a copy beside the
  original; the user decides. Auto-merging LaTeX would produce a file that still
  compiles while saying something the author never wrote.
- **Per-file selective sync.** The exclusion list is glob-based, not a picker.
- **Resuming an interrupted download.** A half-copy is deleted or renamed by
  hand and the download is started again; the empty-folder rule is what keeps a
  partial clone from being reconciled against the remote.
- **Downloading into a folder that already holds files.** Same rule. Importing
  an existing directory is what Import Project is for.

## Adding another provider

Implement `CloudStorageProvider` in `gilzabots/cloud/providers/`, add an entry to
`gilzabots/cloud/registry.py`, and add the module to `hiddenimports` in
`GilzaBots.spec`. `list_projects` is optional — a backend that cannot enumerate
leaves `Capabilities.project_listing` false and the download flow reports that
instead of showing an empty account. `delete_root` is optional too — without it the delete dialog's cloud
option fails with the backend's message and nothing is deleted; set `recoverable_root_deletion` when it
goes to a trash. `ensure_root` must raise `CloudRootMissing` for a recorded root that no longer exists,
never create a replacement. Nothing in the engine or the service layer should need to
change; if it does, the facade has leaked and that is the bug to fix first.

`tests/test_cloud_sync.py` is the contract suite — point its `provider` fixture
at the new backend and it should pass unchanged.

Backends that fit the current facade without stretching it: Dropbox, OneDrive,
WebDAV/Nextcloud, S3-compatible object storage. Each needs to answer three
questions the facade asks: can it report a content checksum, does it support a
conditional write, and what is its maximum file size (`Capabilities`).
