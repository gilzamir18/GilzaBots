"""Tests for dictation: the AgenticBlocks block, the model store and the API.

Nothing here loads a Whisper model or downloads anything. What is pinned down is
the contract around the engine -- that a clip that is not audio is refused
before it reaches the decoder, that a model size from the client cannot name an
arbitrary directory, that a partially downloaded model is discarded rather than
listed as installed, and that a failure arrives classified.
"""

import json
import struct
from unittest.mock import AsyncMock

import pytest

from agenticblocks.blocks.transcription import (
    TranscriptionBlock,
    TranscriptionError,
    TranscriptionInput,
    TranscriptionOutput,
    register_transcription_adapter,
)
from gilzabots import transcription, transcription_config as tc
from gilzabots.ide_server import AsyncHTTPServer


def _wav(frames: int = 16, sample_rate: int = 16000) -> bytes:
    """A structurally valid 16-bit mono WAV."""
    data = b"\x00\x00" * frames
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


def _server_with_capture():
    server = AsyncHTTPServer()
    responses = []
    server.send_response = lambda w, st, body, ct="text/plain": responses.append((st, body, ct))
    return server, responses


def _json_body(response):
    return json.loads(response[1].decode("utf-8"))


# ─── Framework: the block ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_block_dispatches_to_registered_adapter():
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen.update(model=model, audio=request.audio, language=request.language,
                    kwargs=model_kwargs)
        return TranscriptionOutput(text="olá mundo", language="pt")

    register_transcription_adapter("fake_route", fake_adapter)
    block = TranscriptionBlock(
        name="t", model="small", route="fake_route",
        model_kwargs={"model_path": "/models/small"},
    )

    out = await block.run(TranscriptionInput(audio=_wav(), language="pt"))

    assert seen["model"] == "small"
    assert seen["kwargs"] == {"model_path": "/models/small"}
    assert out.text == "olá mundo"
    assert out.route == "fake_route"


@pytest.mark.asyncio
async def test_the_blocks_language_applies_when_the_request_omits_one():
    seen = {}

    async def fake_adapter(model, request, model_kwargs):
        seen["language"] = request.language
        return TranscriptionOutput(text="x")

    register_transcription_adapter("fake_route", fake_adapter)
    block = TranscriptionBlock(name="t", route="fake_route", language="pt")

    await block.run(TranscriptionInput(audio=_wav()))
    assert seen["language"] == "pt"

    await block.run(TranscriptionInput(audio=_wav(), language="fr"))
    assert seen["language"] == "fr"


@pytest.mark.asyncio
async def test_empty_audio_is_refused_before_the_engine_is_reached():
    async def fake_adapter(model, request, model_kwargs):  # pragma: no cover
        raise AssertionError("the adapter must not be reached")

    register_transcription_adapter("fake_route", fake_adapter)
    block = TranscriptionBlock(name="t", route="fake_route")

    with pytest.raises(TranscriptionError) as exc:
        await block.run(TranscriptionInput(audio=b""))

    assert exc.value.kind == "bad_request"


@pytest.mark.asyncio
async def test_an_oversized_clip_is_refused_rather_than_started():
    async def fake_adapter(model, request, model_kwargs):  # pragma: no cover
        raise AssertionError("the adapter must not be reached")

    register_transcription_adapter("fake_route", fake_adapter)
    block = TranscriptionBlock(name="t", route="fake_route", max_audio_bytes=100)

    with pytest.raises(TranscriptionError) as exc:
        await block.run(TranscriptionInput(audio=b"x" * 101))

    assert exc.value.kind == "too_long"


@pytest.mark.asyncio
async def test_an_unknown_route_names_the_ones_that_exist():
    block = TranscriptionBlock(name="t", route="nope")

    with pytest.raises(TranscriptionError) as exc:
        await block.run(TranscriptionInput(audio=_wav()))

    assert exc.value.kind == "unknown_route"
    assert "local_whisper" in str(exc.value)


@pytest.mark.asyncio
async def test_an_empty_transcript_is_an_answer_not_an_error():
    # The user may simply have recorded silence. The caller decides how to
    # present "nothing was heard"; the block does not turn it into a failure.
    async def fake_adapter(model, request, model_kwargs):
        return TranscriptionOutput(text="")

    register_transcription_adapter("fake_route", fake_adapter)
    block = TranscriptionBlock(name="t", route="fake_route")

    out = await block.run(TranscriptionInput(audio=_wav()))
    assert out.text == ""


