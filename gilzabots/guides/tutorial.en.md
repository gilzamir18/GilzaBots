# GilzaBots — Quick usage guide

Welcome to **GilzaBots**, a free AI agent that works inside your projects: it reads
and edits files, runs commands, searches the web, writes code and documents, and keeps
watch with background monitors. Everything runs on your machine, and the AI runs either
on a local Ollama model or on a provider you configure with your own API key. There is
no account, no billing, and no cloud service owned by GilzaBots.

This chat is your tutorial. Pick a question from the menu below and I will answer it
right away — no model needs to be configured for that. Once you have registered a
model, you can also just type your own questions here: I keep this whole guide in
memory for this conversation.

## overview :: How does GilzaBots work?

GilzaBots has three parts working together:

1. **The AI agent** — a chat orchestrator that reads and edits your files, runs
   commands, searches the web, generates office documents (.docx, .pptx), asks
   interactive clarifying questions, follows skills from the **Asset Store** for
   specialised tasks, and hands auxiliary jobs (long reads, log condensing, web
   research) to helper agents.
2. **The workspace** — a source editor with a file explorer, terminal and Git, plus
   previews for Markdown, HTML and PDF, and a rich-text mode.
3. **Document tools** — LaTeX projects get a PDF preview side by side, SyncTeX to jump
   between the PDF and the `.tex` line that produced it, a model-based WYSIWYG mode,
   and local compilation with Tectonic, which needs no system-wide LaTeX installation.

The usual workflow is:

- create or import a project (a project is one folder);
- register a provider connection and a model in Settings, then select the model in the
  chat toolbar;
- ask the assistant for what you need in the chat, or install skills/templates from the
  **Asset Store**;
- compile and inspect the PDF;
- review the assistant's changes in **Review** mode before keeping them.

The assistant runs in one of three **modes**, selectable in the chat toolbar:

- **auto** — full autonomy; it executes tools without asking at each step;
- **plan** — it may not modify anything; it gathers context and proposes a structured plan
  for you to approve first;
- **edit** — it edits files directly, but asks for confirmation before running terminal
  commands.

## installation :: Which systems are supported, and how do I install GilzaBots?

GilzaBots supports **Ubuntu 24.04**, **Ubuntu 26.04** and **Windows 11**. The installer
needs no administrator privileges and installs into your user profile.

**Ubuntu** (x86_64/amd64, with `curl`): open a terminal and run

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh -o gilzabots-install.sh
bash gilzabots-install.sh
```

It downloads the Linux release into `~/.local/share/GilzaBots` and creates a desktop
launcher and the command `~/.local/bin/gilzabots`. If `curl` is missing, install it
first with `sudo apt update && sudo apt install curl`.

**Windows 11**: open PowerShell and run

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 | iex
```

The application goes under `%LOCALAPPDATA%\GilzaBots`, with shortcuts on the Desktop and
in the Start menu.

GilzaBots runs with your normal user permissions, so its integrated terminal and the
assistant's commands see the same programs as your system terminal. Administrative
commands still require your authorization through `sudo`.

## uninstall :: How do I uninstall GilzaBots?

Close GilzaBots first. On Ubuntu, the installation provides:

```bash
gilzabots-uninstall
```

This removes the application and preserves global settings, chats, caches,
credentials, and all project directories. Use `gilzabots-uninstall --purge` only
when you also want to delete the global application data; the command shows the
exact directory and asks for confirmation. Project directories are never removed.

On Windows, use **Settings > Apps > Installed apps > GilzaBots** or the
**Uninstall GilzaBots** shortcut in the Start menu.

## projects :: How do I create and configure a project?

A project is a folder on your disk plus the metadata GilzaBots stores for it (selected
models, mode, chats, memory, shadow Git checkpoints).

- **New project** — the "New Project" dialog asks for a name, a parent folder, and
  optionally an initial model. Only one project per folder is allowed.
- **Import project** — point GilzaBots at an existing folder that already contains your
  `.tex` sources.

Two things surprise new users, and both are deliberate:

- **A project starts with no model configured.** GilzaBots never picks a model for you.
  Until you select one, the assistant refuses to run and tells you to choose a model —
  it does not quietly fall back to some default.
- **Selecting no model is a valid state.** You can clear the selection in Project
  Settings and return to an unconfigured project.

Projects store references to models by catalog ID (`<provider>/<name>`). Global
credentials (API keys and base URLs) belong to the global model catalog, so rotating an
API key in Settings updates every project automatically without re-entering credentials.

## project-settings :: What are all the project settings and parameters?

Project settings are edited through the Project Settings dialog (gear icon in the
sidebar) and are organised into four tabs:

### 1. General Tab (`geral`)

- **Internal ID & Display Name** — `Internal ID` is the permanent database key; `Display
  Name` is the title shown in the interface.
- **Project Path & Main File** — the absolute folder on disk and the entrypoint `.tex`
  file used as the root for compilation (e.g. `main.tex`).
- **LaTeX Compile on Save** — choose between `Disabled`, `Partial` (fast chapter/file
  compilation), or `Full` (recompiles the entire document on every save).
- **User Git Root** — optional custom path to an external Git repository if your source
  control root is located outside the project folder.
- **Mode** — `auto`, `plan`, or `edit`.
- **Description** — optional project summary provided to the assistant as high-level
  context.
- **Share Core Memory** — when checked, all chats in this project share a single
  persistent core memory. When unchecked, each chat maintains its own independent memory.
- **Force Vision** — forces multimodal/image payloads to be sent to both orchestrator and
  worker models, even when provider capability autodetection is conservative.
- **Load Contextual Skills** — button that scans the project directory for custom
  `.gilzabots/skills` and registers them into the project.

### 2. Orchestrator & Worker Tabs

A project configures separate models and runtime settings for the **Orchestrator** (the
conversational agent) and the **Worker** (the model of the helpers that `run_skill` spawns).
Both tabs offer:

- **Model Selection & Hardware Monitor** — select from registered models. Real-time GPU
  VRAM and system RAM indicators show model suitability (green = fits comfortably, yellow
  = may be slow, red = risks VRAM overflow).
