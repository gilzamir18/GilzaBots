# GilzaBots

GilzaBots is a free, open-source AI agent that works inside your projects. It reads and edits files, runs commands, searches the web, writes code and documents, and keeps watch with background monitors — on a local model through Ollama or on a provider you configure with your own API key.

Everything runs on your machine. There is no account and no billing; API keys for third-party AI providers are optional.

## Features

- An agent with tools for files, code search, the shell, the web and office documents, plus skills and helper agents for specialised work
- Monitor agents that watch for conditions in the background and notify you or wake a chat
- Local models through Ollama and configurable external providers
- A workspace with editor, terminal, Git, and Markdown, HTML and PDF previews
- LaTeX support: PDF preview with SyncTeX and local compilation with Tectonic
- English and Brazilian Portuguese interface
- A command-line interface that runs the same agent without the desktop window

## Minimum Software Requirements

- Linux: Ubuntu 24.04 or 26.04
- Windows: Windows 10 or 11

## Install a packaged release

The installer downloads the current GilzaBots Community release from GitHub Releases, extracts it into the user profile, creates shortcuts, and does not require administrator privileges.

### Linux / macOS (Terminal One-Liner via `curl`)

Run terminal and execute:

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh | bash
```

To inspect the script first:

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh -o install.sh
less install.sh
bash install.sh
```

The application is installed under `~/.local/share/GilzaBots`, with a command symlink in `~/.local/bin/gilzabots` and a desktop launcher in `~/.local/share/applications/gilzabots.desktop`.

### Snap installations and the integrated terminal

The snap runs under strict confinement, so its integrated terminal cannot run programs installed on the host. There, `python` is the Python 3.12 bundled with GilzaBots: `python -m venv .venv` works, but the environment it creates is meant to be used from GilzaBots's terminal, and one created in a system terminal will not work inside GilzaBots, because each side has a different `/usr/bin/python3`. If you need your own toolchain (your Python, TeX Live, `make`, and so on) from the terminal or the AI assistant, use the direct installer above.

### Windows (PowerShell One-Liner via `irm`)

Open PowerShell and execute:

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 | iex
```

Alternatively, download and inspect `install.ps1` before running:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 -OutFile install.ps1
Get-Content .\install.ps1
.\install.ps1
```

The application is installed under `%LOCALAPPDATA%\GilzaBots`. The installer adds its executable directory to the user `PATH` and creates shortcuts on the Desktop and Start menu.

## Uninstall a packaged release

Direct Linux and macOS installations provide this command:

```bash
gilzabots-uninstall
```

It removes the application while preserving settings, chats, caches, credentials, and project directories. Pass `--purge` only when you also want to remove the global GilzaBots data directory. Project directories are never removed.

Older direct installations that do not yet provide the command can run:

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/uninstall.sh | bash
```

On Windows, open **Settings > Apps > Installed apps > GilzaBots**, or use **Uninstall GilzaBots** in the Start menu. The Windows uninstaller also preserves user data by default; its optional PowerShell `-Purge` switch removes global application data after confirmation.

For an older Windows installation without that entry, run:

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/uninstall.ps1 | iex
```

Snap installations remain managed by Snap and can be removed from a system terminal with:

```bash
sudo snap remove gilzabots
```

## Feedback and bug reports

