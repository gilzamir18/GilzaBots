"""Web search configuration manager for GilzaBots.

Manages the global web search settings persisted at ~/.gilzabots/web_search.json.

Config schema:
    {
        "enabled": true,          # whether web_search tool is active
        "mcp_url": "",            # MCP server URL, e.g. "http://localhost:8080/mcp"
        "mcp_tool": "web_search"  # name of the tool to call on the MCP server
    }

When mcp_url is empty the built-in ddgs metasearch backend is used (the
"duckduckgo" provider: ddgs scrapes several engines, DuckDuckGo among them).
When mcp_url is set it completely replaces ddgs — no silent fallback.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path.home() / ".gilzabots" / "web_search.json"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mcp_url": "",
    "mcp_tool": "web_search",
    "mcp_api_key": "",
    "provider": "duckduckgo",
}


# ─── Persistence ──────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    """Return the current web search config, creating the file with defaults if absent."""
    try:
        if _CONFIG_PATH.exists():
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            # Backward compatibility: if provider is missing but mcp_url is present, default to mcp
            if "provider" not in raw:
                raw["provider"] = "mcp" if raw.get("mcp_url") else "duckduckgo"
            merged = {**_DEFAULTS, **raw}
            return merged
    except Exception:
        pass
    return dict(_DEFAULTS)


def save_config(config: dict[str, Any]) -> None:
    """Persist *config* to ~/.gilzabots/web_search.json."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Only store known keys
    to_save = {
        "enabled": bool(config.get("enabled", _DEFAULTS["enabled"])),
        "mcp_url": str(config.get("mcp_url", "") or "").strip(),
        "mcp_tool": str(config.get("mcp_tool", _DEFAULTS["mcp_tool"]) or _DEFAULTS["mcp_tool"]).strip(),
        "mcp_api_key": str(config.get("mcp_api_key", "") or "").strip(),
        "provider": str(config.get("provider", "duckduckgo")).strip(),
    }
    _CONFIG_PATH.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")


def is_enabled() -> bool:
    """Quick check — returns True if web search is globally enabled."""
    return bool(load_config().get("enabled", True))


# ─── ddgs metasearch backend ──────────────────────────────────────────────────
#
# `ddgs` is a metasearch library: one text() call scrapes several engines
# (wikipedia, brave, duckduckgo, mojeek, startpage, yahoo, ...).  Those engines
# rate-limit and bot-block scrapers, and ddgs reports a blocked response
# (HTTP 429, DuckDuckGo's HTTP 202, a captcha page) exactly like an empty one.
# Bursts of searches -- e.g. an agent issuing four web_search calls in
# parallel, each fanning out to several engines -- are what trips those blocks,
# so searches run one at a time, a minimum interval apart, through one shared
# DDGS client (which keeps its engines' HTTP sessions and cookies).

_DDGS_MIN_INTERVAL_S = 2.0   # minimum gap between the end of one search and the start of the next
_DDGS_QUEUE_TIMEOUT_S = 15.0  # how long a search waits for earlier ones before giving up
_DDGS_LOCK = threading.Lock()
_ddgs_state: dict[str, Any] = {"cls": None, "client": None, "last_finished": 0.0}


