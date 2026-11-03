"""Tests for provider-reported token usage tracking.

The context indicator used to estimate occupancy as `len(visible text) / 4`,
which cannot see the system prompt, the tool schemas, the tool calls or the tool
results. These tests pin the replacement: the `TokenUsage` records AgenticBlocks
emits through `on_token_usage`, scoped to one conversation.
"""
import asyncio

import pytest

from agenticblocks.runtime.state import TokenUsage
from gilzabots import token_usage


def usage(block_name="chat_orchestrator", prompt=0, completion=0, total=0):
    return TokenUsage(
        block_name=block_name,
        step=1,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


@pytest.fixture(autouse=True)
def clean_usage_state():
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")
    token_usage.set_context_scope("test-scope")
    yield
    token_usage.set_usage_listener(None)
    token_usage.set_context_scope("reset")


def test_extract_usage_reads_provider_counters():
    record = token_usage.extract_usage(usage(prompt=1200, completion=340, total=1540))

    assert record == {
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "total_tokens": 1540,
    }


def test_extract_usage_derives_total_when_provider_omits_it():
    record = token_usage.extract_usage(usage(prompt=1200, completion=340, total=0))

    assert record["total_tokens"] == 1540


def test_extract_usage_ignores_absent_and_all_zero_usage():
    # An all-zero usage object means "this provider did not report", not "the
    # context is empty" — recording it would blank the indicator mid-turn.
    assert token_usage.extract_usage(None) is None
    assert token_usage.extract_usage(usage(prompt=0, completion=0, total=0)) is None


def test_record_usage_updates_context_for_the_chat_orchestrator():
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    assert token_usage.get_context_prompt_tokens() == 900


def test_record_usage_keeps_the_latest_measurement():
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))
    token_usage.record_token_usage(usage("chat_orchestrator", 2400, 150, 2550))

    assert token_usage.get_context_prompt_tokens() == 2400


def test_zero_usage_does_not_erase_a_previous_measurement():
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))
    token_usage.record_token_usage(usage("chat_orchestrator", 0, 0, 0))

    assert token_usage.get_context_prompt_tokens() == 900


def test_sub_agents_do_not_move_the_chat_context():
    # A worker or inline editor runs its own history; its usage must not be
    # reported as occupancy of the conversation's window.
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))
    token_usage.record_token_usage(usage("worker", 30000, 200, 30200))

    assert token_usage.get_context_prompt_tokens() == 900
    assert token_usage.get_last_usage("worker")["prompt_tokens"] == 30000


def test_switching_scope_discards_the_previous_measurement():
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    token_usage.set_context_scope("/other/project::main")

    assert token_usage.get_context_prompt_tokens() is None


def test_same_scope_keeps_the_measurement():
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    token_usage.set_context_scope("test-scope")

    assert token_usage.get_context_prompt_tokens() == 900


def test_get_context_usage_honours_the_requested_scope():
    # The history endpoint may serve any chat; it must never receive the
    # measurement taken for a different conversation.
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    assert token_usage.get_context_usage("test-scope")["prompt_tokens"] == 900
    assert token_usage.get_context_usage("/another/project::main") is None


def test_context_usage_reports_the_declared_window():
    token_usage.set_context_window(32000)
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    assert token_usage.get_context_usage()["context_window"] == 32000


def test_switching_scope_also_drops_the_declared_window():
    token_usage.set_context_window(32000)
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    token_usage.set_context_scope("/other/project::main")
    token_usage.record_token_usage(usage("chat_orchestrator", 50, 10, 60))

    assert "context_window" not in token_usage.get_context_usage()


def test_clearing_a_chat_drops_its_measurement():
    # /clear_chat wipes both project_history and the orchestrator's saved
    # agent_state, so the next request starts from the system prompt again.
    # Keeping the old number would report an erased conversation's occupancy.
    token_usage.record_token_usage(usage("chat_orchestrator", 7800, 200, 8000))

    token_usage.reset_context_usage("test-scope")

    assert token_usage.get_context_prompt_tokens() is None


def test_clearing_another_chat_leaves_this_measurement_alone():
    token_usage.record_token_usage(usage("chat_orchestrator", 7800, 200, 8000))

    token_usage.reset_context_usage("/other/project::main")

    assert token_usage.get_context_prompt_tokens() == 7800


def test_reset_without_scope_clears_unconditionally():
    # Deleting every chat of a project leaves nothing for any scope to describe.
    token_usage.record_token_usage(usage("chat_orchestrator", 7800, 200, 8000))

    token_usage.reset_context_usage()

    assert token_usage.get_context_prompt_tokens() is None


