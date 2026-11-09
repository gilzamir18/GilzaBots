"""Tests for the web_search feature (tool + config module)."""

import asyncio
import json
import pytest
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _fresh_ddgs_throttle(monkeypatch):
    """Each test starts with no shared DDGS client and no inter-search delay."""
    from gilzabots import web_search_config as wsc
    monkeypatch.setattr(wsc, "_DDGS_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(wsc, "_ddgs_state", {"cls": None, "client": None, "last_finished": 0.0})


def _ddgs_patch_target() -> str:
    try:
        import ddgs  # noqa: F401
        return "ddgs.DDGS"
    except ImportError:
        return "duckduckgo_search.DDGS"


def _raw_web_search():
    """The undecorated web_search function (gilzabots_tool wraps it with functools.wraps)."""
    from gilzabots import tools as tools_mod
    return tools_mod.web_search._func.__wrapped__


# ─── Config module tests ───────────────────────────────────────────────────────

def test_load_config_defaults(tmp_path, monkeypatch):
    """If no config file exists, defaults are returned."""
    from gilzabots import web_search_config
    importlib_reload(web_search_config)

    cfg_path = tmp_path / "web_search.json"
    monkeypatch.setattr(web_search_config, "_CONFIG_PATH", cfg_path)

    from gilzabots.web_search_config import load_config
    cfg = load_config()
    assert cfg["enabled"] is True
    assert cfg["mcp_url"] == ""
    assert cfg["mcp_tool"] == "web_search"


def test_save_and_load_config(tmp_path, monkeypatch):
    """Round-trip: save → load produces the same values."""
    cfg_path = tmp_path / "web_search.json"
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    from gilzabots.web_search_config import save_config, load_config

    save_config({
        "enabled": False,
        "mcp_url": "http://localhost:9999/mcp",
        "mcp_tool": "search",
        "mcp_api_key": "some-api-key",
        "provider": "mcp"
    })
    cfg = load_config()
    assert cfg["enabled"] is False
    assert cfg["mcp_url"] == "http://localhost:9999/mcp"
    assert cfg["mcp_tool"] == "search"
    assert cfg["mcp_api_key"] == "some-api-key"
    assert cfg["provider"] == "mcp"


def test_is_enabled_respects_flag(tmp_path, monkeypatch):
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({"enabled": False, "mcp_url": "", "mcp_tool": "web_search"}))
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    from gilzabots.web_search_config import is_enabled
    assert is_enabled() is False


# ─── DuckDuckGo backend ───────────────────────────────────────────────────────

def test_ddg_search_returns_results():
    """DuckDuckGo search returns non-empty string (mocked)."""
    fake_results = [
        {"title": "Python 3.13 Released", "href": "https://python.org/news", "body": "Python 3.13 is out."},
    ]
    patch_target = "duckduckgo_search.DDGS"
    try:
        import ddgs
        patch_target = "ddgs.DDGS"
    except ImportError:
        pass

    with patch(patch_target) as MockDDGS:
        MockDDGS.return_value.text.return_value = fake_results
        MockDDGS.return_value.__enter__.return_value.text.return_value = fake_results
        from gilzabots.web_search_config import _ddg_search
        result = _ddg_search("Python 3.13", max_results=1)
    assert "Python 3.13 Released" in result
    assert "https://python.org/news" in result


def test_ddg_search_no_results():
    patch_target = "duckduckgo_search.DDGS"
    try:
        import ddgs
        patch_target = "ddgs.DDGS"
    except ImportError:
        pass

    with patch(patch_target) as MockDDGS:
        MockDDGS.return_value.text.return_value = []
        MockDDGS.return_value.__enter__.return_value.text.return_value = []
        from gilzabots.web_search_config import _ddg_search
        result = _ddg_search("xyzzy_nonexistent_query_12345", max_results=1)
    assert result.startswith("[web_search] No results for: xyzzy_nonexistent_query_12345")
    assert "429" in result


