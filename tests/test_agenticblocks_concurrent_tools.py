"""Concurrency-safe tool calls run together; every other call runs alone, in order.

`Block.concurrency_safe` lets a tool declare that it may run at the same time as
the other concurrency-safe calls one model response issued. `MemGPTAgentBlock`
gathers consecutive such calls and records every result in the order the calls
were made, so the history a provider receives is the same whatever finished
first. A batch with no concurrency-safe tool runs exactly as it always did.
"""
import asyncio
from types import SimpleNamespace

from agenticblocks.blocks.llm.agent import AgentInput
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from agenticblocks.core.function_block import as_tool


def _tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _response(tool_calls=None, content=""):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _script(agent, responses):
    state = {"n": 0}

    async def fake_acompletion(messages, **kw):
        index = state["n"]
        state["n"] += 1
        return responses[min(index, len(responses) - 1)]

    agent._acompletion = fake_acompletion


def _tool_results(agent):
    return [m for m in agent.internal_history if m.get("role") == "tool"]


def _build(timeline, safe: bool):
    @as_tool(name="slow", description="Wait, then answer.", concurrency_safe=safe)
    async def slow(tag: str, delay: float) -> str:
        timeline.append(("start", tag))
        await asyncio.sleep(delay)
        timeline.append(("end", tag))
        return f"result {tag}"

    @as_tool(name="plain", description="Answer at once.")
    def plain(tag: str) -> str:
        timeline.append(("plain", tag))
        return f"plain {tag}"

    return MemGPTAgentBlock(name="orchestrator", system_prompt="s", tools=[slow, plain], max_heartbeats=10)


def test_the_flag_is_off_by_default_and_set_by_as_tool():
    @as_tool(name="a", description="a")
    def a() -> str:
        return ""

    @as_tool(name="b", description="b", concurrency_safe=True)
    def b() -> str:
        return ""

    assert a.concurrency_safe is False
    assert b.concurrency_safe is True


def test_consecutive_concurrency_safe_calls_overlap_and_keep_their_order():
    timeline = []
    agent = _build(timeline, safe=True)
    _script(agent, [
        _response(tool_calls=[
            # The first call is the slowest: run one after the other, it would
            # end before the second starts.
            _tool_call("c1", "slow", '{"tag": "one", "delay": 0.2}'),
            _tool_call("c2", "slow", '{"tag": "two", "delay": 0.01}'),
        ]),
        _response(content="Done."),
    ])

    out = asyncio.run(agent.run(AgentInput(prompt="go")))

    assert out.response == "Done."
    assert timeline.index(("start", "two")) < timeline.index(("end", "one"))
    results = _tool_results(agent)
    assert [m["tool_call_id"] for m in results] == ["c1", "c2"]
    assert "result one" in results[0]["content"]
    assert "result two" in results[1]["content"]


def test_calls_without_the_flag_run_one_after_the_other():
    timeline = []
    agent = _build(timeline, safe=False)
    _script(agent, [
        _response(tool_calls=[
            _tool_call("c1", "slow", '{"tag": "one", "delay": 0.05}'),
            _tool_call("c2", "slow", '{"tag": "two", "delay": 0.01}'),
        ]),
        _response(content="Done."),
    ])

    asyncio.run(agent.run(AgentInput(prompt="go")))

    assert timeline == [("start", "one"), ("end", "one"), ("start", "two"), ("end", "two")]


def test_an_unsafe_call_between_safe_ones_splits_the_group_and_keeps_its_place():
    timeline = []
    agent = _build(timeline, safe=True)
    _script(agent, [
        _response(tool_calls=[
            _tool_call("c1", "slow", '{"tag": "one", "delay": 0.05}'),
            _tool_call("c2", "plain", '{"tag": "mid"}'),
            _tool_call("c3", "slow", '{"tag": "three", "delay": 0.01}'),
        ]),
        _response(content="Done."),
    ])

    asyncio.run(agent.run(AgentInput(prompt="go")))

    # The plain call waits for the call before it and runs before the one after.
    assert timeline.index(("end", "one")) < timeline.index(("plain", "mid")) < timeline.index(("start", "three"))
    assert [m["tool_call_id"] for m in _tool_results(agent)] == ["c1", "c2", "c3"]


def test_a_failing_call_of_a_group_reports_its_own_error_and_spares_the_others():
    timeline = []

    @as_tool(name="boom", description="Fail.", concurrency_safe=True)
    async def boom() -> str:
        raise RuntimeError("it broke")

    agent = _build(timeline, safe=True)
    agent.tools = [*agent.tools, boom]
    _script(agent, [
        _response(tool_calls=[
            _tool_call("c1", "boom"),
            _tool_call("c2", "slow", '{"tag": "two", "delay": 0.01}'),
        ]),
        _response(content="Done."),
    ])

    asyncio.run(agent.run(AgentInput(prompt="go")))

    results = _tool_results(agent)
    assert "it broke" in results[0]["content"]
    assert "result two" in results[1]["content"]
