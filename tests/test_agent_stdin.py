"""Tests for the stdin/stdout agent server protocol."""

import json
import io
import sys
import asyncio
import shutil
import subprocess
import pytest
from unittest.mock import MagicMock, AsyncMock
from types import SimpleNamespace

from opalatex.agent_stdin import (
    print_event,
    wrap_tool,
    patched_get_helper_tools,
    handle_load_project,
    _empty_response_failure_message,
    _auxiliary_tool_call_thought,
    _friendly_llm_error,
    _record_turn_thought,
    _response_with_thought,
    _sanitize_model_response,
    _normalize_inline_fenced_replacement,
    _validate_inline_replacement_scope,
    _should_emit_iteration_reflection,
    _visible_chat_response,
    _worker_summary_response,
    clear_worker_message_buffer,
    record_worker_message,
)


def test_record_turn_thought_keeps_every_chunk(monkeypatch):
    import opalatex.agent_stdin as stdin_mod

    chunks = []
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_CHUNKS", chunks)
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_METER", None)

    assert _record_turn_thought(r"\u2013" * 80)
    assert _record_turn_thought("more repeated noise")
    assert chunks == [r"\u2013" * 80, "more repeated noise"]


def test_auxiliary_tool_call_thought_uses_compact_text_not_json():
    thought = _auxiliary_tool_call_thought(
        "search_code",
        {
            "query": "path where project files are stored (e.g., './src', 'project/', 'root_dir')",
            "path": ".",
            "use_regex": False,
            "case_sensitive": False,
            "max_results": 50,
            "file_pattern": "*",
        },
    )

    assert thought == (
        "Decided to execute tool 'search_code' "
        "(path=.; query=path where project files are stored (e.g., './src', 'project/', 'root_dir'); max_results=50)."
    )
    assert "{" not in thought
    assert '"query"' not in thought


def test_print_event_auxiliary_tool_call_thought_hides_raw_json(monkeypatch):
    import opalatex.agent_stdin as stdin_mod

    events = []
    chunks = []
    monkeypatch.setattr(stdin_mod, "event_hook", lambda payload: events.append(payload))
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_CHUNKS", chunks)
    monkeypatch.setattr(stdin_mod, "_ACTIVE_THOUGHT_METER", None)

    print_event("tool_call", {
        "tool": "get_project_overview",
        "arguments": {"max_depth": 5, "include_hidden": False},
        "agent": "chat_orchestrator",
    })

    thoughts = [event["content"] for event in events if event["event"] == "thought"]
    assert thoughts == ["Decided to execute tool 'get_project_overview' (max_depth=5)."]
    assert chunks == thoughts
    assert "{" not in thoughts[0]

def test_print_event(monkeypatch):
    """Verify that print_event writes a JSON line containing the event and data to the real stdout."""
    # Create a dummy stream to capture real stdout
    stream = io.StringIO()
    # Temporarily monkeypatch the module's _real_stdout
    import opalatex.agent_stdin
    monkeypatch.setattr(opalatex.agent_stdin, "_real_stdout", stream)

    print_event("test_event", {"foo": "bar", "num": 123})
    
    stream.seek(0)
    output = stream.read().strip()
    
    # Verify the printed event is valid JSON and matches expected values
    parsed = json.loads(output)
    assert parsed["event"] == "test_event"
    assert parsed["foo"] == "bar"
    assert parsed["num"] == 123


def test_print_event_persists_thinking_and_stream_activity(monkeypatch):
    import opalatex.agent_stdin

    saved = []

    class FakeProject:
        name = "proj"
        current_chat_id = "main"

    class FakeStore:
        def append_activity(self, project, event, content="", agent="", payload=None):
            saved.append((project.name, event, content, agent, payload or {}))
            return len(saved)

    monkeypatch.setattr(opalatex.agent_stdin, "current_project", FakeProject())
    monkeypatch.setattr(opalatex.agent_stdin, "current_store", FakeStore())
    monkeypatch.setattr(opalatex.agent_stdin, "event_hook", lambda _payload: None)

    print_event("thought", {"content": "Think this through.", "agent": "chat_orchestrator"})
    print_event("stream_chunk", {"content": "Visible stream.", "agent": "chat_orchestrator"})

    assert saved == [
        ("proj", "thought", "Think this through.", "chat_orchestrator", {"agent": "chat_orchestrator"}),
        ("proj", "stream_chunk", "Visible stream.", "chat_orchestrator", {"agent": "chat_orchestrator"}),
    ]


def test_wrap_tool():
    """Verify that wrap_tool correctly wraps a sync function as an AgenticBlocks tool."""
    # Define a simple function to wrap
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    wrapped = wrap_tool(add)
    assert wrapped.name == "add"
    assert "Add two numbers" in wrapped.description
    
    # We can invoke the wrapped function via its raw _func attribute or directly
    raw = getattr(wrapped, "_func", None) or wrapped
    assert raw(2, 3) == 5


