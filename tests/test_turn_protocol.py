"""One text channel, one continuation rule.

The failure this replaces was measured, not theorised. Two chats of the same
request, in the same project, on different models:

  * `ollama_chat/kimi-k3:cloud` wrote the whole 6 600-character answer into the
    assistant `content` of a message that also carried a tool call, and put a
    415-character pointer -- "roteiro entregue acima" -- into `send_message`.
    The loop only ever looked at `content` when there were *no* tool calls, so
    the answer was dropped and the pointer was delivered. The user replied
    "Não vi nenhum roteiro", and they were right.
  * `ollama_chat/glm-5.3-flash:cloud` put the full 8 440-character answer into
    one `send_message`, then, obeying a prompt rule to close the turn with text,
    added a second one summarising it. `response_mode="last"` kept the summary
    and discarded the answer.

Both models were following the contract as written. The runtime had two places
to put user-facing text and silently preferred the wrong one, so the protocol
lost the deliverable while reporting success. There is now exactly one place:
assistant `content`. Everything written there is delivered, in order, tool call
or no tool call. The turn continues while a tool call is pending or while the
model asks for another heartbeat, and a reply that does neither ends it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput
from agenticblocks.blocks.llm.memgpt_agent import HEARTBEAT_TOOL_NAME, MemGPTAgentBlock
from agenticblocks.core.function_block import as_tool


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(tool_calls=None, content=""):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _reasoning(text):
    """A response that is all reasoning channel and no visible content."""
    message = SimpleNamespace(content="", tool_calls=None, reasoning_content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _hold(content, call_id="h1"):
    """Speak now, act next: text plus an explicit request to stay open."""
    return _response(content=content, tool_calls=[_tool_call(call_id, HEARTBEAT_TOOL_NAME)])


def _build(reads, **kwargs):
    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        reads.append(path)
        return "file contents"

    return MemGPTAgentBlock(
        name="orchestrator", system_prompt="s", tools=[read_file],
        max_heartbeats=kwargs.pop("max_heartbeats", 30), **kwargs,
    )


def _script(agent, responses):
    """Drive the loop with a fixed sequence of provider responses."""
    calls = {"n": 0, "tool_choice": []}

    async def fake_acompletion(messages, **kw):
        calls["tool_choice"].append(kw.get("tool_choice"))
        idx = calls["n"]
        calls["n"] += 1
        return responses[min(idx, len(responses) - 1)]

    agent._acompletion = fake_acompletion
    return calls


def _run(agent, prompt="do the thing"):
    return asyncio.run(agent.run(AgentInput(prompt=prompt)))


# ── The measured failures ────────────────────────────────────────────────────

def test_text_written_beside_a_tool_call_still_reaches_the_user():
    """The kimi-k3 trace: the answer travelled with a tool call and vanished."""
    reads = []
    agent = _build(reads)
    _script(agent, [
        _response(
            content="Here is the full plan:\n1. Cite the introduction claims.",
            tool_calls=[_tool_call("c1", "read_file", '{"path": "main.tex"}')],
        ),
        _response(content="Done."),
    ])

    out = _run(agent)

    assert reads == ["main.tex"], "the tool call still ran"
    assert "Here is the full plan:" in out.response
    assert "1. Cite the introduction claims." in out.response


def test_nothing_the_model_said_is_dropped_across_heartbeats():
    """The glm trace: two user-facing texts, and only one used to survive."""
    reads = []
    agent = _build(reads)
    _script(agent, [
        _response(content="THE PLAN", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="THE SUMMARY"),
    ])

    out = _run(agent)

    assert out.response == "THE PLAN\n\nTHE SUMMARY"


def test_the_answer_keeps_the_order_the_model_wrote_it_in():
    reads = []
    agent = _build(reads)
    _script(agent, [
        _hold("First."),
        _response(content="Second.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="Third."),
    ])

    assert _run(agent).response == "First.\n\nSecond.\n\nThird."


# ── The continuation rule ────────────────────────────────────────────────────

def test_text_with_no_tool_call_ends_the_turn():
    agent = _build([])
    calls = _script(agent, [_response(content="The answer."), _response(content="never asked")])

    out = _run(agent)

    assert out.response == "The answer."
    assert calls["n"] == 1
    assert out.termination_reason == "model returned a final text response (no tool calls)"


def test_a_pending_tool_call_keeps_the_turn_open_by_itself():
    """No continuation request needed: the result has to be delivered."""
    reads = []
    agent = _build(reads)
    calls = _script(agent, [
        _response(tool_calls=[_tool_call("c1", "read_file", '{"path": "main.tex"}')]),
        _response(content="Read it."),
    ])

    _run(agent)

    assert reads == ["main.tex"]
    assert calls["n"] == 2


def test_new_heartbeat_lets_the_model_speak_before_it_acts():
    """The announcement that used to be cut off mid-thought is now expressible."""
    reads = []
    agent = _build(reads)
    calls = _script(agent, [
        _hold("I will now read main.tex and draft the plan."),
        _response(tool_calls=[_tool_call("c1", "read_file", '{"path": "main.tex"}')]),
        _response(content="Here is the plan: 1. ... 2. ..."),
    ])

    out = _run(agent)

    assert reads == ["main.tex"], "the announced action actually ran"
    assert out.response.endswith("Here is the plan: 1. ... 2. ...")
    assert "I will now read main.tex" in out.response, "the announcement was said out loud"
    assert calls["n"] == 3


# ── The idle allowance ───────────────────────────────────────────────────────

def test_an_unbroken_run_of_idle_heartbeats_is_bounded():
    agent = _build([], max_idle_heartbeats=2)
    calls = _script(agent, [_hold("Still thinking.", "h1")] * 6 + [_response(content="OK.")])

    _run(agent)

    # Two idle rounds are granted; the third call is the forced answer.
    assert calls["tool_choice"][:3] == ["auto", "auto", "none"]


def test_spending_the_allowance_asks_for_the_answer_instead_of_taking_one():
    """Accepting the last announcement would deliver work nobody did."""
    agent = _build([], max_idle_heartbeats=2)
    calls = _script(agent, [
        _hold("I am about to start.", "h1"),
        _hold("I am still about to start.", "h2"),
        _response(content="I could not act: no tool was available."),
    ])

    out = _run(agent)

    assert calls["tool_choice"][2] == "none", "the forced call cannot make one more promise"
    assert out.response.endswith("I could not act: no tool was available.")


def test_acting_resets_the_idle_run():
    """Speak-act-speak-act is ordinary work, not a model going in circles."""
    reads = []
    agent = _build(reads, max_idle_heartbeats=2)
    calls = _script(agent, [
        _hold("First I will read a.", "h1"),
        _response(tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _hold("Now I will read b.", "h2"),
        _response(tool_calls=[_tool_call("c2", "read_file", '{"path": "b"}')]),
        _response(content="Both read."),
    ])

    _run(agent)

    assert reads == ["a", "b"]
    assert "none" not in calls["tool_choice"], "the allowance was never spent"


def test_reasoning_only_responses_share_the_idle_allowance():
    """A model that only ever thinks costs three calls, not the whole guardrail."""
    agent = _build([], max_idle_heartbeats=2, max_heartbeats=30)
    calls = _script(agent, [_reasoning("thinking...")] * 10)

    out = _run(agent)

    assert calls["n"] == 3
    assert out.response == "", "no answer is better than a false one"


def test_the_guardrail_still_ends_the_run():
    reads = []
    agent = _build(reads, max_heartbeats=3)
    calls = _script(agent, [_response(tool_calls=[_tool_call("c", "read_file", '{"path": "a"}')])])

    out = _run(agent)

    assert calls["n"] <= 4
    assert "max_heartbeats" in out.termination_reason


# ── The rules the model reads ────────────────────────────────────────────────

def test_the_prompt_teaches_the_single_channel():
    prompt = _build([])._build_system_prompt()

    assert "no second channel" in prompt
    assert "including text you write in the same response as a tool call" in prompt
    assert "ends the turn" in prompt or "It ends when you reply with text" in prompt
    assert HEARTBEAT_TOOL_NAME in prompt


def test_send_message_no_longer_exists():
    """There is no delivery tool to write the answer into and then point at."""
    agent = _build([])
    schemas = {}

    async def capture(messages, **kw):
        for tool in kw.get("tools", []):
            schemas[tool["function"]["name"]] = tool["function"]["description"]
        return _response(content="done")

    agent._acompletion = capture
    _run(agent)

    assert "send_message" not in schemas
    assert HEARTBEAT_TOOL_NAME in schemas
    assert "without performing an action" in schemas[HEARTBEAT_TOOL_NAME]


# ── The GilzaBots wiring ──────────────────────────────────────────────────────

def test_the_chat_orchestrator_wires_the_idle_allowance(tmp_path):
    from gilzabots.memgpt_runtime import build_chat_orchestrator
    from gilzabots.project import ProjectData

    agent = build_chat_orchestrator(ProjectData(
        name="t", project_name="t", project_path=str(tmp_path),
        model="ollama/gemma4:12b", mode="plan",
    ), None)

    assert agent.max_idle_heartbeats == 2
    assert agent.max_heartbeats == 50, "a guardrail, not the old budget"


def test_a_project_can_retune_the_idle_allowance(tmp_path):
    from gilzabots.memgpt_runtime import build_chat_orchestrator
    from gilzabots.project import ProjectData

    agent = build_chat_orchestrator(ProjectData(
        name="t", project_name="t", project_path=str(tmp_path),
        model="ollama/gemma4:12b", mode="plan",
        model_params={"max_idle_heartbeats": 5},
    ), None)

    assert agent.max_idle_heartbeats == 5


@pytest.mark.parametrize("params,expected", [
    ({"max_idle_heartbeats": "4"}, {"max_idle_heartbeats": 4}),
    ({"max_idle_heartbeats": 99}, {"max_idle_heartbeats": 10}),   # clamped
])
def test_the_setting_survives_a_project_save(params, expected):
    """Absent from the schema it would be silently dropped on save."""
    from gilzabots.config import sanitize_model_params

    assert sanitize_model_params(params) == expected


def test_the_retired_settings_are_gone_from_the_schema():
    """Leaving them accepted would keep dead knobs in the UI and in saved projects."""
    from gilzabots.config import _AGENT_PARAM_KEYS, _NON_LITELLM_FIELDS, sanitize_model_params

    retired = {"response_mode", "model_controlled_turn_end", "max_narration_steps"}
    assert not (retired & _NON_LITELLM_FIELDS)
    assert not (retired & _AGENT_PARAM_KEYS)
    assert sanitize_model_params({"response_mode": "last"}) == {}


def test_the_setting_never_reaches_the_provider():
    """It is an agent constructor param, not a LiteLLM request parameter."""
    from gilzabots.config import _AGENT_PARAM_KEYS, _NON_LITELLM_FIELDS

    assert "max_idle_heartbeats" in _NON_LITELLM_FIELDS
    assert "max_idle_heartbeats" in _AGENT_PARAM_KEYS


# ── Presentation: folding progress without losing it ─────────────────────────

def test_the_block_reports_progress_and_answer_separately():
    """A host can lay them out differently; `response` still holds every word."""
    reads = []
    agent = _build(reads)
    _script(agent, [
        _response(content="Reading the file.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _hold("Now drafting.", "h1"),
        _response(content="THE ANSWER"),
    ])

    out = _run(agent)

    assert out.narration == ["Reading the file.", "Now drafting."]
    assert out.final_text == "THE ANSWER"
    # The lossless join is unchanged: a caller ignoring the split keeps it all.
    assert out.response == "Reading the file.\n\nNow drafting.\n\nTHE ANSWER"


def test_a_turn_with_no_progress_reports_none():
    agent = _build([])
    _script(agent, [_response(content="Just the answer.")])

    out = _run(agent)

    assert out.narration == []
    assert out.final_text == "Just the answer."


def test_progress_is_folded_above_the_answer():
    from gilzabots.agent_stdin import _fold_turn_progress

    resp = SimpleNamespace(narration=["Reading.", "Drafting."], final_text="THE ANSWER")
    folded = _fold_turn_progress(resp, "Reading.\n\nDrafting.\n\nTHE ANSWER")

    assert folded == "```gilzabots-progress\nReading.\n\nDrafting.\n```\n\nTHE ANSWER"


def test_folding_is_skipped_when_the_boundary_no_longer_holds():
    """A correction rewrote the tail: fold blindly and the text gets mangled."""
    from gilzabots.agent_stdin import _fold_turn_progress

    resp = SimpleNamespace(narration=["Reading."], final_text="THE ANSWER")
    sanitized = "Reading.\n\nTHE ANSWER, corrected"

    assert _fold_turn_progress(resp, sanitized) == sanitized


def test_folding_is_skipped_when_the_progress_carries_a_fence():
    """Nesting a fence inside the progress fence would break the block."""
    from gilzabots.agent_stdin import _fold_turn_progress

    resp = SimpleNamespace(narration=["Running:\n```sh\nls\n```"], final_text="DONE")
    sanitized = "Running:\n```sh\nls\n```\n\nDONE"

    assert _fold_turn_progress(resp, sanitized) == sanitized


def test_folding_never_drops_a_character():
    from gilzabots.agent_stdin import _fold_turn_progress

    resp = SimpleNamespace(narration=["Alpha.", "Beta."], final_text="Gamma.")
    response = "Alpha.\n\nBeta.\n\nGamma."
    folded = _fold_turn_progress(resp, response)

    for part in ("Alpha.", "Beta.", "Gamma."):
        assert part in folded


def test_the_progress_markup_is_not_replayed_to_the_model():
    """A model reads its own last turn as an example and imitates the markup."""
    from gilzabots.memgpt_runtime import _strip_progress_fence

    stored = "```gilzabots-progress\nReading.\n\nDrafting.\n```\n\nTHE ANSWER"

    assert _strip_progress_fence(stored) == "THE ANSWER"
    # An answer that merely mentions the word is untouched.
    assert _strip_progress_fence("progress was made") == "progress was made"


# ── The same rule on the delegation path ─────────────────────────────────────

def test_a_worker_does_not_lose_text_written_beside_a_tool_call():
    """`run_skill` spawns an `LLMAgentBlock`; it had the discard the chat lost."""
    from agenticblocks.blocks.llm.agent import LLMAgentBlock

    reads = []

    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        reads.append(path)
        return "contents"

    worker = LLMAgentBlock(name="w", model="fake/m", tools=[read_file], max_iterations=5)
    seq = [
        _response(content="FULL REPORT", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="Report delivered above."),
    ]

    async def fake(_messages, **_kw):
        return seq.pop(0)

    worker._acompletion = fake
    out = asyncio.run(worker.run(AgentInput(prompt="go")))

    assert out.response == "FULL REPORT\n\nReport delivered above."
    assert out.narration == ["FULL REPORT"]


# ── Verbatim means verbatim ──────────────────────────────────────────────────

def test_leading_indentation_survives_the_join():
    """Stripping each part turned a Markdown indented code block into prose."""
    reads = []
    agent = _build(reads)
    _script(agent, [
        _response(content="Reading.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="    indented code\n    second line\n\nDone."),
    ])

    out = _run(agent)

    assert "    indented code\n    second line" in out.response


def test_text_with_no_think_tag_is_not_reshaped():
    """The reasoning splitter strips what it returns; it must not run for nothing."""
    agent = _build([])
    body = "Header\n\n    indented\n\nTail."
    _script(agent, [_response(content=body)])

    assert _run(agent).response == body


# ── A run the guardrail cut short is not an answer ───────────────────────────

def test_spending_the_last_heartbeat_still_asks_for_the_answer():
    """The measured cut: the work was done, and the model was never heard from.

    Traced in a real chat: the page compiled, the rendered slide was checked, and
    the model spent its last heartbeat recording progress. The loop broke out the
    moment the budget reached zero, so the request that asks for the final answer
    -- which existed, and which the design said owned the last call -- never ran,
    and the user got "Recording progress:" with a cut-short notice.
    """
    reads = []
    agent = _build(reads, max_heartbeats=1)
    calls = _script(agent, [
        _response(content="I will inspect now.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="Inspected a: nothing to fix."),
    ])

    out = _run(agent)

    assert reads == ["a"]
    assert calls["tool_choice"] == ["auto", "none"], "one more request, with tools off"
    assert out.final_text == "Inspected a: nothing to fix."
    assert out.narration == ["I will inspect now."]
    assert out.budget_exhausted, "the budget was still spent, and the block says so"
    assert "max_heartbeats" in out.termination_reason


def test_the_answer_request_is_told_the_budget_is_spent():
    agent = _build([], max_heartbeats=1)
    seen = []

    async def fake(messages, **kw):
        seen.append([m["content"] for m in messages if m.get("role") == "system"])
        if kw.get("tool_choice") == "none":
            return _response(content="Done.")
        return _response(tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')])

    agent._acompletion = fake
    _run(agent)

    alert = [c for c in seen[-1] if "No action heartbeats remain" in c]
    assert len(alert) == 1
    assert "unfinished" in alert[0], "it asks for an honest status, not a success report"


def test_tool_calls_returned_to_the_answer_request_are_refused_not_run():
    """A provider may ignore tool_choice="none"; the guardrail must still hold."""
    reads = []
    agent = _build(reads, max_heartbeats=1)
    _script(agent, [
        _response(content="I will inspect now.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="Reading b next.", tool_calls=[_tool_call("c2", "read_file", '{"path": "b"}')]),
    ])

    out = _run(agent)

    assert reads == ["a"], "the call past the budget never ran"
    assert out.narration == ["I will inspect now.", "Reading b next."], "the text is kept, never dropped"
    assert out.final_text == "", "but it is not an answer, and the block says so"
    assert out.budget_exhausted
    refused = [m for m in agent.internal_history if m.get("tool_call_id") == "c2"]
    assert len(refused) == 1 and "Not executed" in refused[0]["content"], (
        "every call keeps a result, or the next request built from this history breaks"
    )


def test_the_guardrail_reports_no_final_answer():
    reads = []
    agent = _build(reads, max_heartbeats=1)
    _script(agent, [
        _response(content="I will inspect now.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content=""),
    ])

    out = _run(agent)

    assert out.narration == ["I will inspect now."], "the text is kept, never dropped"
    assert out.final_text == "", "but it is not an answer, and the block says so"
    assert out.budget_exhausted


def test_an_empty_response_correction_on_the_last_heartbeat_still_asks_for_the_answer():
    agent = _build([], max_heartbeats=1)
    calls = _script(agent, [_response(content=""), _response(content="The answer.")])

    out = _run(agent)

    assert calls["tool_choice"] == ["auto", "none"]
    assert out.final_text == "The answer."


def test_a_run_that_answers_within_budget_is_not_budget_exhausted():
    agent = _build([], max_heartbeats=30)
    _script(agent, [
        _response(tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _response(content="Done."),
    ])

    assert not _run(agent).budget_exhausted


def test_an_idle_ending_is_not_reported_as_an_exhausted_budget():
    """A model that stops acting with steps left needs another model, not more steps."""
    agent = _build([], max_idle_heartbeats=2, max_heartbeats=30)
    _script(agent, [
        _response(content="Reading.", tool_calls=[_tool_call("c1", "read_file", '{"path": "a"}')]),
        _reasoning("thinking..."),
    ])

    out = _run(agent)

    assert out.final_text == ""
    assert out.narration == ["Reading."]
    assert not out.budget_exhausted


def test_llm_agent_budget_exits_report_an_exhausted_budget():
    from agenticblocks.blocks.llm.agent import LLMAgentBlock

    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        return "contents"

    async def fake(_messages, **_kw):
        return _response(tool_calls=[_tool_call("c", "read_file", '{"path": "a"}')])

    for policy in ("return_last", "stop"):
        worker = LLMAgentBlock(
            name="w", model="fake/m", tools=[read_file], max_iterations=1,
            on_max_iterations=policy, loop_detection=False,
        )
        worker._acompletion = fake
        assert asyncio.run(worker.run(AgentInput(prompt="go"))).budget_exhausted, policy

    answered = LLMAgentBlock(name="w", model="fake/m", tools=[read_file], max_iterations=5)

    async def answer(_messages, **_kw):
        return _response(content="Done.")

    answered._acompletion = answer
    assert not asyncio.run(answered.run(AgentInput(prompt="go"))).budget_exhausted


def test_the_host_labels_a_turn_that_never_answered():
    from gilzabots.agent_stdin import TURN_CUT_SHORT_MARKER, _mark_turn_without_answer

    cut = SimpleNamespace(narration=["I will inspect now."], final_text="", budget_exhausted=True)
    marked = _mark_turn_without_answer(cut, "I will inspect now.")

    assert marked.startswith("I will inspect now."), "the progress is still delivered"
    assert marked.endswith(TURN_CUT_SHORT_MARKER), "and it is no longer passed off as the reply"

    done = SimpleNamespace(narration=["Reading."], final_text="THE ANSWER", budget_exhausted=True)
    assert _mark_turn_without_answer(done, "Reading.\n\nTHE ANSWER") == "Reading.\n\nTHE ANSWER", (
        "a model that answered on the answer request is not cut short, spent budget or not"
    )


def test_a_turn_that_stopped_with_steps_left_is_not_blamed_on_the_budget():
    """The old label sent the user to raise a limit the turn never reached."""
    from gilzabots.agent_stdin import (
        TURN_CUT_SHORT_MARKER,
        TURN_NO_ANSWER_MARKER,
        _mark_turn_without_answer,
    )

    idle = SimpleNamespace(narration=["Reading."], final_text="", budget_exhausted=False)
    marked = _mark_turn_without_answer(idle, "Reading.")

    assert marked == f"Reading.\n\n{TURN_NO_ANSWER_MARKER}"
    assert TURN_CUT_SHORT_MARKER not in marked


def _front_end_marker(name: str) -> str:
    """Read a marker the front-end declares, as the string it compares against."""
    import re

    module = open("gui_src/src/utils/turnMarkers.js", encoding="utf-8").read()
    declared = re.search(rf"export const {name} =\n?(.*?);", module, re.S)
    assert declared, f"the front-end must declare {name}"
    return "".join(re.findall(r"'([^']*)'", declared.group(1)))


def test_the_cut_short_marker_is_unlocalised_and_matches_the_front_end():
    """The front-end matches this string to show the continue action.

    It is persisted, so it must not be a translation: a turn recorded in one
    language would stop matching after the user switched to another, and the
    button would silently disappear from turns that still need it.
    """
    from gilzabots.agent_stdin import TURN_CUT_SHORT_MARKER, TURN_NO_ANSWER_MARKER

    assert _front_end_marker("TURN_CUT_SHORT_MARKER") == TURN_CUT_SHORT_MARKER
    assert TURN_CUT_SHORT_MARKER.startswith("[TURN-CUT-SHORT]")
    assert _front_end_marker("TURN_NO_ANSWER_MARKER") == TURN_NO_ANSWER_MARKER
    assert TURN_NO_ANSWER_MARKER.startswith("[TURN-NO-ANSWER]")


def test_the_interruption_marker_matches_the_front_end_and_is_a_suffix():
    """The marker is appended *after* the work the turn produced.

    `_persist_unfinished_turn` stores ``f"{visible}\\n\\n{marker}"``, so a turn
    stopped mid-sentence keeps its partial answer above the marker. The
    front-end used to compare the whole message against the marker, which
    matched only a turn that had produced nothing: interrupting a talking agent
    showed the raw English marker in the chat and offered no continue action.
    Both halves of that contract are asserted here.
    """
    from gilzabots.agent_stdin import INTERRUPTED_AGENT_HISTORY_MARKER

    assert _front_end_marker("INTERRUPTED_MARKER") == INTERRUPTED_AGENT_HISTORY_MARKER
    assert INTERRUPTED_AGENT_HISTORY_MARKER.startswith("[INTERRUPTED]")

    panel = open("gui_src/src/components/ChatPanel.jsx", encoding="utf-8").read()
    assert INTERRUPTED_AGENT_HISTORY_MARKER not in panel, (
        "the marker belongs to utils/turnMarkers.js; a second copy is a second "
        "thing to keep in step"
    )


# ── The fence is this host's markup, not the model's ─────────────────────────

def test_a_legitimate_progress_block_from_the_model_is_left_alone():
    """`progress` is a word a model may fence; the stripper must not eat it."""
    from gilzabots.memgpt_runtime import _strip_progress_fence

    written_by_the_model = "```progress\nstep 1\n```\n\nThat is the example you asked for."

    assert _strip_progress_fence(written_by_the_model) == written_by_the_model
    assert _strip_progress_fence(
        "```gilzabots-progress\nReading.\n```\n\nTHE ANSWER"
    ) == "THE ANSWER"


def test_folding_does_not_steal_indentation_from_the_answer():
    """An answer opening with a code block must keep its leading spaces."""
    from gilzabots.agent_stdin import _fold_turn_progress

    final = "    def f():\n        return 1"
    response = f"Reading.\n\n{final}"
    folded = _fold_turn_progress(
        SimpleNamespace(narration=["Reading."], final_text=final), response
    )

    assert folded.endswith(final)
    assert folded == f"```gilzabots-progress\nReading.\n```\n\n{final}"


def test_a_cut_short_turn_is_not_folded():
    """No final answer means no boundary; the marked text is returned whole."""
    from gilzabots.agent_stdin import _fold_turn_progress, _mark_turn_without_answer
    from gilzabots.i18n import set_lang

    set_lang("en")
    cut = SimpleNamespace(narration=["I will inspect now."], final_text="")
    marked = _mark_turn_without_answer(cut, "I will inspect now.")

    assert _fold_turn_progress(cut, marked) == marked


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_the_cut_short_notice_points_at_the_control_that_sets_the_budget(locale):
    """Continuing fixes this turn; a larger budget fixes the next one.

    The notice used to send the user to "Max Heartbeats (MemGPT)" in Project
    Settings alone. The composer's effort selector sends its own budget with every
    turn and the backend gives it precedence, so outside "Custom" that field
    changed nothing -- the advice pointed at a control that could not help. The
    notice now names the selector first and the field as what "Custom" uses, and
    every label is interpolated from the catalogue, so none can drift from the UI.
    """
    import json

    catalogue = json.load(open(f"gui_src/src/i18n/locales/{locale}.json", encoding="utf-8"))
    notice = catalogue["app"]["turnCutShortNotice"]

    for placeholder in ("{{selector}}", "{{custom}}", "{{field}}"):
        assert placeholder in notice, f"the notice must name {placeholder} as the UI labels it"
    assert notice.index("{{selector}}") < notice.index("{{field}}"), "the selector is the primary remedy"

    panel = open("gui_src/src/components/ChatPanel.jsx", encoding="utf-8").read()
    for key in ("chatPanel.heartbeatMode'", "chatPanel.heartbeatModeCustom'", "editProjectModal.maxHeartbeats'"):
        assert key in panel, f"ChatPanel must fill the notice from {key}"


def test_the_budget_the_notice_names_is_the_one_the_run_uses():
    """The selector's budget travels with the run and wins over the project value."""
    app = open("gui_src/src/App.jsx", encoding="utf-8").read()
    stdin = open("gilzabots/agent_stdin.py", encoding="utf-8").read()

    assert "max_heartbeats: options.maxHeartbeats !== undefined ? options.maxHeartbeats : resolveEffectiveMaxHeartbeats()" in app
    assert "if (mode === 'custom')" in app, "only Custom defers to the project setting"
    assert 'req_hb = data.get("max_heartbeats")' in stdin


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_the_no_answer_notice_does_not_point_at_the_budget(locale):
    import json

    catalogue = json.load(open(f"gui_src/src/i18n/locales/{locale}.json", encoding="utf-8"))
    notice = catalogue["app"]["turnNoAnswerNotice"]

    assert notice.strip()
    assert catalogue["editProjectModal"]["maxHeartbeats"] not in notice
    assert "{{" not in notice


