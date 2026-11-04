<p align="center">
  <img src="icon.png" width="120" alt="GilzaBots logo">
</p>

<h1 align="center">GilzaBots</h1>

<p align="center">
  <strong>A free, open-source AI agent that works inside your projects.</strong><br>
  It reads and edits files, runs commands, searches the web, writes code and documents,<br>
  and keeps watch with background monitors, on a local model or on a provider you choose.
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-6d5ce8">
  <img alt="Platforms: Ubuntu 24.04, Ubuntu 26.04 and Windows 11" src="https://img.shields.io/badge/platform-Ubuntu%2024.04%20%7C%2026.04%20%7C%20Windows%2011-6d5ce8">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-6d5ce8">
  <img alt="Interface: English and Brazilian Portuguese" src="https://img.shields.io/badge/UI-English%20%7C%20Portugu%C3%AAs-6d5ce8">
</p>

<p align="center">
  <a href="#-install">Install</a> ·
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-features">Features</a> ·
  <a href="#%EF%B8%8F-command-line-interface">CLI</a> ·
  <a href="#%EF%B8%8F-development">Development</a> ·
  <a href="#-feedback-and-bug-reports">Feedback</a>
</p>

<p align="center">
  <img src="docs/images/workspace-studio.png" alt="GilzaBots Studio layout: a Markdown file in the editor with its live preview, the agent chat and the integrated terminal" width="100%">
  <br>
  <sub>The <b>Studio</b> layout: editor and live preview on top, the agent chat and the integrated terminal below.</sub>
</p>

