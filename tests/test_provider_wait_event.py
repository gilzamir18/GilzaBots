"""The provider's silence reaches both front-ends as `provider_wait`.

Reported from a real session: after a web search, the orchestrator's next
request went out on a pooled connection to the Ollama cloud that had died. The
turn sat on the 600 s HTTP timeout with only "Analyzing the obtained result..."
on screen, then LiteLLM's Router retried on a fresh connection and the answer
arrived in about a second. Nothing told the user that interrupting and pressing
Continue would have recovered it at once. The block reports the silence
(`on_response_wait`, see tests/test_response_wait.py); this covers the host:
the turn binds it, emits the event, and the terminal says what is happening.
"""
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.llm.response_wait import ResponseWait
from opalatex import cli_render


def _wait(waiting, phase="first_response", elapsed=60.04):
    return ResponseWait(
        agent="chat_orchestrator",
        model="ollama_chat/deepseek-v4.1-flash:cloud",
        phase=phase,
        elapsed_seconds=elapsed,
        waiting=waiting,
    )


def test_the_payload_names_the_agent_and_rounds_the_elapsed_time():
    from opalatex.agent_stdin import _provider_wait_payload

    assert _provider_wait_payload(_wait(True), "chat_orchestrator") == {
        "agent": "chat_orchestrator",
        "model": "ollama_chat/deepseek-v4.1-flash:cloud",
        "phase": "first_response",
        "elapsed_seconds": 60.0,
        "waiting": True,
    }


@pytest.mark.asyncio
async def test_a_turn_binds_the_wait_callback_and_emits_the_event(monkeypatch, tmp_path):
    import opalatex.agent_stdin as stdin_mod

    events = []

    class FakeProject:
        name = "proj"
        mode = "auto"
        project_path = str(tmp_path)
        model = "fake/model"
        current_chat_id = "main"
        model_params = {"stream": True}

    class FakeStore:
        def append_message(self, _project, role, content, attachments=None):
            pass

        def append_activity(self, _project, event, content="", agent="", payload=None, timestamp=None):
            pass

        def save(self, _project):
            pass

    class FakeMemGPT:
        model = "fake/model"
        internal_history = []
        _current_worker_messages = []
        _last_worker_summary = ""
        on_response_wait = None

        def __init__(self):
            self.model_kargs = {"stream": True}
            self.model_kwargs = self.model_kargs

        async def _acompletion(self, *args, **kwargs):
            return None

        async def run(self, _agent_input):
            self.on_response_wait(_wait(True))
            self.on_response_wait(_wait(False, elapsed=612.3))
            return SimpleNamespace(response="Done.")

    monkeypatch.setattr(stdin_mod, "print_event", lambda event, data: events.append((event, data)))
    monkeypatch.setattr(stdin_mod, "current_memgpt", FakeMemGPT())
    monkeypatch.setattr(stdin_mod, "current_project", FakeProject())
    monkeypatch.setattr(stdin_mod, "current_store", FakeStore())

    await stdin_mod.handle_run({"agent": "chat_orchestrator", "prompt": "go"})

    waits = [data for event, data in events if event == "provider_wait"]
    assert [(w["agent"], w["waiting"], w["elapsed_seconds"]) for w in waits] == [
        ("chat_orchestrator", True, 60.0),
        ("chat_orchestrator", False, 612.3),
    ]


# ── Terminal ──────────────────────────────────────────────────────────────────


class _Buffer:
    def __init__(self):
        self._parts = []

    def write(self, text):
        self._parts.append(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(self._parts)


def _render(events):
    from rich.console import Console

    import opalatex.terminal as T

    renderer = cli_render.TerminalEventRenderer()
    console = Console(file=_Buffer(), width=400, no_color=True, highlight=False)
    original, T.console = T.console, console
    try:
        renderer.begin_turn()
        for event in events:
            renderer(event)
    finally:
        T.console = original
    return console.file.getvalue()


def _event(**data):
    return {"event": "provider_wait", **data}


def test_the_terminal_says_the_model_is_silent_and_how_to_recover(monkeypatch):
    monkeypatch.setattr(cli_render, "_", lambda key, **kw: f"{key}:{kw.get('seconds')}")

    out = _render([_event(agent="chat_orchestrator", waiting=True, phase="first_response", elapsed_seconds=60.0)])

    assert "cli_provider_wait:60" in out
    assert "chat_orchestrator:" not in out, "the orchestrator is the default speaker"


def test_a_stalled_stream_and_the_end_of_a_wait_have_their_own_lines(monkeypatch):
    monkeypatch.setattr(cli_render, "_", lambda key, **kw: f"{key}:{kw.get('seconds')}")

    out = _render([
        _event(agent="worker:research", waiting=True, phase="stream", elapsed_seconds=61.9),
        _event(agent="worker:research", waiting=False, phase="stream", elapsed_seconds=75.0),
    ])

    assert "worker:research: cli_provider_wait_stream:61" in out
    assert "worker:research: cli_provider_wait_over:75" in out


def test_the_catalogue_has_every_wait_message_in_both_languages():
    from opalatex.i18n import _STRINGS

    for language in ("en", "pt"):
        for key in ("cli_provider_wait", "cli_provider_wait_stream", "cli_provider_wait_over"):
            assert "{seconds}" in _STRINGS[language][key]
