"""Tests for the MemGPT runtime (skills-oriented architecture, docs/specs/02, 06).

Verifies assembly and wiring WITHOUT invoking any LLM:
  - resolve_skill_model maps default/worker/explicit/absent correctly
  - build_chat_orchestrator produces a framework MemGPTAgentBlock with run_skill
    and the memory tools, and embeds Level-1 skill metadata in the system prompt
  - run_skill returns an error for an unknown skill (no LLM needed)
"""

import asyncio
import os

import pytest

from opalatex.memgpt_runtime import (
    resolve_skill_model,
    build_chat_orchestrator,
    build_run_skill_tool,
    _current_date_instruction,
    _is_failed_worker_result,
    _recent_failed_delegations,
    _recent_failed_skill_attempts,
    _sanitize_skill_result_for_prompt,
    _strip_serialized_tool_calls,
    _tool_calls_count,
)
from opalatex.project import ProjectData
from opalatex.config import DEFAULT_MODEL, WORKER_MODEL


def _enable_helper(project_path, name="staged-reader"):
    """Activate a bundled helper skill: run_skill only accepts helpers."""
    import yaml

    with open(os.path.join(str(project_path), "skills.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump({"skills": [name]}, f)


def _project(tmp_path):
    return ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/proj-model",
    )


def test_resolve_skill_model():
    # "default" → the project's main model (falls back to DEFAULT_MODEL when unset).
    assert resolve_skill_model({"model": "default"}, "ollama/x") == "ollama/x"
    assert resolve_skill_model({"model": "default"}, None) == DEFAULT_MODEL
    # "worker" → the project's worker model, else the main project model.
    assert resolve_skill_model({"model": "worker"}, "ollama/x") == "ollama/x"
    assert resolve_skill_model({"model": "worker"}, "ollama/x", "gemini/proj-worker") == "gemini/proj-worker"
    # "alternative" is also supported for backwards compatibility.
    assert resolve_skill_model({"model": "alternative"}, "ollama/x", "gemini/proj-worker") == "gemini/proj-worker"
    # Explicit id used as-is; absent → worker model, falling back to project model.
    assert resolve_skill_model({"model": "ollama/custom"}, "ollama/x") == "ollama/custom"
    assert resolve_skill_model({"model": ""}, "ollama/proj") == "ollama/proj"
    assert resolve_skill_model({}, "openrouter/qwen/qwen3.7-plus", "ollama/gemma4:26b") == "ollama/gemma4:26b"
    assert resolve_skill_model({}, None) == DEFAULT_MODEL


def test_build_chat_orchestrator_has_run_skill_and_memory_tools(tmp_path):
    project = _project(tmp_path)
    project.mode = "auto"
    m = build_chat_orchestrator(project, None)
    names = {getattr(t, "name", None) for t in m.tools}
    assert "run_skill" in names
    assert {"read_core_memory", "append_core_memory", "search_conversation_history"} <= names


def test_orchestrator_enables_loop_detection_by_default(tmp_path):
    m = build_chat_orchestrator(_project(tmp_path), None)
    assert m.loop_detection is True
    assert m.loop_detection_limit == 3


def test_orchestrator_honours_project_loop_detection_settings(tmp_path):
    # The setting is exposed in the project UI; it must actually reach the block.
    # It previously only fed a duplicate breaker in wrap_tool, which skipped workers.
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/proj-model",
        model_params={"loop_detection": False, "loop_detection_limit": 7},
    )
    m = build_chat_orchestrator(project, None)
    assert m.loop_detection is False
    assert m.loop_detection_limit == 7


def test_orchestrator_keeps_the_reasoning_fallback_off_by_default(tmp_path):
    # Publishing the reasoning channel as the user-facing answer is opt-in.
    m = build_chat_orchestrator(_project(tmp_path), None)
    assert m.empty_response_reasoning_fallback is False


def test_orchestrator_honours_the_project_reasoning_fallback_setting(tmp_path):
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/proj-model",
        model_params={"empty_response_reasoning_fallback": True},
    )
    m = build_chat_orchestrator(project, None)
    assert m.empty_response_reasoning_fallback is True


