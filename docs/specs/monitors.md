# Monitor agents — decisions and current state

Status: implemented in [`opalatex/monitors/`](../../opalatex/monitors/). The
architecture is §2.24 of [PROJECT_DESIGN.md](../../PROJECT_DESIGN.md). Verified by
`tests/test_monitors.py` and end to end on Ubuntu/GNOME (daemon, D-Bus
notification, headless wake-up turn). **Not yet verified on Windows or macOS,
nor with a click on a GNOME notification** — see *Open items*.

## Scope decisions

| Question | Decision |
| --- | --- |
| Where monitors run with every front-end closed | A per-user daemon, `opalatex monitors daemon`, registered with the OS login service. Not a tray icon (it would change what closing the window means for everyone) and not only a detached process (it would not survive a logout or reboot). |
| Login service per platform | `systemd --user` (Linux), the snap's `autostart:` app (snap), a LaunchAgent (macOS), `HKCU\…\Run` (Windows). On by default; a toggle in the panel and `opalatex monitors service`. |
| How processes coordinate | One SQLite file, `<opalatex home>/monitors.db`. No socket: nothing has to be running for another process to register, pause or cancel a monitor. |
| Who runs a wake-up while the window is open | The window, for its open project, as an ordinary streamed turn. The daemon runs every other wake-up headless, in a child process. |
| A monitor's turn needs a question or an approval | It does not ask outside its chat: the turn is unattended, every question tool refuses with a diagnostic, and the agent puts what it needs in its reply in the chat (new or origin, as configured), where the user answers. Nothing is approved for the user. *Superseded:* the first version stored the question, notified and waited; in use a vague instruction produced six question dialogs in a row outside the chat. |
| Two turns in the same chat from different processes | Refused: every orchestrator turn holds a per-chat lease. |
| Evaluation | Deterministic first (HTTP/`json_path`, command, file, timer); a model only for an `llm` check, with a 5-minute minimum interval. |
| Notifications | Native: Freedesktop D-Bus (GNOME, KDE…), WinRT toasts on Windows, `osascript` on macOS. A click opens the chat. |
| Delivery | All delivered through the daemon's outbox, because the daemon is the process that receives the click. |
| A watch that fails | Five consecutive errors stop it with a critical notification; it does not retry forever. |
| A wait that times out | The waiting chat is woken with the reason, so the orchestrator decides (wait again or ask the user). |
| Code updated while the daemon runs | The daemon restarts itself on the new code (checked every 30 s). |
| A login service and several data folders | The service records the data folder it serves; another folder can neither replace nor remove it. |
| A wake-up in a new chat with the window open | It runs in the background (the daemon), never in the window: the user's chat is not taken over and the window stays free for their own turns. A notification and a *Recent events* entry with *Open chat* announce it; the activity bar counts unseen events. *Superseded:* the window switched to the new chat. |
| Firing once or every time | The user's choice, never overridden. Unset: a task wait (wakes its own chat) fires once; a watch and a new chat at every firing keep going. A wake-up whose monitor remains `active` is told so in `wake_prompt` and `register_monitor` refuses to re-register while the waking monitor continues (or when an identical active monitor already exists in the project). |
| A watch that ends (checks or time used up) | A low-urgency notification; no chat is opened to report it. A task wait still wakes its chat with the reason. |
| Which chat a wake-up runs in | Chosen per monitor: the chat it was registered from (default — continues a task with its context) or a new chat in the same project for every wake-up (recurring work that should not crowd the original conversation). A chat that no longer exists or cannot be created fails the wake-up with a notification; it is never redirected to another chat. |

## Why a daemon and not the window's server

`ide_server` exits with the window (`os._exit(0)` after `webview.start`), and a
turn binds project, store and orchestrator in module globals, one turn per
process. A monitor that must notify at 3 a.m. with the window closed needs a
process of its own; a wake-up of another project than the one the window has
open needs a process that can bind that project — hence the child per
wake-up.

## Why the window, not the daemon, runs wake-ups of its open project

The user is looking at it. Run by the daemon, the turn would only appear when
the chat reloads, without streaming, thoughts or the window's own approval
dialogs; run by the window it is indistinguishable from a turn the user
started, and every existing contract of §2.6 applies to it unchanged.

## Why HKCU `Run` and not a scheduled task on Windows

A task with a logon trigger fails with "Access is denied" without elevation on
many systems; the per-user `Run` key needs none and is what per-user
background applications use.

## Why `gdbus` and not a Python D-Bus library

No new dependency: `gdbus` ships with GLib wherever GNOME runs, and the snap
stages it (`libglib2.0-bin`). The daemon keeps one `gdbus monitor` for
`ActionInvoked`; every call passes typed GVariant text (`uint32 0`, `@as []`,
`<byte …>`, `int32 -1`), because under Snap confinement the `desktop` plug
denies `Introspect` on `/org/freedesktop/Notifications` and `gdbus` parses
untyped integer literals as `int32`.

## Open items

- **Windows**: the toast, the AppUserModelID registration, the
  `opalatex-monitor:` scheme and the `Run` key are implemented but have not been
  run on Windows.
- **macOS**: the LaunchAgent and `osascript` notifications are untested.
- **GNOME click**: the `ActionInvoked` parsing is unit-tested and the
  notification was shown on GNOME 50, but clicking it has not been exercised by
  a person. GNOME may drop actions of a notification whose sender (the
  short-lived `gdbus call`) has exited; if so, the sender must stay connected.
- **Snap**: `autostart:` and the staged `gdbus` need a snap build to confirm.
- A wake-up with a real model, end to end, has not been run (verified up to
  `handle_run` refusing a project without a model).