def _ddgs_class() -> Any:
    """Return the DDGS class from `ddgs`, or from its legacy name, or None."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return None
    return DDGS


def _ddgs_client(ddgs_cls: Any) -> Any:
    """Return the shared DDGS client, creating it for *ddgs_cls* if needed.

    The caller must hold _DDGS_LOCK.  The context-manager form of DDGS() has a
    known bug returning empty results in v6+, so DDGS is instantiated directly.
    """
    if _ddgs_state["cls"] is not ddgs_cls:
        _ddgs_state["client"] = ddgs_cls()
        _ddgs_state["cls"] = ddgs_cls
    return _ddgs_state["client"]


def _ddgs_engine_names() -> str:
    try:
        from ddgs.engines import ENGINES
        return ", ".join(sorted(ENGINES["text"]))
    except Exception:
        return "its search engines"


def _ddgs_no_results_message(query: str) -> str:
    return (
        f"[web_search] No results for: {query}\n"
        f"The ddgs metasearch queried {_ddgs_engine_names()} and none returned a result. "
        "ddgs reports blocked or rate-limited engine responses (HTTP 429/202, captcha pages) "
        "the same way as an empty result, so this does not distinguish 'nothing matches' from "
        "'the engines are throttling this machine'. If simple, common queries fail too, the "
        "engines are throttling: waiting helps, while immediate or parallel retries prolong the block."
    )


def _ddgs_error_message(query: str, exc: Exception) -> str:
    kind = type(exc).__name__
    if kind == "TimeoutException":
        return (
            f"[web_search] Search timed out for: {query}\n"
            f"No ddgs engine returned a result; the last engine error was: {exc}. "
            "That error names only the last engine that failed -- the other engines returned "
            "nothing, which ddgs also reports for blocked or rate-limited responses."
        )
    if kind == "RatelimitException":
        return f"[web_search] Search engines are rate-limiting this machine ({exc}). Wait before searching again."
    if str(exc).startswith("No results found"):
        return _ddgs_no_results_message(query)
    return f"[web_search] ddgs metasearch failed ({kind}): {exc}"


def _ddg_search(query: str, max_results: int) -> str:
    """Search via the ddgs metasearch and return a plain-text summary of results.

    Searches are serialized and spaced by _DDGS_MIN_INTERVAL_S (see above).
    """
    ddgs_cls = _ddgs_class()
    if ddgs_cls is None:
        return (
            "[web_search] Search package not installed. "
            "Run: pip install ddgs"
        )

    if not _DDGS_LOCK.acquire(timeout=_DDGS_QUEUE_TIMEOUT_S):
        return (
            f"[web_search] Not run: earlier web searches were still running after "
            f"{_DDGS_QUEUE_TIMEOUT_S:g}s. Searches run one at a time, at least "
            f"{_DDGS_MIN_INTERVAL_S:g}s apart, so the search engines do not rate-limit them; "
            "issue web searches one after another rather than in parallel."
        )
    try:
        wait = _DDGS_MIN_INTERVAL_S - (time.monotonic() - _ddgs_state["last_finished"])
        if wait > 0:
            time.sleep(wait)
        try:
            raw = _ddgs_client(ddgs_cls).text(query, max_results=max_results)
        except Exception as exc:
            return _ddgs_error_message(query, exc)
        finally:
            _ddgs_state["last_finished"] = time.monotonic()
    finally:
        _DDGS_LOCK.release()

    results = []
    for r in (raw or []):
        title = r.get("title", "")
        href = r.get("href", "")
        body = r.get("body", "")
        results.append(f"**{title}**\n{href}\n{body}")
    if not results:
        return _ddgs_no_results_message(query)
    header = f"Web search results for: {query}\n\n"
    return header + "\n\n---\n\n".join(results)


# ─── MCP backend ──────────────────────────────────────────────────────────────

async def _mcp_search(mcp_url: str, mcp_tool: str, query: str, max_results: int, mcp_api_key: str = "") -> str:
    """Call a remote MCP server's tool and return the result as plain text.

    Uses the MCP JSON-RPC HTTP transport:
    POST <mcp_url>/tools/call  with body:
        {"method": "tools/call", "params": {"name": <tool>, "arguments": {...}}}
    """
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": mcp_tool,
            "arguments": {"query": query, "max_results": max_results},
        },
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "GilzaBots/1.0",
    }
    if mcp_api_key:
        headers["Authorization"] = f"Bearer {mcp_api_key}"

    req = urllib.request.Request(
        mcp_url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Try to read error body if available
        try:
            err_body = exc.read().decode("utf-8")
            return f"[web_search] MCP call failed: {exc.code} {exc.reason} ({err_body})"
        except Exception:
            return f"[web_search] MCP server returned HTTP error {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return f"[web_search] MCP server unreachable ({mcp_url}): {exc.reason}"
    except Exception as exc:
        return f"[web_search] MCP call failed: {exc}"

    # Try parsing direct JSON, fall back to SSE event-stream
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        data_lines = []
        for line in raw_body.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        joined_data = "".join(data_lines)
        try:
            body = json.loads(joined_data)
        except json.JSONDecodeError as exc:
            return f"[web_search] MCP failed to parse event-stream response: {exc}. Raw response: {raw_body[:500]}"

    # Extract result from JSON-RPC response
    error = body.get("error")
    if error:
        return f"[web_search] MCP error: {error.get('message', str(error))}"

    result = body.get("result", {})
    # MCP tools/call may return content as list of {type, text} blocks
    if isinstance(result, dict):
        is_error = result.get("isError", False)
        content = result.get("content", result)
    else:
        is_error = False
        content = result

    if isinstance(content, list):
        parts = [
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in content
        ]
        text_result = "\n".join(parts)
    else:
        text_result = str(content)

    if is_error:
        return f"[web_search] MCP error: {text_result}"
    return text_result


async def test_mcp(mcp_url: str, mcp_tool: str, mcp_api_key: str = "") -> dict[str, Any]:
    """Test connectivity to an MCP server.

    Returns:
        {"ok": True} on success.
        {"ok": False, "error": "..."} on failure.
    """
    if not mcp_url:
        return {"ok": False, "error": "MCP URL is empty."}
    try:
        result = await _mcp_search(mcp_url, mcp_tool, "test connectivity", max_results=1, mcp_api_key=mcp_api_key)
        if result.startswith("[web_search]"):
            return {"ok": False, "error": result}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─── Public search dispatcher ─────────────────────────────────────────────────

async def do_search(query: str, max_results: int = 5) -> str:
    """Dispatch a search using the active backend (MCP if configured, else ddgs).

    This is the single entry point called by the `web_search` tool in tools.py.
    """
    cfg = load_config()
    provider = cfg.get("provider", "duckduckgo")
    mcp_url = cfg.get("mcp_url", "").strip()
    mcp_tool = cfg.get("mcp_tool", "web_search") or "web_search"
    mcp_api_key = cfg.get("mcp_api_key", "").strip()

    if provider == "mcp" and mcp_url:
        return await _mcp_search(mcp_url, mcp_tool, query, max_results, mcp_api_key)
    else:
        return _ddg_search(query, max_results)