def test_reasoning_fallback_setting_never_reaches_litellm(tmp_path):
    # It is an agent constructor param; providers reject unknown request fields.
    from opalatex.config import _AGENT_PARAM_KEYS, sanitize_litellm_kwargs_for_model

    assert "empty_response_reasoning_fallback" in _AGENT_PARAM_KEYS
    cleaned = sanitize_litellm_kwargs_for_model(
        "ollama/x", {"empty_response_reasoning_fallback": True, "temperature": 0.2}
    )
    assert "empty_response_reasoning_fallback" not in cleaned
    assert cleaned["temperature"] == 0.2


def test_chat_orchestrator_exposes_surgical_edit_tools(tmp_path):
    project = _project(tmp_path)
    project.mode = "auto"
    m = build_chat_orchestrator(project, None)
    names = {getattr(t, "name", None) for t in m.tools}
    assert {"search_code", "read_content_pos", "replace_content_range", "write_content_pos"} <= names
    assert "replace_content_range" in m.system_prompt


def test_chat_orchestrator_exposes_search_code_for_targeted_search(tmp_path):
    m = build_chat_orchestrator(_project(tmp_path), None)
    names = {getattr(t, "name", None) for t in m.tools}

    assert "search_code" in names
    assert "Never guess high line numbers" in m.system_prompt
    assert "rg -n" not in m.system_prompt


def test_chat_orchestrator_exposes_document_creation_tools(tmp_path):
    project = _project(tmp_path)
    project.mode = "auto"
    m = build_chat_orchestrator(project, None)
    names = {getattr(t, "name", None) for t in m.tools}
    assert {"create_docx_file", "create_pptx_file"} <= names
    assert "You can use `create_docx_file`" in m.system_prompt
    assert "You can use `create_pptx_file`" in m.system_prompt


def test_chat_orchestrator_context_limit_defaults_to_project_num_ctx(tmp_path):
    project = _project(tmp_path)
    project.model_params = {"num_ctx": 65536}
    m = build_chat_orchestrator(project, None)

    assert m.max_context_tokens == 65536


def test_chat_orchestrator_context_limit_uses_catalog_num_ctx_for_cloud_model(tmp_path, monkeypatch):
    """Regression: a project with no explicit num_ctx override, pointing at a
    cloud model whose catalog entry sets a large num_ctx, must NOT fall back to
    the hardcoded 8192 default. This previously broke because the fallback
    chain read _llm_kwargs.get("num_ctx", 8192) -- the *sanitized* LiteLLM
    kwargs, which sanitize_litellm_kwargs_for_model strips num_ctx from for any
    non-Ollama provider (cloud endpoints reject the param). max_context_tokens
    is internal eviction bookkeeping, not a wire parameter, so it must be
    resolved independently of that sanitization."""
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="openai/cloud-model",
    )
    monkeypatch.setattr(
        "opalatex.models_store.get_model",
        lambda model_id: {"num_ctx": 255000} if model_id == "openai/cloud-model" else None,
    )
    m = build_chat_orchestrator(project, None)

    assert m.max_context_tokens == 255000


def test_chat_orchestrator_system_prompt_embeds_skill_metadata(tmp_path):
    _enable_helper(tmp_path)
    m = build_chat_orchestrator(_project(tmp_path), None)
    # Bundled skills must surface as Level-1 metadata for routing, each in the
    # list of the tool that accepts it.
    prompt = m.system_prompt
    helpers = prompt.index("## Helpers (delegate with run_skill)")
    loadable = prompt.index("## Skills you carry out yourself (load with load_skill)")
    assert helpers < prompt.index("- staged-reader:") < loadable
    assert prompt.index("- command-line:") > loadable


def test_current_date_instruction_is_prepended_to_system_prompt(tmp_path):
    m = build_chat_orchestrator(_project(tmp_path), None)
    assert m.system_prompt.startswith("Today is ")
    assert "you MUST use the web_search tool before answering" in m.system_prompt
    assert "Never claim that a current, recent, or future-dated event did not happen" in m.system_prompt
    assert "Text content is never a tool call" in m.system_prompt


def test_current_date_instruction_formats_english_date():
    from datetime import datetime

    instruction = _current_date_instruction(datetime(2026, 7, 9))
    assert instruction.startswith("Today is July 9, 2026.")
    assert "web_search" in instruction


