"""Tests for the built-in tutorial chat.

Covers the three contracts the feature rests on:
  1. the guide files are the single source of truth and stay in language parity;
  2. the usage guide reaches the orchestrator's system prompt for the tutorial chat
     only, so no other conversation pays context for it;
  3. the endpoints are idempotent (reopening returns the same chat, not a new welcome)
     and fail loudly on an unknown topic instead of answering the nearest one.
"""

import json
from unittest.mock import AsyncMock

import pytest

from opalatex.ide_server import AsyncHTTPServer
from opalatex.memgpt_runtime import build_chat_orchestrator
from opalatex.onboarding import PILOT_SKILL_NAME, pilot_skill_content
from opalatex.project import ProjectData, ProjectStore, tutorial_chat_id
from opalatex.tutorial import (
    get_topic,
    load_guide,
    load_intro,
    load_topics,
    normalize_lang,
    topic_menu,
    tutorial_system_block,
)


# ---------------------------------------------------------------------------
# 1. The guide files
# ---------------------------------------------------------------------------

def test_normalize_lang_maps_ui_tags_onto_the_two_guides():
    assert normalize_lang("en") == "en"
    assert normalize_lang("en-US") == "en"
    assert normalize_lang("pt") == "pt"
    assert normalize_lang("pt-BR") == "pt"
    # Anything unrecognised falls back to the same default load_ui_settings uses.
    assert normalize_lang("") == "pt"
    assert normalize_lang(None) == "pt"
    assert normalize_lang("de") == "pt"


def test_both_guides_expose_the_same_topics_in_the_same_order():
    en = [t["id"] for t in load_topics("en")]
    pt = [t["id"] for t in load_topics("pt")]
    assert en == pt, "the two guides drifted apart"
    assert len(en) == len(set(en)), f"duplicate topic ids: {en}"


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_every_topic_has_a_question_and_an_answer(lang):
    topics = load_topics(lang)
    assert topics, f"[{lang}] no topics parsed from the guide"
    for topic in topics:
        assert topic["question"].strip(), f"[{lang}] topic '{topic['id']}' has no question"
        assert topic["answer"].strip(), f"[{lang}] topic '{topic['id']}' has no answer"


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_intro_is_present_and_drops_the_file_title(lang):
    intro = load_intro(lang)
    assert intro.strip()
    # The `# OpalaTex …` title is useful in the file but redundant as the first line
    # of a chat message.
    assert not intro.startswith("# ")


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_guide_covers_the_topics_the_tutorial_promises(lang):
    ids = {t["id"] for t in load_topics(lang)}
    assert {
        "providers", "models", "settings", "projects",
        "project-settings", "asset-store", "tools-features",
        "docker-sandbox", "local-models", "local-skills", "cloud-for-big-data",
        "monitors",
    } <= ids


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_docker_sandbox_topic_clarifies_experimental_status(lang):
    """The user requires making it clear that Docker sandbox is experimental."""
    topic = get_topic("docker-sandbox", lang)
    assert topic is not None, f"[{lang}] topic 'docker-sandbox' missing"
    answer = topic["answer"]
    assert "experimental" in answer.lower(), f"[{lang}] 'docker-sandbox' answer must state it is experimental"


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_monitors_topic_clarifies_experimental_status(lang):
    """The user requires making it clear that monitor agents are experimental."""
    topic = get_topic("monitors", lang)
    assert topic is not None, f"[{lang}] topic 'monitors' missing"
    answer = topic["answer"]
    assert "EXPERIMENTAL" in answer, f"[{lang}] 'monitors' answer must state it is experimental"
    # The warning opens the answer, before anything about how to use the feature.
    assert answer.lstrip().startswith(">") and "EXPERIMENTAL" in answer.split("\n\n", 1)[0]
    assert "opalatex monitors" in answer and "register" not in answer.split("\n\n", 1)[0]
    tools = get_topic("tools-features", lang)["answer"]
    assert "(Experimental)" in tools and ("Monitor Agents" in tools or "Agentes Monitores" in tools)


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_asset_store_topic_covers_skills_and_templates(lang):
    answer = get_topic("asset-store", lang)["answer"]
    assert "Skills" in answer or "skills" in answer
    assert "template" in answer.lower()


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_project_settings_covers_empty_response_fallback(lang):
    settings_ans = get_topic("project-settings", lang)["answer"]
    assert "empty_response_reasoning_fallback" in settings_ans
    general_settings_ans = get_topic("settings", lang)["answer"]
    assert "empty_response_reasoning_fallback" in general_settings_ans


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_cloud_topic_names_ollama_cloud_and_openrouter(lang):
    """The user's core ask: steer large-data work at cloud models."""
    answer = get_topic("cloud-for-big-data", lang)["answer"]
    assert "https://ollama.com" in answer
    assert "OpenRouter" in answer
    assert ":cloud" in answer


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_topic_menu_carries_ids_and_questions_only(lang):
    menu = topic_menu(lang)
    assert menu
    for entry in menu:
        assert set(entry) == {"id", "question"}


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_get_topic_returns_none_for_an_unknown_id(lang):
    assert get_topic("no-such-topic", lang) is None
    assert get_topic("", lang) is None


