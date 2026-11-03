import asyncio
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gilzabots.memgpt_runtime import build_chat_orchestrator
from gilzabots.project import ProjectData, ProjectStore
from agenticblocks.blocks.llm.agent import AgentInput

@pytest.mark.integration
@pytest.mark.skip(reason="Manual context-inspection probe; it prints LLM messages and has no automated assertions.")
async def test_context_probe():
    project = ProjectData(
        name="test_proj",
        project_path=os.getcwd(),
        project_name="TestProject",
        model="gemini-2.5-flash",  
        worker_model="gemini-2.5-flash",
    )
    store = ProjectStore(":memory:")
    orchestrator = build_chat_orchestrator(project, store)
    
    # Mock the history as if the user asked for tic-tac-toe and the orchestrator delegated it
    orchestrator.internal_history = [
        {"role": "user", "content": "Implemente o jogo da velha em tictactoe.html, arquivo único, em html e javascript."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "run_skill", "arguments": '{"skill_name": "command-line", "context": "Implemente"}'}}]},
        {"role": "tool", "tool_call_id": "call_123", "name": "run_skill", "content": "[skill 'command-line' finished] Worker's summary/report:\nO arquivo tictactoe.html já foi criado na raiz do projeto em uma tentativa anterior. Vou verificar e garantir que ele existe com o conteúdo correto."}
    ]
    
    # Intercept _acompletion
    orig_acompletion = orchestrator._acompletion
    async def fake_acompletion(messages, **kwargs):
        print("\n\n" + "="*80)
        print("MESSAGES SENT TO LLM:")
        for i, m in enumerate(messages):
            role = m.get("role")
            content = m.get("content", "")
            if isinstance(content, str):
                content = content[:200].replace("\n", " ") + ("..." if len(content)>200 else "")
            tc = m.get("tool_calls", [])
            print(f"[{i}] {role.upper()}: {content} | TOOLS: {tc}")
        print("="*80 + "\n\n")
        # We don't actually need to run the LLM for this test, just throw to stop it
        raise Exception("Intercepted!")
        
    orchestrator._acompletion = fake_acompletion

    print("--- INICIANDO TESTE DE CONTEXTO ---")
    try:
        await orchestrator.run(AgentInput(prompt=None)) # Don't add a new prompt, just run the loop
    except Exception as e:
        print(f"Teste finalizado: {e}")

if __name__ == "__main__":
    asyncio.run(test())


# ---------------------------------------------------------------------------
# Request assembly is a public capability of the block
# ---------------------------------------------------------------------------

def test_build_request_messages_is_what_the_run_loop_sends():
    """The main context: system prompt, recursive summary, then the history.

    `run` assembles its request from this method, so a caller that measures how
    full the window is measures the same list the model receives.
    """
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    agent = MemGPTAgentBlock(
        name="probe", system_prompt="SYSTEM PROMPT", model="fake/model",
    )
    agent.internal_history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    agent.recursive_summary = "Older turns were about LaTeX."

    messages = agent.build_request_messages()

    assert [m["role"] for m in messages] == ["system", "system", "user", "assistant"]
    assert messages[0]["content"].startswith("SYSTEM PROMPT")
    assert "Older turns were about LaTeX." in messages[1]["content"]
    assert messages[2:] == agent.internal_history


def test_count_context_tokens_grows_with_the_history():
    from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock

    agent = MemGPTAgentBlock(name="probe", system_prompt="SYSTEM", model="fake/model")
    empty = agent.count_context_tokens()

    agent.internal_history = [{"role": "user", "content": "word " * 500}]
    filled = agent.count_context_tokens()

    assert empty > 0
    assert filled > empty + 400