def test_current_date_instruction_triggers_on_epistemic_uncertainty():
    """Unknown/misspelled terminology must trigger a search, not a guess or a refusal.

    Small models were reading the old time-sensitivity-only wording as permission to
    answer an unfamiliar concept from memory (e.g. "Random Destilation Network").
    """
    instruction = _current_date_instruction()
    assert "not confidently recognize" in instruction
    assert "misspelled" in instruction
    assert "no information about a" in instruction


def test_current_date_instruction_keeps_web_search_scoped():
    instruction = _current_date_instruction()
    assert "public/external knowledge only" in instruction
    assert "ask_question" in instruction


def test_build_chat_orchestrator_uses_light_prompt_profile_for_light_model(tmp_path, monkeypatch):
    """A model catalog entry with prompt_profile="light" must render the
    condensed chat-orchestrator body/mode-instructions, not the full ones."""
    from opalatex import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    models_store.add_or_update_connection(
        {"id": "c1", "label": "c", "provider": "ollama", "api_key": "", "api_base": ""}
    )
    models_store.add_or_update_model({
        "id": "ollama/light-model", "connection_id": "c1", "name": "light-model",
        "prompt_profile": "light",
    })

    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/light-model", mode="auto",
    )
    m = build_chat_orchestrator(project, None)

    assert "Mode: auto." in m.system_prompt
    assert "PRIMARY RULE" not in m.system_prompt
    assert "\U0001F6A8 **SYSTEM ALERT" not in m.system_prompt


def test_build_chat_orchestrator_default_model_keeps_full_prompt_profile(tmp_path, monkeypatch):
    """An unregistered/default model must keep today's unabridged prompt."""
    from opalatex import models_store

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")

    project = _project(tmp_path)
    project.mode = "auto"
    m = build_chat_orchestrator(project, None)

    assert "PRIMARY RULE" in m.system_prompt
    assert "\U0001F6A8 **SYSTEM ALERT: You are currently in 'auto' mode." in m.system_prompt


def test_uses_framework_memgpt_block(tmp_path):
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
    m = build_chat_orchestrator(_project(tmp_path), None)
    assert isinstance(m, MemGPTAgentBlock)


def test_run_skill_unknown_skill_returns_error(tmp_path):
    m = build_chat_orchestrator(_project(tmp_path), None)
    run_skill = build_run_skill_tool(m, str(tmp_path), "ollama/proj")
    raw = getattr(run_skill, "_func", None) or run_skill
    result = asyncio.run(raw("does-not-exist", "context"))
    assert "[ERROR]" in result
    assert "not found" in result


def test_run_skill_returns_blocked_result_in_plan_mode(tmp_path):
    project = _project(tmp_path)
    project.mode = "plan"
    m = build_chat_orchestrator(project, None)
    run_skill = build_run_skill_tool(m, str(tmp_path), "ollama/proj", _project_ref=project)
    raw = getattr(run_skill, "_func", None) or run_skill

    result = asyncio.run(raw("command-line", "write a file"))

    assert result.startswith("[BLOCKED] run_skill is not available")
    assert "create_plan" in result


def test_run_skill_breaks_consecutive_failed_worker_loop(tmp_path):
    _enable_helper(tmp_path)
    m = build_chat_orchestrator(_project(tmp_path), None)
    m._skill_run_history = [
        {
            "skill": "staged-reader",
            "context": "attempt 1",
            "result": "[AVISO: O worker terminou sem um resumo claro. Ele realizou 0 chamadas de ferramenta e o último texto gerado foi: ]",
        },
        {
            "skill": "staged-reader",
            "context": "attempt 2",
            "result": "[CRITICAL WORKER CRASH] A exceção não tratada interrompeu o worker: litellm.APIConnectionError",
        },
    ]
    run_skill = build_run_skill_tool(m, str(tmp_path), "ollama/proj")
    raw = getattr(run_skill, "_func", None) or run_skill

    result = asyncio.run(raw("staged-reader", "attempt 3"))

    assert "WORKER LOOP BREAKER" in result
    assert "Do NOT call this same worker again" in result
    assert _recent_failed_skill_attempts(m, "staged-reader") == 2