- **Sampling & LiteLLM Parameters**:
  - `temperature` (0.0 to 2.0) — controls creativity vs determinism.
  - `max_tokens` — maximum output tokens per completion.
  - `num_ctx` — context window size in tokens. **Crucial for local models!**
  - `seed` — integer for deterministic sampling.
  - `top_p`, `top_k`, `min_p`, `frequency_penalty`, `presence_penalty`, `repetition_penalty`.
  - `reasoning_effort` — (`none`, `low`, `medium`, `high`, `xhigh`) for reasoning models
    supporting effort scaling (e.g. o1, o3, Gemini, Claude).
  - `thinking` — enables chain-of-thought reasoning tokens (for Ollama, automatically
    remaps to `ollama_chat/` for streaming reasoning).
  - `stream` — enables real-time token streaming.
- **Agent Runtime Parameters**:
  - `max_heartbeats` — maximum consecutive tool iterations per turn. Used only when the
    chat composer's **Effort / Budget** selector is on **Custom**; Low, Medium and High
    send their own budget (20, 50, 100) with each turn. When the budget runs out, the
    agent is asked once more for its answer, with no further tool calls allowed.
  - `max_context_tokens` — context budget ceiling before summarization.
  - `eviction_threshold` — fraction of context (default `0.85`) at which older turns are
    condensed into a running summary.
  - `memory_pressure_threshold` — threshold for aggressive memory compaction.
  - `loop_detection` & `loop_detection_limit` (default `3`) — tracks identical tool calls
    with identical arguments and blocks repeats before execution, breaking infinite loops.
  - **`empty_response_reasoning_fallback`** (*Use Thinking When the Response Is Empty*) —
    when enabled, if a reasoning model puts its entire answer into the reasoning/thinking
    channel and leaves the visible response channel empty, GilzaBots publishes the
    reasoning content as the final answer instead of failing or spending extra turns
    asking the model to repeat itself.
  - `max_idle_heartbeats` — how many times in a row the model may call `new_heartbeat`
    (keep the turn open without acting) before GilzaBots asks it for the answer outright.
  - `debug` — enables verbose agent execution logs.
  - Worker-specific limits: `max_iterations` and `max_tool_calls`.

### 3. Prompt Prefix Tab (`prefixo`)

- **User Prompt Prefix (`user_prompt_prefix`)** — optional custom instructions
  prepended to the first user message of every chat completion (for example, preferred
  language, formatting guidelines, project personas). It shapes the assistant's behavior
  without altering persisted conversation history or consuming core memory space.

## providers :: How do I register a provider and a model?

Registration is a **two-step catalog**, so you type your credentials once and reuse
them for every model that shares them.

**Step 1 — register a provider connection** (Settings → Edit Models → Manage
Connections → Add):

- **Label** — a name for you, e.g. "OpenRouter (personal)".
- **Provider** — the LiteLLM provider slug: `ollama`, `openai`, `gemini`,
  `anthropic`, `mistral`, `groq`, `together_ai`, `openrouter`, …
- **API key** — your own key. It stays on your machine.
- **API base URL** — only when the provider needs one. Examples: local Ollama uses
  `http://localhost:11434/v1`; Ollama Cloud uses `https://ollama.com`; OpenRouter uses
  `https://openrouter.ai/api/v1`.

**Step 2 — register a model against that connection** (Settings → Edit Models → Add):

- **Name** — the model name exactly as the provider spells it, e.g.
  `qwen2.5-coder:7b`, `gpt-5.5`, `gemini-2.5-pro`.
- **Connection** — pick the connection you created; the credentials come from it.
- **Capabilities** — see the "models" topic below.

The model then appears in the catalog with the id `<provider>/<name>`. That id is what
projects store, and it never changes when you edit the connection's credentials — so
rotating an API key does not break any project.

Two useful shortcuts:

- **Import local Ollama models** — one button in Edit Models queries your running
  Ollama at `http://127.0.0.1:11434/api/tags` and registers everything it finds.
- **Deleting a connection that still has models is refused.** That is intentional: a
  project must never point at a model whose credentials cannot be resolved.

## models :: Orchestrator, worker, and model capabilities

A project uses **two model roles**:

- **Orchestrator** — the main agent, the model you talk to in the chat. It does your
  task: it plans, reads, makes **every** change to your files, runs commands, and writes
  the final answers. Give it your best model.
- **Worker** — the model of the **helpers**: ephemeral agents (`run_skill`) that do an
  auxiliary job for the main agent — reading or summarising a long document, extracting
  parts of a file, condensing a log, researching the web — and return what they found.
  Helpers never change your files. Their jobs are narrow, so a smaller/faster model is
  fine and saves tokens. A helper skill can also name its own model; the chat shows each
  helper's model, and whether it can see images, to the main agent.

Skills come in the same two kinds. Most skills are instructions the main agent loads
(`load_skill`) and carries out itself; a skill marked `role: helper` runs as a helper.

When you register a model in the catalog you can declare capabilities:

- **`supports_thinking`** — the model accepts a thinking/reasoning parameter. GilzaBots
  only sends `think` when this is enabled. Thinking is on by default for the
  orchestrator and off by default for workers, because a worker stuck in a reasoning
  loop blocks a tool call; the orchestrator's reasoning is visible and useful to you.
  For an `ollama/` model with thinking enabled, GilzaBots switches to `ollama_chat/`
  internally so reasoning can stream natively.
- **`requires_single_system_message`** — some chat templates reject a request with more
  than one `system` message, with the error `system message must be at the beginning`.
  It depends on the model, not on the server: it shows up both on Ollama-served models
  (seen with qwen3.8) and on the same families served over an OpenAI-compatible endpoint
  (vLLM/NIM). Enable this flag for such a model and GilzaBots merges all system messages
  into one leading message for it — and only for it, whatever the provider.
- **`prompt_profile`** (`full` or `light`) — how much prompt text the model receives.
  `full` delivers the complete guardrails, instructions and examples; `light` delivers a
  condensed version (same rules, less prose) that saves tokens and latency.
- **Auxiliary work** (`direct` or `delegate`) — how much auxiliary work this model, **when
  it is the orchestrator**, hands to helpers. On `direct` it reads and searches itself and
  calls a helper only for a job too big for its own context. On `delegate` it sends
  reading, summarising, extracting, log condensing and web research to helpers by default,
  and keeps its own context for your task. Under both, the orchestrator makes every change
  itself.
- **`force_vision`** (a model parameter) — declares that a model accepts images when
  LiteLLM does not know it (a local llava or Qwen-VL, for instance). It decides whether a
  helper running on that model is offered image work.
