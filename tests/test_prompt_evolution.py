import json
from unittest.mock import AsyncMock
import pytest

from gilzabots.ide_server import AsyncHTTPServer, clean_evolved_prompt


def test_clean_evolved_prompt():
    assert clean_evolved_prompt("  Explain self-attention in Transformers.  ") == (
        "Explain self-attention in Transformers."
    )

    with pytest.raises(ValueError, match="empty refined prompt"):
        clean_evolved_prompt("   ")

    source_prompt = "Ensine-me sobre auto-atenção no transformer."
    with pytest.raises(ValueError, match="original prompt unchanged"):
        clean_evolved_prompt(source_prompt, source_prompt=source_prompt)
    with pytest.raises(ValueError, match="internal task wrapper"):
        clean_evolved_prompt(
            f"Refine this user prompt: {source_prompt}",
            source_prompt=source_prompt,
        )
    with pytest.raises(ValueError, match="internal instructions"):
        clean_evolved_prompt(
            "Refine this user prompt while preserving its language and intent. "
            "Return only a JSON object that conforms to the provided response schema.",
            source_prompt=source_prompt,
        )


def test_clean_evolved_prompt_removes_whole_answer_fence_only():
    # Some models fence a plain-text answer out of habit (glm-5.3 on Ollama cloud
    # did it on every call). A fence around the whole answer is presentation.
    fenced = "```\nRevise a \\section{Introdução} mantendo \\cite{silva2020}.\n```"
    assert clean_evolved_prompt(fenced) == (
        "Revise a \\section{Introdução} mantendo \\cite{silva2020}."
    )
    # A fence inside the refined prompt is content and survives.
    inner = "Format the output like this:\n```latex\n\\begin{table}\n```\nKeep labels."
    assert clean_evolved_prompt(inner) == inner


def test_clean_evolved_prompt_separates_reasoning_markup():
    answer = "<think>The user wants a clearer prompt.</think>Rewrite the abstract in 150 words."
    assert clean_evolved_prompt(answer) == "Rewrite the abstract in 150 words."

    with pytest.raises(ValueError, match="reasoning only"):
        clean_evolved_prompt("<think>Thinking about the prompt...</think>")