def test_sanitize_skill_result_for_prompt_removes_raw_tool_json():
    result = (
        "[CRITICAL WORKER CRASH] litellm.APIConnectionError: OllamaException - "
        "{\"error\":\"error parsing tool call: raw='{\\\"name\\\":\\\"write_file\\\","
        "\\\"arguments\\\":{\\\"content\\\":\\\"very long code\\\"}', err=unexpected end of JSON input\"}"
    )

    sanitized = _sanitize_skill_result_for_prompt(result)

    assert sanitized == "[CRITICAL WORKER CRASH: model produced invalid/truncated JSON tool-call arguments.]"
    assert "write_file" not in sanitized


# A tool call the model printed as text instead of issuing (observed with a 2.6B
# local model delegating to the log skill).
_SERIALIZED_TOOL_CALL = (
    '{"order": 1, "id": "call_35c2d544", "function": {"name": "search_code", '
    '"arguments": {"query": "logs/Experiment_Batch.jsonl", "max_results": 50}}}'
)
# A structured payload the same model started emitting as text and never closed.
_UNCLOSED_REPORT = (
    '{\n  "user": "analise o log logs/Experiment_Batch.jsonl",\n'
    '  "task": "Analyze the JSONL log file.",\n  "constraints": \n  -1.5,\n'
)


def test_sanitize_skill_result_for_prompt_replaces_serialized_tool_call():
    sanitized = _sanitize_skill_result_for_prompt(_SERIALIZED_TOOL_CALL)

    assert "WORKER PROTOCOL ERROR" in sanitized
    assert "search_code" not in sanitized
    assert "arguments" not in sanitized


def test_sanitize_skill_result_for_prompt_keeps_prose_around_serialized_tool_call():
    sanitized = _sanitize_skill_result_for_prompt(
        f"I located the file.\n{_SERIALIZED_TOOL_CALL}\nWaiting for the result."
    )

    assert "I located the file." in sanitized
    assert "Waiting for the result." in sanitized
    assert "WORKER PROTOCOL ERROR" in sanitized
    assert "max_results" not in sanitized


def test_sanitize_skill_result_for_prompt_preserves_legitimate_json_data():
    result = '{"result": "No matches for \'thebibliography\' in report.tex."}'

    assert _sanitize_skill_result_for_prompt(result) == result


def test_sanitize_skill_result_for_prompt_replaces_unclosed_json_report():
    sanitized = _sanitize_skill_result_for_prompt(_UNCLOSED_REPORT)

    assert "WORKER PROTOCOL ERROR" in sanitized
    assert "constraints" not in sanitized


def test_strip_serialized_tool_calls_reports_whether_it_matched():
    _, matched = _strip_serialized_tool_calls(_SERIALIZED_TOOL_CALL)
    assert matched is True

    text = "Condensed 480 lines into logs/summary.csv."
    unchanged, matched = _strip_serialized_tool_calls(text)
    assert matched is False
    assert unchanged == text


def test_worker_result_without_a_summary_counts_as_failure():
    assert _is_failed_worker_result(_SERIALIZED_TOOL_CALL) is True
    assert _is_failed_worker_result(_UNCLOSED_REPORT) is True
    assert _is_failed_worker_result("") is True
    assert _is_failed_worker_result(
        "Condensed 480 log lines into logs/summary.csv (5 columns)."
    ) is False


def test_run_skill_breaks_redelegation_after_reports_without_summary(tmp_path):
    """Two junk reports must trip the loop breaker even when each context differs.

    The identical-call loop detection in the agent block cannot see this: every
    delegation carried a different ``context`` string, so no signature repeated.
    """
    m = build_chat_orchestrator(_project(tmp_path), None)
    m._skill_run_history = [
        {"skill": "log-table-condenser", "context": "analyze the log", "result": _SERIALIZED_TOOL_CALL},
        {"skill": "log-table-condenser", "context": "extract metrics", "result": _UNCLOSED_REPORT},
    ]
    run_skill = build_run_skill_tool(m, str(tmp_path), "ollama/proj")
    raw = getattr(run_skill, "_func", None) or run_skill

    result = asyncio.run(raw("log-table-condenser", "summarize the schema"))

    assert "WORKER LOOP BREAKER" in result
    assert _recent_failed_skill_attempts(m, "log-table-condenser") == 2


