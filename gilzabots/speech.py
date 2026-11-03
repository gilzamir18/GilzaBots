"""Snippet pronunciation used by the document viewers' "Pronounce" action.

The synthesis runs through an AgenticBlocks ``SpeechSynthesisBlock`` against the
OpenAI speech contract, which is what hosted providers *and* the common local
engines (Kokoro-FastAPI, openedai-speech, LocalAI, Piper wrappers) all serve. So
"pronounce this excerpt" is one code path whether the audio is produced by a
paid API or by a server on localhost -- the difference is a catalog entry, which
is what keeps §2.2's offline-by-default promise reachable for this feature.

Nothing here is viewer-specific: any caller that has a text snippet can use it.

Note on the alternative that was measured and rejected: the browser's
``speechSynthesis`` does not work in the desktop shell. QtWebEngine exposes the
API but ships no Chromium TTS backend, so ``getVoices()`` returns an empty list
and ``speak()`` fails with ``not-allowed`` even on a machine with
speech-dispatcher installed. Falling back to it silently would be a hidden
behavior substitution, and it would fail on exactly the platform the app runs
on.
"""

from __future__ import annotations

# A pronunciation request is a passage, not a document. Longer text is rejected
# instead of being truncated: audio that stops mid-sentence sounds exactly like
# audio that finished, so a clipped reading presented as a complete one is the
# hidden substitution the tool-contract rule forbids.
MAX_SPEECH_CHARS = 4000

# Locale codes the "Translate to" setting offers, mapped to the BCP-47 tag a
# speech engine expects. The two settings deliberately share a vocabulary: the
# most useful thing to hear is usually the passage the user just translated.
SPEECH_LANGUAGE_TAGS: dict[str, str] = {
    "pt-BR": "pt-BR",
    "pt": "pt",
    "en": "en",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "it": "it",
    "ja": "ja",
    "zh": "zh",
    "ru": "ru",
}


def resolve_language_tag(*candidates: str | None) -> str:
    """Return the first non-empty candidate as a language tag, or "".

    An empty result is normal and means "let the engine decide": most voices are
    language-specific already, and forcing a tag a server does not know turns a
    working request into a 400.
    """
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if value in SPEECH_LANGUAGE_TAGS:
            return SPEECH_LANGUAGE_TAGS[value]
        base = value.split("-")[0].lower()
        if base in SPEECH_LANGUAGE_TAGS:
            return SPEECH_LANGUAGE_TAGS[base]
        return value
    return ""


async def execute_speech_synthesis(
    text: str,
    voice: str = "",
    language: str = "",
    model: str | None = None,
    speed: float | None = None,
    response_format: str = "",
):
    """Synthesize *text* and return ``(audio_bytes, mime)``.

    Raises ``ValueError`` when the snippet is empty or too long, and
    ``SpeechSynthesisError`` (carrying a classified ``kind``) for anything the
    provider refuses. Nothing falls back to another provider or to silence: a
    failed synthesis reports why, the way image generation does (§2.12).
    """
    from agenticblocks.blocks.speech import SpeechSynthesisInput

    from . import speech_config

    snippet = str(text or "").strip()
    if not snippet:
        raise ValueError("Pronunciation requires a non-empty text snippet.")
    if len(snippet) > MAX_SPEECH_CHARS:
        raise ValueError(
            f"The selected snippet is too long to pronounce "
            f"({len(snippet)} characters, limit {MAX_SPEECH_CHARS})."
        )

    block = speech_config.build_block(model_id=str(model or "").strip())
    block.max_input_chars = MAX_SPEECH_CHARS

    result = await block.run(SpeechSynthesisInput(
        text=snippet,
        voice=str(voice or "").strip(),
        language=str(language or "").strip(),
        response_format=str(response_format or "").strip(),
        speed=speed,
    ))

    artifact = result.audio
    return artifact.to_bytes(), (artifact.mime or "audio/mpeg")
