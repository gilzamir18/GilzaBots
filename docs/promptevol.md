# Walkthrough: Prompt Evolution Feature

Implemented an **"Evolve" ("Evoluir")** prompt evolution button in the Chat panel that uses a transient `AgenticBlocks` agent (`LLMAgentBlock`) to refine and expand user prompts over one or more iterations. Also added settings in **Settings > General** to configure the number of evolution iterations (default: 1) and the maximum generated tokens per iteration (default: 4096).

## Changes Made

### Backend & Core Logic
- **[ui_settings.py](file:///c:/Users/gilza/projetos/GilzaBots/gilzabots/ui_settings.py)**:
  - Added `"prompt_evolution_iterations": 1` and `"prompt_evolution_max_tokens": 4096` to `_DEFAULTS`.
- **[ide_server.py](file:///c:/Users/gilza/projetos/GilzaBots/gilzabots/ide_server.py)**:
  - Added `PromptEvolutionResult`, a Pydantic response schema. The backend uses only its structured `evolved_prompt` field and rejects invalid structured output, unchanged prompt echoes, internal task wrappers, and schema/instruction leakage.
  - Added `_execute_prompt_evolution(prompt, iterations, model, max_tokens)` using a transient `LLMAgentBlock` (from `agenticblocks.blocks.llm.agent`) with a Pydantic response schema and deterministic field extraction. The selected chat model is honored for local/custom providers.
  - Added REST routes:
    - `GET /api/settings/prompt-evolution`: returns `{ prompt_evolution_iterations: int, prompt_evolution_max_tokens: int }`.
    - `POST /api/settings/prompt-evolution`: validates integer settings >= 1 and saves them.
    - `POST /api/chat/evolve-prompt`: evolves a prompt over N iterations and returns `{ success: true, prompt: str }`.
    - `POST /api/chat/cancel-evolve-prompt`: cancels an in-progress prompt evolution task immediately.

### Frontend & Localization
- **[SettingsModal.jsx](file:///c:/Users/gilza/projetos/GilzaBots/gui_src/src/components/modals/SettingsModal.jsx)**:
  - Added state, API fetch, save helper, and input controls for **Prompt Evolution Iterations** (min 1) and **Prompt Evolution Max Tokens** (default: 4096) under the **General** settings tab.
- **[ChatPanel.jsx](file:///c:/Users/gilza/projetos/GilzaBots/gui_src/src/components/ChatPanel.jsx)**:
  - Added `isEvolvingPrompt` state, `handleEvolvePrompt()` callback, and `handleCancelEvolvePrompt()` callback with `AbortController` cancellation.
  - Added the **Evolve** button (`Sparkles` icon / `RefreshCw className="spin"`) next to the chat `textarea` that replaces the prompt input value with the evolved output, and toggles to an active cancellation button (`X` icon) during evolution.
  - Added a direct **Cancel** button in the evolution progress banner to allow immediate cancellation and original prompt preservation.
- **[en.json](file:///c:/Users/gilza/projetos/GilzaBots/gui_src/src/i18n/locales/en.json)** & **[pt-BR.json](file:///c:/Users/gilza/projetos/GilzaBots/gui_src/src/i18n/locales/pt-BR.json)**:
  - Added localization keys for `evolve`, `evolvePrompt`, `cancelEvolution`, `evolving`, `evolveCancelled`, `promptEvolutionIterations`, `promptEvolutionIterationsHint`, `promptEvolutionMaxTokens`, and `promptEvolutionMaxTokensHint`.

### Automated Testing
- **[test_prompt_evolution.py](file:///c:/Users/gilza/projetos/GilzaBots/tests/test_prompt_evolution.py)**:
  - Added unit tests for prompt cleaning logic, internal-wrapper rejection, settings GET/POST validation, selected-model forwarding, max-token forwarding, evolution endpoint handling, and cancel endpoint execution.

---

## Verification Results

### Automated Tests
- `python -m pytest tests/test_prompt_evolution.py tests/test_i18n_coverage.py` passed all 57 tests.
