"""Persistent project instructions reach each provider request without editing history."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agenticblocks.blocks.llm.agent import LLMAgentBlock
from agenticblocks.blocks.llm.memgpt_agent import MemGPTAgentBlock
from agenticblocks.utils.messages import prepend_user_prompt
from test_update_project_git_root import api, _update


@pytest.mark.parametrize('agent_class', [LLMAgentBlock, MemGPTAgentBlock])
@pytest.mark.asyncio
async def test_each_provider_call_retains_prefix_without_mutating_history(agent_class, monkeypatch):
    provider = AsyncMock(return_value=SimpleNamespace(choices=[]))
    monkeypatch.setattr('litellm.acompletion', provider)
    agent = agent_class(name='test', model='test/model', use_shared_router=False,
                        user_prompt_prefix='  Keep citations.\nUse English.  ')
    history = [
        {'role': 'system', 'content': 'System'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Inspect'},
                                   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': 'Result'},
    ]
    original = copy.deepcopy(history)
    for _ in range(2):
        await agent._acompletion(history)
        sent = provider.call_args.kwargs['messages']
        assert sent[1]['content'][0]['text'] == agent.user_prompt_prefix + '\n\n'
        assert sent[1]['content'][1:] == original[1]['content']
        assert sent[2:] == original[2:]
        assert history == original
    await agent._acompletion([{'role': 'system', 'content': 'Compacted context'}])
    assert provider.call_args.kwargs['messages'][1] == {'role': 'user', 'content': agent.user_prompt_prefix}


def test_empty_and_text_prefix():
    messages = [{'role': 'user', 'content': 'Question'}]
    assert prepend_user_prompt(messages, '') == messages
    assert prepend_user_prompt(messages, 'Rules')[0]['content'] == 'Rules\n\nQuestion'
    assert messages[0]['content'] == 'Question'


@pytest.mark.asyncio
async def test_project_prefix_save_reload_clear_and_validation(api):
    assert api.store.load('myproj').user_prompt_prefix == ''
    prefix = '  Preserve citations.\n\nKeep equations intact.  '
    status, body = await _update(api, {'project_name': 'myproj', 'user_prompt_prefix': prefix})
    assert status == 200
    assert body['user_prompt_prefix'] == prefix
    assert api.store.load('myproj').user_prompt_prefix == prefix
    assert api.store.list_projects()[0]['user_prompt_prefix'] == prefix
    await _update(api, {'project_name': 'myproj', 'description': 'Changed'})
    assert api.store.load('myproj').user_prompt_prefix == prefix
    for value in (None, 1, [], {}):
        status, _ = await _update(api, {'project_name': 'myproj', 'user_prompt_prefix': value})
        assert status == 400
        assert api.store.load('myproj').user_prompt_prefix == prefix
    status, _ = await _update(api, {'project_name': 'myproj', 'user_prompt_prefix': ''})
    assert status == 200
    assert api.store.load('myproj').user_prompt_prefix == ''


def test_single_shot_transforms_never_receive_the_prefix():
    """The prefix instructs the chat agent; translation and prompt evolution are transforms.

    ``prepend_user_prompt`` fuses the prefix into the same user message as the
    payload, so a translator that received it translated it and showed it to
    the user as the translation, and prompt evolution rewrote it into the
    refined prompt. Neither call site takes the parameter any more.
    """
    import inspect
    from gilzabots.ide_server import _execute_prompt_evolution
    from gilzabots.translation import execute_translation

    for func in (execute_translation, _execute_prompt_evolution):
        assert 'user_prompt_prefix' not in inspect.signature(func).parameters


def test_existing_database_migrates_without_losing_project(api):
    import sqlite3
    from gilzabots.project import ProjectStore
    with sqlite3.connect(api.store.db_path) as connection:
        connection.execute('ALTER TABLE projects DROP COLUMN user_prompt_prefix')
    reopened = ProjectStore(db_path=api.store.db_path)
    assert reopened.load('myproj').user_prompt_prefix == ''
    assert reopened.load('myproj').description == 'before'
    assert reopened.list_projects()[0]['user_prompt_prefix'] == ''
