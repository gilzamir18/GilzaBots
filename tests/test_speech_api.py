"""Tests for the ``/api/speech`` and ``/api/settings/speech`` endpoints.

The synthesis itself is covered by ``test_speech_synthesis.py``; what is pinned
down here is the HTTP boundary -- that the audio crosses the wire as bytes under
a type the browser can decode, and that a failure arrives as a *classified*
reason the UI can act on rather than a generic 500.
"""

import json
from unittest.mock import AsyncMock

import pytest

from agenticblocks.blocks.speech import SpeechSynthesisError
from gilzabots.ide_server import AsyncHTTPServer

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-mp3-payload"


def _server_with_capture():
    """Capture responses without decoding the body, since audio is not JSON."""
    server = AsyncHTTPServer()
    responses = []

    def mock_send_response(_writer, status_code, body, content_type="text/plain"):
        responses.append((status_code, body, content_type))

    server.send_response = mock_send_response
    return server, responses


def _json_body(response):
    return json.loads(response[1].decode("utf-8"))


@pytest.fixture
def configured_speech(tmp_path, monkeypatch):
    """Point the speech config at a temp file and mark it ready."""
    monkeypatch.setattr("gilzabots.speech_config._CONFIG_PATH", tmp_path / "speech.json")
    monkeypatch.setattr("gilzabots.speech_config.configuration_problem", lambda: "")
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")


# ─── /api/settings/speech ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_round_trip_and_clamping(tmp_path, monkeypatch):
    monkeypatch.setattr("gilzabots.speech_config._CONFIG_PATH", tmp_path / "speech.json")
    server, responses = _server_with_capture()
    writer = AsyncMock()

    await server.route_api(
        "POST", "/api/settings/speech", {}, {},
        json.dumps({
            # Explicitly the remote engine: this case is about the model-catalog
            # settings, and "local" is the default.
            "enabled": True, "engine": "remote",
            "model": "openai/kokoro", "voice": "  af_bella  ",
            "response_format": "AIFF", "speed": 99,
        }).encode("utf-8"),
        writer,
    )
    saved = _json_body(responses[-1])
    assert saved["success"] is True
    assert saved["model"] == "openai/kokoro"
    assert saved["voice"] == "af_bella"
    # A format no provider serves is corrected rather than stored: the browser
    # would receive audio it cannot decode, which presents as silence.
    assert saved["response_format"] == "mp3"
    assert saved["speed"] == 4.0

    await server.route_api("GET", "/api/settings/speech", {}, {}, b"", writer)
    cfg = _json_body(responses[-1])
    assert cfg["model"] == "openai/kokoro"
    assert cfg["voice"] == "af_bella"
    assert "audio_speech" in cfg["routes"]
    assert "mp3" in cfg["formats"]
    assert cfg["engine"] == "remote"
    assert "local" in cfg["engines"]
    # The viewers read `problem` to decide whether to offer the action at all.
    assert cfg["problem"] == ""


@pytest.mark.asyncio
async def test_settings_report_why_speech_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("gilzabots.speech_config._CONFIG_PATH", tmp_path / "speech.json")
    server, responses = _server_with_capture()

    await server.route_api("GET", "/api/settings/speech", {}, {}, b"", AsyncMock())

    cfg = _json_body(responses[-1])
    # Ships disabled, and says so rather than presenting an enabled-looking UI.
    assert cfg["enabled"] is False
    assert "disabled" in cfg["problem"]


# ─── /api/speech ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speech_endpoint_returns_audio_bytes_not_json(configured_speech, monkeypatch):
    captured = {}

    async def fake_execute(text, voice="", language="", model=None, speed=None, response_format=""):
        captured.update(text=text, voice=voice, language=language, model=model)
        return MP3, "audio/mpeg"

    monkeypatch.setattr("gilzabots.speech.execute_speech_synthesis", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({
            "text": "  bonjour tout le monde  ",
            "voice": "nova",
            "lang": "fr",
            "model": "openai/tts-1",
        }).encode("utf-8"),
        AsyncMock(),
    )

    status, body, content_type = responses[-1]
    assert status == 200
    # The bytes are the response: base64 in a JSON envelope would cost a third
    # of the payload again and an <audio> element takes a URL.
    assert body == MP3
    assert content_type == "audio/mpeg"
    assert captured["voice"] == "nova"
    assert captured["language"] == "fr"
    assert captured["model"] == "openai/tts-1"


@pytest.mark.asyncio
async def test_speech_endpoint_requires_text(configured_speech):
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({"text": "   "}).encode("utf-8"), AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert "text is required" in _json_body(responses[-1])["error"]


@pytest.mark.asyncio
async def test_an_unconfigured_install_answers_with_the_instruction(tmp_path, monkeypatch):
    # The feature is opt-in and needs an endpoint nothing else in the app
    # requires, so "not configured" is a first-class answer with a fix in it,
    # delivered before any request is attempted.
    monkeypatch.setattr("gilzabots.speech_config._CONFIG_PATH", tmp_path / "speech.json")
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui_settings.json")

    async def must_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("no synthesis may be attempted when unconfigured")

    monkeypatch.setattr("gilzabots.speech.execute_speech_synthesis", must_not_run)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({"text": "hello"}).encode("utf-8"), AsyncMock(),
    )

    status, _, _ = responses[-1]
    payload = _json_body(responses[-1])
    assert status == 503
    assert payload["kind"] == "not_configured"
    assert "Settings" in payload["error"]