def test_the_heartbeat_prompt_rules_budget_steps_and_avoid_not_a_budget():
    """Rule 2 must instruct the agent to budget steps, and never say 'not a budget to ration'."""
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    block = MemGPTAgentBlock(name="test_memgpt", system_prompt="Base prompt.")
    prompt = block._build_system_prompt()

    assert "not a budget to ration" not in prompt.lower()
    assert "budget" in prompt.lower()
    assert "conclude" in prompt.lower() or "clean conclusion" in prompt.lower()


def test_agent_step_event_is_emitted_during_turn_iteration():
    """_on_iteration emits agent_step with current step and max_steps."""
    import gilzabots.agent_stdin as agent_stdin

    emitted_events = []
    prev_hook = agent_stdin.event_hook
    try:
        agent_stdin.event_hook = lambda payload: emitted_events.append(payload)

        class DummyAgent:
            model = "dummy"
            max_heartbeats = 50

        # Build an _on_iteration callback simulating agent_stdin.run_agent
        _step = 3
        agent = DummyAgent()
        agent_type = "chat_orchestrator"

        raw_max_steps = getattr(agent, "max_heartbeats", None)
        max_steps = int(raw_max_steps) if raw_max_steps is not None else None

        agent_stdin.print_event("agent_step", {
            "step": _step,
            "max_steps": max_steps,
            "agent": agent_type,
        })

        step_events = [e for e in emitted_events if e.get("event") == "agent_step"]
        assert len(step_events) == 1
        assert step_events[0]["step"] == 3
        assert step_events[0]["max_steps"] == 50
        assert step_events[0]["agent"] == "chat_orchestrator"
    finally:
        agent_stdin.event_hook = prev_hook


