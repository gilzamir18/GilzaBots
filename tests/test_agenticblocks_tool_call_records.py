"""`AgentOutput.tool_calls`: what happened to every tool call of an LLMAgentBlock run.

A run's final text is the model's own account. The block's corrective message
tells the model to correct a tool error silently, so a host that delegates to
the block cannot learn from the prose that a call failed. The record says it.
"""
import asyncio
import json
from types import SimpleNamespace

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock, ToolCallRecord
from agenticblocks.core.function_block import as_tool


@as_tool(name="divide", description="Divide a by b.")
def divide(a: int, b: int) -> float:
    return a / b


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _scripted_agent(steps, **kwargs):
    kwargs.setdefault("max_tool_calls", 10)  # the block's default (2) would end the scripts early
    agent = LLMAgentBlock(name="t", model="mock/model", system_prompt="s", tools=[divide], **kwargs)
    replies = iter(steps)

    async def fake_acompletion(_messages, **_kwargs):
        return next(replies)

    agent._acompletion = fake_acompletion
    return agent


def test_every_call_is_recorded_in_order_with_its_outcome():
    agent = _scripted_agent([
        _response("", [_tool_call("c1", "divide", {"a": 1, "b": 0})]),
        _response("", [_tool_call("c2", "divide", {"a": 4, "b": 2})]),
        _response("", [_tool_call("c3", "no_such_tool", {})]),
        _response("done"),
    ])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert [(r.index, r.tool, r.ok) for r in out.tool_calls] == [
        (1, "divide", False), (2, "divide", True), (3, "no_such_tool", False),
    ]
    assert "division by zero" in out.tool_calls[0].error
    # The record keeps the error, not the corrective instruction sent to the model.
    assert "Correct it silently" not in out.tool_calls[0].error
    assert "no_such_tool" in out.tool_calls[2].error
    assert out.tool_calls[1].error == ""
    assert out.tool_calls_made == 3


def test_a_call_refused_by_loop_detection_is_recorded_as_failed():
    same = {"a": 1, "b": 0}
    agent = _scripted_agent(
        [_response("", [_tool_call(f"c{i}", "divide", same)]) for i in range(3)] + [_response("ok")],
        loop_detection=True, loop_detection_limit=2,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert [r.ok for r in out.tool_calls] == [False, False, False]
    assert "division by zero" in out.tool_calls[1].error
    assert "division by zero" not in out.tool_calls[2].error  # refused, never executed


def test_a_run_without_tool_calls_reports_an_empty_record():
    out = asyncio.run(_scripted_agent([_response("hello")]).run(AgentInput(prompt="hi")))
    assert out.tool_calls == []


def test_the_record_survives_a_forced_final_response():
    """Every exit of the loop carries the record, including the budget exits."""
    agent = _scripted_agent(
        [_response("", [_tool_call("c1", "divide", {"a": 1, "b": 0})]), _response("final")],
        max_tool_calls=1,
    )

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.budget_exhausted
    assert [r.ok for r in out.tool_calls] == [False]


def test_tool_call_record_defaults():
    record = ToolCallRecord(index=1, tool="x", ok=True)
    assert record.error == ""