def test_run_skill_breaks_delegation_loop_when_the_orchestrator_alternates_skills(tmp_path):
    """Switching specialist must not reset the breaker.

    Observed run: log-table-condenser, log-table-condenser, command-line,
    log-table-condenser, log-table-condenser — five delegations, none with a
    report, because the per-skill counter stops scanning at the first entry
    belonging to another skill.
    """
    m = build_chat_orchestrator(_project(tmp_path), None)
    m._skill_run_history = [
        {"skill": "log-table-condenser", "context": "c1", "result": _SERIALIZED_TOOL_CALL},
        {"skill": "log-table-condenser", "context": "c2", "result": _UNCLOSED_REPORT},
        {"skill": "command-line", "context": "c3", "result": _UNCLOSED_REPORT},
    ]
    run_skill = build_run_skill_tool(m, str(tmp_path), "ollama/proj")
    raw = getattr(run_skill, "_func", None) or run_skill

    # The per-skill counter cannot see this: the newest entry is another skill.
    assert _recent_failed_skill_attempts(m, "log-table-condenser") == 0
    assert _recent_failed_delegations(m) == 3

    result = asyncio.run(raw("log-table-condenser", "c4"))

    assert "DELEGATION LOOP BREAKER" in result
    assert "ask_question" in result


def test_delegation_counter_resets_after_a_usable_report(tmp_path):
    m = build_chat_orchestrator(_project(tmp_path), None)
    m._skill_run_history = [
        {"skill": "log-table-condenser", "context": "c1", "result": _SERIALIZED_TOOL_CALL},
        {"skill": "command-line", "context": "c2", "result": _UNCLOSED_REPORT},
        {"skill": "log-table-condenser", "context": "c3",
         "result": "Condensed 480 log lines into logs/summary.csv (5 columns)."},
    ]

    assert _recent_failed_delegations(m) == 0


def test_tool_calls_count_accepts_int_like_values():
    assert _tool_calls_count(0) == 0
    assert _tool_calls_count("2") == 2
    assert _tool_calls_count("?") is None


def test_run_skill_worker_disables_shared_router(tmp_path, monkeypatch):
    """Worker sub-agents must not reuse a LiteLLM Router created for the orchestrator."""
    import opalatex.memgpt_runtime as runtime
    from types import SimpleNamespace

    captured = {}

    class FakeLLMAgentBlock:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.name = kwargs.get("name", "")
            self.model = kwargs.get("model", "")
            self.tools = kwargs.get("tools", [])
            self.model_kwargs = kwargs.get("model_kwargs", {})
            self.on_iteration = None
            self.on_thinking = None
            self.on_chunk = None

        async def _acompletion(self, _messages, **_kwargs):
            return SimpleNamespace(choices=[])

        async def run(self, _input):
            return SimpleNamespace(response="done", tool_calls_made=1)

    monkeypatch.setattr(runtime, "LLMAgentBlock", FakeLLMAgentBlock)
    project = _project(tmp_path)
    project.mode = "auto"
    project.worker_model = "ollama/gemma4:26b"
    project.user_prompt_prefix = "Preserve citations."
    m = build_chat_orchestrator(project, None)
    assert m.user_prompt_prefix == project.user_prompt_prefix
    run_skill = build_run_skill_tool(
        m,
        str(tmp_path),
        project_model=project.model,
        project_worker=project.worker_model,
        _project_ref=project,
    )
    raw = getattr(run_skill, "_func", None) or run_skill

    _enable_helper(tmp_path)
    result = asyncio.run(raw("staged-reader", "inspect the project"))

    assert "done" in result
    assert captured["user_prompt_prefix"] == project.user_prompt_prefix
    assert captured["model"] == "ollama_chat/gemma4:26b"
    assert captured["use_shared_router"] is False
    assert "search_code" in {getattr(tool, "name", None) for tool in captured["tools"]}