@pytest.mark.asyncio
async def test_an_overlong_snippet_is_refused_as_a_bad_request(configured_speech, monkeypatch):
    async def fake_execute(text, **kwargs):
        raise ValueError("The selected snippet is too long to pronounce (9000 characters, limit 4000).")

    monkeypatch.setattr("gilzabots.speech.execute_speech_synthesis", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({"text": "x" * 9000}).encode("utf-8"), AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert "too long" in _json_body(responses[-1])["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [
    ("auth", 401),
    ("bad_request", 400),
    ("too_long", 400),
    ("connection", 503),
    ("unsupported", 503),
    ("unknown_route", 500),
    ("unknown", 500),
])
async def test_provider_failures_keep_their_classification(configured_speech, monkeypatch, kind, status):
    # The UI renders a different instruction per kind, so the classification has
    # to survive the HTTP boundary instead of collapsing into a generic 500.
    async def fake_execute(text, **kwargs):
        raise SpeechSynthesisError("boom", kind=kind)

    monkeypatch.setattr("gilzabots.speech.execute_speech_synthesis", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({"text": "hello"}).encode("utf-8"), AsyncMock(),
    )

    assert responses[-1][0] == status
    assert _json_body(responses[-1])["kind"] == kind


@pytest.mark.asyncio
async def test_nothing_falls_back_to_silence(configured_speech, monkeypatch):
    """A failure is reported, never answered with empty audio."""
    async def fake_execute(text, **kwargs):
        raise SpeechSynthesisError("no route here", kind="unsupported")

    monkeypatch.setattr("gilzabots.speech.execute_speech_synthesis", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech", {}, {},
        json.dumps({"text": "hello"}).encode("utf-8"), AsyncMock(),
    )

    status, body, content_type = responses[-1]
    assert status != 200
    assert content_type == "application/json"
    assert not body.startswith(b"ID3")


# ─── Engine selection and the voice endpoints ────────────────────────────────

@pytest.fixture
def local_engine(tmp_path, monkeypatch):
    from gilzabots import speech_config, voice_store
    monkeypatch.setattr(speech_config, "_CONFIG_PATH", tmp_path / "speech.json")
    monkeypatch.setattr(voice_store, "voices_dir", lambda: tmp_path / "voices")
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui.json")
    return tmp_path


def _install(root, key):
    target = root / "voices" / key
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{key}.onnx").write_bytes(b"model")
    (target / f"{key}.onnx.json").write_text("{}")


def test_local_is_the_default_engine(local_engine):
    """Offline-capable by default, rather than implying a paid endpoint."""
    from gilzabots import speech_config

    assert speech_config.load_config()["engine"] == "local"
    assert speech_config.sanitize_engine("nonsense") == "local"
    assert speech_config.sanitize_engine("remote") == "remote"


def test_each_missing_piece_names_the_one_thing_to_do_next(local_engine, monkeypatch):
    from gilzabots import speech_config

    speech_config.save_config({"enabled": True, "engine": "local", "local_voice": ""})
    assert "download one" in speech_config.configuration_problem()

    speech_config.save_config({"enabled": True, "engine": "local", "local_voice": "absent"})
    assert "not installed" in speech_config.configuration_problem()

    _install(local_engine, "pt_BR-faber-medium")
    speech_config.save_config({
        "enabled": True, "engine": "local", "local_voice": "pt_BR-faber-medium",
    })
    # The last gate is espeak-ng, which the app cannot install for the user.
    monkeypatch.setattr("agenticblocks.blocks.speech.availability_problem", lambda: "")
    assert speech_config.configuration_problem() == ""


def test_the_local_engine_builds_a_local_onnx_block(local_engine):
    from gilzabots import speech_config

    _install(local_engine, "pt_BR-faber-medium")
    speech_config.save_config({
        "enabled": True, "engine": "local",
        "local_voice": "pt_BR-faber-medium", "speed": 1.5,
    })

    block = speech_config.build_block()

    assert block.route == "local_onnx"
    assert block.speed == 1.5
    # The path is resolved by the host; the framework block stays unaware of
    # where this application keeps its voices.
    assert block.model_kwargs["model_path"].endswith("pt_BR-faber-medium.onnx")


@pytest.mark.asyncio
async def test_voices_endpoint_lists_installed_even_when_offline(local_engine, monkeypatch):
    from gilzabots import voice_store

    _install(local_engine, "pt_BR-faber-medium")
    monkeypatch.setattr(
        voice_store, "list_catalog",
        lambda refresh=False: (_ for _ in ()).throw(
            voice_store.VoiceStoreError("no network", kind="connection")),
    )

    server, responses = _server_with_capture()
    await server.route_api("GET", "/api/speech/voices", {}, {}, b"", AsyncMock())

    status, _, _ = responses[-1]
    body = _json_body(responses[-1])
    assert status == 200
    # The local engine's whole point is working without a network; an
    # unreachable catalog must not hide what is already installed.
    assert body["installed"] == ["pt_BR-faber-medium"]
    assert body["catalog_error"]


@pytest.mark.asyncio
async def test_a_bad_voice_key_is_refused_by_the_endpoint(local_engine):
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech/voices/download", {}, {},
        json.dumps({"key": "../../etc/passwd"}).encode(), AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert _json_body(responses[-1])["kind"] == "bad_request"


@pytest.mark.asyncio
async def test_removing_the_selected_voice_clears_the_setting(local_engine):
    from gilzabots import speech_config

    _install(local_engine, "pt_BR-faber-medium")
    speech_config.save_config({
        "enabled": True, "engine": "local", "local_voice": "pt_BR-faber-medium",
    })

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/speech/voices/remove", {}, {},
        json.dumps({"key": "pt_BR-faber-medium"}).encode(), AsyncMock(),
    )

    assert responses[-1][0] == 200
    # A configuration pointing at a deleted voice would fail at synthesis time.
    assert speech_config.load_config()["local_voice"] == ""