def test_ddg_search_reports_ddgs_no_results_as_possible_throttling():
    """ddgs raises 'No results found.' when every engine came back empty -- which
    includes engines that answered 429/202/captcha -- so the message must say so
    and must not blame DuckDuckGo alone."""
    from ddgs.exceptions import DDGSException
    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.side_effect = DDGSException("No results found.")
        from gilzabots.web_search_config import _ddg_search
        result = _ddg_search("Quem foi Machado de Assis", max_results=5)
    assert result.startswith("[web_search] No results for: Quem foi Machado de Assis")
    assert "throttling" in result
    assert "DuckDuckGo search failed" not in result


def test_ddg_search_timeout_names_last_engine_only():
    from ddgs.exceptions import TimeoutException
    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.side_effect = TimeoutException("https://www.mojeek.com/search operation timed out")
        from gilzabots.web_search_config import _ddg_search
        result = _ddg_search("UVA Sobral", max_results=5)
    assert result.startswith("[web_search] Search timed out for: UVA Sobral")
    assert "mojeek.com" in result
    assert "only the last engine" in result


def test_ddg_search_other_errors_name_the_exception_type():
    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.side_effect = ValueError("boom")
        from gilzabots.web_search_config import _ddg_search
        result = _ddg_search("q", max_results=1)
    assert result == "[web_search] ddgs metasearch failed (ValueError): boom"


def test_ddg_search_reuses_one_client():
    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.return_value = [{"title": "T", "href": "https://e.com", "body": "b"}]
        from gilzabots.web_search_config import _ddg_search
        _ddg_search("a", max_results=1)
        _ddg_search("b", max_results=1)
    assert MockDDGS.call_count == 1
    assert MockDDGS.return_value.text.call_count == 2


def test_ddg_searches_never_overlap():
    """Parallel web_search calls must reach the engines one at a time."""
    import threading
    import time
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    def fake_text(query, max_results):
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with guard:
            active["now"] -= 1
        return [{"title": query, "href": "https://e.com", "body": "b"}]

    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.side_effect = fake_text
        from gilzabots.web_search_config import _ddg_search
        results = []
        threads = [threading.Thread(target=lambda q=q: results.append(_ddg_search(q, 1))) for q in "abcd"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert active["max"] == 1
    assert len(results) == 4 and all(r.startswith("Web search results for:") for r in results)


def test_ddg_searches_are_spaced_by_min_interval(monkeypatch):
    import time
    from gilzabots import web_search_config as wsc
    monkeypatch.setattr(wsc, "_DDGS_MIN_INTERVAL_S", 0.3)
    starts = []

    def fake_text(query, max_results):
        starts.append(time.perf_counter())
        return [{"title": query, "href": "https://e.com", "body": "b"}]

    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.side_effect = fake_text
        wsc._ddg_search("a", 1)
        wsc._ddg_search("b", 1)
    assert starts[1] - starts[0] >= 0.3


def test_ddg_search_gives_up_waiting_for_earlier_searches(monkeypatch):
    from gilzabots import web_search_config as wsc
    monkeypatch.setattr(wsc, "_DDGS_QUEUE_TIMEOUT_S", 0.05)
    with patch(_ddgs_patch_target()) as MockDDGS:
        assert wsc._DDGS_LOCK.acquire(timeout=1)
        try:
            result = wsc._ddg_search("q", 1)
        finally:
            wsc._DDGS_LOCK.release()
    assert result.startswith("[web_search] Not run: earlier web searches were still running")
    MockDDGS.return_value.text.assert_not_called()


# ─── MCP backend ──────────────────────────────────────────────────────────────

def test_mcp_search_success():
    """MCP search parses JSON-RPC response correctly."""
    import urllib.request

    mock_response_data = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "Python 3.13 released with new features."}
            ]
        }
    }).encode("utf-8")

    class FakeResponse:
        def read(self):
            return mock_response_data
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        from gilzabots.web_search_config import _mcp_search
        result = asyncio.run(_mcp_search("http://localhost:9999/mcp", "web_search", "Python 3.13", 5))
    assert "Python 3.13 released" in result


