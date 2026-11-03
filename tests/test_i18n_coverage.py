"""Tests for i18n coverage and correctness.

Covers:
1. All English keys have a Portuguese translation
2. Keys added during refactoring sessions are present
3. Keys with format parameters work correctly in both languages
4. No key silently falls back to its raw key name
"""

import pytest
from gilzabots.i18n import _, set_lang, _STRINGS


EN_KEYS = set(_STRINGS["en"].keys())
PT_KEYS = set(_STRINGS["pt"].keys())


# ---------------------------------------------------------------------------
# 1. Parity between languages
# ---------------------------------------------------------------------------

def test_all_en_keys_have_pt_translation():
    missing = EN_KEYS - PT_KEYS
    assert not missing, (
        f"Keys present in 'en' but missing in 'pt': {sorted(missing)}"
    )


def test_all_pt_keys_have_en_translation():
    missing = PT_KEYS - EN_KEYS
    assert not missing, (
        f"Keys present in 'pt' but missing in 'en': {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# 2. Keys added during refactoring must exist
# ---------------------------------------------------------------------------

REFACTORED_KEYS = [
    "intent_unclear",
    "command_hint_suggestion",
    "evaluating_complexity",
    "selecting_skills",
    "routing_complex_task",
    "api_key_missing_fallback",
    "alt_model_error",
    "fallback_to_model",
    "empty_response_unresolved_error",
]


@pytest.mark.parametrize("key", REFACTORED_KEYS)
def test_refactored_key_exists_in_en(key):
    assert key in EN_KEYS, f"Key '{key}' missing from English strings"


@pytest.mark.parametrize("key", REFACTORED_KEYS)
def test_refactored_key_exists_in_pt(key):
    assert key in PT_KEYS, f"Key '{key}' missing from Portuguese strings"


# ---------------------------------------------------------------------------
# 3. Parametrised keys produce correct output
# ---------------------------------------------------------------------------

PARAM_KEYS = [
    ("command_hint_suggestion", {"cmd": "clear"}),
    ("routing_complex_task", {"model": "gemini/x"}),
    ("api_key_missing_fallback", {"model": "ollama/test-model"}),
    ("alt_model_error", {"model": "gemini/x", "err": "timeout"}),
    ("fallback_to_model", {"model": "ollama/test-model"}),
    ("unknown_command", {"cmd": "/foo"}),
    ("session_renamed", {"name": "myproject"}),
    ("err_insufficient_quota", {"model": "openai/gpt-5.5"}),
]


@pytest.mark.parametrize("lang", ["en", "pt"])
@pytest.mark.parametrize("key,params", PARAM_KEYS)
def test_parametrised_key_formats_without_error(lang, key, params):
    set_lang(lang)
    result = _(key, **params)
    assert isinstance(result, str)
    assert result != key, f"Key '{key}' returned its own name (missing translation?)"
    # All param values should appear in the result
    for value in params.values():
        assert str(value) in result, (
            f"[{lang}] Key '{key}': param value {value!r} not in result {result!r}"
        )


# ---------------------------------------------------------------------------
# 4. No key silently returns its own name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "pt"])
def test_no_key_returns_raw_key_name(lang):
    """Keys whose English value is a common word that legitimately translates
    to itself (e.g. 'error', 'completed') are excluded from this check."""
    LEGITIMATE_SELF_TRANSLATIONS = {"failed", "completed", "error"}
    set_lang(lang)
    failures = []
    for key in EN_KEYS:
        if key in LEGITIMATE_SELF_TRANSLATIONS:
            continue
        val = _STRINGS[lang].get(key)
        if isinstance(val, str) and val == key:
            failures.append(key)
    assert not failures, (
        f"[{lang}] These keys return their own name as value: {failures}"
    )


# ---------------------------------------------------------------------------
# 5. Keys without parameters don't raise on call
# ---------------------------------------------------------------------------

SIMPLE_KEYS = [
    "intent_unclear",
    "evaluating_complexity",
    "selecting_skills",
    "agent_thinking",
    "exiting",
    "plan_approved",
    "refining",
]


@pytest.mark.parametrize("lang", ["en", "pt"])
@pytest.mark.parametrize("key", SIMPLE_KEYS)
def test_simple_key_returns_nonempty_string(lang, key):
    set_lang(lang)
    result = _(key)
    assert isinstance(result, str)
    assert result.strip() != ""
