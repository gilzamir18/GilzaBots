"""Tests for the PDF viewer's snippet translation feature.

Covers the target-language resolution rules, the guard rails in
``execute_translation``, and the ``/api/settings/translation`` +
``/api/translate`` endpoints.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.translation import (
    MAX_TRANSLATION_CHARS,
    build_translation_system_prompt,
    execute_translation,
    resolve_target_language,
    strip_model_reasoning,
    strip_plain_text_wrapper,
)


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return server, responses


def test_resolve_target_language_prefers_the_first_non_empty_candidate():
    assert resolve_target_language("pt-BR", "en") == "Brazilian Portuguese"
    assert resolve_target_language("", None, "ja") == "Japanese"


def test_resolve_target_language_falls_back_to_the_base_locale():
    """'pt-PT' has no entry of its own but must still resolve to Portuguese."""
    assert resolve_target_language("pt-PT") == "Portuguese"
    assert resolve_target_language("en-GB") == "English"


def test_resolve_target_language_passes_free_text_through():
    """The settings field offers an "Other" option, so unknown names must survive."""
    assert resolve_target_language("Norwegian") == "Norwegian"
    assert resolve_target_language("  Klingon  ") == "Klingon"


def test_resolve_target_language_defaults_to_english_when_nothing_is_configured():
    assert resolve_target_language() == "English"
    assert resolve_target_language("", None, "") == "English"


def test_system_prompt_names_the_target_language_and_forbids_answering_the_snippet():
    prompt = build_translation_system_prompt("Brazilian Portuguese")
    assert "Brazilian Portuguese" in prompt
    assert "do not complete it" in prompt
    assert "answer any question" in prompt


def test_system_prompt_asks_for_plain_text_only():
    """A JSON envelope is one more way to fail; the translation is the payload."""
    prompt = build_translation_system_prompt("English")
    assert "plain text" in prompt
    assert "no JSON" in prompt
    assert "no code fences" in prompt


def test_strip_plain_text_wrapper_removes_a_fence_around_the_whole_answer():
    assert strip_plain_text_wrapper("```\ntranslated\n```") == "translated"
    assert strip_plain_text_wrapper("```text\ntranslated\n```") == "translated"
    assert strip_plain_text_wrapper("  translated  ") == "translated"


def test_strip_plain_text_wrapper_keeps_a_fence_that_is_part_of_the_text():
    """Only a fence enclosing the entire answer is formatting, not content."""
    assert strip_plain_text_wrapper("before ```x``` after") == "before ```x``` after"
    assert strip_plain_text_wrapper("```\na\n```\nb\n```") == "```\na\n```\nb\n```"


@pytest.mark.asyncio
async def test_execute_translation_rejects_an_empty_snippet():
    with pytest.raises(ValueError, match="non-empty text snippet"):
        await execute_translation("   ", "English")


@pytest.mark.asyncio
async def test_execute_translation_rejects_an_oversized_snippet():
    """Truncating silently would present a partial translation as a complete one."""
    with pytest.raises(ValueError, match="too long to translate"):
        await execute_translation("x" * (MAX_TRANSLATION_CHARS + 1), "English")


def _stub_agent(monkeypatch, response):
    """Replace the ephemeral agent with one that answers *response*."""
    import agenticblocks.blocks.llm.agent as agent_mod

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, agent_input):
            captured["prompt"] = agent_input.prompt
            return SimpleNamespace(response=response, structured_output=None)

    monkeypatch.setattr(agent_mod, "LLMAgentBlock", FakeAgent)
    monkeypatch.setattr("gilzabots.litellm_compat.wrap_agent_litellm_compat", lambda agent: agent)
    return captured


@pytest.mark.asyncio
async def test_execute_translation_asks_for_no_response_schema(monkeypatch):
    """A prose answer is a good translation; requiring JSON only adds a failure mode."""
    captured = _stub_agent(monkeypatch, "Uma frase.")

    assert await execute_translation("A sentence.", "Brazilian Portuguese") == "Uma frase."
    assert "response_schema" not in captured
    assert captured["prompt"] == "A sentence."


@pytest.mark.asyncio
async def test_execute_translation_ignores_the_project_prompt_prefix(monkeypatch):
    """Regression: the chat agent's standing instructions came back translated.

    The prefix shares its user message with the excerpt, so a translator that
    received it translated it too. It is chat instruction, not translation
    input, and this agent never sees it.
    """
    captured = _stub_agent(monkeypatch, "O gato sentou.")

    with pytest.raises(TypeError):
        await execute_translation(
            "The cat sat.", "Brazilian Portuguese", user_prompt_prefix="# TESTING"
        )
    assert await execute_translation("The cat sat.", "Brazilian Portuguese") == "O gato sentou."
    assert "user_prompt_prefix" not in captured
    assert captured["prompt"] == "The cat sat."


@pytest.mark.asyncio
async def test_execute_translation_unwraps_a_fenced_answer(monkeypatch):
    _stub_agent(monkeypatch, "```text\nUma frase.\n```")

    assert await execute_translation("A sentence.", "Brazilian Portuguese") == "Uma frase."


def test_system_prompt_forbids_thinking_out_loud():
    """A reasoning model's monologue reaches the popup as if it were the translation."""
    prompt = build_translation_system_prompt("English")
    assert "Do not think out loud" in prompt


def test_strip_model_reasoning_drops_a_think_block():
    assert strip_model_reasoning("<think>The user wants...</think>Takeaway") == "Takeaway"


def test_strip_model_reasoning_drops_an_orphan_closing_tag():
    """Chat templates seed the opening <think>, so only the closing tag is generated."""
    assert strip_model_reasoning(
        "The user wants me to translate.\nIt is already English.\n</think>Takeaway"
    ) == "Takeaway"


