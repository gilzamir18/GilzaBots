"""Unit tests for the ask_question tool and its integration across agents and skills."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from opalatex.tools import ask_question, ask_human, get_helper_tools
from opalatex.agent_stdin import ALL_TOOLS_MAP
from opalatex.memgpt_runtime import build_chat_orchestrator
from opalatex.project import ProjectData
from opalatex.skills import parse_skill_md, find_skill_dir


@pytest.mark.asyncio
async def test_ask_question_tool_execution():
    """Test ask_question async invocation and return string format with options."""
    raw = getattr(ask_question, "_func", None) or ask_question
    with patch("opalatex.terminal.aask", new=AsyncMock(return_value="timestamp, level, service")) as mock_aask:
        res = await raw("Which fields to extract?", options=["Option 1", "Option 2"])
        assert res == "User response: timestamp, level, service"
        mock_aask.assert_called_once_with("Which fields to extract?", options=["Option 1", "Option 2"], is_multi_select=False)

    # Test empty / cancelled response
    with patch("opalatex.terminal.aask", new=AsyncMock(return_value="")):
        res = await raw("Which fields to extract?")
        assert "did not provide an answer or cancelled" in res


@pytest.mark.asyncio
async def test_ask_question_forwards_multi_select_and_returns_json_array():
    raw = getattr(ask_question, "_func", None) or ask_question
    response = '["Title", "Author"]'
    with patch("opalatex.terminal.aask", new=AsyncMock(return_value=response)) as mock_aask:
        result = await raw(
            "Which fields should be included?",
            options=["Title", "Date", "Author"],
            is_multi_select=True,
        )

    assert result == f"User response: {response}"
    mock_aask.assert_called_once_with(
        "Which fields should be included?",
        options=["Title", "Date", "Author"],
        is_multi_select=True,
    )


def test_ask_question_schema_explains_multi_select_contract():
    from agenticblocks.tools.a2a_bridge import block_to_tool_schema

    schema = block_to_tool_schema(ask_question)["function"]

    assert schema["parameters"]["properties"]["is_multi_select"]["type"] == "boolean"
    assert "Set 'is_multi_select' to true" in schema["description"]
    assert "JSON array" in schema["description"]


def test_ask_human_alias():
    """ask_human must alias ask_question for backward compatibility."""
    assert ask_human == ask_question


def test_ask_question_in_available_tools():
    """get_helper_tools must include ask_question (and ask_human) for helpers."""
    tools = get_helper_tools()
    assert ask_question in tools
    assert ask_human in tools
    tool_names = [getattr(t, "name", getattr(t, "__name__", str(t))) for t in tools]
    assert "ask_question" in tool_names



def test_ask_question_in_all_tools_map():
    """ALL_TOOLS_MAP must contain ask_question."""
    assert "ask_question" in ALL_TOOLS_MAP
    assert ALL_TOOLS_MAP["ask_question"] == ask_question


def test_chat_orchestrator_exposes_ask_question(tmp_path):
    """Chat orchestrator must expose ask_question tool and document it in prompt."""
    project = ProjectData(
        name="test_proj",
        project_name="test_proj",
        project_path=str(tmp_path),
        model="ollama/proj-model",
    )
    orch = build_chat_orchestrator(project, None)
    tool_names = {getattr(t, "name", None) for t in orch.tools}
    assert "ask_question" in tool_names
    assert "ask_question" in orch.system_prompt


def test_log_table_condenser_skill_prompt_mentions_ask_question(tmp_path):
    """log-table-condenser SKILL.md must instruct the agent to use ask_question."""
    skill_dir = find_skill_dir("log-table-condenser", str(tmp_path))
    assert skill_dir is not None
    meta = parse_skill_md(skill_dir)
    assert meta is not None
    assert "ask_question" in meta["body"]
    assert "CAPTURE USER INFORMATION BEFORE CONDENSING OR ANALYZING" in meta["body"]
