"""A tool called under a garbled name, and its arguments printed as the answer.

Observed with `ollama_chat/gpt-oss:20b` in a chat orchestrator: after two good
`ask_question` calls, the model issued a third under the name `expressions?`
with valid `ask_question` arguments. The loop answered only
`Tool 'expressions?' not found.`, and the model's next message was that
argument object as plain text:

    {"is_multi_select":false,"options":[...],"question":"Which method ..."}

That reached the chat, because the serialized-call detector only knew the
`name` + `arguments` envelope and markup. Covered here:
  1. the unknown-tool result names the available tools and the tool whose
     parameters the arguments fit (a hint -- nothing is redirected);
  2. both agent blocks report the failed call through `on_unknown_tool`;
  3. the orchestrator's final text is checked for a bare arguments object that
     fits an offered tool's schema, and such a response enters the existing
     bounded correction path instead of the chat.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from agenticblocks.core.function_block import as_tool
from agenticblocks.tools.a2a_bridge import block_to_tool_schema
from agenticblocks.utils.tool_calls import tools_accepting_arguments, unknown_tool_message


OBSERVED_ARGUMENTS = (
    '{"is_multi_select":false,"options":["Use Web Audio API and Web Speech API",'
    '"Use external speech recognition API (Google, Azure, Whisper)",'
    '"Use a custom backend for audio processing"],'
    '"question":"Which method would you like to use for speech recognition and audio processing?"}'
)


def _make_tools(executed):
    @as_tool(name="ask_question", description="Ask the user a question.")
    def ask_question(question: str, options: list[str] | None = None, is_multi_select: bool = False) -> str:
        executed.append(("ask_question", question))
        return "User response: ok"

    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        executed.append(("read_file", path))
        return "content"

    @as_tool(name="list_things", description="Only optional parameters.")
    def list_things(limit: int = 10) -> str:
        return "things"

    return [ask_question, read_file, list_things]


@pytest.fixture
def schemas():
    return [block_to_tool_schema(tool) for tool in _make_tools([])]


# ── 1. Matching an argument object against the offered tools ─────────────────

def test_observed_arguments_fit_ask_question_only(schemas):
    assert tools_accepting_arguments(OBSERVED_ARGUMENTS, schemas) == ["ask_question"]


def test_a_dict_is_accepted_as_well_as_a_json_string(schemas):
    assert tools_accepting_arguments({"path": "a.tex"}, schemas) == ["read_file"]


@pytest.mark.parametrize("arguments", [
    '{"question": "q", "colour": "red"}',       # an undeclared key
    '{"options": ["a", "b"]}',                  # the required key is missing
    "{}",                                       # nothing to identify
    '["question"]',                             # not an object
    "not json",
])
def test_objects_that_do_not_fit_exactly_match_nothing(schemas, arguments):
    assert tools_accepting_arguments(arguments, schemas) == []


def test_a_tool_without_required_parameters_never_fits(schemas):
    """Almost any object is a subset of optional keys, so such a match means nothing."""
    assert tools_accepting_arguments('{"limit": 3}', schemas) == []


def test_unknown_tool_message_names_the_real_tools_and_the_fitting_one(schemas):
    message = unknown_tool_message("expressions?", OBSERVED_ARGUMENTS, schemas)

    assert message.startswith("Tool 'expressions?' not found.")
    assert "Available tools: ask_question, read_file, list_things." in message
    assert "fit the parameters of 'ask_question'" in message
    assert "Nothing was executed" in message


def test_unknown_tool_message_suggests_a_close_spelling(schemas):
    message = unknown_tool_message("ask_questoin", "{}", schemas)

    assert "Did you mean 'ask_question'?" in message
    assert "fit the parameters" not in message


# ── 2. Both agent blocks: corrective result, no execution, callback ──────────

def _tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _script(agent, responses):
    async def fake_acompletion(_messages, **_kwargs):
        return responses.pop(0) if responses else _response(content="done")

    agent._acompletion = fake_acompletion


@pytest.mark.parametrize("asynchronous", [False, True])
def test_memgpt_block_answers_an_unknown_tool_with_the_catalog(asynchronous):
    executed, reported = [], []
    if asynchronous:
        async def on_unknown_tool(name, arguments):
            reported.append((name, arguments))
    else:
        def on_unknown_tool(name, arguments):
            reported.append((name, arguments))

    agent = MemGPTAgentBlock(
        name="chat_orchestrator",
        model="fake/model",
        max_heartbeats=3,
        tools=_make_tools(executed),
        on_unknown_tool=on_unknown_tool,
    )
    _script(agent, [
        _response(tool_calls=[_tool_call("c1", "expressions?", OBSERVED_ARGUMENTS)]),
        _response(content="final answer"),
    ])

    asyncio.run(agent.run(AgentInput(prompt="go")))

    tool_results = [m for m in agent.internal_history if m.get("role") == "tool"]
    error = json.loads(tool_results[0]["content"])["error"]
    assert "Available tools: ask_question, read_file, list_things, new_heartbeat." in error
    assert "fit the parameters of 'ask_question'" in error
    assert executed == [], "the call is never redirected to the tool it resembles"
    assert reported == [("expressions?", OBSERVED_ARGUMENTS)]


def test_llm_agent_block_answers_an_unknown_tool_with_the_catalog():
    executed, reported = [], []
    agent = LLMAgentBlock(
        name="worker",
        system_prompt="s",
        tools=_make_tools(executed),
        max_iterations=3,
        on_unknown_tool=lambda name, arguments: reported.append(name),
    )
    captured = []
    responses = [
        _response(tool_calls=[_tool_call("c1", "reed_file", '{"path": "a.tex"}')]),
        _response(content="done"),
    ]

    async def fake_acompletion(messages, **_kwargs):
        captured.append(list(messages))
        return responses.pop(0) if responses else _response(content="done")

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(AgentInput(prompt="go")))

    tool_results = [m for m in captured[-1] if m.get("role") == "tool"]
    error = json.loads(tool_results[0]["content"])["error"]
    assert "Did you mean 'read_file'?" in error
    assert "fit the parameters of 'read_file'" in error
    assert "Do NOT apologize" in error, "the worker-specific instruction is kept"
    assert executed == []
    assert reported == ["reed_file"]


# ── 3. The orchestrator's final text ─────────────────────────────────────────

def test_detector_catches_the_observed_bare_arguments(schemas):
    from gilzabots.agent_stdin import _has_unfenced_tool_call_payload

    assert _has_unfenced_tool_call_payload(OBSERVED_ARGUMENTS, schemas)
    assert _has_unfenced_tool_call_payload("Let me ask you:\n" + OBSERVED_ARGUMENTS, schemas)


def test_detector_needs_the_schemas_to_see_bare_arguments():
    """Without the offered tools there is nothing to fit, so the old behaviour holds."""
    from gilzabots.agent_stdin import _has_unfenced_tool_call_payload

    assert not _has_unfenced_tool_call_payload(OBSERVED_ARGUMENTS)


@pytest.mark.parametrize("response", [
    "```json\n" + OBSERVED_ARGUMENTS + "\n```",       # shown on purpose, fenced
    'The config is {"retries": 3, "debug": false}.',    # data fitting no tool
    'Use {"question": "q", "extra": 1} as the template.',
    "I read the file and fixed the citation.",
])
def test_detector_leaves_legitimate_answers_alone(schemas, response):
    from gilzabots.agent_stdin import _has_unfenced_tool_call_payload

    assert not _has_unfenced_tool_call_payload(response, schemas)


class _FakeOrchestrator:
    internal_history = []

    def __init__(self, scripted):
        self.tools = _make_tools([])
        self._scripted = list(scripted)
        self.prompts = []

    async def run(self, agent_input):
        self.prompts.append(agent_input)
        reply = self._scripted.pop(0) if self._scripted else ""
        return SimpleNamespace(response=reply)


def _correct(first, rest, monkeypatch):
    import gilzabots.agent_stdin as stdin_mod

    monkeypatch.setattr(stdin_mod, "print_event", lambda *a, **k: None)
    monkeypatch.setattr(stdin_mod, "apply_meta_params", lambda *a, **k: contextlib.nullcontext())
    agent = _FakeOrchestrator(rest)
    corrected, _ = asyncio.run(stdin_mod._correct_serialized_tool_calls(agent, first, [], {}))
    return corrected, agent


def test_bare_arguments_as_the_answer_are_pushed_back(monkeypatch):
    corrected, agent = _correct(OBSERVED_ARGUMENTS, ["Here is the plan."], monkeypatch)

    assert corrected == "Here is the plan."
    assert len(agent.prompts) == 1
    assert agent.prompts[0].role == "system"
    assert "Which method" not in agent.prompts[0].prompt, "the payload is never replayed"


def test_bare_arguments_that_never_go_away_fail_loudly(monkeypatch):
    with pytest.raises(RuntimeError):
        _correct(OBSERVED_ARGUMENTS, [OBSERVED_ARGUMENTS] * 5, monkeypatch)


def test_unknown_tool_is_reported_in_problems_only(monkeypatch):
    import gilzabots.agent_stdin as stdin_mod

    events = []
    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))

    stdin_mod._report_unknown_tool_call("expressions?")

    assert [event for event, _ in events] == ["problem"], "never in the Thinking stream"
    assert "expressions?" in events[0][1]["message"]
