"""Dictation: turning a recording from the chat composer into text.

The recording is made in the browser and arrives here as a 16 kHz mono WAV.
That conversion happens on the client on purpose: `MediaRecorder` produces
WebM/Opus, and decoding Opus in Python would mean depending on ffmpeg, while
the browser already has `decodeAudioData` and `OfflineAudioContext` and can
resample without help (see `gui_src/src/utils/audioWav.js`).

Nothing here is chat-specific: any caller with recorded audio can use it.

This is the direction Whisper actually runs in. The symmetry with §2.13.2 is
only apparent -- Piper is a single forward pass, while Whisper is an
autoregressive decode with a tokenizer, which is why this one leans on
faster-whisper instead of driving ONNX directly.
"""

from __future__ import annotations

# A dictated passage, not a recording session. Roughly ten minutes of 16 kHz
# mono PCM; past that the wait stops being distinguishable from a hang.
MAX_AUDIO_BYTES = 20 * 1024 * 1024

# A WAV always starts this way. Checked because the browser is not the only
# thing that can post here, and handing arbitrary bytes to the decoder turns a
# clear "that is not audio" into an opaque engine error.
_WAV_MAGIC = (b"RIFF", b"WAVE")


def looks_like_wav(audio: bytes) -> bool:
    return (
        len(audio) > 12
        and audio[:4] == _WAV_MAGIC[0]
        and audio[8:12] == _WAV_MAGIC[1]
    )


async def execute_transcription(
    audio: bytes,
    language: str = "",
    prompt: str = "",
):
    """Transcribe *audio* and return the ``TranscriptionOutput``.

    Raises ``ValueError`` when the clip is empty, too long or not a WAV, and
    ``TranscriptionError`` (carrying a classified ``kind``) for anything the
    engine refuses.
    """
    from agenticblocks.blocks.transcription import TranscriptionInput

    from . import transcription_config

    if not audio:
        raise ValueError("Transcription requires a recording.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ValueError(
            f"The recording is too long to transcribe "
            f"({len(audio)} bytes, limit {MAX_AUDIO_BYTES})."
        )
    if not looks_like_wav(audio):
        raise ValueError("The recording is not a WAV file.")

    block = transcription_config.build_block()
    block.max_audio_bytes = MAX_AUDIO_BYTES

    return await block.run(TranscriptionInput(
        audio=audio,
        language=str(language or "").strip(),
        prompt=str(prompt or "").strip(),
    ))