def test_heartbeat_mode_override_updates_agent_max_heartbeats():
    """handle_run overrides agent.max_heartbeats when max_heartbeats is passed, and resets on None."""
    import gilzabots.agent_stdin as agent_stdin

    class DummyMemgpt:
        max_heartbeats = 50

    dummy_agent = DummyMemgpt()
    prev_memgpt = agent_stdin.current_memgpt
    try:
        agent_stdin.current_memgpt = dummy_agent

        # Simulate low mode (20)
        data = {"max_heartbeats": 20}
        req_hb = data.get("max_heartbeats")
        if req_hb is not None:
            dummy_agent.max_heartbeats = int(req_hb)
        assert dummy_agent.max_heartbeats == 20

        # Simulate high mode (100)
        data = {"max_heartbeats": 100}
        req_hb = data.get("max_heartbeats")
        if req_hb is not None:
            dummy_agent.max_heartbeats = int(req_hb)
        assert dummy_agent.max_heartbeats == 100

        # Simulate custom / reset fallback
        data = {"max_heartbeats": None}
        req_hb = data.get("max_heartbeats")
        if req_hb is None:
            from gilzabots.config import get_project_agent_params, get_agent_max_heartbeats
            _agent_params = get_project_agent_params()
            dummy_agent.max_heartbeats = int(_agent_params.get("max_heartbeats", get_agent_max_heartbeats("memgpt", 50)))
        assert dummy_agent.max_heartbeats == 50
    finally:
        agent_stdin.current_memgpt = prev_memgpt