def test_local_count_keeps_the_indicator_moving_between_provider_replies():
    # A tool result lands in history long before the next `usage` comes back.
    token_usage.record_token_usage(usage("chat_orchestrator", 4000, 100, 4100))
    token_usage.record_context_tokens(90_000)

    assert token_usage.get_context_prompt_tokens() == 90_000


def test_context_scope_key_is_stable_for_the_same_conversation():
    assert (
        token_usage.context_scope_key("/proj", "chat-7")
        == token_usage.context_scope_key("/proj", "chat-7")
    )
    assert (
        token_usage.context_scope_key("/proj", "chat-7")
        != token_usage.context_scope_key("/proj", "chat-8")
    )


def test_listener_receives_every_recorded_call():
    seen = []
    token_usage.set_usage_listener(lambda agent, record: seen.append((agent, record)))

    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))
    token_usage.record_token_usage(usage("worker", 50, 10, 60))

    assert [agent for agent, _ in seen] == ["chat_orchestrator", "worker"]
    assert seen[0][1]["prompt_tokens"] == 900


def test_listener_failure_does_not_break_recording():
    def _boom(agent, record):
        raise RuntimeError("listener exploded")

    token_usage.set_usage_listener(_boom)
    token_usage.record_token_usage(usage("chat_orchestrator", 900, 100, 1000))

    assert token_usage.get_context_prompt_tokens() == 900


class _FakeProviderResponse:
    """Minimal stand-in for the aggregated LiteLLM response."""

    class _Usage:
        def __init__(self, prompt, completion, total):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    def __init__(self, prompt, completion, total):
        self.usage = self._Usage(prompt, completion, total)


def test_memgpt_block_reports_usage_through_the_framework_callback():
    # MemGPTAgentBlock only pushed TokenUsage into a WorkflowExecutor context,
    # which this project never runs, so the chat orchestrator's usage was
    # unreachable. It now honours on_token_usage like LLMAgentBlock does.
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    block = MemGPTAgentBlock(name="chat_orchestrator", model="openai/gpt-5.5")
    token_usage.attach_usage_tracking(block)

    asyncio.run(block._emit_token_usage(_FakeProviderResponse(4321, 200, 4521), step=3))

    assert token_usage.get_context_prompt_tokens() == 4321


def test_llm_agent_block_reports_usage_through_the_same_callback():
    from agenticblocks.blocks.llm.agent import LLMAgentBlock

    block = LLMAgentBlock(name="worker", model="openai/gpt-5.5")
    token_usage.attach_usage_tracking(block)

    asyncio.run(block._emit_token_usage(_FakeProviderResponse(700, 60, 760), step=1))

    assert token_usage.get_last_usage("worker")["prompt_tokens"] == 700


def test_attach_usage_tracking_is_idempotent():
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    block = MemGPTAgentBlock(name="chat_orchestrator", model="openai/gpt-5.5")
    token_usage.attach_usage_tracking(block)
    token_usage.attach_usage_tracking(block)

    assert block.on_token_usage is token_usage.record_token_usage


def test_resolve_context_window_prefers_the_agent_declared_window():
    from gilzabots.agent_stdin import _resolve_context_window

    class FakeAgent:
        max_context_tokens = 32000
        model_kwargs = {"num_ctx": 16000}

    assert _resolve_context_window(FakeAgent(), {"num_ctx": 8192}) == 32000


def test_resolve_context_window_falls_back_to_model_params():
    from gilzabots.agent_stdin import _resolve_context_window

    class FakeAgent:
        model_kwargs = {}

    assert _resolve_context_window(FakeAgent(), {}, {"num_ctx": 24000}) == 24000


def test_resolve_context_window_reports_unknown_when_nothing_declares_one():
    # A window nobody declared is unknown (0), never a guessed size: the panel
    # would otherwise draw a battery for a window that does not exist.
    from gilzabots.agent_stdin import _resolve_context_window

    class FakeAgent:
        pass

    assert _resolve_context_window(FakeAgent(), {}, {}) == 0


def test_resolve_context_window_ignores_unusable_values():
    from gilzabots.agent_stdin import _resolve_context_window

    class FakeAgent:
        max_context_tokens = None
        model_kwargs = {"num_ctx": "not-a-number"}

    assert _resolve_context_window(FakeAgent(), {"num_ctx": 0}, {"num_ctx": 12000}) == 12000
