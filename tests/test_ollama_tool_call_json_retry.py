"""A tool call Ollama refuses as invalid JSON is retried, in workers too.

Ollama parses tool-call arguments server-side: a model that writes a raw newline
inside a JSON string (a LaTeX file passed to write_file, say) makes the request
fail instead of producing a tool call. MemGPTAgentBlock already answered that
with a bounded system alert; LLMAgentBlock -- every skill worker -- let the
provider error end the worker on the spot.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.agent import AgentInput, LLMAgentBlock
from agenticblocks.core.function_block import as_tool
from agenticblocks.utils.tool_calls import (
    OLLAMA_TOOL_CALL_JSON_ALERT,
    is_ollama_tool_call_json_error,
)

# Verbatim from a recorded session (llama-server-backed Ollama build).
LLAMA_SERVER_REJECTION = (
    "litellm.BadRequestError: Ollama_chatException - KeyError: 'message', Got unexpected "
    "response from Ollama: {'error': 'llama-server returned invalid tool call arguments "
    "for \"write_file\": invalid character '\\n' in string literal'}"
)


def _response(content=""):
    message = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


@as_tool
def write_file(path: str, content: str) -> str:
    """Write a file."""
    return "ok"


def _worker(**kwargs):
    return LLMAgentBlock(
        name="skill_command-line",
        model="ollama_chat/test",
        system_prompt="worker",
        tools=[write_file],
        max_tool_calls=5,
        **kwargs,
    )


@pytest.mark.parametrize("text", [
    LLAMA_SERVER_REJECTION,
    "Ollama_chatException - error parsing tool call: invalid character ',' in string escape code",
    "GILZABOTS_OLLAMA_TOOL_CALL_JSON_ESCAPE: Ollama rejected a model tool-call argument",
])
def test_every_known_ollama_wording_is_recognized(text):
    assert is_ollama_tool_call_json_error(Exception(text))


def test_unrelated_ollama_errors_are_not_tool_call_json_errors():
    assert not is_ollama_tool_call_json_error(
        Exception("Ollama_chatException - model 'x' not found, try pulling it first")
    )
    assert not is_ollama_tool_call_json_error(Exception("OllamaException - Internal Server Error"))


def test_a_worker_retries_a_rejected_tool_call_with_a_system_alert():
    agent = _worker()
    sent = []

    async def fake_acompletion(messages, **_kw):
        sent.append([dict(m) for m in messages])
        if len(sent) == 1:
            raise Exception(LLAMA_SERVER_REJECTION)
        return _response(content="File written.")

    agent._acompletion = fake_acompletion
    out = asyncio.run(agent.run(AgentInput(prompt="write lista.tex")))

    assert out.response == "File written."
    assert len(sent) == 2
    assert sent[1][-1] == {"role": "system", "content": OLLAMA_TOOL_CALL_JSON_ALERT}


def test_the_worker_retry_is_bounded_and_then_raises_the_provider_error():
    agent = _worker(max_tool_call_json_retries=2)
    calls = []

    async def always_rejected(messages, **_kw):
        calls.append(1)
        raise Exception(LLAMA_SERVER_REJECTION)

    agent._acompletion = always_rejected
    with pytest.raises(Exception, match="invalid tool call arguments"):
        asyncio.run(agent.run(AgentInput(prompt="write lista.tex")))
    # One original request plus two retries, then the error surfaces unchanged.
    assert len(calls) == 3


def test_other_provider_errors_are_not_retried():
    agent = _worker()
    calls = []

    async def failing(messages, **_kw):
        calls.append(1)
        raise Exception("OllamaException - Internal Server Error")

    agent._acompletion = failing
    with pytest.raises(Exception, match="Internal Server Error"):
        asyncio.run(agent.run(AgentInput(prompt="write lista.tex")))
    assert len(calls) == 1