- **`num_ctx`** — optional catalog-level context window override for this model.

Prompt profile and auxiliary work are **two independent settings**: the first says how
much text the model reads, the second where the reading and research happen. All four
combinations are valid.

### Which combination to use

A new model defaults to `full` + `direct`. Tune it according to the project's two models:

| Your setup | Prompt profile | Auxiliary work |
| --- | --- | --- |
| Small local model everywhere (orchestrator and worker) | `light` | `direct` |
| Large model as orchestrator, small one as worker | `full` | `delegate` |
| Large model in both roles (cloud) | `full` | `direct` |

Why each row:

- **Small everywhere → short prompt, direct.** A small model follows a short prompt
  better, and delegating here only swaps one small model for another: you pay an extra
  round-trip plus a helper that starts with no memory at all, and gain no capability.
- **Large + small → full prompt, delegate.** This is where helpers pay off. The large
  model keeps its context — the expensive one, and the one you want to last across the
  conversation — for your task, while the bulk reading and research run on the small
  model's tokens. The large model still makes every change.
- **Large everywhere → full prompt, direct.** Context is plentiful and quality is not the
  bottleneck; fewer round-trips means faster answers.

Since the orchestrator makes every change, a small orchestrator with a large worker no
longer moves editing to the stronger model: put the stronger model in the orchestrator.
`delegate` costs one extra model call per auxiliary job; if answers feel slow, this is the
first setting to reconsider.

You'll find auxiliary work in **Edit Models**, in the model form right below the prompt profile, and
as a column in the models list.

## settings :: Which settings should I use?

**Context window (`num_ctx`)** — this is the single most important setting for local
models. The number you set is the real cap, regardless of what the model advertises: a
"128K model" runs at whatever `num_ctx` your project sets. Size it against your VRAM
(see the local-models topic). The chat's context indicator measures against this value,
shows how much of the window is consumed, and reports the tokens the provider actually
charged, not an estimate.

**Mode** — start in **auto** for everyday work. Use **plan** when you want to review
what the assistant intends to do before it touches anything, and **edit** for quick,
targeted fixes.

**Streaming** — leave it on. You see the answer as it is produced, and you can
interrupt a run that is going the wrong way.

**Loop-detection limit** — default `3`. When the assistant repeats the same tool call
with the same arguments this many times, the repeat is blocked before it executes and
the model is told to change its approach. Lower it if a small local model tends to spin;
raise it only if you have a legitimate repetitive workflow.

**Eviction threshold** — default `0.85`. Older turns start being summarised out of the
working context once the window is 85% full, rather than waiting for it to overflow.

**Thinking & Reasoning Fallback** — leave thinking off unless the model's documentation
confirms support. If you use a reasoning model locally (such as DeepSeek-R1 or Qwen
thinking variants) and find that it sometimes leaves the response blank, enable
`empty_response_reasoning_fallback` in Project Settings.

**Skills** — activate only what you need. Every active skill adds its description to the
system prompt, which costs context on every single turn.

### Global IDE Settings (gear icon in the bottom-left ActivityBar)

- **Appearance & Editor** — UI scaling, light/dark theme, editor font sizes, tab size,
  word wrap, and minimap.
- **External Tools** — one-click installer and status checker for Tectonic and Pandoc.
- **Productivity & Media**:
  - Target language for selection translation.
  - Text-to-speech voice selection (in-process Piper voice models or OpenAI TTS).
  - Voice dictation via local Whisper models.
  - Image generation model and output directory settings (`generate_image`).
- **Docker Sandbox (Experimental)** — execute agent shell commands and scripts inside
  an isolated Docker container instead of the host machine. Because it is **experimental**,
  it is disabled by default.

## asset-store :: How do the Asset Store, skills, and templates work?

The **Asset Store** (Store icon in the ActivityBar) is your hub for extending GilzaBots
with reusable skills and LaTeX document templates.

### 1. Skills (`Skills` tab)

Skills are markdown instruction packages (with optional scripts) that teach the AI
specialised capabilities.

- **Catalog sub-tab** — browse and install skills from the repository into your
  project (`<project>/.gilzabots/skills/<name>/`).
- **Active in project sub-tab** — toggle skills on or off for the current project.
- **Local copies and updates** — when a project-local skill is installed, it shadows any
  bundled version of the same name. If the catalog version is updated, the Asset Store
  detects the drift and shows an **"Update local copy"** button. If you want to revert to
  the built-in version, click **"Restore bundled"**.
- **Skills that need other skills** — some skills load others while they work. Installing
  one also installs and activates the skills it needs (a copy you already have is left
  as it is).
- **Two ways a skill runs** — the agent picks a skill by itself when your request fits
  its description, or you run it by typing **`/name`** in the chat, followed by what it
  should work on (`/grill-with-docs my plan`). Typing `/` lists the commands and, marked
  **skill**, the skills you can run. Some skills run only that way: they never appear to
  the agent until you type them. See the engineering skills topic below.

### 2. LaTeX Templates (`LaTeX templates` tab)

Templates provide complete starting document structures (articles, theses, books,
beamer presentations, CVs).

- **One-click installation** — templates unpack directly at your project root.
- **Conflict detection & safety** — if any template file already exists in your project,
  GilzaBots detects the conflict, warns you, lists the conflicting files, and asks for
  explicit confirmation before overwriting anything.

## skill-pack :: How do I use the engineering skills (grill-with-docs, tdd, code-review…)?

The Asset Store carries a pack of 26 skills for planning and building software, adapted
from Matt Pocock's MIT-licensed skills. They take you from an idea to tested, reviewed
code in small, deliberate steps.

### Installing

- **In the window:** Asset Store → Skills → Catalog → install the skill. The skills it
  needs come with it, and all of them are activated in the project.
- **In the terminal:** `/load_asset skill grill-with-docs` installs it and what it needs;
  then activate each one with `/addskill` (here: `grilling`, `domain-modeling`,
  `grill-with-docs`).

Installed skills live in `<project>/.gilzabots/skills/`. `/skills` lists them.

### Running a skill

Type `/` in the chat, pick the skill, and write after its name what it should work on:

```
/grill-with-docs I want to export chats to PDF
```