@pytest.mark.parametrize("lang", ["en", "pt"])
def test_system_block_contains_the_whole_guide(lang):
    block = tutorial_system_block(lang)
    assert load_guide(lang).strip() in block
    for topic in load_topics(lang):
        assert topic["question"] in block


# ---------------------------------------------------------------------------
# 2. The guide reaches the orchestrator for the tutorial chat only
# ---------------------------------------------------------------------------

def _project(tmp_path, chat_id):
    return ProjectData(
        name="t", project_name="t",
        project_path=str(tmp_path), model="ollama/proj-model",
        current_chat_id=chat_id,
    )


def test_tutorial_chat_id_is_reserved_and_derived():
    assert tutorial_chat_id("myproj") == "tutorial_myproj"


def test_orchestrator_loads_the_guide_in_the_tutorial_chat(tmp_path):
    agent = build_chat_orchestrator(_project(tmp_path, tutorial_chat_id("t")), None)
    assert "OpalaTex Tutorial (this chat only)" in agent.system_prompt
    assert "cloud-for-big-data ::" in agent.system_prompt


def test_orchestrator_does_not_load_the_guide_in_other_chats(tmp_path):
    agent = build_chat_orchestrator(_project(tmp_path, "main_t"), None)
    assert "OpalaTex Tutorial (this chat only)" not in agent.system_prompt


# ---------------------------------------------------------------------------
# 3. The endpoints
# ---------------------------------------------------------------------------

def _api_harness(tmp_path, monkeypatch):
    """A server whose responses are collected instead of written to a socket."""
    db_path = str(tmp_path / "projects.db")
    store = ProjectStore(db_path=db_path)
    monkeypatch.setattr("opalatex.config.DEFAULT_DB_PATH", db_path)

    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, json.loads(body.decode("utf-8")), content_type))

    server.send_response = mock_send_response
    return store, server, responses


async def _post(server, path, payload):
    await server.route_api(
        "POST", path, {}, {}, json.dumps(payload).encode("utf-8"), AsyncMock()
    )


@pytest.mark.asyncio
async def test_open_creates_the_reserved_chat_and_seeds_the_intro(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))

    await _post(server, "/api/tutorial/open", {"project_name": "myproj", "lang": "en"})

    status_code, data, _ = responses[0]
    assert status_code == 200
    assert data["chat_id"] == "tutorial_myproj"
    assert data["created"] is True
    assert len(data["history"]) == 1
    assert data["history"][0]["role"] == "assistant"
    assert data["history"][0]["content"] == load_intro("en")
    assert [t["id"] for t in data["topics"]] == [t["id"] for t in load_topics("en")]


@pytest.mark.asyncio
async def test_open_is_idempotent_and_does_not_restack_the_intro(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))

    await _post(server, "/api/tutorial/open", {"project_name": "myproj", "lang": "en"})
    await _post(server, "/api/tutorial/open", {"project_name": "myproj", "lang": "en"})

    first, second = responses[0][1], responses[1][1]
    assert first["chat_id"] == second["chat_id"]
    assert second["created"] is False
    assert len(second["history"]) == 1

    chats = store.load("myproj").chats
    assert sum(1 for c in chats if c["id"] == "tutorial_myproj") == 1


@pytest.mark.asyncio
async def test_open_rejects_an_unknown_project(tmp_path, monkeypatch):
    _store, server, responses = _api_harness(tmp_path, monkeypatch)

    await _post(server, "/api/tutorial/open", {"project_name": "nope"})

    assert responses[0][0] == 404


