"""Tests for the excerpt "Look up" query (gilzabots/snippet_query.py).

Covers the project-bound read-only reader, the guard rails and prompt of
``execute_snippet_query``, a full agent run with tool calls against a scripted
completion, and the ``/api/snippet-query`` endpoint.
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from gilzabots.ide_server import AsyncHTTPServer
from gilzabots.snippet_query import (
    MAX_QUERY_EXCERPT_CHARS,
    MAX_QUERY_QUESTION_CHARS,
    ProjectReader,
    SnippetQuerySource,
    build_snippet_query_prompt,
    build_snippet_query_system_prompt,
    execute_snippet_query,
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "chapters").mkdir(parents=True)
    (root / "main.tex").write_text("\\section{Intro}\nThe Amazon is a tropical rainforest.\n", encoding="utf-8")
    (root / "chapters" / "biomes.md").write_text("# Biomes\n\nRainforests are dense.\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "main.aux").write_text("aux", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return root


def _pdf(path, pages):
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


# ── The reader ───────────────────────────────────────────────────────────────

def test_the_reader_refuses_paths_outside_the_project(project, tmp_path):
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    reader = ProjectReader(str(project))

    with pytest.raises(ValueError, match="outside the project"):
        reader.read_file("../secret.txt")
    with pytest.raises(ValueError, match="outside the project"):
        reader.read_file(str(tmp_path / "secret.txt"))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_cannot_lead_out_of_the_project(project, tmp_path):
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    os.symlink(tmp_path / "secret.txt", project / "link.txt")
    reader = ProjectReader(str(project))

    with pytest.raises(ValueError, match="outside the project"):
        reader.read_file("link.txt")


def test_the_reader_lists_project_files_without_hidden_dirs_or_build_artifacts(project):
    listing = ProjectReader(str(project)).list_files()

    assert "main.tex" in listing
    assert "chapters/biomes.md" in listing
    assert ".git" not in listing
    assert "main.aux" not in listing


def test_reads_are_paged_and_say_where_to_continue(project):
    (project / "long.txt").write_text("a" * 50, encoding="utf-8")
    reader = ProjectReader(str(project))

    first = reader.read_file("long.txt", offset=0, limit=20)
    assert first.startswith("[long.txt: characters 0-20 of 50. Continue with offset=20.]")
    assert first.endswith("a" * 20)
    last = reader.read_file("long.txt", offset=40, limit=20)
    assert "End of file." in last


def test_a_binary_file_is_refused_rather_than_decoded(project):
    with pytest.raises(ValueError, match="binary"):
        ProjectReader(str(project)).read_file("blob.bin")


def test_a_missing_file_names_only_the_lookup_tools(project):
    with pytest.raises(ValueError) as excinfo:
        ProjectReader(str(project)).read_file("nope.tex")
    message = str(excinfo.value)
    assert "list_project_files" in message
    for foreign in ("read_file(", "run_command", "run_skill", "write_file"):
        assert foreign not in message


def test_a_pdf_is_read_with_page_markers(project):
    _pdf(project / "paper.pdf", ["First page text", "Second page text"])
    text = ProjectReader(str(project)).read_file("paper.pdf", limit=12000)

    assert "[Page 1/2]" in text and "First page text" in text
    assert "[Page 2/2]" in text and "Second page text" in text


def test_search_reports_lines_and_pdf_pages(project):
    _pdf(project / "paper.pdf", ["nothing here", "the rainforest canopy"])
    result = ProjectReader(str(project)).search("rainforest")

    assert "main.tex:2:" in result
    assert "chapters/biomes.md:3:" in result
    assert "paper.pdf (page 2): the rainforest canopy" in result


def test_search_requires_a_query(project):
    with pytest.raises(ValueError, match="query"):
        ProjectReader(str(project)).search("  ")


def test_the_toolset_is_read_only(project):
    names = [tool.name for tool in ProjectReader(str(project)).tools()]
    assert names == ["list_project_files", "read_project_file", "search_project"]


# ── Prompt and guard rails ───────────────────────────────────────────────────

def test_the_prompt_carries_the_locator_excerpt_and_question():
    prompt = build_snippet_query_prompt(
        "The Amazon is the largest forest.",
        "What kind of forest is it?",
        SnippetQuerySource(surface="pdf", document_path="main.pdf", source_file="main.tex", page=3, total_pages=10),
    )
    assert "Document: main.pdf" in prompt
    assert "LaTeX source open in the editor: main.tex" in prompt
    assert "Page: 3 of 10" in prompt
    assert "<excerpt>\nThe Amazon is the largest forest.\n</excerpt>" in prompt
    assert prompt.endswith("Question:\nWhat kind of forest is it?")


def test_a_markdown_source_is_located_by_section():
    prompt = build_snippet_query_prompt(
        "x", "y", SnippetQuerySource(surface="markdown", document_path="notes.md", heading="Biomes"),
    )
    assert "Section: Biomes" in prompt
    assert "Page:" not in prompt


def test_the_system_prompt_names_the_tools_only_when_they_exist():
    assert "read_project_file" in build_snippet_query_system_prompt(True)
    assert "read_project_file" not in build_snippet_query_system_prompt(False)
    assert "language the question is written in" in build_snippet_query_system_prompt(False)


@pytest.mark.asyncio
@pytest.mark.parametrize("excerpt,question,match", [
    ("  ", "why?", "non-empty excerpt"),
    ("text", "  ", "requires a question"),
    ("x" * (MAX_QUERY_EXCERPT_CHARS + 1), "why?", "too long to look up"),
    ("text", "q" * (MAX_QUERY_QUESTION_CHARS + 1), "question is too long"),
])
async def test_bad_input_is_refused_not_truncated(excerpt, question, match):
    with pytest.raises(ValueError, match=match):
        await execute_snippet_query(excerpt, question)


def _stub_agent(monkeypatch, *, final_text, truncated=False, budget_exhausted=False):
    import agenticblocks.blocks.llm.agent as agent_mod

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run(self, agent_input):
            captured["prompt"] = agent_input.prompt
            return SimpleNamespace(
                response="Let me check.\n\n" + final_text,
                final_text=final_text,
                truncated=truncated,
                budget_exhausted=budget_exhausted,
            )

    monkeypatch.setattr(agent_mod, "LLMAgentBlock", FakeAgent)
    monkeypatch.setattr("gilzabots.litellm_compat.wrap_agent_litellm_compat", lambda agent: agent)
    return captured


@pytest.mark.asyncio
async def test_the_answer_is_the_final_text_without_the_progress_beside_tool_calls(monkeypatch, project):
    captured = _stub_agent(monkeypatch, final_text="A **tropical rainforest**.")

    result = await execute_snippet_query("The Amazon", "What kind?", project_path=str(project))

    assert result.answer == "A **tropical rainforest**."
    assert result.truncated is False
    assert [tool.name for tool in captured["tools"]] == [
        "list_project_files", "read_project_file", "search_project",
    ]
    assert "user_prompt_prefix" not in captured
    assert "response_schema" not in captured


@pytest.mark.asyncio
async def test_without_a_project_the_agent_gets_no_tools(monkeypatch):
    captured = _stub_agent(monkeypatch, final_text="Answer.")

    await execute_snippet_query("The Amazon", "What kind?")

    assert captured["tools"] == []
    assert "read_project_file" not in captured["system_prompt"]


@pytest.mark.asyncio
async def test_a_truncated_answer_is_reported_as_such(monkeypatch):
    _stub_agent(monkeypatch, final_text="The Amazon is a", truncated=True)

    result = await execute_snippet_query("The Amazon", "What kind?")

    assert result.truncated is True


@pytest.mark.asyncio
async def test_reasoning_only_is_reported_not_shown(monkeypatch):
    _stub_agent(monkeypatch, final_text="<think>pondering</think>")

    with pytest.raises(ValueError, match="reasoning only"):
        await execute_snippet_query("The Amazon", "What kind?")


@pytest.mark.asyncio
async def test_an_ollama_model_runs_on_the_native_chat_route(monkeypatch, project):
    """Regression: the "answer" was a tool call printed as text.

    Sent as `ollama/<name>`, LiteLLM used Ollama's /api/generate path, which
    flattens the history into a prompt with each earlier call written as
    "Tool Calls: [...]"; glm-5.3-flash then answered in that same text. The
    look-up must take the route the orchestrator and the workers take.
    """
    captured = _stub_agent(monkeypatch, final_text="Answer.")

    await execute_snippet_query("x", "y", project_path=str(project), model="ollama/glm-test:cloud")

    assert captured["model"] == "ollama_chat/glm-test:cloud"


@pytest.mark.asyncio
async def test_the_forced_final_call_is_told_the_budget_is_spent(monkeypatch, project):
    from gilzabots.snippet_query import SNIPPET_QUERY_FINAL_ANSWER_PROMPT

    captured = _stub_agent(monkeypatch, final_text="Answer.")

    await execute_snippet_query("x", "y", project_path=str(project))

    assert captured["final_answer_prompt"] == SNIPPET_QUERY_FINAL_ANSWER_PROMPT
    assert "no tools are available" in SNIPPET_QUERY_FINAL_ANSWER_PROMPT


@pytest.mark.asyncio
async def test_a_spent_budget_without_an_answer_says_so(monkeypatch, project):
    _stub_agent(monkeypatch, final_text="", budget_exhausted=True)

    with pytest.raises(ValueError, match="research budget"):
        await execute_snippet_query("x", "y", project_path=str(project))


@pytest.mark.asyncio
async def test_an_invalid_project_path_is_an_error(monkeypatch, tmp_path):
    _stub_agent(monkeypatch, final_text="Answer.")

    with pytest.raises(ValueError, match="Project directory not found"):
        await execute_snippet_query("x", "y", project_path=str(tmp_path / "missing"))


# ── A real agent run against a scripted completion ───────────────────────────

def _completion(content="", tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=None)


def _call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_the_agent_reads_the_project_and_answers(monkeypatch, project):
    import agenticblocks.blocks.llm.agent as agent_mod

    sent = []
    script = [
        _completion("Checking the source.", [_call("c1", "read_project_file", {"path": "main.tex"})]),
        _completion("It is a **tropical rainforest** (main.tex)."),
    ]

    async def fake_acompletion(self, messages, **kwargs):
        sent.append({"messages": [dict(m) for m in messages], "tools": kwargs.get("tools")})
        return script[len(sent) - 1]

    monkeypatch.setattr(agent_mod.LLMAgentBlock, "_acompletion", fake_acompletion)

    result = asyncio.run(execute_snippet_query(
        "The Amazon", "What kind of forest?", project_path=str(project),
    ))

    assert result.answer == "It is a **tropical rainforest** (main.tex)."
    assert result.files_read == ["main.tex"]
    tool_result = next(m for m in sent[1]["messages"] if m["role"] == "tool")
    assert "tropical rainforest" in tool_result["content"]
    declared = {schema["function"]["name"] for schema in sent[0]["tools"]}
    assert declared == {"list_project_files", "read_project_file", "search_project"}


def test_the_agent_run_reports_a_cut_off_answer(monkeypatch):
    import agenticblocks.blocks.llm.agent as agent_mod

    async def fake_acompletion(self, messages, **kwargs):
        return _completion("The Amazon is a", finish_reason="length")

    monkeypatch.setattr(agent_mod.LLMAgentBlock, "_acompletion", fake_acompletion)

    result = asyncio.run(execute_snippet_query("The Amazon", "What kind?"))

    assert result.answer == "The Amazon is a"
    assert result.truncated is True


# ── Endpoint ─────────────────────────────────────────────────────────────────

def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8"))))

    server.send_response = mock_send_response
    return server, responses


async def _post(server, payload):
    from unittest.mock import AsyncMock

    await server.route_api(
        "POST", "/api/snippet-query", {}, {}, json.dumps(payload).encode("utf-8"), AsyncMock(),
    )


@pytest.mark.asyncio
async def test_endpoint_requires_an_excerpt_and_a_question():
    server, responses = _server_with_capture()

    await _post(server, {"excerpt": "x", "question": " "})

    assert responses[0][0] == 400


@pytest.mark.asyncio
async def test_endpoint_returns_the_answer_and_passes_the_source(monkeypatch, project):
    captured = {}

    async def fake_execute(excerpt, question, *, source, project_path, model):
        from gilzabots.snippet_query import SnippetQueryResult

        captured.update(excerpt=excerpt, question=question, source=source,
                        project_path=project_path, model=model)
        return SnippetQueryResult(answer="Answer.", truncated=True, files_read=["main.tex"])

    monkeypatch.setattr("gilzabots.snippet_query.execute_snippet_query", fake_execute)
    server, responses = _server_with_capture()

    await _post(server, {
        "excerpt": "The Amazon",
        "question": "What kind?",
        "projectPath": str(project),
        "model": "ollama/test",
        "source": {"surface": "pdf", "documentPath": "main.pdf", "page": 2, "totalPages": 9},
    })

    status, body = responses[0]
    assert status == 200
    assert body == {"success": True, "answer": "Answer.", "truncated": True, "files_read": ["main.tex"]}
    assert captured["source"].page == 2 and captured["source"].document_path == "main.pdf"
    assert captured["project_path"] == str(project)
    assert captured["model"] == "ollama/test"


@pytest.mark.asyncio
async def test_endpoint_reports_a_failed_query_as_400(monkeypatch):
    async def fake_execute(*_args, **_kwargs):
        raise ValueError("The model returned an empty answer.")

    monkeypatch.setattr("gilzabots.snippet_query.execute_snippet_query", fake_execute)
    server, responses = _server_with_capture()

    await _post(server, {"excerpt": "x", "question": "y"})

    assert responses[0] == (400, {"error": "The model returned an empty answer."})
