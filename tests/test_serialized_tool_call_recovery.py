"""Tests for the three defences against a tool call written as text.

Observed with `ollama/glm-5.2:cloud`: the orchestrator printed
`{"name": "web_search", "arguments": {...}}` as its answer. Text is never a tool
call, so nothing ran, yet the turn ended looking answered because the
orchestrator's own output -- unlike a worker report -- was never inspected.

Three defences, tested here:
  1. the orchestrator prompt carries the native-tool-call reminder that until now
     only skill workers received;
  2. a serialized call in the final response is pushed back with a diagnostic and
     a bounded breaker, never parsed into a real call;
  3. the root cause itself: Ollama models now always reach the native chat route
     (see `test_agent_config.py`), so turning thinking off no longer costs the
     agent its ability to issue tool calls.

Later observed with `openai/nvidia/Qwen3.6-35B-A3B-NVFP4` served locally: after
its native `ask_question` calls were rejected for a bad argument type, the model
fell back to its chat template's markup and printed
`<tool_call><function=ask_question>...` to the user. Defences 1 and 2 both missed
it -- the detector scanned for JSON only, and the reminder was gated on `ollama`,
so a self-hosted model reached through the plain `openai/` route never got it.
Both are widened here.
"""

import pytest

from gilzabots.agent_stdin import _has_unfenced_tool_call_payload
from gilzabots.memgpt_runtime import (
    _NATIVE_TOOL_CALL_REMINDER,
    _needs_native_tool_call_reminder,
    build_chat_orchestrator,
)
from gilzabots.project import ProjectData


# ── 1. The prompt reminder reaches the orchestrator ──────────────────────────

@pytest.mark.parametrize("model", [
    "ollama/glm-5.2:cloud",
    "ollama_chat/gemma4:26b",
    "some-local-model",
])
def test_ollama_served_models_need_the_reminder(model):
    assert _needs_native_tool_call_reminder(model)


@pytest.mark.parametrize("model", [
    "gemini/gemini-3.7-flash",
    "openrouter/qwen/qwen3.7-plus",
    "",
])
def test_other_providers_do_not_get_the_reminder(model):
    assert not _needs_native_tool_call_reminder(model)


def test_orchestrator_prompt_carries_the_reminder_for_ollama(tmp_path):
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/glm-5.2:cloud", mode="auto",
    )
    assert _NATIVE_TOOL_CALL_REMINDER.strip() in build_chat_orchestrator(project, None).system_prompt


@pytest.mark.parametrize("model", [
    "openai/nvidia/Qwen3.6-35B-A3B-NVFP4",
    "hosted_vllm/meta-llama/Llama-3.3-70B-Instruct",
    "lm_studio/qwen3-coder",
])
def test_self_hosted_models_outside_ollama_need_the_reminder(model):
    """The reported failure: a local Qwen on the plain `openai/` route.

    Gating on `ollama` covered only where the failure was first seen, not the
    class it belongs to -- any model the user serves themselves.
    """
    assert _needs_native_tool_call_reminder(model)


@pytest.mark.parametrize("model", [
    "openai/gpt-4o",
    "openai/o3",
    "anthropic/claude-opus-5",
])
def test_first_party_openai_models_do_not_get_the_reminder(model):
    """A genuine OpenAI model id has no second path segment, which is the tell."""
    assert not _needs_native_tool_call_reminder(model)


def test_orchestrator_prompt_carries_the_reminder_for_self_hosted_openai_route(tmp_path):
    project = ProjectData(
        name="t", project_name="t", project_path=str(tmp_path),
        model="openai/nvidia/Qwen3.6-35B-A3B-NVFP4", mode="auto",
    )
    assert _NATIVE_TOOL_CALL_REMINDER.strip() in build_chat_orchestrator(project, None).system_prompt


def test_orchestrator_prompt_omits_the_reminder_for_cloud_providers(tmp_path):
    project = ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="gemini/gemini-3.7-flash", mode="auto",
    )
    assert _NATIVE_TOOL_CALL_REMINDER.strip() not in build_chat_orchestrator(project, None).system_prompt


# ── 2. Detecting a serialized call without eating legitimate JSON ────────────

def test_bare_tool_call_json_is_detected():
    assert _has_unfenced_tool_call_payload(
        '{"name": "web_search", "arguments": {"query": "Andrew Ng agentic AI"}}'
    )


