"""Monitor agents: background watchers that notify the user or wake a chat.

A monitor is a stored, deterministic description of *what to check* (a timer,
an HTTP endpoint, a command, a file, or -- the one expensive kind -- a question
answered by a model), *when* (a fixed interval, an optional first delay, a
check budget and an expiry) and *what to do when its condition holds* (an OS
notification, waking the chat's orchestrator, or both).

The scheduler runs in the monitor daemon (``gilzabots monitors daemon``), a
process of its own, so monitors keep working with every front-end closed. The
daemon is registered with the operating system's per-user service manager and
restored at login (``gilzabots.monitors.service``). Every process -- the window,
the CLI, the daemon and the turns it starts -- shares state through one SQLite
file, ``<gilzabots home>/monitors.db`` (``gilzabots.monitors.store``).

See PROJECT_DESIGN.md section 2.24 and docs/specs/monitors.md.
"""
