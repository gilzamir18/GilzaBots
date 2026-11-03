"""Tests for gilzabots.chat_meta_params."""

import pytest
from gilzabots.chat_meta_params import parse_meta_params, apply_meta_params


# ── parse_meta_params ────────────────────────────────────────────────────────

def test_parse_single_int_param():
    text, overrides = parse_meta_params("Hello @max_tokens=512@ world")
    assert text == "Hello  world"
    assert overrides == {"max_tokens": 512}


def test_parse_float_param():
    _, overrides = parse_meta_params("@temperature=0.5@")
    assert overrides["temperature"] == pytest.approx(0.5)


def test_parse_quoted_string_param():
    _, overrides = parse_meta_params('@system_prompt="be ironic"@')
    assert overrides["system_prompt"] == "be ironic"


def test_parse_multiple_params():
    text, overrides = parse_meta_params("@max_tokens=5@ hey @temperature=0.1@")
    assert "max_tokens" in overrides
    assert "temperature" in overrides
    assert "hey" in text


def test_parse_unknown_key_ignored():
    _, overrides = parse_meta_params("@unknown_key=123@")
    assert overrides == {}


def test_parse_max_tokens_clamped_to_minimum():
    _, overrides = parse_meta_params("@max_tokens=5@")
    assert overrides["max_tokens"] == 256


def test_parse_max_tokens_at_minimum_is_accepted():
    _, overrides = parse_meta_params("@max_tokens=256@")
    assert overrides["max_tokens"] == 256


def test_parse_max_tokens_above_minimum_passes():
    _, overrides = parse_meta_params("@max_tokens=512@")
    assert overrides["max_tokens"] == 512


def test_parse_no_params():
    text, overrides = parse_meta_params("just a normal message")
    assert text == "just a normal message"
    assert overrides == {}


def test_parse_removes_token_from_text():
    text, _ = parse_meta_params("do this @max_tokens=3@ please")
    assert "@" not in text
    assert "max_tokens" not in text


# ── apply_meta_params ────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self):
        self.system_prompt = "original"
        self.model_kargs = {"temperature": 0.7, "max_tokens": 4096}


def test_apply_restores_system_prompt():
    agent = _FakeAgent()
    with apply_meta_params(agent, {"system_prompt": "be ironic"}):
        assert agent.system_prompt == "be ironic"
    assert agent.system_prompt == "original"


def test_apply_restores_litellm_kwarg():
    agent = _FakeAgent()
    with apply_meta_params(agent, {"max_tokens": 3}):
        assert agent.model_kargs["max_tokens"] == 3
    assert agent.model_kargs["max_tokens"] == 4096


def test_apply_restores_on_exception():
    agent = _FakeAgent()
    with pytest.raises(RuntimeError):
        with apply_meta_params(agent, {"max_tokens": 1, "system_prompt": "tmp"}):
            raise RuntimeError("boom")
    assert agent.system_prompt == "original"
    assert agent.model_kargs["max_tokens"] == 4096


def test_apply_adds_new_kwarg_then_removes():
    agent = _FakeAgent()
    with apply_meta_params(agent, {"top_p": 0.9}):
        assert agent.model_kargs["top_p"] == pytest.approx(0.9)
    assert "top_p" not in agent.model_kargs


def test_apply_empty_overrides_is_noop():
    agent = _FakeAgent()
    original_prompt = agent.system_prompt
    original_kargs = dict(agent.model_kargs)
    with apply_meta_params(agent, {}):
        pass
    assert agent.system_prompt == original_prompt
    assert agent.model_kargs == original_kargs