The line you typed stays in the chat; the agent receives the skill's instructions with
your text. Some skills (the ones that *orchestrate* a flow, like `grill-with-docs`,
`to-spec` or `implement`) run **only** when you type them. Others (`tdd`, `code-review`,
`diagnosing-bugs`, `prototype`, `research`…) can also be picked by the agent itself
when your request fits, and the flows above load them along the way.

If you are not sure which one fits, run **`/which-skill what you want to do`**: it
points you to the flow and tells you which skills are still missing.

### The main flow: idea → shipped code

1. **`/grill-with-docs <idea>`** — an interview in rounds. Each round lists numbered
   questions with a recommended answer (➡️); answer them all at once ("1 yes, 2 no,
   prefer X"). The agent looks up facts in the code and on the web by itself and asks
   you only for decisions. Along the way it records the project's terms in
   `GLOSSARY.md` and offers to record hard-to-reverse decisions as ADRs in `docs/adr/`.
   It ends when no question is left, and confirms with you before acting.
2. **`/to-spec`** — turns the conversation into a spec (no new questions).
3. **`/to-tickets`** — splits it into small tickets, each saying which ones block it.
4. **`/implement`** — builds the work test-first (`tdd`) and closes with a two-axis
   review (`code-review`: the project's standards, and the spec), then commits.
5. **`/retro`** — afterwards, suggests improvements to the agent's environment
   (checks, `AGENTS.md`, coding standards).

Keep steps 1–3 in **one chat**, so the spec and tickets build on the same thinking.
Before the first `/to-spec`, run **`/setup-engineering-skills`** once: it asks where
tickets live (GitHub, GitLab or local files under `.scratch/`) and writes that down in
`docs/agents/` and in `AGENTS.md`.

### The other skills

- **Interviews:** `/grill-me` (the same interview, writing no files), `grilling` (the
  interview routine the others use), `/to-questionnaire` (a questionnaire for someone
  who knows what you don't), `/wait-what` (re-explain the last message plainly).
- **Building and fixing:** `tdd`, `diagnosing-bugs` (hard bugs: builds a reproduction
  that fails first, then fixes with a regression test), `code-review`, `prototype`
  (throwaway code that answers a design question), `pr` (the shape of a pull request
  description), `wizard` (a script that walks *you* through steps only a person can
  do, such as creating API keys).
- **Planning big work:** `/wayfinder` (a map of decision tickets for work too big for
  one session), `/triage` (sort incoming issues and write briefs an agent can act on),
  `/improve-codebase-architecture` (a visual report of refactoring opportunities).
- **Knowledge:** `research` (reads primary sources in the background and saves a cited
  note), `domain-modeling` (the glossary and ADRs), `codebase-design` (vocabulary for
  designing modules), `/teach` (learn a topic over several sessions), `/handoff` (a
  summary for another session or agent), `writing-for-agents` (how to write skills and
  `AGENTS.md`).

### Good to know

- **`AGENTS.md`** at the project root is read by the agent on every turn: put the
  project's standing instructions there (how to run the tests, conventions).
- Several skills start **subagents**: helper agents that only read (files, Git, the
  web) and report back, several at a time. They never change your files; the activity
  log shows them as `subagent:…`.
- When a flow says to start fresh, open a **new chat** (New Chat in the chat panel, or
  `/chat new <name>`). Do **not** use
  `/clear`: in GilzaBots it erases every chat of the project.
- If a skill answers "installed but not active", activate it with `/addskill <name>` or
  in the Asset Store's *Active in project* tab.

## local-models :: Tips for local models (Ollama)

Running locally is free and private, and it works well when you respect the limits.

- **Ollama 0.30.5 or newer.** Older builds do not support tool calls properly, and
  tool calls are how the assistant reads and edits your files.
- **Choose a model that actually supports tool calling.** A model without it can chat
  but cannot do anything in your project. Qwen2.5-Coder and similar tool-capable
  families are the safe choice.
- **Size `num_ctx` against your VRAM, not against ambition.** A context that does not
  fit in VRAM spills to system RAM and the model becomes unusably slow. Rough starting
  points: ~8 GB VRAM → a 7B model at 8K–16K context; ~12–16 GB → a 7B–14B model at
  16K–32K; less than 6 GB → a 3B model, and treat it as a way to try the interface
  rather than to do real work.
- **With small models in both roles, use the `light` profile and `direct` writes.** The
  short prompt is easier to follow, and delegating between two small models adds a
  round-trip without adding capability. If the orchestrator is a large model and the
  worker a small one, the recommendation flips to `full` + `delegate`. See the table in
  the models topic.
- **Use a smaller worker model than the orchestrator.** Workers are tool executors;
  they do not need the reasoning quality you want in the chat.
- **Keep thinking off for workers.** A local reasoning model given a complex input can
  loop for a very long time inside a tool call.
- **Enable `empty_response_reasoning_fallback`** if your local reasoning model writes
  its output into the thinking channel instead of the response channel.
- **Keep the active skill set small.** Each one costs context on every turn.
- **Be realistic.** Small local models are excellent for short edits, formatting, and
  learning the interface. They struggle with long documents, large data, and
  multi-step refactors — which is what the cloud models topic is about.

## local-skills :: Which skills suit small local models?

Skills are markdown instruction files that teach the assistant a specialised job. Most
are instructions the assistant loads and follows itself; a few (`log-table-condenser`,
`staged-reader`, `elementary-ops`, `web-search`) are helpers it hands an auxiliary job to
with `run_skill`, which spawns a fresh, stateless agent that returns what it found.
For a small local model, prefer skills that turn a big job into a **script** instead of
into a long conversation:

- **`command-line`** — the workhorse. Terminal commands, build scripts, Python
  snippets, bulk file operations. When a job is large, a small model does far better
  writing a ten-line Python script and running it than reasoning over the data itself.
- **`log-table-condenser`** — for large logs and tables (`.jsonl`, `.csv`, `.tsv`,
  `.log`). It streams and condenses the file instead of loading it into the context
  window. Use it whenever a data file is bigger than a few hundred lines.
- **`latex-assistant`** — explains compiler errors and generates LaTeX fragments and
  mathematics. Short, well-bounded prompts, which is exactly what small models handle
  well.

Seeing what you have open takes no skill at all: the assistant already reads your open
tabs, the focused file, and the selected text through the native `get_editor_state` tool.
Say "this file" or "the selected text" and it will know what you mean.

Rules of thumb for small models:

- activate two or three skills, not all of them;
- prefer surgical edits (`search_code` → `read_content_pos` →
  `edit_file` or `replace_content_range`) over asking for a whole rewritten file — long tool-call JSON
  gets truncated by output limits;
- if the same skill fails twice in a row, stop and change the approach; GilzaBots
  detects that and will tell the assistant to stop redelegating.

## tools-features :: What tools and features are available (documents, web search, questions)?

Beyond basic LaTeX file editing, GilzaBots includes powerful built-in tools and capabilities:

- **Native Document Exporters (`create_docx_file`, `create_pptx_file`)** — the assistant
  can generate formatted Microsoft Word (`.docx`) and PowerPoint (`.pptx`) presentations
  natively, complete with styled headings, tables, bullet lists, and embedded images.
- **Live Web Search** — the assistant can search the web in real-time to look up CTAN
  package documentation, LaTeX error solutions, bibtex citations, or library syntax.
- **Human-in-the-Loop Clarifications (`ask_question`)** — when the assistant needs
  clarification, preference choices, or column selections, it triggers an interactive
  modal (`AskModal`) in the UI, letting you pick options or write in responses without
  breaking the flow.
- **Safe Confirmations** — potentially destructive actions and plan executions prompt a
  clean confirmation modal before proceeding.
- **Surgical Text Editing** — `search_code`, `read_content_pos`, `edit_file`,
  `replace_content_range` and `write_content_pos` allow the AI to locate and modify precise
  passages without reading or rewriting huge files. `edit_file` finds the passage by its exact
  text, so earlier edits cannot shift it. The last three only edit a file that already exists; a
  new file is always created with `write_file`.
- **Attachments & Multimodal Input** — drag and drop images, PDFs, text, or data files
  into the chat. GilzaBots automatically extracts text from documents or forwards images to
  vision-capable models.
- **AI Image Generation (`generate_image`)** — generate figures, diagrams, and illustrations
  directly into your project folder from text descriptions.
- **Selection Snippet Actions** — highlight any passage in the PDF or preview to
  translate it, pronounce it aloud, or look it up with an instant AI explanation card.
- **Voice Dictation** — dictate prompts directly into the chat composer using local Whisper
  speech-to-text.
- **Diagnostic Activity & Thoughts** — expandable "AI Thoughts" blocks on assistant
  messages let you inspect the model's reasoning trace without cluttering the final text.
- **Docker Sandbox (Experimental)** — opt-in execution of commands and scripts within an
  isolated Docker container mounted to your project folder.
- **Monitor Agents (Experimental)** — background watchers that notify you, or wake a chat
  so the assistant resumes a task, when something happens — even with the window closed.
  See *What are monitor agents, and how do I use them?*
- **Connectors (MCP)** — let the assistant use external services (a social network, an
  issue tracker, a local server) through MCP servers you connect. See *How do I connect
  external services (MCP connectors)?*

## docker-sandbox :: What is the Docker sandbox and how does it work?

> **IMPORTANT: The Docker sandbox is currently an EXPERIMENTAL feature.** It is
> disabled by default and intended for users who want the commands and scripts the AI
> runs to execute inside a container instead of directly on their machine.

### What it does

When enabled, the assistant's terminal commands (`run_command`), Python scripts
(`run_python_script`), interactive commands (`run_interactive_command`) and background
commands (`run_background_command`) execute inside a **Docker container** instead of
directly on your host operating system. This applies to both the **Orchestrator** agent
and **Worker** sub-agents. Your own integrated terminal keeps running on your machine.

Key characteristics:

- **What is isolated** — the processes the assistant starts. Your project directory is
  mounted read-write at `/workspace`, so files created or modified there reflect directly
  in your project, and the assistant's file tools keep editing the project on your machine
  as before. The sandbox does not protect the project folder itself.
- **Your files stay yours** — on Linux, commands run with your user and group IDs, so
  files they create in the project belong to you (with rootless Docker or on
  Windows/macOS the image's user is kept, which Docker already maps to you).
- **Persistent environment** — GilzaBots maintains a persistent container per project
  (`gilzabots-sandbox-<hash>`). Packages installed with `pip` (into the user site, since
  commands do not run as root) remain available in later turns until the container is
  reset. Run installed tools with `python3 -m <tool>`. System packages (`apt-get`) need a
  custom image that already contains them.
- **Network modes** — `bridge` (default, allows downloading packages and accessing
  external APIs) or `none` (no internet access).
- **Skills** — skill folders outside the project are mounted read-only, so skill scripts
  still run. Scripts anywhere else outside the project are refused: copy them into the
  project first.
- **Tools not in the image** — Tectonic and Pandoc are not inside the container; the
  IDE's own compile and export keep using the ones on your machine.

### How to configure it

Open **Settings** (gear icon in the lower left) and scroll to the **Docker Sandbox**
section under Preferences:

1. **Enable toggle** — check "Execute agent commands in Docker sandbox".
2. **Docker Image** — choose the image (defaults to `python:3.12-slim`). If you need
   specific tools (like full TeX Live or compilers), you can specify a custom image.
3. **Network** — select `bridge` or `none`.
4. **Verification & Status**:
   - Click **Verify Docker** to test CLI availability, daemon reachability, permissions
     and whether the image is downloaded.
   - If the image has not been downloaded yet, click **Pull Docker Image**. The sandbox
     never downloads it on its own in the middle of a command.
   - Changing the image or the network recreates the container at the next command,
     discarding what was installed in it.
   - If a container gets into a bad state, or a command left something running in it,
     click **Reset Project Container** to recreate it cleanly.

### Requirements & Troubleshooting

- **Docker Engine / Desktop** must be installed and running on your machine:
  - On Linux: your user must be in the `docker` group (`sudo usermod -aG docker $USER`).
    **The group only takes effect in a new login session**: log out and back in (or
    restart the computer). Restarting GilzaBots alone is not enough, because it inherits
    the groups of the session it was started from.
  - On Windows/macOS: make sure Docker Desktop is running and WSL2 backend integration is
    enabled.

Because this feature is **experimental**, standard local execution remains the default.
If you encounter permission issues or do not need container isolation, you can safely
leave it disabled.

## monitors :: What are monitor agents, and how do I use them?

> **IMPORTANT: Monitor agents are currently an EXPERIMENTAL feature.** They work on
> Linux (tested on Ubuntu/GNOME), but the Windows and macOS integrations (notifications,
> start at login) have not yet been tested on real machines, and a wake-up that resumes a
> task with a real model has not been verified end to end. Expect rough edges, keep an eye
> on what your monitors do, and report problems.

A **monitor** watches something in the background and acts when a condition holds: it
shows a **desktop notification**, **wakes a chat** so the assistant continues a task, or
both. Monitors keep running when the GilzaBots window is closed.

### Two ways to use them

- **Ask to be told about something** — just say it in the chat: *"tell me whenever the
  dollar rises more than 10 cents against the real"*, *"warn me when this page changes"*,
  *"remind me in two hours to review chapter 3"*. The assistant registers a monitor that
  notifies you each time the condition fires (or once, if you ask for a single notice).
- **Let the assistant wait instead of spinning** — when a task depends on something that
  finishes later (a long build, a download, a command started with
  `run_background_command`), the assistant can register a monitor, tell you what it is
  waiting for, and end its turn. When the condition holds, the chat is woken with a
  **⏰ Monitor** message and the assistant carries on where it stopped. If the wait runs
  out (too many checks or a time limit), the chat is woken anyway with the reason, so the
  assistant can decide to wait longer or ask you whether to cancel.

**Which chat is woken.** Each monitor that wakes the assistant does it in one of two ways:

- **in the chat where it was created** (the default) — the assistant continues with that
  conversation and everything it remembers from it. This is the right choice for waiting
  on a task.
- **in a new chat every time** — each wake-up opens a fresh chat in the same project (named
  *⏰ monitor name · date and time*), and the original conversation is left untouched. It
  works **in the background**: the window never switches away from what you are doing.
  When it finishes you get a notification, the radar icon shows a count, and the Monitors
  panel lists it under *Recent events* ("New event from monitor …") with **Open chat**.
  This suits recurring work, such as *"each time the dollar moves, write a short analysis"*.
  The new chat starts empty, so the monitor's instructions must say everything the
  assistant needs.

**Once or every time.** A monitor fires every time its condition holds unless you ask for a
single time (**Fire only once** in the form, or say so in the chat). If you say nothing, a
monitor that wakes the chat it was created in fires once (it is waiting for a task), and
every other monitor keeps firing.

Ask for it in your own words (*"…and open a new chat each time"*), or pick it in the
**Wake a chat** field of the New monitor form. If the chat a monitor should wake was
deleted, the wake-up fails with a notification rather than going to another chat.

### What a monitor can check

- **Web address (HTTP)** — reads a page or a JSON API; a JSON path such as `USDBRL.bid`
  or a regular expression extracts the value. This is the cheap, reliable choice for
  prices and status endpoints.
- **Command** — runs a command in the project folder (inside the Docker sandbox when it
  is enabled) and looks at its output or exit code.
- **File** — whether a file exists, or whether its modification time or content changed.
- **Timer** — no check at all: it simply fires after the time you set.
- **Question for the model** — the project's worker model answers a question, with web
  search. Use it only when the condition needs judgment ("only if the news involves AI"):
  each check is a model call that costs tokens, so the minimum interval is 5 minutes.

Conditions include *changes*, *moves by* (e.g. ±0.10 since the last notice), *greater /
less than*, *equals*, *contains*, *matches pattern*, *exit code is*, *exists / is missing*.
The first check runs when the monitor is registered: if the address does not answer or
the JSON path matches nothing, you get the error right away instead of a monitor that
fails silently.

### Where it runs

Monitors run in a small background service, `gilzabots monitors daemon`, separate from the
window. **Start at login** is on by default so your monitors survive a restart (a
`systemd --user` service on Linux, the per-user `Run` key on Windows); you can turn it off in the Monitors panel.
The service stops by itself after a while when there is nothing left to watch.

- **Tray icon** — while the service runs, a GilzaBots icon in the system tray shows it is
  working in the background, even with the window closed (closing the window still closes
  it). Its menu shows the active monitors and new events (a red dot marks unseen ones) and
  offers **Open GilzaBots**, **Monitors panel…**, **Pause all monitors** / **Resume
  monitors**, and **Stop background service** (monitors then wait until GilzaBots is opened
  or you log in again). On Ubuntu it needs the *Ubuntu AppIndicators* extension, which is on
  by default; vanilla GNOME needs the AppIndicator extension. Turn it off with **Tray icon**
  in the Monitors panel or `gilzabots monitors tray off`.
- **Notifications** — native ones: the GNOME/KDE notification center on Linux, toasts on
  Windows, Notification Center on macOS. Clicking a notification (on Linux and Windows)
  opens GilzaBots on the chat it belongs to.
- **When the window is open** on that project, a wake-up runs right there as a normal,
  streamed turn. Otherwise it runs in the background and a notification brings you its
  answer.
- **Everything happens in the chat** — a monitor's turn runs on its own and never opens a
  question or approval dialog outside its chat (the new one or the original, as you
  chose). If it needs something from you — an answer, or approval for an action that
  `edit` mode would ask about — it says so in its reply in that chat, and you answer there.
  Nothing is approved for you. So when you ask for a monitor, give the assistant enough
  detail (what to look for, where, how to report it) for it to finish without asking.
- **One turn per chat** — while a background turn is running in a chat, a new message to
  that same chat is refused until it finishes.

### Managing monitors

- **Monitors panel** — the radar icon in the left activity bar (a count shows events you have not
  seen). It shows the *Recent events* with **Open chat** (**Clear** empties that list; the chats
  stay), and lists every monitor with its type (*Task wait* or
  *Watch*), trigger, last check, last value and errors, with **Pause/Resume**, **Test**
  (checks now without changing anything), **Cancel/Remove**, the history of each monitor,
  a **New monitor** form, the service status, **Start at login**, **Tray icon** and **Test
  notification**.
- **In the chat** — ask the assistant to list, pause or cancel monitors
  (`list_monitors`, `pause_monitor`, `cancel_monitor`).
- **In the terminal** — `gilzabots monitors list`, `info <id>`, `pause <id>`,
  `resume <id>`, `cancel <id>`, `test <id>`,
  `daemon --status`, `service status|install|uninstall`, `tray status|on|off`, `notify-test`; `/monitors` does
  the same inside `gilzabots --cli`.

### Cautions

- Registering a monitor counts as an action: in **edit** mode the assistant asks before
  creating one, and in **plan** mode it cannot. A command monitor runs its command
  repeatedly with nobody watching — read it before approving.
- A monitor that fails five checks in a row stops and notifies you.
- If notifications do not appear, use **Test notification** (or
  `gilzabots monitors notify-test`), and check `gilzabots monitors daemon --status`. The
  service writes a log at `<GilzaBots data folder>/monitors/daemon.log`.

## connectors :: How do I connect external services (MCP connectors)?

A **connector** links GilzaBots to an external service through an **MCP server** (Model
Context Protocol). Whatever tools the server offers — publishing a post, reading issues,
searching notes — become tools the assistant can use in chat.

**From the catalog.** Open **Connectors** (cable icon in the activity bar) → **Add connector**.
The **catalog** at the top has ready-made entries: pick one, fill in what it asks for and save.

- **Notion, Linear, Sentry** — OAuth: your browser opens to sign in as soon as you save.
- **GitHub, X (read-only)** — ask for a token (a GitHub personal access token, or your X app's
  bearer token).
- **X developer docs, Hugging Face, DeepWiki, Context7** — open, nothing to fill in.
- **Files (a local folder), Fetch** — run on your computer; need Node.js (`npx`) or uv (`uvx`).

**By hand**, for any other server:

- **Remote or local URL** — the server's address (`https://…/mcp`; plain `http://` only for
  `localhost`). Choose the authentication the service uses: **OAuth** (you sign in through
  your browser, like authorizing Google Drive), a **bearer token**, or none. With OAuth,
  GilzaBots registers itself when the service allows it; if the service requires an app you
  registered with it, enter that app's **client id**, and optionally the **scopes** to request.
- **Local command** — a server GilzaBots starts on your computer (for example `npx` with
  its arguments, one per line). It runs with your permissions: only use commands you trust.

Then click **Connect** (OAuth connectors connect on save). The connector shows *connected* and
lists its tools. Under **Tools the agent may use** you can keep only the ones you want — a
server may offer hundreds.

**Approval.** By default (**Always ask**) every call shows an approval prompt with the
exact arguments — the text of a post, the recipient of a message — even in auto mode. With
**Follow the chat mode**, calls behave like the built-in tools (auto runs, edit asks, plan
blocks). Background monitor turns can never use an *Always ask* connector.

### Your X account

X only accepts sign-in through an app you register with it, and bills API usage to the app's
owner, so GilzaBots does not ship one: you use your own (and pay your own usage).

1. At **developer.x.com**, create a project and an app, and set a spending limit.
2. In the app's **User authentication settings**: OAuth 2.0 on, type **Native App**,
   permissions **Read and write**, and these three callback URLs:
   `http://127.0.0.1:47821/oauth/callback`, `http://127.0.0.1:47822/oauth/callback`,
   `http://127.0.0.1:47823/oauth/callback`.
3. Copy the **OAuth 2.0 Client ID** (a Native App has no secret).
4. In GilzaBots, **Add connector** by hand: URL `https://api.x.com/mcp`, authentication
   **OAuth**, your **client id**, scopes `tweet.read tweet.write users.read offline.access`,
   approval **Always ask**. Save, and sign in to X in your browser.

X's own rules still apply: no automated likes or follows, and AI-generated replies need X's
approval; using an MCP server does not change that. To only read, the **X (read-only)**
catalog entry takes your app's bearer token instead.

**Good to know.**

- Tool descriptions and results come from the server; the assistant treats them as data.
- If a connector says it needs authorization, open Connectors and click **Connect** again.
  **Sign out** deletes the stored credentials.
- From the terminal: `/connectors` (see `/help /connectors`).

## cloud-for-big-data :: When should I use cloud models?

**When your problem involves a large amount of data, use a cloud model.** This is the
single most useful piece of advice in this guide.

Large documents, long logs, big datasets, whole-project refactors, and long multi-step
tasks need a large context window and a model that gets tool calls right the first
time. A small local model in that situation will fill its window, start evicting the
conversation, produce malformed tool calls, and burn far more of your time than the API
call would have cost.

Two good options, both configured exactly like any other provider:

- **Ollama Cloud** — the same Ollama you already know, with the models running on
  their infrastructure. Register a connection with provider `ollama`, API base
  `https://ollama.com`, and your key; model names carry a `:cloud` suffix. GilzaBots
  recognises those ids and applies a 65K context window by default. This is the
  smallest step up if you are already using local Ollama.
- **API providers** — **OpenRouter** gives you one key and one connection for models
  from many vendors, which makes it easy to switch models without re-registering
  credentials (provider slug `openrouter`, API base `https://openrouter.ai/api/v1`).
  Gemini, OpenAI, Anthropic, Mistral, Groq and Together AI are also supported directly.

A practical hybrid that works well: keep a local model as the **worker** for cheap
mechanical steps, and use a cloud model as the **orchestrator** for the reasoning. You
can change either one from the chat toolbar at any moment, without touching the project.

## context :: How does the context window work here?

The chat header shows a context indicator. It reports the tokens the provider actually
charged for the request — not a character-count guess — and it measures against the
effective window (`num_ctx`), which is the real cap regardless of what the model
advertises. The number is how much is **consumed**, and the bar drains as the window
fills.

Two behaviours follow from that, and both are intentional:

- **`read_file` refuses a file that does not fit the remaining budget.** It tells you
  the file size, the budget left, and the paging path to use instead. An oversized read
  is unrecoverable: it would land in the history, the provider would truncate the
  request from the front, and the assistant would end up answering a question it can no
  longer see.
- **`read_content_pos` pages through a large file** and always says so when it returns
  less than you asked for, including the exact call to resume with. The normal recipe
  for a big file is `search_code` to find the line numbers, then `read_content_pos` for
  just that range.

When the window fills anyway, the oldest turns are summarised into a running summary
rather than dropped — and the turn in progress is never evicted.

## compile-git :: Compiling, the PDF, and reviewing changes

**Compiling** — Tectonic compiles locally; no system LaTeX installation is needed. You
get a full compile, a partial compile (a single chapter or file), and a fast single-pass
draft for quick checks. Compilation errors can be pasted at the assistant, or fixed
straight from the problems panel.

**PDF and SyncTeX** — the preview sits next to the source, and SyncTeX maps a place in
the PDF back to the `.tex` line that produced it.

**Checkpoints and Review mode** — every agent turn is wrapped in shadow-Git
checkpoints: one before it runs, one after. If the turn changed nothing, both are
discarded. If it did change something, **Review** mode shows that turn as a single row
whose diff is the whole start-to-end change, so you can see exactly what the assistant
did and undo it if you disagree. Regular Git (your own commits, branches, history) is
available in the Source Control sidebar and is entirely separate from those checkpoints.

## chat-memory :: Chats, memory, and editing messages

**Multiple chats** — a project can hold many conversations. Each one has its own
history and its own working context, so a long debugging session does not pollute the
chat where you are writing your paper. Create one with the `+` in the chat sidebar.

**Memory** — the assistant keeps persistent facts in *core memory* (things worth
remembering across conversations) and can search the full conversation history of the
project. It writes to core memory after meaningful decisions; you do not have to manage
it.

**Clearing a chat** — "Clear chat" is a real server-side operation: it erases the
stored history, resets the assistant's working state, drops that chat's archival
entries, and resets the context indicator. It is not a UI reset that leaves the model
still remembering everything.

**Editing a message** — nothing is ever destroyed:

- editing your **last** message marks it and everything after it as superseded — the
  answer disappears from view, but the history stays auditable;
- editing an **earlier** message branches the conversation into a new chat and leaves
  the original untouched.

**Interrupting** — you can stop a run at any time. What the assistant had already done
is kept as context, so you can tell it to continue with corrected instructions instead
of starting over.

**Slash commands** — messages starting with `/` are GilzaBots commands rather than
prompts: `/help`, `/clear`, `/skills`, `/models`, `/set-main-model <id>`,
`/set-worker-model <id>`, `/undo`, `/commit <msg>`, and others. Typing `/` opens a
list of the commands that narrows as you type: ↑/↓ to pick one, Enter to run it
(a command that needs an argument is put in the box for you to complete), Tab to
complete it, Esc to close the list. A command's output opens in a window of its
own and is never added to the conversation.

## command-line :: Can I run GilzaBots from the terminal, without the window?

Yes. The same agent, the same projects, the same skills — rendered in a terminal
instead of a window.

**Starting it** — `gilzabots --cli` opens the interactive session. Run it inside a
project directory and it opens that project; add `--here` to register the current
directory as a new project, or `--project <name>` to pick one by name.

**A single question** — `gilzabots -p "fix the compilation errors in chapter 3"` runs one
turn and exits, which is the form to use in a script or a Makefile. `-p -` reads the
prompt from standard input.

**What you see while it works** — the answer streams in as the model writes it, each
tool call appears as `⏺ tool_name(...)` with its result underneath, and the reasoning
is shown dimmed. `/thoughts off` and `/tools off` quieten either one.

**Interrupting** — `Ctrl+C` stops the running turn, not the session: whatever the agent
had already written is saved, and `/resume` continues from there. `Ctrl+D` or `/exit`
quits.

**Approving things** — in `edit` mode the agent asks in the terminal before each change,
and in `plan` mode it shows the plan for approval the same way the window does. `/mode`
switches between `auto`, `plan` and `edit`.

**Commands** — `/help` lists them by section with the arguments each one takes, and
`/help <command>` shows that command's worked examples (`/help /models`,
`/help /providers`, `/help /chat`). Everything in the desktop chat works here, plus a
few that only make sense in a terminal: `/chat` to list, create and switch conversations,
`/effort` to set the current chat's reasoning effort, `/compile` to build the PDF
with Tectonic, `/cost` to see how full the context window is, `/resume`, and
`/monitors` to manage the background monitors (experimental; also `gilzabots monitors`).

**Installing just this** — the command-line interface is the base install:
`pip install gilzabots` or `uv tool install gilzabots` gives you the agent without the
desktop stack. The window needs `pip install "gilzabots[gui]"`. LaTeX compilation uses a
`tectonic` found on your `PATH`.

**What it cannot do** — anything that is the editor rather than the agent: the PDF
preview, SyncTeX, the rich-text and WYSIWYG modes, PDF annotations, PDF presentation mode,
dictation and read-aloud. Those live in the window.

## cli-models :: How do I register a model and a provider from the terminal?

A usable model is two records, and you create both from any text front-end — the
`--cli` session, `gilzabots -p`, the stdin protocol, or this chat's command box.

**1. The provider connection** holds the credentials, shared by every model under it:

```
/providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1
/providers add label="OpenAI" provider=openai api_key=sk-...
/providers                       # list them, with how many models each one carries
```

The id is derived from the label (`ollama-gil`); pass `id=<your-id>` to choose it.
Running `add` again with the same id edits that connection, and leaving `api_key` out
keeps the stored one — only `api_key=` (empty) clears it.

**2. The catalog model** names a model under a connection and describes it:

```
/models add name=gemma4:26b connection=ollama-gil num_ctx=65000 supports_thinking=true
/models add name=gpt-4o-mini connection=openai temperature=0.3
/models list
/models show ollama/gemma4:26b
/models set ollama/gemma4:26b profile=light policy=delegate
/models remove ollama/gemma4:26b
```

**3. Point the project at it:**

```
/set-main-model ollama/gemma4:26b
/set-worker-model ollama/gemma4:26b
```

**Why both steps matter** — `/set-main-model` only stores an id. Without a catalog
entry behind it there is no API base, no key and no capabilities, so the run reaches the
provider unauthenticated and every per-model setting reads as unset.

**Fields** — `name`, `connection` (alias of `connection_id`), `id`, `num_ctx` (alias
`context`), `supports_thinking` (alias `thinking`), `requires_single_system_message`,
`prompt_profile` (alias `profile`: `full` or `light`), `orchestrator_policy` (alias
`policy`: `direct` or `delegate`), `temperature`, `max_tokens`, `seed`, `top_p`, `top_k`,
`min_p`, `frequency_penalty`, `presence_penalty`, `repetition_penalty`,
`reasoning_effort`, `supports_image_generation`, `image_route`,
`supports_speech_synthesis`, `speech_route`.

Anything else is refused rather than guessed at, so a typo cannot become an invisible
setting. A parameter you *do* want forwarded to the provider is written with a prefix:
`extra.keep_alive=30m`.

Values with spaces are quoted: `label="My provider"`.