def test_mcp_search_parses_sse_event_stream():
    import urllib.request

    mock_sse_data = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"SSE content payload"}]}}\n'
    ).encode("utf-8")

    class FakeResponse:
        def read(self):
            return mock_sse_data
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        from gilzabots.web_search_config import _mcp_search
        result = asyncio.run(_mcp_search("http://localhost:9999/mcp", "web_search", "Python 3.13", 5))
    assert "SSE content payload" in result


def test_mcp_search_handles_is_error():
    import urllib.request

    mock_error_data = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "Not found: Unknown tool: 'web_search'"}],
            "isError": True
        }
    }).encode("utf-8")

    class FakeResponse:
        def read(self):
            return mock_error_data
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        from gilzabots.web_search_config import _mcp_search
        result = asyncio.run(_mcp_search("http://localhost:9999/mcp", "web_search", "Python 3.13", 5))
    assert result.startswith("[web_search] MCP error:")
    assert "Unknown tool" in result


def test_mcp_search_sends_authorization_header():
    import urllib.request

    headers_sent = {}
    def fake_urlopen(req, *args, **kwargs):
        nonlocal headers_sent
        headers_sent = req.headers
        class FakeResponse:
            def read(self):
                return json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": "ok"
                }).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResponse()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        from gilzabots.web_search_config import _mcp_search
        asyncio.run(_mcp_search("http://localhost:9999/mcp", "web_search", "test", 5, mcp_api_key="secret-key"))

    assert headers_sent.get("Authorization") == "Bearer secret-key"


def test_mcp_search_connection_error():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        from gilzabots.web_search_config import _mcp_search
        result = asyncio.run(_mcp_search("http://localhost:9999/mcp", "web_search", "test", 5))
    assert "unreachable" in result.lower() or "MCP" in result


def test_test_mcp_empty_url():
    from gilzabots.web_search_config import test_mcp
    result = asyncio.run(test_mcp("", "web_search"))
    assert result["ok"] is False
    assert "empty" in result["error"].lower()


# ─── do_search dispatcher ─────────────────────────────────────────────────────

def test_do_search_uses_ddg_when_no_mcp(tmp_path, monkeypatch):
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({"enabled": True, "mcp_url": "", "mcp_tool": "web_search"}))
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    fake_results = [{"title": "Test", "href": "https://example.com", "body": "Example body"}]
    with patch(_ddgs_patch_target()) as MockDDGS:
        MockDDGS.return_value.text.return_value = fake_results
        from gilzabots.web_search_config import do_search
        result = asyncio.run(do_search("test query", max_results=1))
    assert "Test" in result


def test_do_search_respects_provider(tmp_path, monkeypatch):
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({
        "enabled": True,
        "mcp_url": "http://localhost:9999/mcp",
        "mcp_tool": "search",
        "provider": "duckduckgo"
    }))
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    fake_results = [{"title": "DDG Result", "href": "https://example.com", "body": "Example body"}]

    patch_target = "duckduckgo_search.DDGS"
    try:
        import ddgs
        patch_target = "ddgs.DDGS"
    except ImportError:
        pass

    with patch(patch_target) as MockDDGS:
        MockDDGS.return_value.text.return_value = fake_results
        MockDDGS.return_value.__enter__.return_value.text.return_value = fake_results
        from gilzabots.web_search_config import do_search
        result = asyncio.run(do_search("test query", max_results=1))

    assert "DDG Result" in result


# ─── tools.py web_search integration ─────────────────────────────────────────