@pytest.mark.asyncio
async def test_wrap_tool_async():
    """Verify that wrap_tool correctly wraps an async function."""
    async def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        await asyncio.sleep(0.001)
        return a * b

    wrapped = wrap_tool(multiply)
    assert wrapped.name == "multiply"
    assert "Multiply two numbers" in wrapped.description
    
    raw = getattr(wrapped, "_func", None) or wrapped
    res = await raw(3, 4)
    assert res == 12


def test_patched_get_helper_tools():
    """Verify that patched_get_helper_tools returns wrapped tools."""
    tools = patched_get_helper_tools()
    assert len(tools) > 0
    # Every tool returned should be wrapped (which can be checked if its function is wrapped)
    # The wrapped function will have the wrapped decorators applied.
    for t in tools:
        assert hasattr(t, "name")
        assert hasattr(t, "description")


def test_empty_response_failure_message_is_localized():
    from opalatex.i18n import set_lang

    set_lang("en")
    assert _empty_response_failure_message() == "The agent finished without a non-empty final response after automatic correction attempts. No fallback response was saved."

    set_lang("pt")
    assert _empty_response_failure_message() == "O agente terminou sem uma resposta final não vazia após as tentativas automáticas de correção. Nenhuma resposta fallback foi salva."


def test_ollama_model_not_found_error_is_localized():
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gemma")
    exc = Exception("model not found, try pulling it first")

    set_lang("en")
    en_msg = _friendly_llm_error(exc, project)
    assert "The model `gemma` was not found locally" in en_msg
    assert "O modelo" not in en_msg

    set_lang("pt")
    pt_msg = _friendly_llm_error(exc, project)
    assert "O modelo `gemma` não foi encontrado localmente" in pt_msg


def test_remote_ollama_model_not_found_error_does_not_claim_local_download():
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://100.85.255.111:11434/v1")
    exc = Exception("model not found, try pulling it first")

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "was not found locally" not in msg
    assert "Model ollama/gpt-oss:20b was not found" in msg


def test_ollama_404_page_not_found_is_reported_as_connection_error():
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://100.85.255.111:11434/v1")
    exc = Exception("OllamaException - 404 page not found")

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "was not found locally" not in msg
    assert "Could not connect to ollama/gpt-oss:20b" in msg


def test_stream_stalled_mid_response_is_not_reported_as_connection_error():
    """A stream that goes silent until the read timeout fires.

    Text captured from LiteLLM against a server that streamed thinking chunks
    and then stopped sending data.
    """
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://100.85.255.111:11434/v1")
    exc = Exception(
        "litellm.MidStreamFallbackError: litellm.APIConnectionError: "
        "Ollama_chatException - Timeout on reading data from socket Original exception: "
        "APIConnectionError: litellm.APIConnectionError: Ollama_chatException - "
        "Timeout on reading data from socket"
    )

    set_lang("en")
    en_msg = _friendly_llm_error(exc, project)
    assert "Could not connect" not in en_msg
    assert "ollama/gpt-oss:20b started responding, then stopped sending data" in en_msg

    set_lang("pt")
    pt_msg = _friendly_llm_error(exc, project)
    assert "Falha de conexão" not in pt_msg
    assert "parou de enviar dados" in pt_msg