def test_intent_sentence_followed_by_tool_call_json_is_detected():
    """The exact shape observed: a sentence, then raw calls, and nothing ran."""
    assert _has_unfenced_tool_call_payload(
        "Vou pesquisar o conceito e ler o arquivo atual.\n"
        '{"name": "web_search", "arguments": {"query": "Andrew Ng"}}\n'
        '{"name": "read_file", "arguments": {"path": "AULA_00/resumo.md"}}'
    )


def test_tool_calls_wrapper_is_detected():
    assert _has_unfenced_tool_call_payload('{"tool_calls": [{"name": "read_file"}]}')


def test_fenced_json_is_left_alone():
    """Answering "show me a tool-call example" must still reach the user.

    The payload detector matches any object carrying `name` plus `arguments`, a
    shape that occurs in real data, so fencing is what separates showing JSON
    from failing to issue a call.
    """
    assert not _has_unfenced_tool_call_payload(
        "Here is what a tool call looks like:\n\n"
        '```json\n{"name": "web_search", "arguments": {"query": "x"}}\n```\n\n'
        "Note that GilzaBots issues it natively instead."
    )


def test_markup_tool_call_is_detected():
    """The reported shape: Qwen chat-template markup, carrying no JSON object.

    `_iter_json_spans` finds nothing here, so before this the whole block was
    handed to the user as if it were the answer.
    """
    assert _has_unfenced_tool_call_payload(
        "The error suggests the options parameter is being serialized as a string.\n"
        "<tool_call>\n"
        "<function=ask_question>\n"
        "<parameter=question>\nO que voce quer com as imagens?\n</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )


def test_hermes_markup_wrapping_json_is_detected_once():
    assert _has_unfenced_tool_call_payload(
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.tex"}}</tool_call>'
    )


def test_truncated_markup_tool_call_is_detected():
    """A block the model never closed is still a call it failed to issue."""
    assert _has_unfenced_tool_call_payload("Vou ler o arquivo.\n<tool_call>\n<function=read_file>")


def test_bare_function_element_is_detected():
    assert _has_unfenced_tool_call_payload("<function=web_search>\n<parameter=query>x</parameter>\n</function>")


def test_fenced_markup_is_left_alone():
    """Explaining the markup to the user must still reach them."""
    assert not _has_unfenced_tool_call_payload(
        "Your model emitted this instead of a real call:\n\n"
        "```xml\n<tool_call><function=ask_question></function></tool_call>\n```\n\n"
        "That is why nothing ran."
    )


def test_ordinary_markup_and_math_are_left_alone():
    """LaTeX and HTML use angle brackets too; only the call shapes may match."""
    assert not _has_unfenced_tool_call_payload("Use <br> to break the line when $a < b$.")
    assert not _has_unfenced_tool_call_payload("The \\texttt{<function>} macro is undefined here.")


def test_ordinary_prose_and_data_json_are_left_alone():
    assert not _has_unfenced_tool_call_payload("I read the file and fixed the citation.")
    assert not _has_unfenced_tool_call_payload('The config is {"retries": 3, "debug": false}.')


def test_announced_intent_without_json_is_not_flagged():
    """The other half of the report is a different failure, not this one.

    "Vou pesquisar e ler o arquivo" with no payload is the empty/no-action path;
    flagging it here would mislabel it and skip the right recovery.
    """
    assert not _has_unfenced_tool_call_payload(
        "Vou pesquisar o conceito de IA Agêntica e ler o arquivo atual para reformulá-lo."
    )


def test_breaker_is_bounded():
    from gilzabots.agent_stdin import SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS

    assert 1 <= SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS <= 3


def test_corrective_prompt_and_failure_message_are_localized():
    from gilzabots.agent_stdin import (
        _serialized_tool_call_failure_message,
        _serialized_tool_call_retry_prompt,
    )
    from gilzabots.i18n import set_lang

    for lang in ("en", "pt"):
        set_lang(lang)
        nudge = _serialized_tool_call_retry_prompt()
        failure = _serialized_tool_call_failure_message()
        assert nudge and not nudge.startswith("serialized_tool_call")
        assert failure and not failure.startswith("serialized_tool_call")
    set_lang("en")


def test_rejected_payload_goes_to_diagnostics_not_the_chat(monkeypatch):
    """Discarding the text with no trace makes a false positive undebuggable."""
    import gilzabots.agent_stdin as stdin_mod

    events = []
    monkeypatch.setattr(stdin_mod, "print_event", lambda ev, data: events.append((ev, data)))
    payload = '{"name": "web_search", "arguments": {"query": "Andrew Ng"}}'

    stdin_mod._report_rejected_serialized_response(payload)

    assert len(events) == 1
    event, data = events[0]
    assert event == "problem"
    assert data["severity"] == "error"
    assert "web_search" in data["message"]


# ── 2b. The correction loop itself ───────────────────────────────────────────

class _FakeAgent:
    """Minimal stand-in for the orchestrator: replays scripted responses."""

    internal_history = []

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.prompts = []

    async def run(self, agent_input):
        from types import SimpleNamespace

        self.prompts.append(agent_input)
        reply = self._scripted.pop(0) if self._scripted else ""
        return SimpleNamespace(response=reply)


def _correct(scripted_first, scripted_rest, monkeypatch):
    import asyncio
    import contextlib

    import gilzabots.agent_stdin as stdin_mod

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "apply_meta_params", lambda *a, **k: contextlib.nullcontext())

    agent = _FakeAgent(scripted_rest)
    corrected, _resp_obj = asyncio.run(
        stdin_mod._correct_serialized_tool_calls(agent, scripted_first, [], {})
    )
    return corrected, agent


