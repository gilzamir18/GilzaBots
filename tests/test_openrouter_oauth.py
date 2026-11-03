"""Tests for OpenRouter OAuth PKCE flow and default free model provisioning."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from gilzabots import cli_catalog, context_discovery, models_store, openrouter_oauth
from gilzabots.cli_commands import REPLState
from gilzabots.cloud import oauth, transport
from gilzabots.cloud.base import CloudAuthError


@pytest.fixture
def isolated_catalog(tmp_path, monkeypatch):
    store_path = tmp_path / "models.json"
    monkeypatch.setattr(models_store, "_MODELS_STORE_PATH", store_path)
    models_store._invalidate_models_cache()
    monkeypatch.setattr(
        context_discovery,
        "discover_context_window",
        lambda model: 200000 if model.get("name") == "openrouter/free" else None,
    )
    yield store_path
    models_store._invalidate_models_cache()


def test_begin_openrouter_authorization_builds_pkce_challenge_and_callback(monkeypatch):
    monkeypatch.setattr(oauth, "open_in_browser", lambda url: True)

    challenge = openrouter_oauth.begin_openrouter_authorization(label="My OpenRouter")
    try:
        parsed = urllib.parse.urlparse(challenge.authorization_url)
        query = urllib.parse.parse_qs(parsed.query)
        state = challenge.session["state"]
        verifier = challenge.session["code_verifier"]

        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )

        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://openrouter.ai/auth"
        assert query["code_challenge"] == [expected_challenge]
        assert query["code_challenge_method"] == ["S256"]
        assert query["state"] == [state]
        assert query["callback_url"][0].endswith(f"/callback/{state}")
        assert challenge.session["expires_at"] > challenge.session["started_at"]
    finally:
        openrouter_oauth.cancel_openrouter_authorization(challenge.session)


def test_loopback_receiver_recovers_state_from_callback_path():
    receiver = oauth.LoopbackReceiver()
    try:
        captured = {}

        def wait():
            captured.update(receiver.wait(timeout=5))

        waiter = threading.Thread(target=wait)
        waiter.start()

        # OpenRouter appends `?code=...` to `callback_url`; verify `/callback/<state>`
        # still populates `state` when the query string only carries `code`.
        urllib.request.urlopen(
            f"{receiver.redirect_uri}/callback/state-from-path?code=or-code-123",
            timeout=5,
        ).read()
        waiter.join(timeout=5)

        assert captured == {"code": "or-code-123", "state": "state-from-path"}
    finally:
        receiver.close()


def test_complete_openrouter_authorization_saves_connection_and_free_model(
    isolated_catalog, monkeypatch
):
    monkeypatch.setattr(oauth, "open_in_browser", lambda url: False)
    sent_requests = []

    def fake_post_json(url, payload, timeout=30.0):
        sent_requests.append((url, payload))
        return {"key": "sk-or-v1-oauth-key-999"}

    monkeypatch.setattr(oauth, "post_json", fake_post_json)

    challenge = openrouter_oauth.begin_openrouter_authorization()
    result = openrouter_oauth.complete_openrouter_authorization(
        challenge.session,
        response={"code": "auth-code-xyz", "state": challenge.session["state"]},
    )

    assert len(sent_requests) == 1
    url, payload = sent_requests[0]
    assert url == "https://openrouter.ai/api/v1/auth/keys"
    assert payload == {
        "code": "auth-code-xyz",
        "code_verifier": challenge.session["code_verifier"],
        "code_challenge_method": "S256",
    }

    conn = result["connection"]
    assert conn["id"] == "openrouter"
    assert conn["label"] == "OpenRouter"
    assert conn["provider"] == "openrouter"
    assert conn["api_key"] == "sk-or-v1-oauth-key-999"
    assert conn["api_base"] == "https://openrouter.ai/api/v1"

    assert result["added_default_model"] is True
    default_model = result["default_model"]
    assert default_model is not None
    assert default_model["id"] == "openrouter/openrouter/free"
    assert default_model["name"] == "openrouter/free"
    assert default_model["provider"] == "openrouter"
    assert default_model["connection_id"] == "openrouter"
    assert default_model["supports_thinking"] is True
    assert default_model["discovered_num_ctx"] == 200000

    # Re-authenticating rotates the key in place without duplicating the free model.
    monkeypatch.setattr(oauth, "post_json", lambda u, p, timeout=30.0: {"key": "sk-or-v1-rotated"})
    challenge2 = openrouter_oauth.begin_openrouter_authorization()
    result2 = openrouter_oauth.complete_openrouter_authorization(
        challenge2.session,
        response={"code": "auth-code-2", "state": challenge2.session["state"]},
    )
    assert result2["connection"]["id"] == "openrouter"
    assert result2["connection"]["api_key"] == "sk-or-v1-rotated"
    assert result2["added_default_model"] is False
    assert [m["id"] for m in models_store.load_models()] == ["openrouter/openrouter/free"]


def test_expired_session_is_rejected_before_network_exchange(isolated_catalog, monkeypatch):
    monkeypatch.setattr(oauth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(
        oauth, "post_json", lambda *a, **k: pytest.fail("expired session must not call post_json")
    )

    challenge = openrouter_oauth.begin_openrouter_authorization()
    expired_session = dict(challenge.session)
    expired_session["expires_at"] = time.time() - 1.0

    with pytest.raises(CloudAuthError, match="expired"):
        openrouter_oauth.complete_openrouter_authorization(
            expired_session,
            response={"code": "late-code", "state": expired_session["state"]},
        )


def test_session_cannot_be_replayed(isolated_catalog, monkeypatch):
    monkeypatch.setattr(oauth, "open_in_browser", lambda url: False)
    calls = []

    def fake_post_json(url, payload, timeout=30.0):
        calls.append(payload)
        return {"key": "sk-or-v1-first"}

    monkeypatch.setattr(oauth, "post_json", fake_post_json)

    challenge = openrouter_oauth.begin_openrouter_authorization()
    response = {"code": "one-time-code", "state": challenge.session["state"]}
    openrouter_oauth.complete_openrouter_authorization(challenge.session, response=response)

    with pytest.raises(CloudAuthError, match="already been used"):
        openrouter_oauth.complete_openrouter_authorization(challenge.session, response=response)
    assert len(calls) == 1


def test_stale_pending_receivers_are_reaped():
    stale_receiver = oauth.LoopbackReceiver()
    stale_receiver.created_at = time.monotonic() - (oauth.DEFAULT_AUTH_TIMEOUT + 60.0)
    with oauth._PENDING_LOCK:
        oauth._PENDING["stale-state"] = stale_receiver

    oauth._reap_expired_receivers()
    with oauth._PENDING_LOCK:
        assert "stale-state" not in oauth._PENDING


def test_post_json_extracts_nested_openrouter_error_message(monkeypatch):
    body = json.dumps({"error": {"message": "Invalid code", "code": 400}}).encode("utf-8")

    def fake_urlopen(request, timeout=30.0):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(transport, "urlopen", fake_urlopen)

    with pytest.raises(CloudAuthError, match="Token request rejected: Invalid code"):
        oauth.post_json("https://openrouter.ai/api/v1/auth/keys", {"code": "bad"})


def test_cli_providers_add_and_login_seed_openrouter_free_model(
    isolated_catalog, monkeypatch
):
    state = None

    # 1. `/providers add` with provider=openrouter seeds `openrouter/openrouter/free`
    asyncio.run(
        cli_catalog.cmd_providers(
            state,
            ["add", 'label="OpenRouter Manual"', "provider=openrouter", "api_key=sk-or-manual"],
        )
    )
    models = models_store.load_models()
    assert len(models) == 1
    assert models[0]["id"] == "openrouter/openrouter/free"
    assert models[0]["connection_id"] == "openrouter-manual"
    assert models[0]["discovered_num_ctx"] == 200000

    # 2. `/providers login openrouter` runs OAuth and provisions default connection + model
    monkeypatch.setattr(oauth, "post_json", lambda u, p, timeout=30.0: {"key": "sk-or-cli-oauth"})

    def auto_complete_in_browser(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        callback_url = query["callback_url"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(
                f"{callback_url}?code=cli-oauth-code", timeout=5
            ).read(),
            daemon=True,
        ).start()
        return True

    monkeypatch.setattr(oauth, "open_in_browser", auto_complete_in_browser)
    asyncio.run(cli_catalog.cmd_providers(state, ["login", "openrouter"]))

    or_conn = models_store.get_connection("openrouter")
    assert or_conn is not None
    assert or_conn["api_key"] == "sk-or-cli-oauth"
    second_model = models_store.get_model("openrouter/openrouter/free#openrouter")
    assert second_model is not None
    assert second_model["connection_id"] == "openrouter"
    assert second_model["discovered_num_ctx"] == 200000