def test_request_timeout_is_not_reported_as_connection_error():
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://100.85.255.111:11434/v1")
    # A 500 s timeout must not be read as an Ollama HTTP 500 either.
    exc = Exception(
        "litellm.Timeout: Ollama_chatException - litellm.Timeout: Connection timed out "
        "after 500.0 seconds."
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "Could not connect" not in msg
    assert "HTTP 500" not in msg
    assert "ollama/gpt-oss:20b did not respond before the request timed out" in msg


def test_refused_connection_is_still_reported_as_connection_error():
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://10.255.255.1:11434")
    exc = Exception(
        "litellm.APIConnectionError: Ollama_chatException - Cannot connect to host "
        "10.255.255.1:11434 [Connect call failed ('10.255.255.1', 11434)]"
    )

    set_lang("en")
    assert "Could not connect to ollama/gpt-oss:20b" in _friendly_llm_error(exc, project)


def test_insufficient_quota_error_is_localized():
    from opalatex.i18n import set_lang
    project = SimpleNamespace(model="openai/gpt-5.5")
    exc = Exception("OpenAIException - {\"error\": {\"message\": \"You exceeded your current quota, please check your plan and billing details.\", \"type\": \"insufficient_quota\"}}")

    set_lang("en")
    en_msg = _friendly_llm_error(exc, project)
    assert "insufficient credits" in en_msg.lower() or "billing settings" in en_msg.lower()

    set_lang("pt")
    pt_msg = _friendly_llm_error(exc, project)
    assert "cota de uso" in pt_msg.lower() or "créditos suficientes" in pt_msg.lower()


def test_worker_summary_response_prefers_current_worker_messages():
    clear_worker_message_buffer()
    record_worker_message("stale recorded worker message")

    class Agent:
        _current_worker_messages = ["Created test.tex successfully."]
        _last_worker_summary = "stale summary"

    assert _worker_summary_response(Agent()) == "Created test.tex successfully."
    clear_worker_message_buffer()


def test_worker_summary_response_falls_back_to_last_worker_summary():
    clear_worker_message_buffer()

    class Agent:
        _current_worker_messages = []
        _last_worker_summary = "Worker finished the requested task."

    assert _worker_summary_response(Agent()) == "Worker finished the requested task."


def test_worker_summary_response_prefers_visible_intermediate_agent_response():
    clear_worker_message_buffer()

    import opalatex.agent_stdin as stdin_mod

    original_hook = stdin_mod.event_hook
    stdin_mod.event_hook = lambda _payload: None
    try:
        stdin_mod.print_event("agent_response", {
            "response": "Already visible in chat.",
            "intermediate": True,
        })
    finally:
        stdin_mod.event_hook = original_hook

    class Agent:
        _last_worker_chat_response = ""
        _current_worker_messages = []
        _last_worker_summary = ""
        internal_history = []

    assert _worker_summary_response(Agent()) == "Already visible in chat."
    clear_worker_message_buffer()


def test_worker_summary_response_extracts_run_skill_tool_result():
    clear_worker_message_buffer()

    class Agent:
        _current_worker_messages = []
        _last_worker_summary = ""
        internal_history = [
            {
                "role": "tool",
                "content": (
                    "[skill 'command-line' finished] Worker's summary/report:\n"
                    "(Tools used by worker: 2)\n"
                    "Created test.tex and saved the requested LaTeX article."
                ),
            }
        ]

    assert _worker_summary_response(Agent()) == "Created test.tex and saved the requested LaTeX article."


def test_worker_summary_response_uses_recorded_worker_message_when_agent_has_no_summary():
    clear_worker_message_buffer()
    record_worker_message("Worker report reached stdout.")

    class Agent:
        _current_worker_messages = []
        _last_worker_summary = ""
        internal_history = []

    assert _worker_summary_response(Agent()) == "Worker report reached stdout."
    clear_worker_message_buffer()


def test_response_with_thought_returns_visible_chat_content_only():
    assert _response_with_thought("Done.", ["Thinking 1\n", "Thinking 2"]) == "Done."
    assert _response_with_thought("Done.", []) == "Done."
    assert _response_with_thought("<think>\nAlready wrapped\n</think>\n\nDone.", ["Thought"]) == "Done."


def test_visible_chat_response_strips_thought_blocks():
    assert _visible_chat_response("Done.") == "Done."
    assert _visible_chat_response("<think>\nold\n</think>\n\nDone.") == "Done."
    assert _visible_chat_response("<think>\n\n</think>\n\nDone.") == "Done."


def test_visible_chat_response_strips_reasoning_channels():
    assert _visible_chat_response(
        "<|channel|>analysis<|message|>private note<|end|>"
        "<|channel|>final<|message|>Visible answer."
    ) == "Visible answer."



def test_sanitize_model_response_moves_channel_thought_to_snapshot():
    thoughts = []

    visible = _sanitize_model_response(
        (
            "<think>\n\n</think>\n"
            "<|channel|>thought<|message|>I should inspect the prompt.<|end|>"
            "<|channel|>final<|message|>Here is the explanation."
        ),
        thoughts,
    )

    assert visible == "Here is the explanation."
    assert thoughts == ["I should inspect the prompt."]


def test_sanitize_model_response_consolidates_multiple_think_blocks():
    thoughts = ["live thought."]

    visible = _sanitize_model_response(
        "<think>first internal note</think>\n\nVisible answer.\n\n<think>second internal note</think>",
        thoughts,
    )

    assert visible == "Visible answer."
    assert thoughts == ["live thought.", "first internal note", "second internal note"]


def test_sanitize_model_response_treats_thought_only_channel_as_empty_response():
    thoughts = []

    visible = _sanitize_model_response(
        "<|channel|>thought\nWait, I see what happened.",
        thoughts,
    )

    assert visible == ""
    assert thoughts == ["Wait, I see what happened."]


def test_iteration_reflection_only_accepts_assistant_prose():
    assert _should_emit_iteration_reflection({
        "role": "assistant",
        "content": "I inspected the methodology section.",
    })

    assert not _should_emit_iteration_reflection({
        "role": "tool",
        "content": '{"result": "projeto_pesquisa.tex:309: \\\\section{Metodologia}"}',
    })
    assert not _should_emit_iteration_reflection({
        "role": "user",
        "content": "SYSTEM ALERT: You returned an empty response.",
    })
    assert not _should_emit_iteration_reflection({
        "role": "assistant",
        "content": '{"name": "read_file", "arguments": {"path": "main.tex"}}',
    })


@pytest.mark.asyncio
async def test_handle_run_inline_normalizes_remote_ollama_api_base(monkeypatch):
    import opalatex.agent_stdin as stdin_mod
    import opalatex.config as config_mod
    import opalatex.litellm_compat as compat_mod

    captured = {}
    events = []

    class FakeInlineAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.name = kwargs["name"]
            self.model = kwargs["model"]
            self.model_kargs = kwargs["model_kwargs"]
            self.model_kwargs = kwargs["model_kwargs"]
            self.internal_history = []
            self.on_thinking = None
            self.on_chunk = None
            self.on_iteration = None

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            assert self.on_thinking is None
            assert self.on_chunk is None
            assert self.on_iteration is None
            return SimpleNamespace(response="ok")

    monkeypatch.setattr(stdin_mod, "LLMAgentBlock", FakeInlineAgent)
    monkeypatch.setattr(compat_mod, "wrap_agent_litellm_compat", lambda agent: agent)
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(config_mod, "get_agent_model", lambda _agent_name, default=None: default)
    monkeypatch.setattr(
        config_mod,
        "get_agent_llm_kwargs",
        # `model_override` is passed so the kwargs belong to the model that will
        # actually run, not to the role's default catalog entry.
        lambda _agent_name, model_override=None: {"api_base": "http://100.85.255.111:11434/v1"},
    )
    monkeypatch.setattr(config_mod, "model_supports_thinking", lambda _model: False)
    monkeypatch.setattr(stdin_mod, "current_project", None)
    monkeypatch.setattr(stdin_mod, "current_store", None)
    monkeypatch.setattr(stdin_mod, "current_memgpt", None)

    await stdin_mod.handle_run({
        "agent": "inline_editor",
        "model": "ollama/gpt-oss:20b",
        "system_prompt": "Return a replacement.",
        "prompt": "Replace this text.",
        "tools": [],
        "inline_response_contract": "replacement_only",
        "model_params": {
            "max_tokens": 123,
            "stream": True,
        },
    })

    # Ollama models always run on the native chat route now, independently of
    # thinking (see test_agent_config.py); this test is about the api_base below.
    assert captured["model"] == "ollama_chat/gpt-oss:20b"
    assert captured["model_kwargs"]["api_base"] == "http://100.85.255.111:11434"
    assert events == [("agent_response", {"response": "ok"})]
    assert captured["tools"] == []
    assert captured["model_kwargs"]["max_tokens"] == 123
    assert captured["model_kwargs"]["stream"] is False


@pytest.mark.asyncio
async def test_handle_run_retries_orchestrator_before_using_worker_summary(monkeypatch):
    import opalatex.agent_stdin as stdin_mod

    events = []
    prompts = []

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        calls = 0

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            self.calls += 1
            prompts.append(agent_input.prompt)
            if self.calls == 1:
                self._current_worker_messages = ["Worker created test.tex successfully."]
                return SimpleNamespace(response="")
            return SimpleNamespace(response="Created test.tex successfully. Please verify the file.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", None)
    monkeypatch.setattr(stdin_mod, "current_store", None)

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create test.tex",
    })

    assert len(prompts) == 2
    assert "Worker created test.tex successfully." in prompts[1]
    assert "send_message" not in prompts[1]
    agent_responses = [data["response"] for event, data in events if event == "agent_response"]
    assert agent_responses == ["Created test.tex successfully. Please verify the file."]


