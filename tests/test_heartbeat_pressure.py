"""The step budget warns before it runs out.

A turn that reaches `max_heartbeats` is cut off mid-work: the user is shown
work in progress instead of an answer and has to resume the run by hand. Until
now the only signal of how much budget was left was the `[System: You have N
heartbeats remaining.]` tail appended to every tool result, which reads as
bookkeeping and says nothing about what to do with the number.

`heartbeat_pressure_threshold` turns the last stretch of the budget into an
explicit instruction, the same way `memory_pressure_threshold` does for the
context window: past the threshold every request opens with a SYSTEM ALERT
naming the steps left and asking the model to be brief and close the turn. The
alert is transient -- it is appended to the request, never to `internal_history`
-- so the model reads one current warning instead of a pile of stale ones.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from agenticblocks.core.function_block import as_tool


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(tool_calls=None, content=""):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _work(call_id):
    """A step that actually acts, so the idle allowance never interferes."""
    return _response(tool_calls=[_tool_call(call_id, "read_file", '{"path": "a.tex"}')])


def _build(**kwargs):
    @as_tool(name="read_file", description="Read a file.")
    def read_file(path: str) -> str:
        return "file contents"

    return MemGPTAgentBlock(
        name="orchestrator", system_prompt="s", tools=[read_file], **kwargs,
    )


def _script(agent, responses):
    """Drive the loop with a fixed sequence of provider responses.

    Records the system messages carried by each request, which is where the
    pressure alert has to appear for the model to ever read it.
    """
    calls = {"n": 0, "alerts": []}

    async def fake_acompletion(messages, **kw):
        calls["alerts"].append([
            m["content"] for m in messages
            if m.get("role") == "system" and "SYSTEM ALERT: Heartbeat Pressure" in str(m.get("content", ""))
        ])
        idx = calls["n"]
        calls["n"] += 1
        return responses[min(idx, len(responses) - 1)]

    agent._acompletion = fake_acompletion
    return calls


def test_alert_fires_only_once_the_threshold_is_crossed():
    agent = _build(max_heartbeats=4, heartbeat_pressure_threshold=0.5)
    calls = _script(agent, [
        _work("c1"), _work("c2"), _work("c3"), _response(content="Done."),
    ])

    asyncio.run(agent.run(input=AgentInput(prompt="go")))

    # Requests are made with 0, 1, 2 and 3 heartbeats spent; the threshold of
    # 0.5 * 4 = 2 puts the warning on the last two.
    assert [len(a) for a in calls["alerts"]] == [0, 0, 1, 1]


def test_alert_names_the_steps_left_and_asks_for_brevity():
    agent = _build(max_heartbeats=4, heartbeat_pressure_threshold=0.5)
    calls = _script(agent, [
        _work("c1"), _work("c2"), _work("c3"), _response(content="Done."),
    ])

    asyncio.run(agent.run(input=AgentInput(prompt="go")))

    first_warning = calls["alerts"][2][0]
    assert "2 of 4" in first_warning
    assert "brief" in first_warning.lower()
    # The next request counts one step further down, so the model can tell the
    # budget is still draining rather than reading a fixed banner.
    assert "1 of 4" in calls["alerts"][3][0]


def test_alert_is_transient_and_never_accumulates_in_history():
    agent = _build(max_heartbeats=4, heartbeat_pressure_threshold=0.5)
    calls = _script(agent, [
        _work("c1"), _work("c2"), _work("c3"), _response(content="Done."),
    ])

    asyncio.run(agent.run(input=AgentInput(prompt="go")))

    # One warning per warned request, never two stacked in the same one.
    assert all(len(a) <= 1 for a in calls["alerts"])
    stored = [
        m for m in agent.internal_history
        if "SYSTEM ALERT: Heartbeat Pressure" in str(m.get("content", ""))
    ]
    assert stored == []


def test_threshold_of_one_disables_the_warning():
    agent = _build(max_heartbeats=3, heartbeat_pressure_threshold=1.0)
    calls = _script(agent, [_work("c1"), _work("c2"), _response(content="Done.")])

    asyncio.run(agent.run(input=AgentInput(prompt="go")))

    assert all(a == [] for a in calls["alerts"])


def test_last_step_that_could_act_carries_the_warning():
    """The last request allowed to act was warned; the answer request follows it.

    Once the budget is spent the run asks for the answer with tool calls off,
    under the exhausted-budget alert alone: a brevity warning there would compete
    with the one thing that request exists to obtain.
    """
    agent = _build(max_heartbeats=2, heartbeat_pressure_threshold=0.5)
    calls = _script(agent, [_work("c1"), _work("c2"), _response(content="Done.")])

    out = asyncio.run(agent.run(input=AgentInput(prompt="go")))

    assert out.termination_reason.startswith("max_heartbeats")
    assert calls["n"] == 3
    assert len(calls["alerts"][1]) == 1
    assert "1 of 2" in calls["alerts"][1][0]
    assert calls["alerts"][2] == []
    assert out.final_text == "Done."


def test_pressure_does_not_preempt_the_forced_final_answer():
    """Spending the idle allowance still ends in a request for the answer."""
    from agenticblocks.blocks.llm.memgpt_agent import HEARTBEAT_TOOL_NAME

    agent = _build(
        max_heartbeats=8, heartbeat_pressure_threshold=0.0, max_idle_heartbeats=2,
    )
    seen = {"choice": [], "pressure": []}

    async def fake_acompletion(messages, **kw):
        seen["choice"].append(kw.get("tool_choice"))
        seen["pressure"].append(
            any("SYSTEM ALERT: Heartbeat Pressure" in str(m.get("content", "")) for m in messages)
        )
        if kw.get("tool_choice") == "none":
            return _response(content="Final answer.")
        return _response(
            content="Working on it.",
            tool_calls=[_tool_call(f"h{len(seen['choice'])}", HEARTBEAT_TOOL_NAME)],
        )

    agent._acompletion = fake_acompletion
    asyncio.run(agent.run(input=AgentInput(prompt="go")))

    assert seen["choice"] == ["auto", "auto", "none"]
    # A threshold of 0 warns from the first request, but the forced-answer
    # branch owns the last one: no pressure alert rides along with it.
    assert seen["pressure"] == [True, True, False]


def test_the_project_setting_survives_the_save_path():
    """A threshold the dialog offers has to reach the agent constructor.

    `model_params` is filtered twice on its way in -- `sanitize_model_params`
    drops anything outside the schema, and `get_project_agent_params` keeps only
    the keys that name agent constructor fields. A knob missing from either list
    is accepted by the dialog and silently ignored at runtime.
    """
    from gilzabots.config import _AGENT_PARAM_KEYS, sanitize_model_params

    assert sanitize_model_params({"heartbeat_pressure_threshold": 0.6}) == {
        "heartbeat_pressure_threshold": 0.6
    }
    assert "heartbeat_pressure_threshold" in _AGENT_PARAM_KEYS
    # Out-of-range values are clamped rather than dropped, like the other
    # fractions: 1.0 is the documented way to switch the warning off.
    assert sanitize_model_params({"heartbeat_pressure_threshold": 4}) == {
        "heartbeat_pressure_threshold": 1.0
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
