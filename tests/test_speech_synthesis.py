"""Tests for speech synthesis: the AgenticBlocks block and the GilzaBots service.

Nothing here reaches a provider. The framework tests register a fake adapter and
fake LiteLLM entry points; the GilzaBots tests point the config at a stub block.
What is being pinned down is the contract every engine must be normalised into
-- audio bytes with a type the browser can actually decode, a refusal rather
than a truncation when the passage is too long, and a classified error when
synthesis cannot happen.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest

from agenticblocks.blocks.speech import (
    DEFAULT_VOICE,
    SpeechArtifact,
    SpeechSynthesisBlock,
    SpeechSynthesisError,
    SpeechSynthesisInput,
    extension_for_audio_mime,
    mime_for_audio_format,
    read_audio_bytes,
    register_speech_adapter,
    sniff_audio_mime,
)
from agenticblocks.blocks.speech import adapters as speech_adapters

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00fake-mp3-payload"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ─── Framework: block dispatch and validation ────────────────────────────────

def test_block_dispatches_to_registered_adapter():
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen["model"] = model
        seen["text"] = request.text
        seen["voice"] = request.voice
        seen["kwargs"] = model_kwargs
        return SpeechArtifact(data_b64=_b64(MP3), mime="audio/mpeg")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(
        name="t", model="provider/tts", route="fake_route",
        voice="nova", model_kwargs={"api_key": "k"},
    )

    out = asyncio.run(block.run(SpeechSynthesisInput(text="bonjour")))

    assert seen["model"] == "provider/tts"
    assert seen["text"] == "bonjour"
    assert seen["kwargs"] == {"api_key": "k"}
    assert out.audio.to_bytes() == MP3
    assert out.route == "fake_route"


def test_a_request_overrides_the_blocks_defaults():
    # The block's settings are defaults, not a lock: a caller that names a voice
    # per request and one that configures it once must both work.
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen.update(voice=request.voice, fmt=request.response_format, speed=request.speed)
        return SpeechArtifact(data_b64=_b64(MP3), mime="audio/mpeg")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(
        name="t", model="m", route="fake_route",
        voice="nova", response_format="mp3", speed=1.0,
    )

    asyncio.run(block.run(SpeechSynthesisInput(
        text="x", voice="echo", response_format="wav", speed=1.5,
    )))

    assert seen == {"voice": "echo", "fmt": "wav", "speed": 1.5}


def test_the_blocks_defaults_apply_when_the_request_omits_them():
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen.update(voice=request.voice, fmt=request.response_format, speed=request.speed)
        return SpeechArtifact(data_b64=_b64(MP3), mime="audio/mpeg")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(
        name="t", model="m", route="fake_route",
        voice="nova", response_format="wav", speed=0.75,
    )

    asyncio.run(block.run(SpeechSynthesisInput(text="x")))

    assert seen == {"voice": "nova", "fmt": "wav", "speed": 0.75}


def test_no_model_is_not_configured_rather_than_a_provider_error():
    block = SpeechSynthesisBlock(name="t", model="")

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(block.run(SpeechSynthesisInput(text="hello")))

    assert exc.value.kind == "not_configured"


def test_empty_text_is_refused_before_a_request_is_made():
    async def fake_adapter(model, request, model_kwargs):  # pragma: no cover
        raise AssertionError("the adapter must not be reached")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(name="t", model="m", route="fake_route")

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(block.run(SpeechSynthesisInput(text="   ")))

    assert exc.value.kind == "bad_request"


def test_overlong_text_is_refused_never_truncated():
    # The decisive property: audio that stops mid-sentence is indistinguishable
    # from audio that finished, so a clipped reading must never be presented as
    # a complete one.
    async def fake_adapter(model, request, model_kwargs):  # pragma: no cover
        raise AssertionError("the adapter must not be reached")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(name="t", model="m", route="fake_route", max_input_chars=10)

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(block.run(SpeechSynthesisInput(text="x" * 11)))

    assert exc.value.kind == "too_long"
    assert "11" in str(exc.value) and "10" in str(exc.value)


def test_an_unknown_route_names_the_ones_that_exist():
    block = SpeechSynthesisBlock(name="t", model="m", route="nope")

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(block.run(SpeechSynthesisInput(text="hello")))

    assert exc.value.kind == "unknown_route"
    assert "audio_speech" in str(exc.value)


def test_an_adapter_that_returns_no_audio_is_an_empty_result():
    async def fake_adapter(model, request, model_kwargs):
        return SpeechArtifact(data_b64="", mime="audio/mpeg")

    register_speech_adapter("fake_route", fake_adapter)
    block = SpeechSynthesisBlock(name="t", model="m", route="fake_route")

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(block.run(SpeechSynthesisInput(text="hello")))

    assert exc.value.kind == "empty"


def test_an_empty_artifact_refuses_to_hand_over_zero_bytes():
    # Returning b"" would produce a player that looks like it loaded something.
    with pytest.raises(SpeechSynthesisError) as exc:
        SpeechArtifact().to_bytes()

    assert exc.value.kind == "empty"


# ─── Framework: the audio_speech adapter ─────────────────────────────────────

def _fake_litellm(monkeypatch, *, response=None, raises=None):
    import litellm

    captured = {}

    async def fake_aspeech(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(litellm, "aspeech", fake_aspeech, raising=False)
    return captured


def test_audio_speech_sends_the_openai_contract(monkeypatch):
    captured = _fake_litellm(monkeypatch, response=SimpleNamespace(content=MP3))

    artifact = asyncio.run(speech_adapters.audio_speech_adapter(
        "openai/tts-1",
        SpeechSynthesisInput(text="hello", voice="nova", response_format="mp3", speed=1.25),
        {"api_key": "k", "api_base": "http://127.0.0.1:8880/v1", "temperature": 0.7},
    ))

    assert captured["model"] == "openai/tts-1"
    assert captured["input"] == "hello"
    assert captured["voice"] == "nova"
    assert captured["response_format"] == "mp3"
    assert captured["speed"] == 1.25
    assert captured["api_base"] == "http://127.0.0.1:8880/v1"
    # Chat sampling parameters are whitelisted out: a speech endpoint rejects
    # them, and several local servers 400 on an unknown field.
    assert "temperature" not in captured
    assert artifact.to_bytes() == MP3


def test_a_local_server_needs_only_an_api_base(monkeypatch):
    # The whole point of the route: Kokoro/openedai-speech/LocalAI/Piper differ
    # from a hosted provider by a catalog entry, not by code.
    captured = _fake_litellm(monkeypatch, response=SimpleNamespace(content=WAV))

    artifact = asyncio.run(speech_adapters.audio_speech_adapter(
        "openai/kokoro",
        SpeechSynthesisInput(text="olá", response_format="wav"),
        {"api_base": "http://127.0.0.1:8880/v1"},
    ))

    assert captured["api_base"] == "http://127.0.0.1:8880/v1"
    assert captured["voice"] == DEFAULT_VOICE
    assert artifact.mime == "audio/wav"


def test_the_language_hint_travels_only_when_set(monkeypatch):
    captured = _fake_litellm(monkeypatch, response=SimpleNamespace(content=MP3))
    asyncio.run(speech_adapters.audio_speech_adapter(
        "m", SpeechSynthesisInput(text="x"), {}))
    assert "language" not in captured

    captured = _fake_litellm(monkeypatch, response=SimpleNamespace(content=MP3))
    asyncio.run(speech_adapters.audio_speech_adapter(
        "m", SpeechSynthesisInput(text="x", language="pt-BR"), {}))
    assert captured["language"] == "pt-BR"


def test_the_served_type_is_sniffed_not_taken_on_trust(monkeypatch):
    # Providers routinely answer application/octet-stream for an MP3, and a
    # browser will not decode audio served under a type that does not match the
    # payload. The request asked for wav; the bytes are an mp3, and the bytes win.
    _fake_litellm(monkeypatch, response=SimpleNamespace(content=MP3))

    artifact = asyncio.run(speech_adapters.audio_speech_adapter(
        "m", SpeechSynthesisInput(text="x", response_format="wav"), {}))

    assert artifact.mime == "audio/mpeg"


def test_the_requested_format_is_the_fallback_when_bytes_say_nothing(monkeypatch):
    _fake_litellm(monkeypatch, response=SimpleNamespace(content=b"\x01\x02\x03\x04unrecognised"))

    artifact = asyncio.run(speech_adapters.audio_speech_adapter(
        "m", SpeechSynthesisInput(text="x", response_format="flac"), {}))

    assert artifact.mime == "audio/flac"


def test_an_empty_body_is_reported_rather_than_served(monkeypatch):
    _fake_litellm(monkeypatch, response=SimpleNamespace(content=b""))

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(speech_adapters.audio_speech_adapter(
            "m", SpeechSynthesisInput(text="x"), {}))

    assert exc.value.kind == "empty"


@pytest.mark.parametrize("exc_name,kind", [
    ("AuthenticationError", "auth"),
    ("APIConnectionError", "connection"),
    ("NotFoundError", "unsupported"),
    ("BadRequestError", "bad_request"),
])
def test_provider_failures_are_classified(monkeypatch, exc_name, kind):
    import litellm

    exc_cls = getattr(litellm.exceptions, exc_name)
    try:
        raised = exc_cls(message="boom", llm_provider="openai", model="m")
    except TypeError:
        raised = exc_cls("boom")

    _fake_litellm(monkeypatch, raises=raised)

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(speech_adapters.audio_speech_adapter(
            "m", SpeechSynthesisInput(text="x"), {}))

    assert exc.value.kind == kind


def test_a_404_reads_as_no_speech_route_not_as_a_missing_model(monkeypatch):
    # A plain Ollama serves no /v1/audio/speech at all and answers 404, exactly
    # as it does for the images route. That must read as "this endpoint has no
    # speech route", which is actionable, and not as "the model is missing".
    import litellm

    try:
        raised = litellm.exceptions.NotFoundError(
            message="404 page not found", llm_provider="openai", model="m")
    except TypeError:
        raised = litellm.exceptions.NotFoundError("404 page not found")
    _fake_litellm(monkeypatch, raises=raised)

    with pytest.raises(SpeechSynthesisError) as exc:
        asyncio.run(speech_adapters.audio_speech_adapter(
            "m", SpeechSynthesisInput(text="x"), {}))

    assert exc.value.kind == "unsupported"
    assert "no speech synthesis route" in str(exc.value)


# ─── Framework: payload and type helpers ─────────────────────────────────────

def test_read_audio_bytes_accepts_both_shapes_and_refuses_the_rest():
    assert read_audio_bytes(MP3) == MP3
    assert read_audio_bytes(SimpleNamespace(content=MP3)) == MP3
    assert read_audio_bytes(SimpleNamespace(read=lambda: MP3)) == MP3

    with pytest.raises(SpeechSynthesisError) as exc:
        read_audio_bytes({"unexpected": "shape"})
    assert exc.value.kind == "empty"


@pytest.mark.parametrize("data,mime", [
    (b"ID3\x03\x00", "audio/mpeg"),
    (b"\xff\xfb\x90\x00", "audio/mpeg"),
    (b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"OggS\x00\x02", "audio/ogg"),
    (b"fLaC\x00\x00", "audio/flac"),
    (b"", ""),
    (b"not audio at all", ""),
])
def test_audio_mime_is_sniffed_from_magic_bytes(data, mime):
    assert sniff_audio_mime(data) == mime


def test_format_and_extension_tables_agree():
    assert mime_for_audio_format("mp3") == "audio/mpeg"
    assert mime_for_audio_format("opus") == "audio/ogg"
    assert mime_for_audio_format("nonsense") == ""
    assert extension_for_audio_mime("audio/wav") == ".wav"
    assert extension_for_audio_mime("audio/unknown") == ".mp3"


# ─── GilzaBots: config sanitising ─────────────────────────────────────────────

def test_speech_config_clamps_what_providers_would_reject():
    from gilzabots import speech_config

    assert speech_config.sanitize_response_format("WAV") == "wav"
    assert speech_config.sanitize_response_format("aiff") == "mp3"
    assert speech_config.sanitize_response_format(None) == "mp3"

    assert speech_config.sanitize_speed(0.01) == speech_config.MIN_SPEED
    assert speech_config.sanitize_speed(99) == speech_config.MAX_SPEED
    assert speech_config.sanitize_speed("nonsense") == speech_config.DEFAULT_SPEED
    assert speech_config.sanitize_speed(float("nan")) == speech_config.DEFAULT_SPEED
    assert speech_config.sanitize_speed(1.5) == 1.5


def test_speech_ships_disabled_and_says_what_to_configure(monkeypatch):
    # Speech needs something the install does not have yet — a downloaded voice,
    # or a configured endpoint — so an unconfigured install must not offer a
    # menu item that can only fail.
    from gilzabots import speech_config

    monkeypatch.setattr(speech_config, "load_config", lambda: dict(speech_config._DEFAULTS))
    problem = speech_config.configuration_problem()
    assert "disabled" in problem and "Settings" in problem

    # The remote engine points at the model catalog...
    monkeypatch.setattr(
        speech_config, "load_config",
        lambda: {**speech_config._DEFAULTS, "enabled": True, "engine": "remote"},
    )
    problem = speech_config.configuration_problem()
    assert "No speech synthesis model" in problem
    # ...and names the local servers, because those keep it usable offline.
    assert "Kokoro" in problem or "Piper" in problem

    monkeypatch.setattr(
        speech_config, "load_config",
        lambda: {
            **speech_config._DEFAULTS,
            "enabled": True, "engine": "remote", "model": "tts",
        },
    )
    assert speech_config.configuration_problem() == ""

    # The local engine asks for a voice instead, and says how big it is.
    monkeypatch.setattr(
        speech_config, "load_config",
        lambda: {**speech_config._DEFAULTS, "enabled": True, "engine": "local"},
    )
    problem = speech_config.configuration_problem()
    assert "download one" in problem and "offline" in problem


# ─── GilzaBots: the service wrapper ───────────────────────────────────────────

def test_language_tags_resolve_like_the_translation_setting():
    from gilzabots.speech import resolve_language_tag

    assert resolve_language_tag("pt-BR") == "pt-BR"
    # An unknown regional variant falls back to its base language rather than
    # being dropped.
    assert resolve_language_tag("pt-PT") == "pt"
    assert resolve_language_tag("", None, "fr") == "fr"
    # Free text is forwarded verbatim, which is what makes the settings field's
    # "Other" option work without a code change.
    assert resolve_language_tag("klingon") == "klingon"
    # Empty means "let the engine decide" — forcing a tag a server does not
    # know turns a working request into a 400.
    assert resolve_language_tag(None, "", None) == ""


def test_execute_speech_refuses_an_empty_or_overlong_snippet(monkeypatch):
    from gilzabots import speech

    with pytest.raises(ValueError):
        asyncio.run(speech.execute_speech_synthesis("   "))

    with pytest.raises(ValueError) as exc:
        asyncio.run(speech.execute_speech_synthesis("x" * (speech.MAX_SPEECH_CHARS + 1)))
    assert "too long" in str(exc.value)


def test_execute_speech_returns_bytes_and_a_servable_type(monkeypatch):
    from gilzabots import speech, speech_config

    async def fake_adapter(model, request, model_kwargs):
        return SpeechArtifact(data_b64=_b64(MP3), mime="audio/mpeg")

    register_speech_adapter("fake_route", fake_adapter)
    monkeypatch.setattr(
        speech_config, "build_block",
        lambda model_id="", route="": SpeechSynthesisBlock(
            name="t", model="m", route="fake_route"),
    )

    audio, mime = asyncio.run(speech.execute_speech_synthesis("hello", voice="nova"))

    assert audio == MP3
    assert mime == "audio/mpeg"