@pytest.mark.asyncio
async def test_the_local_adapter_refuses_without_a_model_path():
    from agenticblocks.blocks.transcription import local_whisper_adapter

    with pytest.raises(TranscriptionError) as exc:
        await local_whisper_adapter("small", TranscriptionInput(audio=_wav()), {})

    assert exc.value.kind == "not_configured"


def test_a_model_directory_that_is_absent_is_not_configured(tmp_path):
    from agenticblocks.blocks.transcription import load_whisper_model

    with pytest.raises(TranscriptionError) as exc:
        load_whisper_model(str(tmp_path / "absent"))

    assert exc.value.kind == "not_configured"


# ─── GilzaBots: the service wrapper ───────────────────────────────────────────

def test_only_a_real_wav_reaches_the_decoder():
    # Handing arbitrary bytes to the engine turns a clear "that is not audio"
    # into an opaque decoding error.
    assert transcription.looks_like_wav(_wav()) is True
    assert transcription.looks_like_wav(b"") is False
    assert transcription.looks_like_wav(b"not audio at all") is False
    assert transcription.looks_like_wav(b"RIFF____NOPE" + b"\x00" * 40) is False


@pytest.mark.asyncio
async def test_execute_transcription_refuses_empty_long_and_non_wav():
    with pytest.raises(ValueError):
        await transcription.execute_transcription(b"")

    with pytest.raises(ValueError) as exc:
        await transcription.execute_transcription(b"x" * (transcription.MAX_AUDIO_BYTES + 1))
    assert "too long" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        await transcription.execute_transcription(b"definitely not a wav file")
    assert "not a WAV" in str(exc.value)


# ─── GilzaBots: the model store ───────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_CONFIG_PATH", tmp_path / "transcription.json")
    monkeypatch.setattr(tc, "models_dir", lambda: tmp_path / "whisper")
    monkeypatch.setattr("gilzabots.ui_settings._SETTINGS_PATH", tmp_path / "ui.json")
    return tmp_path / "whisper"


def _install(root, size):
    target = root / size
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.bin").write_bytes(b"weights")
    (target / "tokenizer.json").write_text("{}")


@pytest.mark.parametrize("value", ["../../etc", "huge", "", None, "tiny.en"])
def test_an_unoffered_model_size_falls_back_rather_than_naming_a_directory(store, value):
    # The size arrives from the client and becomes a path segment.
    assert tc.sanitize_model(value) == tc.DEFAULT_MODEL
    assert tc.model_path(value).parent == store


def test_small_is_the_default_because_smaller_models_mishear(store):
    # Measured on a Portuguese sentence: tiny and base both substituted a word,
    # small reproduced it exactly. Outside English the first sizes are not
    # merely worse, they are wrong often enough to be annoying.
    assert tc.DEFAULT_MODEL == "small"
    assert tc.load_config()["model"] == "small"


def test_english_only_variants_are_not_offered(store):
    # This application is bilingual by design, and a `.en` model silently
    # mistranscribes everything else rather than failing.
    assert not any(size.endswith(".en") for size in tc.MODEL_SIZES)


def test_a_model_needs_weights_and_a_tokenizer_to_count_as_installed(store):
    target = store / "small"
    target.mkdir(parents=True)
    (target / "model.bin").write_bytes(b"weights")

    # A directory with only one of them would load and then fail mid-run.
    assert tc.is_installed("small") is False

    (target / "tokenizer.json").write_text("{}")
    assert tc.is_installed("small") is True
    assert tc.installed_models() == ["small"]


def test_a_partial_download_is_discarded(store, monkeypatch):
    def half_download(size, output_dir=None):
        (tc.models_dir() / size).mkdir(parents=True, exist_ok=True)
        (tc.models_dir() / size / "model.bin").write_bytes(b"weights")
        # tokenizer never arrives

    monkeypatch.setattr("faster_whisper.utils.download_model", half_download)

    with pytest.raises(tc.ModelStoreError) as exc:
        tc.download_model("tiny")

    assert exc.value.kind == "corrupt"
    assert not tc.is_installed("tiny")
    assert not (store / "tiny").exists()


def test_removing_a_model_deletes_it_and_frees_the_loaded_one(store, monkeypatch):
    _install(store, "small")
    cleared = []
    import agenticblocks.blocks.transcription as pkg
    monkeypatch.setattr(pkg, "clear_cache", lambda: cleared.append(True))

    tc.remove_model("small")

    assert not (store / "small").exists()
    assert cleared == [True]


def test_removing_a_model_that_is_not_there_says_so(store):
    with pytest.raises(tc.ModelStoreError) as exc:
        tc.remove_model("small")

    assert exc.value.kind == "not_found"