@pytest.mark.asyncio
async def test_handle_run_does_not_promote_worker_message_without_orchestrator_response(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        calls = 0

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            self.calls += 1
            if self.calls == 1:
                # A worker reported, and the orchestrator itself said nothing. The
                # report must not be promoted into the user-facing answer.
                stdin_mod.record_worker_message("Worker created test.tex successfully.")
                self._current_worker_messages = []
                self._last_worker_summary = ""
                return SimpleNamespace(response="")
            assert "Worker created test.tex successfully." in agent_input.prompt
            assert "send_message" not in agent_input.prompt
            return SimpleNamespace(response="The file test.tex was created. Please verify it.")

    fake_agent = FakeMemGPT()
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", fake_agent)
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create test.tex",
    })

    assistant_messages = [content for role, content in saved_messages if role == "assistant"]
    assert assistant_messages == ["The file test.tex was created. Please verify it."]

    agent_responses = [data["response"] for event, data in events if event == "agent_response"]
    assert agent_responses == ["The file test.tex was created. Please verify it."]
    assert fake_agent.calls == 2


@pytest.mark.asyncio
async def test_handle_run_reports_error_when_orchestrator_never_sends_message(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod
    from opalatex.i18n import set_lang

    set_lang("en")

    events = []
    saved_messages = []
    saved_activity = []
    prompts = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, agent_input):
            prompts.append(agent_input.prompt)
            self._current_worker_messages = ["Worker claims it completed the job."]
            return SimpleNamespace(response="")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create test.tex",
    })

    assistant_messages = [content for role, content in saved_messages if role == "assistant"]
    assert assistant_messages == []
    assert any(event == "error" for event, _data in events)

    agent_responses = [data["response"] for event, data in events if event == "agent_response"]
    assert agent_responses == []
    errors = [data["message"] for event, data in events if event == "error"]
    assert errors == [
        "The agent finished without a non-empty final response after automatic correction attempts. No fallback response was saved."
    ]
    assert len(prompts) == 3
    assert "send_message" not in prompts[1]
    assert "send_message" not in prompts[2]
    assert "O trabalho solicitado parece ter sido concluído" not in "\n".join(agent_responses)