def test_web_search_tool_disabled(tmp_path, monkeypatch):
    """When web search is disabled the tool returns a user-friendly message."""
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({"enabled": False, "mcp_url": "", "mcp_tool": "web_search"}))
    # Patch the config path used at runtime
    import gilzabots.web_search_config as wsc
    original = wsc._CONFIG_PATH
    wsc._CONFIG_PATH = cfg_path
    try:
        result = _raw_web_search()(query="test")
        assert "disabled" in result.lower()
    finally:
        wsc._CONFIG_PATH = original


def test_web_search_tool_timeout_returns_promptly(tmp_path, monkeypatch):
    """A hung backend must not hold the tool past its timeout, and the message says what happened."""
    import time
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({"enabled": True, "mcp_url": "", "mcp_tool": "web_search"}))
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    async def slow_search(query, max_results=5):
        await asyncio.sleep(1.0)
        return "late"

    from gilzabots import tools as tools_mod
    monkeypatch.setattr("gilzabots.web_search_config.do_search", slow_search)
    monkeypatch.setattr(tools_mod, "_WEB_SEARCH_TIMEOUT_S", 0.1)
    started = time.monotonic()
    result = _raw_web_search()(query="hangs")
    assert time.monotonic() - started < 0.8
    assert result.startswith("[web_search] Search did not finish within 0.1s")


# ─── Context-budget truncation ────────────────────────────────────────────────

def test_truncate_to_context_budget_leaves_short_text_untouched(tmp_path):
    from gilzabots import tools as tools_mod

    tools_mod.set_project_context(SimpleNamespace(project_path=str(tmp_path), model_params={"num_ctx": 8192}, history=[]))
    try:
        text = "short result"
        assert tools_mod._truncate_to_context_budget(text) == text
    finally:
        tools_mod.set_project_context(None)


def test_truncate_to_context_budget_truncates_when_over_free_context(tmp_path):
    from gilzabots import tools as tools_mod

    # num_ctx=8192 tokens ~= 32768 chars total budget; a large fake history eats
    # most of it, so a big search result must be cut down to what remains.
    fake_history = [{"role": "user", "content": "x" * 28000}]
    tools_mod.set_project_context(SimpleNamespace(project_path=str(tmp_path), model_params={"num_ctx": 8192}, history=fake_history))
    try:
        huge_result = "y" * 20000
        truncated = tools_mod._truncate_to_context_budget(huge_result)
        assert len(truncated) < len(huge_result)
        assert "[Result truncated:" in truncated
    finally:
        tools_mod.set_project_context(None)


def test_web_search_tool_truncates_oversized_ddg_result(tmp_path, monkeypatch):
    """Large web_search results must be bounded by the active model's free context budget."""
    cfg_path = tmp_path / "web_search.json"
    cfg_path.write_text(json.dumps({"enabled": True, "mcp_url": "", "mcp_tool": "web_search"}))
    monkeypatch.setattr("gilzabots.web_search_config._CONFIG_PATH", cfg_path)

    from gilzabots import tools as tools_mod

    fake_history = [{"role": "user", "content": "x" * 28000}]
    tools_mod.set_project_context(SimpleNamespace(project_path=str(tmp_path), model_params={"num_ctx": 8192}, history=fake_history))

    huge_body = "z" * 50000
    fake_results = [{"title": "Big Page", "href": "https://example.com", "body": huge_body}]
    patch_target = "duckduckgo_search.DDGS"
    try:
        import ddgs
        patch_target = "ddgs.DDGS"
    except ImportError:
        pass

    try:
        with patch(patch_target) as MockDDGS:
            MockDDGS.return_value.text.return_value = fake_results
            MockDDGS.return_value.__enter__.return_value.text.return_value = fake_results

            result = _raw_web_search()(query="big query")
    finally:
        tools_mod.set_project_context(None)

    assert len(result) < len(huge_body)
    assert "[Result truncated:" in result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def importlib_reload(module):
    import importlib
    importlib.reload(module)