def test_a_serialized_call_is_pushed_back_and_recovered(monkeypatch):
    corrected, agent = _correct(
        '{"name": "web_search", "arguments": {"query": "Andrew Ng"}}',
        ["I searched and here is the summary."],
        monkeypatch,
    )

    assert corrected == "I searched and here is the summary."
    assert len(agent.prompts) == 1, "one corrective round-trip"
    assert agent.prompts[0].role == "system", "runtime feedback is not the user's voice"


def test_the_corrected_run_output_travels_with_its_response(monkeypatch):
    """The caller labels the turn from this output; the pushed-back run is stale."""
    import asyncio
    import contextlib
    from types import SimpleNamespace

    import gilzabots.agent_stdin as stdin_mod

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "apply_meta_params", lambda *a, **k: contextlib.nullcontext())

    stale = SimpleNamespace(response="stale")
    agent = _FakeAgent(["The real answer."])
    corrected, resp_obj = asyncio.run(stdin_mod._correct_serialized_tool_calls(
        agent, '{"name": "web_search", "arguments": {"query": "x"}}', [], {}, stale,
    ))

    assert corrected == "The real answer."
    assert resp_obj is not stale and resp_obj.response == "The real answer."

    untouched, same = asyncio.run(
        stdin_mod._correct_serialized_tool_calls(agent, "Fine as it is.", [], {}, stale)
    )
    assert (untouched, same) == ("Fine as it is.", stale)


def test_a_clean_response_is_returned_untouched(monkeypatch):
    corrected, agent = _correct("I fixed the citation on line 42.", [], monkeypatch)

    assert corrected == "I fixed the citation on line 42."
    assert agent.prompts == [], "no round-trip for a healthy answer"


def test_the_payload_is_never_turned_into_a_real_call(monkeypatch):
    """The fix must not execute what the model only described.

    Parsing the JSON into an actual call would invent an action the model never
    issued -- the silent substitution CLAUDE.md rule 1.1 forbids.
    """
    corrected, agent = _correct(
        '{"name": "run_command", "arguments": {"command": "rm -rf build"}}',
        ["Done."],
        monkeypatch,
    )

    assert corrected == "Done."
    # The only thing sent back is the corrective nudge, never the payload.
    assert all("rm -rf build" not in str(p.prompt) for p in agent.prompts)


def test_an_unrecoverable_model_fails_loudly_instead_of_showing_json(monkeypatch):
    import pytest as _pytest

    with _pytest.raises(RuntimeError) as excinfo:
        _correct(
            '{"name": "web_search", "arguments": {"query": "x"}}',
            ['{"name": "web_search", "arguments": {"query": "x"}}'] * 5,
            monkeypatch,
        )

    assert "tool calls as text" in str(excinfo.value)


def test_the_breaker_stops_after_the_configured_attempts(monkeypatch):
    from gilzabots.agent_stdin import SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS
    import pytest as _pytest

    payload = '{"name": "read_file", "arguments": {"path": "a.tex"}}'

    import asyncio
    import contextlib

    import gilzabots.agent_stdin as stdin_mod

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "apply_meta_params", lambda *a, **k: contextlib.nullcontext())
    agent = _FakeAgent([payload] * 10)

    with _pytest.raises(RuntimeError):
        asyncio.run(stdin_mod._correct_serialized_tool_calls(agent, payload, [], {}))

    assert len(agent.prompts) == SERIALIZED_TOOL_CALL_MAX_CORRECTION_ATTEMPTS