@pytest.mark.asyncio
async def test_answer_persists_the_question_and_the_guide_answer(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))
    await _post(server, "/api/tutorial/open", {"project_name": "myproj", "lang": "en"})

    await _post(server, "/api/tutorial/answer", {
        "project_name": "myproj", "topic_id": "providers", "lang": "en",
    })

    status_code, data, _ = responses[-1]
    assert status_code == 200
    topic = get_topic("providers", "en")
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][0]["content"] == topic["question"]
    assert data["messages"][1]["content"] == topic["answer"]

    # Persisted, not UI-only: it must survive a reload and become model context.
    stored = store.load("myproj", chat_id="tutorial_myproj").history
    assert len(stored) == 3
    assert stored[-1]["content"] == topic["answer"]


@pytest.mark.asyncio
async def test_answer_rejects_an_unknown_topic(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))
    await _post(server, "/api/tutorial/open", {"project_name": "myproj"})

    await _post(server, "/api/tutorial/answer", {
        "project_name": "myproj", "topic_id": "does-not-exist",
    })

    status_code, data, _ = responses[-1]
    assert status_code == 404
    assert "does-not-exist" in data["error"]
    # Nothing was appended in place of the missing topic.
    assert len(store.load("myproj", chat_id="tutorial_myproj").history) == 1


@pytest.mark.asyncio
async def test_answer_before_open_reports_the_missing_chat(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))

    await _post(server, "/api/tutorial/answer", {
        "project_name": "myproj", "topic_id": "overview",
    })

    assert responses[-1][0] == 404


@pytest.mark.asyncio
async def test_chat_list_publishes_the_tutorial_chat_id_only_once_it_exists(tmp_path, monkeypatch):
    """The client must not reconstruct the reserved id to recognise the chat."""
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))

    async def chat_list():
        await server.route_api(
            "GET", "/api/chat/list", {"project_name": ["myproj"]}, {}, b"", AsyncMock()
        )
        return responses[-1][1]

    assert (await chat_list())["tutorial_chat_id"] == ""

    await _post(server, "/api/tutorial/open", {"project_name": "myproj"})

    assert (await chat_list())["tutorial_chat_id"] == "tutorial_myproj"


@pytest.mark.asyncio
async def test_topics_endpoint_serves_the_menu_alone(tmp_path, monkeypatch):
    _store, server, responses = _api_harness(tmp_path, monkeypatch)

    await server.route_api(
        "GET", "/api/tutorial/topics", {"lang": ["en"]}, {}, b"", AsyncMock()
    )

    status_code, data, _ = responses[-1]
    assert status_code == 200
    assert [t["id"] for t in data["topics"]] == [t["id"] for t in load_topics("en")]
    assert "history" not in data


@pytest.mark.asyncio
async def test_tutorial_history_is_readable_through_the_normal_chat_endpoint(tmp_path, monkeypatch):
    store, server, responses = _api_harness(tmp_path, monkeypatch)
    store.create("myproj", "auto", "", project_path=str(tmp_path / "project"))
    await _post(server, "/api/tutorial/open", {"project_name": "myproj", "lang": "en"})

    await server.route_api(
        "GET",
        "/api/chat/history",
        {"project_name": ["myproj"], "chat_id": ["tutorial_myproj"]},
        {},
        b"",
        AsyncMock(),
    )

    status_code, data, _ = responses[-1]
    assert status_code == 200
    assert data["chat_id"] == "tutorial_myproj"
    assert data["history"][0]["content"] == load_intro("en")


# ---------------------------------------------------------------------------
# 4. The pilot-project skill is generated from the same guide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "pt"])
def test_pilot_skill_embeds_the_shared_guide(lang):
    content = pilot_skill_content(lang)
    assert content.startswith("---\n")
    assert f"name: {PILOT_SKILL_NAME}\n" in content
    assert load_guide(lang).strip() in content


def test_pilot_skill_no_longer_advertises_unimplemented_commands():
    """The hardcoded blobs it replaced promised `/goal` and `/grill-me`."""
    for lang in ("en", "pt"):
        content = pilot_skill_content(lang)
        assert "/goal" not in content
        assert "/grill-me" not in content