def test_run_skill_worker_uses_light_prompt_profile_and_skill_variant(tmp_path, monkeypatch):
    """A worker model catalog entry with prompt_profile="light" must render the
    condensed worker preamble and pick up the delegated skill's SKILL.light.md
    body (command-line ships one) instead of the full SKILL.md."""
    import opalatex.memgpt_runtime as runtime
    from opalatex import models_store
    from types import SimpleNamespace

    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", tmp_path / "models_store.json")
    models_store.add_or_update_connection(
        {"id": "c1", "label": "c", "provider": "ollama", "api_key": "", "api_base": ""}
    )
    models_store.add_or_update_model({
        "id": "ollama/light-worker", "connection_id": "c1", "name": "light-worker",
        "prompt_profile": "light",
    })

    captured = {}

    class FakeLLMAgentBlock:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.name = kwargs.get("name", "")
            self.model = kwargs.get("model", "")
            self.tools = kwargs.get("tools", [])
            self.model_kwargs = kwargs.get("model_kwargs", {})
            self.on_iteration = None
            self.on_thinking = None
            self.on_chunk = None

        async def _acompletion(self, _messages, **_kwargs):
            return SimpleNamespace(choices=[])

        async def run(self, _input):
            return SimpleNamespace(response="done", tool_calls_made=1)

    monkeypatch.setattr(runtime, "LLMAgentBlock", FakeLLMAgentBlock)
    project = _project(tmp_path)
    project.mode = "auto"
    project.worker_model = "ollama/light-worker"
    m = build_chat_orchestrator(project, None)
    run_skill = build_run_skill_tool(
        m,
        str(tmp_path),
        project_model=project.model,
        project_worker=project.worker_model,
        _project_ref=project,
    )
    raw = getattr(run_skill, "_func", None) or run_skill

    _enable_helper(tmp_path)
    result = asyncio.run(raw("staged-reader", "inspect the project"))

    assert "done" in result
    system_prompt = captured["system_prompt"]
    assert "Tools: get_project_overview" in system_prompt
    assert "You are the **staged-reader** worker" in system_prompt  # its SKILL.light.md
    assert "RECOMMENDATION FOR EXECUTION AND TERMINATION" not in system_prompt
    assert "CRITICAL: LARGE FILES & TRUNCATION PREVENTION" not in system_prompt


def test_build_chat_orchestrator_scopes_project_path(tmp_path):
    """Regression: building the MemGPT must set the global project context so the
    sub-agent's file tools act inside the project, not the OpalaTex repo root."""
    from opalatex.tools import get_project_path
    build_chat_orchestrator(_project(tmp_path), None)
    assert os.path.abspath(get_project_path()) == os.path.abspath(str(tmp_path))

def test_chat_orchestrator_ignores_legacy_agent_error_in_model_history(tmp_path):
    project = _project(tmp_path)
    project.history = [
        {"role": "user", "content": "Explain transformers."},
        {"role": "assistant", "content": "Agent Error: Could not connect to ollama_chat/gpt-oss:20b."},
        {"role": "user", "content": "Continue."},
    ]

    agent = build_chat_orchestrator(project, None)
    contents = [message["content"] for message in agent.internal_history]

    assert "Explain transformers." in contents
    assert "Continue." in contents
    assert not any(str(content).startswith("Agent Error:") for content in contents)