@pytest.mark.asyncio
async def test_prompt_evolution_settings_endpoints(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    # GET default settings
    await server.route_api("GET", "/api/settings/prompt-evolution", {}, {}, b"", writer)
    assert responses[-1] == (200, {"prompt_evolution_iterations": 1, "prompt_evolution_max_tokens": 4096}, "application/json")

    # POST new iterations setting = 4
    await server.route_api(
        "POST",
        "/api/settings/prompt-evolution",
        {},
        {},
        json.dumps({"prompt_evolution_iterations": 4, "prompt_evolution_max_tokens": 6000}).encode("utf-8"),
        writer,
    )
    assert responses[-1] == (200, {"success": True, "prompt_evolution_iterations": 4, "prompt_evolution_max_tokens": 6000}, "application/json")

    # GET updated settings
    await server.route_api("GET", "/api/settings/prompt-evolution", {}, {}, b"", writer)
    assert responses[-1] == (200, {"prompt_evolution_iterations": 4, "prompt_evolution_max_tokens": 6000}, "application/json")

    # POST invalid/zero iteration setting clamps to 1
    await server.route_api(
        "POST",
        "/api/settings/prompt-evolution",
        {},
        {},
        json.dumps({"prompt_evolution_iterations": -2, "prompt_evolution_max_tokens": -2}).encode("utf-8"),
        writer,
    )
    assert responses[-1] == (200, {"success": True, "prompt_evolution_iterations": 1, "prompt_evolution_max_tokens": 1}, "application/json")


@pytest.mark.asyncio
async def test_evolve_prompt_endpoint(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    # Empty prompt returns 400
    await server.route_api(
        "POST",
        "/api/chat/evolve-prompt",
        {},
        {},
        json.dumps({"prompt": "   "}).encode("utf-8"),
        writer,
    )
    assert responses[-1][0] == 400

    # Mock _execute_prompt_evolution
    endpoint_call = {}

    async def mock_execute(prompt, iterations=1, model=None, max_tokens=4096):
        endpoint_call["model"] = model
        endpoint_call["max_tokens"] = max_tokens
        return f"Evolved {iterations}x: {prompt}"

    monkeypatch.setattr("gilzabots.ide_server._execute_prompt_evolution", mock_execute)

    await server.route_api(
        "POST",
        "/api/chat/evolve-prompt",
        {},
        {},
        json.dumps({"prompt": "Write a report on renewable energy", "iterations": 2, "model": "test-provider/selected-chat-model"}).encode("utf-8"),
        writer,
    )
    assert responses[-1] == (
        200,
        {"success": True, "prompt": "Evolved 2x: Write a report on renewable energy"},
        "application/json",
    )
    assert endpoint_call["model"] == "test-provider/selected-chat-model"
    assert endpoint_call["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_execute_prompt_evolution_invokes_agent(monkeypatch):
    from gilzabots.ide_server import _execute_prompt_evolution
    from unittest.mock import MagicMock

    mock_agent_instance = MagicMock()
    mock_agent_instance.run = AsyncMock(return_value=MagicMock(response="Detailed Evolved Prompt"))

    agent_kwargs = {}

    def mock_agent_class(*args, **kwargs):
        agent_kwargs.update(kwargs)
        return mock_agent_instance

    monkeypatch.setattr("agenticblocks.blocks.llm.agent.LLMAgentBlock", mock_agent_class)
    monkeypatch.setattr("gilzabots.litellm_compat.wrap_agent_litellm_compat", lambda a: a)

    selected_model = "test-provider/prompt-model"
    captured = {}

    def mock_get_llm_kwargs(agent_name, model_override=None, reasoning_effort_override=None):
        captured["agent_name"] = agent_name
        captured["model_override"] = model_override
        captured["reasoning_effort_override"] = reasoning_effort_override
        return {"think": True}

    monkeypatch.setattr("gilzabots.config.get_agent_llm_kwargs", mock_get_llm_kwargs)

    result = await _execute_prompt_evolution(
        "Short prompt",
        iterations=1,
        model=selected_model,
        max_tokens=4096,
    )
    assert result == "Detailed Evolved Prompt"
    # The project prompt prefix is chat instruction, not text to rewrite: fusing
    # it into the same user message made it part of the refined prompt.
    assert "user_prompt_prefix" not in agent_kwargs
    assert agent_kwargs["model"] == selected_model
    assert agent_kwargs["model_kwargs"]["think"] is True
    assert agent_kwargs["model_kwargs"]["max_tokens"] == 4096
    # Plain text, like the translator: Ollama cloud ignores `format`, and a
    # response_schema miss cost a second completion before failing.
    assert "response_schema" not in agent_kwargs
    assert "Do not answer the prompt" in agent_kwargs["system_prompt"]
    run_input = mock_agent_instance.run.call_args.args[0]
    assert run_input.prompt == "Short prompt"
    assert "Refine this user prompt" not in run_input.prompt
    # A one-shot rewrite does not inherit the chat's full reasoning effort.
    assert captured == {
        "agent_name": "orchestrator",
        "model_override": selected_model,
        "reasoning_effort_override": "low",
    }


@pytest.mark.asyncio
async def test_cancel_evolve_prompt_endpoint(tmp_path, monkeypatch):
    import asyncio
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server = AsyncHTTPServer()
    writer = AsyncMock()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response

    # 1. When no task is running, returns success: false
    await server.route_api("POST", "/api/chat/cancel-evolve-prompt", {}, {}, b"", writer)
    assert responses[-1] == (200, {"success": False, "message": "No active prompt evolution running"}, "application/json")

    # 2. When a task is running, cancel-evolve-prompt cancels it
    hanging_event = asyncio.Event()
    cancelled_observed = False

    async def mock_slow_execute(prompt, iterations=1, model=None, max_tokens=4096):
        nonlocal cancelled_observed
        try:
            await hanging_event.wait()
            return f"Evolved: {prompt}"
        except asyncio.CancelledError:
            cancelled_observed = True
            raise

    monkeypatch.setattr("gilzabots.ide_server._execute_prompt_evolution", mock_slow_execute)

    # Launch evolve-prompt in background task
    evolve_task = asyncio.create_task(
        server.route_api(
            "POST",
            "/api/chat/evolve-prompt",
            {},
            {},
            json.dumps({"prompt": "Long running prompt"}).encode("utf-8"),
            writer,
        )
    )

    # Yield to let evolve-prompt start and register task
    await asyncio.sleep(0.01)
    assert server.active_prompt_evolution_task is not None

    # Cancel via endpoint
    await server.route_api("POST", "/api/chat/cancel-evolve-prompt", {}, {}, b"", writer)
    assert responses[-1] == (200, {"success": True, "message": "Prompt evolution cancelled"}, "application/json")

    # Wait for evolve_task to finish cancelling
    with pytest.raises(asyncio.CancelledError):
        await evolve_task

    assert cancelled_observed is True
    assert server.active_prompt_evolution_task is None


def _catalog(monkeypatch, entry):
    import gilzabots.models_store as store
    monkeypatch.setattr(store, "get_model", lambda _id: dict(entry))
    monkeypatch.setattr(store, "get_model_by_runtime_id", lambda _id: dict(entry))
    monkeypatch.setattr("gilzabots.tools._PROJECT_SESSION", None, raising=False)


@pytest.mark.parametrize(
    "model, entry, expected",
    [
        # Ollama: the effort travels as the `think` level, never as its own field.
        (
            "ollama/glm-5.3-flash:cloud",
            {"supports_thinking": True, "api_base": "https://ollama.com"},
            {"think": "low"},
        ),
        # A catalog effort is replaced, not merged with.
        (
            "ollama/glm-5.3:cloud",
            {"supports_thinking": True, "reasoning_effort": "high", "api_base": "https://ollama.com"},
            {"think": "low"},
        ),
        # OpenAI-compatible providers take the parameter itself.
        (
            "openai/google/gemini-3.7-flash",
            {"supports_thinking": True, "api_base": "https://openrouter.ai/api/v1"},
            {"reasoning_effort": "low"},
        ),
    ],
)
def test_reasoning_effort_override_reaches_the_provider(monkeypatch, model, entry, expected):
    from gilzabots.config import get_agent_llm_kwargs

    _catalog(monkeypatch, entry)
    kwargs = get_agent_llm_kwargs(
        "orchestrator", model_override=model, reasoning_effort_override="low"
    )
    for key, value in expected.items():
        assert kwargs.get(key) == value
    if "think" in expected:
        assert "reasoning_effort" not in kwargs


def test_reasoning_effort_override_skips_models_that_do_not_reason(monkeypatch):
    from gilzabots.config import get_agent_llm_kwargs

    _catalog(monkeypatch, {"supports_thinking": False, "api_base": "https://openrouter.ai/api/v1"})
    kwargs = get_agent_llm_kwargs(
        "orchestrator",
        model_override="openai/some/plain-model",
        reasoning_effort_override="low",
    )
    assert "reasoning_effort" not in kwargs
    assert "think" not in kwargs


def test_without_override_the_catalog_effort_still_applies(monkeypatch):
    from gilzabots.config import get_agent_llm_kwargs

    _catalog(monkeypatch, {"supports_thinking": True, "api_base": "https://ollama.com"})
    kwargs = get_agent_llm_kwargs("orchestrator", model_override="ollama/glm-5.3:cloud")
    assert kwargs["think"] is True