> [!NOTE]
> **Everything runs on your machine.** There is no account, no subscription and no telemetry.
> Use a local model through [Ollama](https://ollama.com), bring your own API key for a third-party provider, or sign in to [OpenRouter](https://openrouter.ai) with OAuth.

---

## Contents

- [Features](#-features)
- [How it works](#-how-it-works)
- [Install](#-install)
- [Quick start](#-quick-start)
- [The workspace](#%EF%B8%8F-the-workspace)
- [Monitors](#-monitors)
- [Connectors (MCP)](#-connectors-mcp)
- [Command-line interface](#%EF%B8%8F-command-line-interface)
- [Uninstall](#%EF%B8%8F-uninstall)
- [Feedback and bug reports](#-feedback-and-bug-reports)
- [Development](#%EF%B8%8F-development)
- [Security and credentials](#-security-and-credentials)
- [Contributing](#-contributing)
- [License](#-license)

---

## ✨ Features

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>🤖 An agent that does the work</h3>
      Tools for files, code search, the shell, the web and office documents, plus <b>skills</b> and <b>helper agents</b> for specialised work.
    </td>
    <td width="50%" valign="top">
      <h3>🔒 Local and private</h3>
      Local models through Ollama, or external providers configured with your own key. Your projects stay in your folders.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>🛡️ You stay in control</h3>
      Choose how much the agent may do on its own (<code>auto</code>, <code>edit</code> or <code>plan</code>). Agent turns are checkpointed, so changes can be reviewed and undone.
    </td>
    <td valign="top">
      <h3>⏰ Background monitors</h3>
      Watch a page, a file, a command or a timer, then notify you or wake a chat. Monitors keep running while the window is closed.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>🧑‍💻 A complete workspace</h3>
      Editor, terminal, Git, and Markdown, HTML and PDF previews, in several layouts.
    </td>
    <td valign="top">
      <h3>📄 LaTeX built in</h3>
      PDF preview with SyncTeX, plus local compilation with Tectonic.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>⌨️ Command-line interface</h3>
      The same agent, projects and skills in a terminal, with no desktop window.
    </td>
    <td valign="top">
      <h3>🌐 Bilingual interface</h3>
      English and Brazilian Portuguese.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h3>🔑 OpenRouter sign-in (OAuth)</h3>
      Connect <a href="https://openrouter.ai">OpenRouter</a> with your browser instead of pasting a key. GilzaBots creates the key through OAuth (PKCE) and adds a free model to start with.
    </td>
    <td valign="top">
      <h3>🔌 Connectors (MCP)</h3>
      External services through MCP servers, with OAuth, tokens or local commands.
    </td>
  </tr>
</table>

---

## 🧭 How it works

GilzaBots has **two front-ends and one agent engine**. The desktop window and the command-line interface drive the same agent. The agent works on your project folder and talks only to the model provider you configured.

```mermaid
flowchart TB
    subgraph machine["Your machine"]
        GUI["🖥️ Desktop window"] -->|HTTP + streamed events| Server["Local server"]
        Server --> Engine["Agent engine"]
        CLI["⌨️ Terminal CLI"] --> Engine
        Daemon["⏰ Monitor service"] -.->|wakes a chat| Engine
        Engine --> Agent["Main agent<br/>+ helper agents and skills"]
        Agent --> Tools["Tools: files · shell · web · documents · Git"]
        Tools --> Project[("📁 Your project")]
    end
    Agent <-->|your key, or none| Provider["Ollama (local)<br/>or a provider you configure"]
```

The architecture and design decisions are documented in [PROJECT_DESIGN.md](PROJECT_DESIGN.md).

---

## 📦 Install

### Supported systems

| Operating system | Supported versions |
|------------------|--------------------|
| 🐧 Ubuntu        | 24.04 and 26.04 (x86_64/amd64) |
| 🪟 Windows       | Windows 11 |

The installer downloads the current GilzaBots Community release from GitHub Releases, extracts it into your user profile and creates shortcuts. It does **not** require administrator privileges.

### Ubuntu

Open a terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh | bash
```

<details>
<summary>Prefer to inspect the script first?</summary>

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh -o install.sh
less install.sh
bash install.sh
```

</details>

| What                | Where |
|---------------------|-------|
| Application         | `~/.local/share/GilzaBots` |
| Command             | `~/.local/bin/gilzabots` (symlink) |
| Desktop launcher    | `~/.local/share/applications/gilzabots.desktop` |

### Windows

Open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 | iex
```

<details>
<summary>Prefer to inspect the script first?</summary>

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 -OutFile install.ps1
Get-Content .\install.ps1
.\install.ps1
```

</details>

The application is installed under `%LOCALAPPDATA%\GilzaBots`. The installer adds its executable directory to the user `PATH` and creates shortcuts on the Desktop and in the Start menu.

### Command line only

To install the agent without the desktop window:

```bash
uv tool install gilzabots     # or: pip install gilzabots
```

The desktop application needs the `gui` extra: `pip install "gilzabots[gui]"`. LaTeX compilation uses a `tectonic` found on your `PATH`.

---

## 🚀 Quick start

```mermaid
flowchart LR
    A["1 · Open or create<br/>a project"] --> B["2 · Connect<br/>a model"] --> C["3 · Choose it for<br/>the project"] --> D["4 · Ask the agent"]
```

1. **Open or create a project.** A project is a folder on your disk. Use the **+** (new project) or the folder (import) button in the explorer.
2. **Connect a model.** Open **Connections and models** in the left activity bar.
   - **Ollama:** click **Load from Local Ollama** to add the models your Ollama server already has.
   - **OpenRouter, without copying a key:** click **Sign in with OpenRouter**. Your browser opens OpenRouter's authorization page; once you approve, GilzaBots receives a key of your own through OAuth (PKCE, on a `127.0.0.1` callback) and adds the free model `openrouter/free`, ready to use.
   - **Another hosted provider:** use **Manage Connections…** to add the provider and your API key, then **Add Model**.
3. **Choose it for the project** in the **Orchestrator** selector at the top of the chat.
4. **Ask.** For example: *"Add unit tests for `src/clean.py` and run them"*, *"Summarise the changes since the last commit"* or *"Fix the compilation errors in chapter 3"*.

<p align="center">
  <img src="docs/images/model-catalog.png" alt="The Edit Models / Providers dialog with a local Ollama model in the catalog" width="85%">
  <br>
  <sub>The model catalog. Each model sits under a provider connection that holds its credentials.</sub>
</p>

---

## 🖥️ The workspace

The buttons at the top of the activity bar switch between layouts, so you can give the chat or the files the space they need:

| Layout | What it shows |
|--------|---------------|
| **Chat** | The conversation, with the chat history and project explorer beside it |
| **Chat with bottom panel** | The chat plus the output, problems, agent-thinking and terminal panel |
| **Studio** | Editor and preview on top, chat and terminal below (screenshot at the top of this page) |
| **Document** | The open file and its preview, side by side |
| **Review** | The changed files next to the editor, to stage, discard or restore an agent checkpoint |

<p align="center">
  <img src="docs/images/layout-chat.png" alt="GilzaBots Chat layout: chat history and project explorer on the left, the chat with model selectors on the right" width="100%">
  <br>
  <sub>The <b>Chat</b> layout, with the orchestrator model and web search toggle at the top of the conversation.</sub>
</p>

The window also includes:

- **Source control:** Git status, diffs, staging and commits.
- **Previews:** Markdown, HTML and PDF.
- **LaTeX:** PDF preview with SyncTeX forward and inverse search, and local compilation with Tectonic.
- **Reading and voice tools:** PDF annotations, a PDF presentation mode, dictation in the chat, read-aloud, and translating or asking about a selected excerpt.

These editor features belong to the window and have no terminal equivalent.

---

## ⏰ Monitors

A monitor is a background watcher. You define **what** to check (a timer, a web page, a command, a file, or a question for the model), **when** to check it, and **what to do** when the condition holds: show a system notification, wake the chat that asked for it, or both.

Ask for one in the chat, for example *"tell me whenever the dollar moves 10 cents"*, or create one in the **Monitors** panel. Monitors run in a service of their own, so they keep working while GilzaBots is closed. That service can start at login.

<p align="center">
  <img src="docs/images/monitors.png" alt="The Monitors panel, showing the monitor service status, a start-at-login option and the list of monitors" width="85%">
</p>

From a terminal, `gilzabots monitors` (or `/monitors` inside the CLI) lists and manages them.

---

## 🔌 Connectors (MCP)

Connectors let the assistant use external services through [MCP](https://modelcontextprotocol.io) (Model Context Protocol) servers. Whatever tools a server offers become tools of the chat agent.

**Add one:** open **Connectors** (cable icon in the activity bar) → **Add connector**.

- **From the catalog**: pick an entry and fill in what it asks for. OAuth services (Notion, Linear, Sentry) open your browser to sign in as soon as you save. Services that need a token (GitHub, X read-only) ask for it. Open ones (X developer docs, Hugging Face, DeepWiki, Context7) need nothing. Local ones (a folder of your files, web fetching) need Node.js or uv.
- **By hand**, for any other server:
  - **Remote or local URL**: the server's `https://…/mcp` address (plain `http://` only for `localhost`), with OAuth, a bearer token or no authentication. For OAuth, GilzaBots registers itself automatically when the server allows it. Otherwise enter the client id of an app you registered with the service, and optionally the scopes to request.
  - **Local command (stdio)**: the command that starts the server and its arguments, one per line, plus any environment variables it needs.

Then click **Connect** and choose, under **Tools the agent may use**, which of the server's tools the assistant may hold.

**Approval.** By default every call shows an approval prompt with its exact arguments, even in auto mode (**Always ask**). **Follow the chat mode** makes calls behave like the built-in tools.

**Your X account.** X only accepts sign-in through an app you register with it, and bills API usage to that app's owner, so GilzaBots does not ship one. To let the assistant read and post on your account:

1. At [developer.x.com](https://developer.x.com), create a project and an app, and set a spending limit (usage is billed to you).
2. Under *User authentication settings*, configure:
   - OAuth 2.0 on;
   - type **Native App**;
   - permissions **Read and write**;
   - callback URLs `http://127.0.0.1:47821/oauth/callback`, `http://127.0.0.1:47822/oauth/callback` and `http://127.0.0.1:47823/oauth/callback`.
3. Copy the app's **OAuth 2.0 Client ID**. A Native App has no secret.
4. In GilzaBots, add a connector by hand:
   - URL `https://api.x.com/mcp`;
   - authentication **OAuth**;
   - your client id;
   - scopes `tweet.read tweet.write users.read offline.access`;
   - approval **Always ask**.

   Save, then sign in to X in the browser.

X's own rules still apply: no automated likes or follows, and AI-generated replies need X's approval. For reading only, the **X (read-only)** catalog entry takes your app's bearer token instead.

From a terminal, `/connectors` inside the CLI does the same (`/help /connectors`).

---

## ⌨️ Command-line interface

The same agent, projects and skills, in a terminal instead of a window.

### Usage

```bash
cd my-thesis
gilzabots --cli                       # interactive session for the project in this folder
gilzabots --cli --here                # register this folder as a new project first
gilzabots --cli --project my-thesis   # pick a project by name

gilzabots -p "fix the compilation errors in chapter 3"   # one turn, then exit
echo "summarise the changes" | gilzabots -p -            # prompt from standard input
```

| While a session runs | |
|---|---|
| Streaming | The answer streams as the model writes it. Tool calls appear as `⏺ tool_name(...)` with their results, and reasoning is dimmed. |
| Quieter output | `/thoughts off` and `/tools off` |
| Interrupting | `Ctrl+C` interrupts the running turn without ending the session. What the agent had already written is kept, and `/resume` continues from there. |
| Permissions | In `edit` mode the agent asks for permission in the terminal before each change. In `plan` mode it presents the plan for approval. |
| Help | `/help` lists the commands by section with their arguments. `/help <command>` shows worked examples, such as `/help /models`, `/help /providers` or `/help /chat`. |

Beyond the commands shared with the desktop chat, the terminal adds `/chat` (list, create and switch conversations), `/effort` (this chat's reasoning effort), `/compile`, `/cost` and `/resume`.

### Registering a model

A usable model is two records: a **provider connection** that holds the credentials, and a **catalog entry** that names a model under it. Both can be managed from any text front-end.

```mermaid
flowchart LR
    P["Provider connection<br/>/providers add<br/>credentials + API base"] --> M["Catalog entry<br/>/models add<br/>model name + capabilities"] --> S["Project<br/>/set-main-model"]
```

```bash
/providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1
/providers add label="OpenAI" provider=openai api_key=sk-...
/providers login openrouter                   # sign in to OpenRouter with OAuth, adds openrouter/free
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

> [!TIP]
> `/set-main-model` only stores an id. Without a catalog entry behind it there is no API base, no key and no capabilities. An unrecognised field is refused rather than guessed at, so a typo cannot become an invisible setting. To forward a parameter to the provider, write it as `extra.<name>`, for example `extra.keep_alive=30m`.

---

## 🗑️ Uninstall

Uninstalling keeps your settings, chats, caches, credentials and project directories by default. **Project directories are never removed.**

| Installation | How to uninstall |
|--------------|------------------|
| Ubuntu | `gilzabots-uninstall`. Add `--purge` to also remove the global GilzaBots data directory. |
| Windows | **Settings › Apps › Installed apps › GilzaBots**, or **Uninstall GilzaBots** in the Start menu. The PowerShell `-Purge` switch removes global application data after confirmation. |

<details>
<summary>Older installations without an uninstall command</summary>

Ubuntu:

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/uninstall.sh | bash
```

Windows:

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/uninstall.ps1 | iex
```

</details>

---

## 💬 Feedback and bug reports

- **Suggestions, criticism and feature ideas:** use the [GilzaBots feedback form](https://forms.gle/MsfYN4Yx9Cy8QNAR6), also available inside GilzaBots under **Settings › About › Send feedback (form)**.
- **Bugs:** use **Settings › About › Send feedback on GitHub**. The application opens a pre-filled issue at the [GilzaBots issue tracker](https://github.com/gilzamir18/GilzaBots/issues) with version and runtime information. Nothing is submitted automatically, so review the issue and remove private information before publishing it.

---

## 🛠️ Development

Requirements: **Python 3.11 or newer**, **Node.js/npm** and **Git**.

<details open>
<summary><b>Bash (Ubuntu)</b></summary>

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

</details>

<details>
<summary><b>PowerShell (Windows)</b></summary>

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

</details>

### Project layout

| Path | What lives there |
|------|------------------|
| [`gilzabots/`](gilzabots/) | Python backend: local server, agent engine, tools, CLI, monitors |
| [`agenticblocks/`](agenticblocks/) | The AgenticBlocks agent framework, first-party source in this repository |
| [`gui_src/`](gui_src/) | React/Vite desktop interface |
| [`skills/`](skills/) | Built-in agent skills |
| [`tests/`](tests/) | Test suite |
| [`docs/`](docs/) | Specifications, knowledge base and images |

### Build and test

| Task | Command |
|------|---------|
| Run the test suite (from the repository root) | `python -m pytest` |
| Rebuild only the web interface | `npm run build --prefix gui_src` |
| Packaged desktop build on Windows | `.\build_exe.ps1` |
| Packaged desktop build on Ubuntu | `bash ./build_exe.sh` |

The build scripts install the build dependencies, build the React/Vite interface, download pinned Tectonic and Pandoc releases, and package the application with PyInstaller. Outputs are written under `dist/`.

<details>
<summary><b>Release publishing (maintainers only)</b></summary>

The `binpacking.ps1` and `binpacking.sh` scripts package an existing build and upload it over SSH. They intentionally contain no server address, username, credential or private server path. Maintainers must provide:

- `GILZABOTS_RELEASE_HOST`
- `GILZABOTS_RELEASE_USER`
- `GILZABOTS_RELEASE_DIR`

Authentication is handled by the local SSH client, preferably with an SSH key or agent. Credentials must never be committed to the repository.

</details>

---

## 🔐 Security and credentials

- API keys configured in the application are local user data and must not be committed.
- Connector secrets (tokens, header values, environment variables) and OAuth credentials stay on your machine in owner-only files; the Connectors panel never shows a stored secret again.
- Environment files, private keys, credentials, databases, logs, build outputs and local runtime data are excluded by `.gitignore`.
- Report a suspected leaked credential privately to the maintainers. Revoke and rotate any credential that may have entered Git history: deleting it only from the latest commit is not enough.

---

## 🤝 Contributing

GilzaBots is open source under the MIT License, but the repository does **not** accept pull requests or code contributions from third parties.

To request a feature or report a bug, please **[open an issue](https://github.com/gilzamir18/GilzaBots/issues)** describing what you would like to see. The maintainer will review and prioritise requests.

---

## 📜 License

GilzaBots is available under the [MIT License](LICENSE).

If you would like to support its maintenance, **Settings › About** has a donation link. Donations are voluntary and do not unlock any feature.