Suggestions, criticism and feature ideas are welcome in the [GilzaBots feedback form](https://forms.gle/MsfYN4Yx9Cy8QNAR6), also available inside GilzaBots under **Settings > About > Send feedback (form)**.

To report a bug, use **Settings > About > Send feedback on GitHub**: the application opens a pre-filled issue at the [GilzaBots issue tracker](https://github.com/gilzamir18/GilzaBots/issues) with version and runtime information. Nothing is submitted automatically, so review the issue and remove private information before publishing it.

## Command-line interface

The same agent, projects and skills, in a terminal instead of a window. It is the base
install, with no desktop stack:

```bash
uv tool install gilzabots     # or: pip install gilzabots
```

The desktop application needs the `gui` extra:

```bash
pip install "gilzabots[gui]"
```

LaTeX compilation uses a `tectonic` found on your `PATH`.

### Usage

```bash
cd my-thesis
gilzabots --cli                       # interactive session for the project in this folder
gilzabots --cli --here                # register this folder as a new project first
gilzabots --cli --project my-thesis   # pick a project by name

gilzabots -p "fix the compilation errors in chapter 3"   # one turn, then exit
echo "summarise the changes" | gilzabots -p -            # prompt from standard input
```

The answer streams as the model writes it, tool calls appear as `⏺ tool_name(...)` with
their results, and reasoning is dimmed (`/thoughts off`, `/tools off` to quieten them).
`Ctrl+C` interrupts the running turn without ending the session — what the agent had
already written is kept, and `/resume` continues from there. In `edit` mode the agent
asks for permission in the terminal before each change; in `plan` mode it presents the
plan for approval.

`/help` lists the commands by section with the arguments each one takes, and
`/help <command>` shows worked examples — `/help /models`, `/help /providers`,
`/help /chat`. Beyond the commands shared with the desktop chat, the terminal adds
`/chat` (list, create, switch conversations), `/effort` (this chat's reasoning effort),
`/compile`, `/cost` and `/resume`.

### Registering a model

A usable model is two records: a provider connection holding the credentials, and a
catalog entry naming a model under it. Both are managed from any text front-end.

```bash
/providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1
/providers add label="OpenAI" provider=openai api_key=sk-...
/providers                                    # list connections

/models add name=gemma4:26b connection=ollama-gil num_ctx=65000 supports_thinking=true
/models list                                  # the catalog
/models show ollama/gemma4:26b
/models set ollama/gemma4:26b profile=light policy=delegate
/models remove ollama/gemma4:26b

/set-main-model ollama/gemma4:26b             # point this project at it
```

Any of these also runs as a one-shot, so a machine can be set up from a script:

```bash
gilzabots -p '/providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1'
gilzabots -p '/models add name=gemma4:26b connection=ollama-gil num_ctx=65000'
```

`/set-main-model` only stores an id; without a catalog entry behind it there is no API
base, no key and no capabilities. An unrecognised field is refused rather than guessed
at, so a typo cannot become an invisible setting — a parameter you do want forwarded to
the provider is written `extra.keep_alive=30m`.

The editor features — PDF preview, SyncTeX, rich-text and WYSIWYG modes, PDF
annotations, PDF presentation mode, dictation and read-aloud — are part of the window and
have no terminal equivalent.

## Development setup

Requirements: Python 3.10 or newer, Node.js/npm, and Git.

### PowerShell

```powershell
git clone https://github.com/gilzamir18/GilzaBots.git
cd GilzaBots
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm install --prefix .\gui_src
npm run build --prefix .\gui_src
python .\main.py
```

If script execution is disabled for the current PowerShell process, run `Set-ExecutionPolicy -Scope Process Bypass` before activating the virtual environment.

### Bash (Linux/macOS)

```bash
git clone https://github.com/gilzamir18/GilzaBots.git
cd GilzaBots
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm install --prefix ./gui_src
npm run build --prefix ./gui_src
python main.py
```

## Build and test

Run the test suite from the repository root:

```bash
python -m pytest
```

Rebuild only the web interface with:

```bash
npm run build --prefix gui_src
```

Create a packaged desktop build with the platform-specific script:

```powershell
.\build_exe.ps1
```

```bash
bash ./build_exe.sh
```

These scripts install build dependencies, build the React/Vite interface, download pinned Tectonic and Pandoc releases, and package the application with PyInstaller. Outputs are written under `dist/`.

## Release publishing

The `binpacking.ps1` and `binpacking.sh` scripts are maintainer tools. They package an existing build and upload it over SSH. They intentionally contain no server address, username, credential, or private server path. Maintainers must provide:

- `GILZABOTS_RELEASE_HOST`
- `GILZABOTS_RELEASE_USER`
- `GILZABOTS_RELEASE_DIR`

Authentication is handled by the local SSH client (preferably with an SSH key or agent); credentials must never be committed to the repository.

## Security and credentials

- API keys configured in the application are local user data and must not be committed.
- Environment files, private keys, credentials, databases, logs, build outputs, and local runtime data are excluded by `.gitignore`.
- Report a suspected leaked credential privately to the maintainers. Revoke and rotate any credential that may have entered Git history; deleting it only from the latest commit is not sufficient.

## Contributing

Although GilzaBots is open-source under the MIT License, the repository does **not** accept pull requests or code contributions from third parties.

If you would like to request a specific feature or report a bug, please **open an Issue** describing what you would like to see in GilzaBots. The maintainer will review and prioritize requests accordingly.

## License

GilzaBots is available under the [MIT License](LICENSE).