@pytest.mark.asyncio
async def test_handle_run_does_not_duplicate_user_message_on_failed_retries(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod
    from opalatex.project import ProjectStore

    db_path = str(tmp_path / "projects.db")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store = ProjectStore(db_path=db_path)
    project = store.create(
        name="proj",
        mode="auto",
        model="fake/model",
        project_name="Project",
        project_path=str(project_dir),
    )
    store.create_chat("proj", "chat-proj", "Chat")
    project = store.load("proj")

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            raise RuntimeError("simulated api failure")

    events = []
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", project)
    monkeypatch.setattr(stdin_mod, "current_store", store)

    payload = {
        "agent": "chat_orchestrator",
        "prompt": "review main.tex",
        "client_message_id": "client-turn-1",
    }
    await stdin_mod.handle_run(payload)
    await stdin_mod.handle_run(payload)
    await stdin_mod.handle_run(payload)

    loaded = store.load("proj", chat_id=project.current_chat_id)
    # User message must appear exactly once (no duplicates despite 3 retried calls)
    user_messages = [(m["role"], m["content"]) for m in loaded.history if m["role"] == "user"]
    assert user_messages == [("user", "review main.tex")]
    assert loaded.history[0].get("client_message_id") == "client-turn-1" or any(
        m.get("client_message_id") == "client-turn-1" for m in loaded.history if m["role"] == "user"
    )
    # Each handle_run call records a [MODE] turn-start entry as diagnostic activity.
    # It must never land in project_history: the chat holds the visible conversation
    # only, which is exactly what the model reads back (PROJECT_DESIGN 2.5).
    assert not any(m["role"] == "system" for m in loaded.history)
    mode_activity = [
        item for item in store.list_activity("proj", project.current_chat_id)
        if item["event"] == "mode" and item["content"].startswith("[MODE] Agent turn started")
    ]
    assert len(mode_activity) == 3, f"Expected 3 [MODE] entries, got {len(mode_activity)}"
    assistant_errors = [m["content"] for m in loaded.history if m["role"] == "assistant"]
    assert assistant_errors == []
    assert [data["message"] for event, data in events if event == "error"] == [
        "simulated api failure",
        "simulated api failure",
        "simulated api failure",
    ]



@pytest.mark.asyncio
async def test_handle_run_persists_visible_response_without_thought_snapshot(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        on_thinking = None

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            self.on_thinking("I should save the file.")
            return SimpleNamespace(response="The file was saved.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "save file",
    })

    assistant_messages = [content for role, content in saved_messages if role == "assistant"]
    expected_persisted = "The file was saved."
    assert assistant_messages == [expected_persisted]

    agent_responses = [data["response"] for event, data in events if event == "agent_response"]
    assert agent_responses == ["The file was saved."]
    persisted_responses = [data["persisted_response"] for event, data in events if event == "agent_response"]
    assert persisted_responses == [expected_persisted]


@pytest.mark.asyncio
async def test_handle_run_does_not_persist_auxiliary_thought_events_in_chat_history(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        on_thinking = None

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            stdin_mod.print_event("tool_result", {
                "tool": "write_file",
                "result": "ok",
                "is_error": False,
                "agent": "chat_orchestrator",
            })
            return SimpleNamespace(response="The file was saved.")

    monkeypatch.setattr(stdin_mod, "event_hook", lambda payload: events.append(payload))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "save file",
    })

    assistant_messages = [content for role, content in saved_messages if role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0] == "The file was saved."
    assert "<think>" not in assistant_messages[0]
    assert "Received successful return from tool 'write_file'" not in assistant_messages[0]

    agent_responses = [payload["response"] for payload in events if payload["event"] == "agent_response"]
    assert agent_responses == ["The file was saved."]
    persisted_responses = [payload["persisted_response"] for payload in events if payload["event"] == "agent_response"]
    assert persisted_responses == assistant_messages
    thoughts = [payload["content"] for payload in events if payload["event"] == "thought"]
    assert any("Received successful return from tool 'write_file'" in thought for thought in thoughts)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_handle_run_checkpoints_direct_file_writes(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            (tmp_path / "script_created.tex").write_text("created outside tools\n", encoding="utf-8")
            return SimpleNamespace(response="Created the file.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create file through python",
    })

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    status = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "status",
            "--porcelain",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" in log
    assert "Agent turn end checkpoint" in log
    assert status == ""
    assert (tmp_path / "script_created.tex").read_text(encoding="utf-8") == "created outside tools\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_handle_run_removes_turn_checkpoints_without_file_changes(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            return SimpleNamespace(response="No file changes were needed.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "explain only",
    })

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    assert "Agent turn start checkpoint" not in log
    assert "Agent turn end checkpoint" not in log


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_handle_run_finalizes_checkpoint_when_agent_errors(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []
    saved_messages = []
    saved_activity = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            saved_messages.append((role, content))

        def append_activity(self, _project, event, content="", agent="", payload=None):
            saved_activity.append((event, content))

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            (tmp_path / "partial.tex").write_text("partial change\n", encoding="utf-8")
            raise RuntimeError("agent failed")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create file then fail",
    })

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    status = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "status",
            "--porcelain",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" in log
    assert "Agent turn end checkpoint" in log
    assert status == ""
    assert (tmp_path / "partial.tex").read_text(encoding="utf-8") == "partial change\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_handle_run_finalizes_checkpoint_when_cancelled(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod
    import opalatex.vcs as vcs
    import threading

    changed = asyncio.Event()
    events = []
    finalize_started = threading.Event()
    release_finalize = threading.Event()
    real_finalize = vcs.finalize_agent_turn_checkpoint_details

    def delayed_finalize(project_path, start_checkpoint=None, agent_label=None):
        finalize_started.set()
        release_finalize.wait(timeout=5)
        return real_finalize(project_path, start_checkpoint, agent_label)

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        model_params = {}
        current_chat_id = "main"
        user_prompt_prefix = ""

    class FakeStore:
        closed = False

        def append_message(self, _project, role, content, attachments=None, **_kwargs):
            return 1

        def append_activity(self, _project, event, content="", agent="", payload=None):
            pass

        def save(self, _project):
            pass

        def close_activity_connection(self):
            self.closed = True

    class FakeMemGPT:
        model = "fake/model"
        model_kargs = {}
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        system_prompt = ""

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            (tmp_path / "cancelled.tex").write_text("partial change\n", encoding="utf-8")
            changed.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    store = FakeStore()
    monkeypatch.setattr(stdin_mod, "current_store", store)
    monkeypatch.setattr(vcs, "finalize_agent_turn_checkpoint_details", delayed_finalize)

    task = asyncio.create_task(stdin_mod.handle_run({
        "agent": "chat_orchestrator",
        "prompt": "create a partial file",
        "chat_id": "main",
    }))
    await asyncio.wait_for(changed.wait(), timeout=5)
    task.cancel()
    assert await asyncio.to_thread(finalize_started.wait, 5)
    task.cancel()
    release_finalize.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    log = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "log",
            "--format=%s",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    status = subprocess.run(
        [
            "git",
            f"--git-dir={tmp_path / '.opalatex' / '.shadowgit'}",
            f"--work-tree={tmp_path}",
            "status",
            "--porcelain",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "Agent turn start checkpoint" in log
    assert "Agent turn end checkpoint" in log
    assert status == ""
    assert store.closed is True
    assert ("checkpoint_finalized", {
        "agent": "chat_orchestrator",
        "success": True,
        "changed_files": ["cancelled.tex"],
        "project_path": str(tmp_path),
    }) in events


@pytest.mark.asyncio
async def test_server_interrupt_acknowledges_immediately_and_cancels_once():
    from opalatex.ide_server import AsyncHTTPServer

    server = AsyncHTTPServer()
    event_queue = asyncio.Queue()
    blocker = asyncio.Event()
    agent_task = asyncio.create_task(blocker.wait())

    server._emit_agent_cancelled_once(agent_task, event_queue)
    event = event_queue.get_nowait()

    assert event["event"] == "cancelled"
    assert not agent_task.done()
    assert server._request_agent_cancel_once(agent_task) is True
    assert server._request_agent_cancel_once(agent_task) is False

    with pytest.raises(asyncio.CancelledError):
        await agent_task


@pytest.mark.asyncio
async def test_project_handlers(tmp_path, monkeypatch):
    """Test project listing, creation, and deletion via stdin handlers."""
    from opalatex.agent_stdin import handle_create_project, handle_list_projects, handle_delete_project
    
    db_file = str(tmp_path / "test_db.sqlite")
    
    # Capture print_event calls
    events = []
    def mock_print_event(event, data):
        events.append((event, data))
        
    import opalatex.agent_stdin
    monkeypatch.setattr(opalatex.agent_stdin, "print_event", mock_print_event)
    
    # 1. Create project
    await handle_create_project({
        "db": db_file,
        "project_name": "Test Project",
        "project_path": str(tmp_path),
        "description": "My test desc",
    })
    
    assert len(events) == 1
    assert events[0][0] == "project_created"
    assert events[0][1]["project_name"] == "Test Project"
    
    # 2. List projects
    events.clear()
    await handle_list_projects({
        "db": db_file,
    })
    assert len(events) == 1
    assert events[0][0] == "projects_list"
    assert len(events[0][1]["projects"]) == 1
    assert events[0][1]["projects"][0]["project_name"] == "Test Project"
    
    # 3. Delete project
    events.clear()
    await handle_delete_project({
        "db": db_file,
        "project_name": "test_project", # name field is db key which is lowercase slug
    })
    assert len(events) == 1
    assert events[0][0] == "project_deleted"
    
    # 4. List projects again (should be empty)
    events.clear()
    await handle_list_projects({
        "db": db_file,
    })
    assert len(events) == 1
    assert len(events[0][1]["projects"]) == 0


def test_skip_directories_in_collect_python_files(tmp_path):
    """Verify that _collect_python_files skips tests, opalatex, skills, and debug directories."""
    from opalatex.tools import _collect_python_files
    import os
    
    # Create structure
    (tmp_path / "opalatex").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "debug").mkdir()
    
    # Write python files inside skipped directories
    (tmp_path / "opalatex" / "main.py").write_text("print('core')")
    (tmp_path / "tests" / "test_app.py").write_text("print('test')")
    (tmp_path / "skills" / "run.py").write_text("print('skill')")
    (tmp_path / "debug" / "debug.py").write_text("print('debug')")
    
    # Write python files in root (should be collected)
    (tmp_path / "app.py").write_text("print('app')")
    
    # Run collector
    collected = _collect_python_files(str(tmp_path), str(tmp_path))
    
    # Assert that only app.py was collected
    collected_basenames = [os.path.basename(f) for f in collected]
    assert "app.py" in collected_basenames
    assert "main.py" not in collected_basenames
    assert "test_app.py" not in collected_basenames
    assert "run.py" not in collected_basenames
    assert "debug.py" not in collected_basenames
    assert len(collected) == 1

def test_ollama_tool_call_parse_error_is_not_reported_as_connection_error():
    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://localhost:11434")
    msg = _friendly_llm_error(
        Exception(
            "litellm.APIConnectionError: Ollama_chatException - "
            "error parsing tool call: invalid character ',' in string escape code"
        ),
        project,
    )

    assert "tool-call JSON was invalid" in msg
    assert "Could not connect" not in msg

def test_llama_server_tool_call_rejection_is_not_reported_as_a_parameter_value():
    """Newer Ollama builds word the rejection differently; it is still the model's
    malformed tool-call JSON, not a model parameter the user set."""
    project = SimpleNamespace(model="ollama/hf.co/LiquidAI/LFM2.5-2.6B-GGUF:F16", api_base="")
    msg = _friendly_llm_error(Exception((
    "litellm.BadRequestError: Ollama_chatException - KeyError: 'message', Got unexpected "
    "response from Ollama: {'error': 'llama-server returned invalid tool call arguments "
    "for \"run_skill\": invalid character \\'\\\\n\\' in string literal'}"
)), project)

    assert "tool-call JSON was invalid" in msg
    assert "rejected a parameter value" not in msg

def test_ollama_http_500_is_not_misclassified_as_connection_error():
    project = SimpleNamespace(model="ollama/gpt-oss:20b", api_base="http://localhost:11434")
    msg = _friendly_llm_error(
        Exception("litellm.APIConnectionError: OllamaException - Internal Server Error"),
        project,
    )

    assert "HTTP 500" in msg
    assert "not a connection error" in msg
    assert "Ollama server log" in msg


def test_ollama_context_exceeded_400_is_reported_as_context_exceeded_not_generic_http_error():
    """A context-size-exceeded 400 from Ollama must surface the specific,
    actionable context message instead of the generic 'reproduce it' HTTP fallback.
    """
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="ollama_chat/hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M", api_base="")
    exc = Exception(
        "litellm.APIConnectionError: Ollama_chatException - "
        '{"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"request (8401 tokens) '
        'exceeds the available context size (8192 tokens), try increasing it\\",'
        '\\"type\\":\\"exceed_context_size_error\\",\\"n_prompt_tokens\\":8401,\\"n_ctx\\":8192}}"}'
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "exceeded the context window" in msg
    assert "HTTP 400" not in msg
    assert "Reproduce it" not in msg


def test_ollama_system_message_order_error_suggests_enabling_catalog_flag(monkeypatch):
    from opalatex.i18n import set_lang
    from opalatex import config as config_mod

    monkeypatch.setattr(config_mod, "model_requires_single_system_message", lambda model: False)

    project = SimpleNamespace(model="ollama_chat/qwen3.8:latest", api_base="")
    exc = Exception(
        "litellm.APIConnectionError: Ollama_chatException - "
        '{"error":"system message must be at the beginning"}. '
        "Received Model Group=ollama_chat/qwen3.8:latest"
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "Could not connect" not in msg
    assert "single system message" in msg
    assert "Requires a single system message" in msg


def test_ollama_system_message_order_error_reports_unresolved_when_flag_already_set(monkeypatch):
    from opalatex.i18n import set_lang
    from opalatex import config as config_mod

    monkeypatch.setattr(config_mod, "model_requires_single_system_message", lambda model: True)

    project = SimpleNamespace(model="ollama_chat/qwen3.8:latest", api_base="")
    exc = Exception(
        "litellm.APIConnectionError: Ollama_chatException - "
        '{"error":"system message must be at the beginning"}. '
        "Received Model Group=ollama_chat/qwen3.8:latest"
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "Could not connect" not in msg
    assert "already enabled" in msg
    assert "please report" in msg


def test_badrequest_system_message_order_error_is_not_reported_as_parameter_rejection(monkeypatch):
    """An OpenAI-compatible server reports this as a BadRequestError.

    The generic invalid-parameter branch matches ``badrequest``/
    ``invalid_request_error`` too, so the ordering diagnostic must be checked
    first or the actionable message is lost.
    """
    from opalatex.i18n import set_lang
    from opalatex import config as config_mod

    monkeypatch.setattr(config_mod, "model_requires_single_system_message", lambda model: False)

    project = SimpleNamespace(model="openai/nvidia/Qwen3.6-35B-A3B-NVFP4", api_base="")
    exc = Exception(
        "litellm.BadRequestError: OpenAIException - could not encode request: "
        "System message must be at the beginning.. "
        "Received Model Group=openai/nvidia/Qwen3.6-35B-A3B-NVFP4"
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "rejected a parameter value" not in msg
    assert "Requires a single system message" in msg


def test_server_prompt_budget_error_is_not_reported_as_connection_failure():
    """LiteLLM wraps the server's prompt-too-long refusal in APIConnectionError.

    The server answered, so blaming the connection hides the only two things
    the user can act on: the host's KV cache budget and the catalog num_ctx.
    """
    from opalatex.i18n import set_lang

    project = SimpleNamespace(model="openai/Qwen3.6-35B-A3B-NVFP4", api_base="")
    exc = Exception(
        "litellm.MidStreamFallbackError: litellm.APIConnectionError: APIConnectionError: "
        "OpenAIException - prompt is too long: 13557 tokens > 8221 maximum "
        "(prompt + generation); shorten the prompt or increase the KV cache budget. "
        "Received Model Group=openai/Qwen3.6-35B-A3B-NVFP4"
    )

    set_lang("en")
    msg = _friendly_llm_error(exc, project)

    assert "Could not connect" not in msg
    assert "13557" in msg
    assert "8221" in msg
    assert "num_ctx" in msg


def test_prompt_budget_from_error_ignores_unrelated_failures():
    from opalatex.agent_stdin import _prompt_budget_from_error

    assert _prompt_budget_from_error("litellm.APIConnectionError: connection refused") is None
    assert _prompt_budget_from_error("prompt is too long: 13557 tokens > 8221 maximum") == (13557, 8221)


def test_inline_response_contract_discards_preamble_and_orphan_think_close():
    response = "This is the final output.</think>```tex\ncontent\nA replacement paragraph.\n```"
    assert _normalize_inline_fenced_replacement(response) == "A replacement paragraph."


def test_inline_response_contract_accepts_unfenced_replacement_text():
    assert _normalize_inline_fenced_replacement("Refined replacement text.") == "Refined replacement text."


def test_inline_scope_rejects_full_document_for_selected_replacement():
    source = ("before " * 48) + "selected paragraph" + (" after" * 48)
    replacement = source.replace("selected paragraph", "refined paragraph")

    with pytest.raises(RuntimeError, match="surrounding document context"):
        _validate_inline_replacement_scope(replacement, source, "selected paragraph")


def test_handle_create_project_reports_a_taken_folder_without_crashing(tmp_path, monkeypatch):
    """The duplicate-folder branch used to raise NameError instead of reporting.

    `handle_create_project` called the i18n `_` without importing it, so the one
    path that tells the user a folder is already taken died with NameError and
    the real message never reached them.
    """
    import asyncio

    import opalatex.agent_stdin as stdin_mod

    events = []
    monkeypatch.setattr(stdin_mod, "print_event", lambda ev, data: events.append((ev, data)))

    db = tmp_path / "store.sqlite3"
    project_dir = tmp_path / "shared_folder"
    project_dir.mkdir()

    payload = {
        "db": str(db),
        "project_name": "first",
        "project_path": str(project_dir),
    }
    asyncio.run(stdin_mod.handle_create_project(dict(payload)))

    events.clear()
    asyncio.run(stdin_mod.handle_create_project({**payload, "project_name": "second"}))

    errors = [d for ev, d in events if ev == "error"]
    assert errors, f"expected an error event, got {events}"
    assert "first" in errors[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_kwargs, expect_published",
    [
        ({"think": True}, True),
        ({}, False),
    ],
)
async def test_thinking_panel_follows_the_resolved_capability_not_a_project_setting(
    monkeypatch, tmp_path, wire_kwargs, expect_published
):
    """The reasoning is shown exactly when the provider was asked to isolate it.

    `think` reaches the agent's kwargs only for a model whose catalog entry
    declares `supports_thinking` (config.resolve_think_request). Publishing used
    to be gated by a separate per-project checkbox instead, so a project could
    have the provider reasoning on every turn while the Thinking panel stayed
    empty — two switches, one behaviour, and no way to tell which one was lying.
    """
    import opalatex.agent_stdin as stdin_mod

    events = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"
        # A project can no longer store `think`; a stale value must not revive it.
        model_params = {"think": False, "stream": True}

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            pass

        def append_activity(self, _project, event, content="", agent="", payload=None):
            pass

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        on_thinking = None

        def __init__(self):
            self.model_kargs = dict(wire_kwargs)
            self.model_kwargs = self.model_kargs

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            self.on_thinking("weighing the options")
            return SimpleNamespace(response="Done.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "go"})

    thoughts = [data["content"] for event, data in events if event == "thought"]
    assert thoughts == (["weighing the options"] if expect_published else [])