def test_memgpt_retries_ollama_invalid_json_tool_call_with_heartbeat():
    from types import SimpleNamespace
    from agenticblocks.blocks.llm.agent import AgentInput
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    agent = MemGPTAgentBlock(
        name="retry-test",
        model="ollama_chat/gpt-oss:20b",
        max_heartbeats=2,
        system_prompt="test",
    )
    calls = []

    async def fake_completion(_messages, **_kwargs):
        calls.append([dict(message) for message in _messages])
        if len(calls) == 1:
            raise Exception(
                "Ollama_chatException - error parsing tool call: "
                "invalid character ',' in string escape code"
            )
        message = SimpleNamespace(
            content="Recovered response",
            reasoning_content=None,
            tool_calls=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    agent._acompletion = fake_completion
    result = asyncio.run(agent.run(AgentInput(prompt="test")))

    assert result.response == "Recovered response"
    assert len(calls) == 2
    assert "native tool-calling API" in calls[1][-1]["content"]

def test_memgpt_retries_a_llama_server_tool_call_rejection_through_the_compat_layer():
    """Recorded failure: a llama-server-backed Ollama rejected run_skill arguments
    carrying a raw newline, and the wording matched no detector, so the turn died
    with "The model rejected a parameter value" instead of a bounded retry."""
    from types import SimpleNamespace
    from agenticblocks.blocks.llm.agent import AgentInput
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
    from opalatex.litellm_compat import wrap_agent_litellm_compat

    agent = MemGPTAgentBlock(
        name="chat_orchestrator",
        model="ollama_chat/hf.co/LiquidAI/LFM2.5-2.6B-GGUF:F16",
        max_heartbeats=3,
        system_prompt="test",
    )
    calls = []

    async def fake_completion(_messages, **_kwargs):
        calls.append([dict(message) for message in _messages])
        if len(calls) == 1:
            raise Exception(
                "litellm.BadRequestError: Ollama_chatException - KeyError: 'message', Got "
                "unexpected response from Ollama: {'error': 'llama-server returned invalid "
                "tool call arguments for \"run_skill\": invalid character \\'\\\\n\\' "
                "in string literal'}"
            )
        message = SimpleNamespace(content="Recovered", reasoning_content=None, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    agent._acompletion = fake_completion
    wrap_agent_litellm_compat(agent)
    result = asyncio.run(agent.run(AgentInput(prompt="save the list")))

    assert result.response == "Recovered"
    assert len(calls) == 2
    alert = calls[1][-1]
    assert alert["role"] == "system"
    assert "native tool-calling API" in alert["content"]
    assert "\\n" in alert["content"]


def test_memgpt_block_recognizes_the_llama_server_wording_on_its_own():
    """The framework detector must not depend on OpalaTex's compat normalization."""
    from agenticblocks.blocks.llm.memgpt_agent import _is_ollama_tool_call_json_escape_error

    raw = Exception(
        "Ollama_chatException - Got unexpected response from Ollama: {'error': "
        "'llama-server returned invalid tool call arguments for \"write_file\": "
        "invalid character \\'\\\\n\\' in string literal'}"
    )
    assert _is_ollama_tool_call_json_escape_error(raw)
    # An unrelated Ollama failure is not a tool-call JSON error.
    assert not _is_ollama_tool_call_json_escape_error(
        Exception("Ollama_chatException - model 'x' not found, try pulling it first")
    )


def test_memgpt_returns_plain_json_content_without_tool_recovery():
    from types import SimpleNamespace
    from agenticblocks.blocks.llm.agent import AgentInput
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    response_text = '```json\n{"name":"read_file","arguments":{"path":"example"}}\n```'
    agent = MemGPTAgentBlock(
        name="json-response-test",
        model="test/model",
        max_heartbeats=2,
        system_prompt="test",
    )
    calls = []

    async def fake_completion(_messages, **_kwargs):
        calls.append(_kwargs)
        message = SimpleNamespace(
            content=response_text,
            reasoning_content=None,
            tool_calls=[],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

    agent._acompletion = fake_completion
    result = asyncio.run(agent.run(AgentInput(prompt="Show a JSON example.")))

    assert result.response == response_text
    assert result.tool_calls_made == 0


@pytest.mark.parametrize(
    "worker_kwargs, expect_wired",
    [
        ({"stream": True, "think": True}, True),
        ({"stream": True}, False),
    ],
)
def test_worker_reasoning_display_follows_its_own_model_capability(
    tmp_path, monkeypatch, worker_kwargs, expect_wired
):
    """A worker publishes reasoning exactly when its model was asked to isolate it.

    The gate used to be a separate `worker_model_params["think"]` checkbox, so a
    stale project value decided whether a reasoning channel the provider was
    already filling ever reached the panel.
    """
    import opalatex.memgpt_runtime as runtime
    from types import SimpleNamespace

    built = []

    class FakeLLMAgentBlock:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.model = kwargs.get("model", "")
            self.tools = kwargs.get("tools", [])
            self.model_kwargs = kwargs.get("model_kwargs", {})
            self.on_iteration = None
            self.on_thinking = None
            self.on_chunk = None
            built.append(self)

        async def _acompletion(self, _messages, **_kwargs):
            return SimpleNamespace(choices=[])

        async def run(self, _input):
            return SimpleNamespace(response="done", tool_calls_made=1)

    monkeypatch.setattr(runtime, "LLMAgentBlock", FakeLLMAgentBlock)
    monkeypatch.setattr(
        runtime, "get_agent_llm_kwargs",
        lambda agent_name, **_kwargs: dict(worker_kwargs) if agent_name == "worker" else {},
    )

    project = _project(tmp_path)
    project.mode = "auto"
    project.worker_model = "ollama/gemma4:26b"
    # A stale per-project value must not resurrect the retired setting.
    project.worker_model_params = {"think": not expect_wired, "stream": True}

    m = build_chat_orchestrator(project, None)
    run_skill = build_run_skill_tool(
        m,
        str(tmp_path),
        project_model=project.model,
        project_worker=project.worker_model,
        _project_ref=project,
    )
    raw = getattr(run_skill, "_func", None) or run_skill
    _enable_helper(tmp_path)
    asyncio.run(raw("staged-reader", "inspect the project"))

    assert built, "the worker sub-agent should have been constructed"
    assert (built[-1].on_thinking is not None) is expect_wired


def test_worker_history_keeps_the_answer_it_is_asked_to_save(tmp_path, monkeypatch):
    """Recorded failure: after the user asked to save "the list", the orchestrator
    made a few tool calls and delegated. Slicing history[-10:] before filtering let
    those tool calls and results fill the window, so the worker received empty
    "ASSISTANT:" lines and never the answer holding the list."""
    import opalatex.memgpt_runtime as runtime
    from types import SimpleNamespace

    captured = {}

    class FakeLLMAgentBlock:
        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "")
            self.model = kwargs.get("model", "")
            self.tools = kwargs.get("tools", [])
            self.model_kwargs = kwargs.get("model_kwargs", {})
            self.on_iteration = None
            self.on_thinking = None
            self.on_chunk = None

        async def _acompletion(self, _messages, **_kwargs):
            return SimpleNamespace(choices=[])

        async def run(self, agent_input):
            captured["prompt"] = agent_input.prompt
            return SimpleNamespace(response="done", tool_calls_made=1)

    monkeypatch.setattr(runtime, "LLMAgentBlock", FakeLLMAgentBlock)
    project = _project(tmp_path)
    project.mode = "auto"
    m = build_chat_orchestrator(project, None)
    m.internal_history = [
        {"role": "user", "content": "Create an exercise list on LRTA*."},
        {"role": "assistant", "content": "\\section{Level 1} THE LIST BODY"},
        {"role": "user", "content": "Save it to lista.tex."},
    ]
    for i in range(6):
        m.internal_history.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        m.internal_history.append({"role": "tool", "tool_call_id": f"c{i}", "content": "Error"})
    m.internal_history.append({"role": "system", "content": "SYSTEM ALERT: something"})

    run_skill = build_run_skill_tool(
        m, str(tmp_path), project_model=project.model, _project_ref=project,
    )
    raw = getattr(run_skill, "_func", None) or run_skill
    _enable_helper(tmp_path)
    asyncio.run(raw("staged-reader", "write lista.tex"))

    history = captured["prompt"].split("MAIN AGENT'S REQUEST:")[0]
    assert "ASSISTANT: \\section{Level 1} THE LIST BODY" in history
    assert "USER: Save it to lista.tex." in history
    assert "ASSISTANT: \n" not in history
    assert "SYSTEM ALERT" not in history
    # Oldest first, as a conversation reads.
    assert history.index("Create an exercise list") < history.index("THE LIST BODY")


def test_worker_history_is_bounded_to_recent_conversation():
    from opalatex.memgpt_runtime import _recent_conversation_for_worker

    history = [{"role": "user", "content": f"message {i}"} for i in range(15)]
    history.append({"role": "user", "content": [{"type": "text", "text": "with attachment"},
                                                {"type": "image_url", "image_url": {"url": "x"}}]})

    out = _recent_conversation_for_worker(history, limit=3)

    assert out.splitlines() == ["USER: message 13", "USER: message 14", "USER: with attachment"]