def test_strip_model_reasoning_drops_channel_markup():
    assert strip_model_reasoning(
        "<|channel|>analysis<|message|>Already English, return unchanged.<|end|>"
        "<|start|>assistant<|channel|>final<|message|>Takeaway"
    ) == "Takeaway"


def test_strip_model_reasoning_leaves_an_ordinary_translation_alone():
    assert strip_model_reasoning("Uma frase com <think> no meio.") == "Uma frase com <think> no meio."


@pytest.mark.asyncio
async def test_execute_translation_returns_only_the_answer_channel(monkeypatch):
    """The reported bug: the whole chain of thought was shown as the translation."""
    _stub_agent(
        monkeypatch,
        "The user wants me to detect the language and translate it.\n"
        "\"Takeaway\" is already English, so I return it unchanged.\n"
        "</think>Takeaway",
    )

    assert await execute_translation("Takeaway", "English") == "Takeaway"


@pytest.mark.asyncio
async def test_execute_translation_reports_an_answer_that_is_only_reasoning(monkeypatch):
    """Silently showing the reasoning is what this replaces; a fenced-off answer is required."""
    _stub_agent(monkeypatch, "<think>Let me think about this snippet.</think>")

    with pytest.raises(ValueError, match="reasoning only"):
        await execute_translation("Takeaway", "English")


@pytest.mark.asyncio
async def test_execute_translation_reports_an_empty_answer(monkeypatch):
    _stub_agent(monkeypatch, "   ")

    with pytest.raises(ValueError, match="empty translation"):
        await execute_translation("A sentence.", "Brazilian Portuguese")


@pytest.mark.asyncio
async def test_translation_settings_endpoints_round_trip(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server, responses = _server_with_capture()
    writer = AsyncMock()

    await server.route_api("GET", "/api/settings/translation", {}, {}, b"", writer)
    status, body, _ = responses[-1]
    assert status == 200
    assert body["translate_target_lang"] == ""
    assert "pt-BR" in body["known_languages"]

    await server.route_api(
        "POST",
        "/api/settings/translation",
        {},
        {},
        json.dumps({"translate_target_lang": "  fr  "}).encode("utf-8"),
        writer,
    )
    assert responses[-1][:2] == (200, {"success": True, "translate_target_lang": "fr"})

    await server.route_api("GET", "/api/settings/translation", {}, {}, b"", writer)
    assert responses[-1][1]["translate_target_lang"] == "fr"


@pytest.mark.asyncio
async def test_translate_endpoint_returns_the_translation_and_target_language(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    captured = {}

    async def fake_execute(text, target_language, model=None):
        captured.update(text=text, target_language=target_language, model=model)
        return "Uma amostra de texto."

    monkeypatch.setattr("gilzabots.translation.execute_translation", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/translate",
        {},
        {},
        json.dumps({
            "text": "A sample of text.",
            "target_lang": "pt-BR",
            "model": "ollama/llama3",
        }).encode("utf-8"),
        AsyncMock(),
    )

    status, body, _ = responses[-1]
    assert status == 200
    assert body == {
        "success": True,
        "target_language": "Brazilian Portuguese",
        "translated_text": "Uma amostra de texto.",
    }
    assert captured == {
        "text": "A sample of text.",
        "target_language": "Brazilian Portuguese",
        "model": "ollama/llama3",
    }


@pytest.mark.asyncio
async def test_translate_endpoint_falls_back_to_the_saved_setting(tmp_path, monkeypatch):
    """With no target_lang in the request, the editor's "Translate to" wins."""
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)
    from gilzabots.ui_settings import save_ui_settings

    save_ui_settings({"translate_target_lang": "de", "lang": "pt-BR"})

    captured = {}

    async def fake_execute(text, target_language, model=None):
        captured["target_language"] = target_language
        return "Ein Text."

    monkeypatch.setattr("gilzabots.translation.execute_translation", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/translate",
        {},
        {},
        json.dumps({"text": "Some text."}).encode("utf-8"),
        AsyncMock(),
    )

    assert captured["target_language"] == "German"
    assert responses[-1][1]["target_language"] == "German"


@pytest.mark.asyncio
async def test_translate_endpoint_falls_back_to_the_ui_language(tmp_path, monkeypatch):
    """"Translate to" left on "Same as interface language" follows the UI locale."""
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)
    from gilzabots.ui_settings import save_ui_settings

    save_ui_settings({"translate_target_lang": "", "lang": "pt-BR"})

    captured = {}

    async def fake_execute(text, target_language, model=None):
        captured["target_language"] = target_language
        return "Um texto."

    monkeypatch.setattr("gilzabots.translation.execute_translation", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/translate",
        {},
        {},
        json.dumps({"text": "Some text."}).encode("utf-8"),
        AsyncMock(),
    )

    assert captured["target_language"] == "Brazilian Portuguese"


@pytest.mark.asyncio
async def test_translate_endpoint_rejects_an_empty_snippet(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/translate",
        {},
        {},
        json.dumps({"text": "   "}).encode("utf-8"),
        AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert responses[-1][1]["error"] == "text is required"


@pytest.mark.asyncio
async def test_translate_endpoint_reports_a_model_failure_instead_of_guessing(tmp_path, monkeypatch):
    settings_file = tmp_path / "ui_settings.json"
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", settings_file)

    async def fake_execute(text, target_language, model=None):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr("gilzabots.translation.execute_translation", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST",
        "/api/translate",
        {},
        {},
        json.dumps({"text": "Some text."}).encode("utf-8"),
        AsyncMock(),
    )

    assert responses[-1][0] == 500
    assert "provider unreachable" in responses[-1][1]["error"]
