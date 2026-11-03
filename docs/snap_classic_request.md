# Classic confinement request for `gilzabots`

> Draft for the `store-requests` category of https://forum.snapcraft.io.
> Before posting, fill in the fields marked `TODO`.

---

**Title:** Classic confinement request for gilzabots

Hello reviewers,

I'd like to request **classic confinement** for the `gilzabots` snap.

- **Snap name:** `gilzabots`
- **Publisher:** TODO (Snap Store account name)
- **Upstream:** https://github.com/gilzamir18/GilzaBots (MIT license)
- **Website:** https://github.com/gilzamir18/GilzaBots
- **Contact:** https://github.com/gilzamir18/GilzaBots/issues
- **Affiliation:** I am the upstream developer and maintainer of GilzaBots. The
  snap is built from the repository's own `snapcraft.yaml`.

## What GilzaBots is

GilzaBots is a desktop IDE for LaTeX projects with a built-in AI assistant. A project is
a folder the user chooses anywhere on disk. The application has:

- an editor with a side-by-side PDF preview, SyncTeX, Git integration and a file explorer;
- an **integrated terminal**, a PTY running the user's `/bin/bash` in the project folder;
- an **AI coding assistant** that reads and edits project files and **runs shell commands
  in the project directory** (`run_command`, background and interactive commands). It uses
  local Ollama models or a third-party provider configured with the user's own API key.

## Categories

The request matches two categories listed in *Process for reviewing classic confinement
snaps*:

1. **IDEs**, and
2. **"AI agent/assistant that runs arbitrary user-directed code and needs to reach files
   and programs that aren't known at build time."**

## Why strict confinement is not enough

Both the terminal and the assistant run commands that the user chooses, or that the user
approves the assistant running, inside the user's own projects. The programs involved are
whatever the user's documents need. They cannot be known when the snap is built.
Examples from real LaTeX work:

- building with the user's **TeX Live** toolchain (`latexmk`, `pdflatex`/`xelatex`/
  `lualatex`, `biber`, `makeglossaries`), with packages and engines the bundled
  Tectonic does not cover;
- `--shell-escape` workflows that call external programs: `pygmentize` (minted),
  `gnuplot`, `inkscape` (SVG import), `graphviz`, `R`/`python3` scripts that generate
  figures and tables from the user's own environments;
- project build systems (`make`, custom scripts) and the user's `git` with its
  configuration, hooks and credential helpers;
- Python virtual environments in the project folder. In the strict snap, `python -m venv`
  in the terminal builds on the `core24` Python 3.12, so an environment made there does
  not run in the user's own shell, and one made with the host's Python does not run
  inside the snap.

Under strict confinement the snap's processes run on the `core24` runtime, so the host's
executables and their libraries are not available to them. No interface exposes an
arbitrary, user-chosen set of host programs to a strict snap. `personal-files` and
`system-files` need paths that are fixed at build time, and the set of tools here is open
ended by nature. Bundling them is not a realistic alternative either: we would have to
ship a complete TeX distribution plus every tool any document might call, and it would
still differ from the environment the user has configured.

Today we can only point strict-snap users to the direct (non-snap) installer when they hit
this limit. The snap then fails at the product's main purpose: letting the assistant and
the terminal work with the user's toolchain.

## What classic is *not* requested for

- **Not for privilege escalation.** GilzaBots never runs `sudo`/`pkexec` and never asks for
  root. Everything runs as the logged-in user.
- **Not for dot-file access, `/etc` access or hard-coded paths.** GilzaBots only needs its
  own data directory (`.gilzabots` in the home directory by default, and the user can move
  it). This already works under strict confinement.
- **Not to install other packages or snaps**, and not to work around missing dependencies.
  The application's own runtime (Python, Qt/QtWebEngine, Tectonic, pandoc, git) is
  bundled in the snap.

## Safeguards on command execution

The assistant has three execution modes. The user chooses one when creating a project and
can change it at any time in the chat toolbar:

- **plan**: cannot modify anything or run commands; it proposes a plan for approval;
- **edit**: edits files but **asks for confirmation before every terminal command**;
- **auto**: runs tools without asking at each step.

Commands run as the user, with the project folder as the working directory. The
assistant's changes can be reviewed in the Review mode and rolled back through checkpoints
before the user keeps them.

## Packaging changes for classic

The classic build will:

- drop `plugs:` and the `gnome` extension;
- use `build-attributes: [enable-patchelf]` so the bundled binaries (Python, the PyQt6/
  QtWebEngine libraries, Tectonic, pandoc, git) resolve their interpreter and libraries
  inside `$SNAP` instead of on the host;
- avoid exporting `LD_LIBRARY_PATH`, and clear snap-internal variables from the
  environment given to the terminal and to commands. That way host programs run with the
  host's own libraries.

## Existing users

`gilzabots` is currently published with strict confinement. TODO: choose one:

- (a) move the default track to classic and tell users to run
  `snap refresh gilzabots --classic`, or
- (b) keep strict on the default track and publish classic on a separate track, as the
  review process document suggests for snaps that switch.

We are happy to follow the reviewers' recommendation on this.

Thank you for your time!