def test_each_missing_piece_names_the_one_thing_to_do_next(store):
    assert "disabled" in tc.configuration_problem()

    tc.save_config({"enabled": True, "model": "small"})
    problem = tc.configuration_problem()
    assert "not downloaded" in problem and "486 MB" in problem

    _install(store, "small")
    assert tc.configuration_problem() == ""


def test_the_block_is_wired_to_the_installed_model(store):
    _install(store, "small")
    tc.save_config({"enabled": True, "model": "small", "language": "pt"})

    block = tc.build_block()

    assert block.route == "local_whisper"
    assert block.language == "pt"
    assert block.model_kwargs["model_path"].endswith("small")
    assert block.model_kwargs["compute_type"] == "int8"


# ─── GilzaBots: the endpoints ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_recording_is_the_request_body(store, monkeypatch):
    _install(store, "small")
    tc.save_config({"enabled": True, "model": "small"})

    captured = {}

    async def fake_execute(audio, language="", prompt=""):
        captured.update(audio=audio, language=language)
        return TranscriptionOutput(text="olá mundo", language="pt", duration=1.5)

    monkeypatch.setattr("gilzabots.transcription.execute_transcription", fake_execute)

    wav = _wav()
    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/transcribe", {"lang": ["pt"]}, {}, wav, AsyncMock(),
    )

    status, _, _ = responses[-1]
    body = _json_body(responses[-1])
    assert status == 200
    # The body is the audio, not JSON: base64 in an envelope would cost a third
    # of the payload again for nothing.
    assert captured["audio"] == wav
    assert captured["language"] == "pt"
    assert body["text"] == "olá mundo"
    assert body["language"] == "pt"


@pytest.mark.asyncio
async def test_an_unconfigured_install_answers_with_the_instruction(store, monkeypatch):
    async def must_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("no transcription may be attempted when unconfigured")

    monkeypatch.setattr("gilzabots.transcription.execute_transcription", must_not_run)

    server, responses = _server_with_capture()
    await server.route_api("POST", "/api/transcribe", {}, {}, _wav(), AsyncMock())

    assert responses[-1][0] == 503
    assert _json_body(responses[-1])["kind"] == "not_configured"


@pytest.mark.asyncio
async def test_audio_that_is_not_a_wav_is_a_bad_request(store):
    _install(store, "small")
    tc.save_config({"enabled": True, "model": "small"})

    server, responses = _server_with_capture()
    await server.route_api(
        "POST", "/api/transcribe", {}, {}, b"this is not audio", AsyncMock(),
    )

    assert responses[-1][0] == 400
    assert "not a WAV" in _json_body(responses[-1])["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [
    ("bad_request", 400), ("too_long", 400), ("not_configured", 503),
    ("connection", 503), ("unknown", 500),
])
async def test_engine_failures_keep_their_classification(store, monkeypatch, kind, status):
    _install(store, "small")
    tc.save_config({"enabled": True, "model": "small"})

    async def fake_execute(audio, **kwargs):
        raise TranscriptionError("boom", kind=kind)

    monkeypatch.setattr("gilzabots.transcription.execute_transcription", fake_execute)

    server, responses = _server_with_capture()
    await server.route_api("POST", "/api/transcribe", {}, {}, _wav(), AsyncMock())

    assert responses[-1][0] == status
    assert _json_body(responses[-1])["kind"] == kind


@pytest.mark.asyncio
async def test_settings_round_trip_and_clamping(store):
    server, responses = _server_with_capture()
    writer = AsyncMock()

    await server.route_api(
        "POST", "/api/settings/transcription", {}, {},
        json.dumps({
            "enabled": True, "model": "enormous",
            "language": "  pt  ", "compute_type": "float64",
        }).encode(), writer,
    )
    saved = _json_body(responses[-1])
    assert saved["model"] == "small"        # unoffered size falls back
    assert saved["compute_type"] == "int8"  # unsupported quantization falls back
    assert saved["language"] == "pt"

    await server.route_api("GET", "/api/settings/transcription", {}, {}, b"", writer)
    cfg = _json_body(responses[-1])
    assert cfg["enabled"] is True
    assert [m["size"] for m in cfg["models"]] == list(tc.MODEL_SIZES)
    assert cfg["problem"]  # not downloaded yet


@pytest.mark.asyncio
async def test_removing_a_model_through_the_endpoint(store):
    _install(store, "tiny")
    server, responses = _server_with_capture()

    await server.route_api(
        "POST", "/api/transcription/models", {}, {},
        json.dumps({"size": "tiny", "action": "remove"}).encode(), AsyncMock(),
    )

    assert responses[-1][0] == 200
    assert not tc.is_installed("tiny")
