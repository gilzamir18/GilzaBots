"""OpenRouter OAuth PKCE flow and default free-model provisioning.

OpenRouter supports a single-step PKCE authorization flow for desktop and CLI
applications without requiring pre-registered client credentials:

1. Direct the system browser to ``https://openrouter.ai/auth`` with a loopback
   ``callback_url``, ``code_challenge`` (SHA-256), ``code_challenge_method=S256``,
   and CSRF ``state``.
2. Receive the one-time authorization ``code`` on ``127.0.0.1``.
3. Exchange the ``code`` and ``code_verifier`` via
   ``POST https://openrouter.ai/api/v1/auth/keys`` for a dedicated user API key.

The loopback listener, PKCE generator, browser launcher, and HTTP transport in
:mod:`opalatex.cloud.oauth` are reused directly so OpenRouter and Google Drive
share the same consent-screen and cancellation lifecycle.
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from collections import deque
from typing import Any, Dict, Optional

from .cloud import oauth
from .cloud.base import AuthChallenge, CloudAuthError
from .context_discovery import ensure_provider_context_window
from .models_store import (
    OPENROUTER_API_BASE,
    OPENROUTER_DEFAULT_CONNECTION_ID,
    OPENROUTER_DEFAULT_CONNECTION_LABEL,
    OPENROUTER_FREE_MODEL_NAME,
    add_or_update_connection,
    ensure_default_openrouter_free_model,
    get_connection,
    get_model,
    load_models,
    suggest_connection_id,
)

OPENROUTER_AUTH_ENDPOINT = "https://openrouter.ai/auth"
OPENROUTER_KEYS_ENDPOINT = "https://openrouter.ai/api/v1/auth/keys"

# OpenRouter authorization codes expire after 10 minutes; we bound our local
# authorization session to 5 minutes (matching DEFAULT_AUTH_TIMEOUT) so stale
# listeners are reaped promptly and expired codes fail fast before exchange.
OPENROUTER_CODE_TTL_SECONDS = oauth.DEFAULT_AUTH_TIMEOUT

_CONSUMED_LOCK = threading.Lock()
_CONSUMED_STATES: deque[str] = deque(maxlen=256)


def _mark_state_consumed(state: str) -> bool:
    """Record *state* as consumed. Returns False if it was already consumed."""
    with _CONSUMED_LOCK:
        if state in _CONSUMED_STATES:
            return False
        _CONSUMED_STATES.append(state)
        return True


def _remaining_session_ttl(session: dict, default_timeout: float) -> float:
    """Return the remaining lifetime in seconds for *session*, or raise if expired."""
    expires_at = session.get("expires_at")
    if expires_at is None:
        return default_timeout
    try:
        remaining = float(expires_at) - time.time()
    except (TypeError, ValueError):
        return default_timeout
    if remaining <= 0:
        raise CloudAuthError(
            "The authorization session has expired. Please start the OpenRouter sign-in again."
        )
    return min(float(default_timeout), remaining)


def begin_openrouter_authorization(
    *,
    label: str = "",
    connection_id: str = "",
) -> AuthChallenge:
    """Start an OpenRouter OAuth PKCE flow on a loopback listener."""
    receiver = oauth.LoopbackReceiver()
    state = secrets.token_urlsafe(24)
    pkce = oauth.generate_pkce()
    oauth.register_pending_receiver(state, receiver)

    # Embed `state` both in the callback path and as a query parameter so CSRF
    # verification succeeds whether the authorization server preserves query
    # parameters on `callback_url` or only appends `?code=...` to its path.
    callback_url = f"{receiver.redirect_uri}/callback/{state}"
    params = {
        "callback_url": callback_url,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
        "state": state,
    }
    url = f"{OPENROUTER_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    launched = oauth.open_in_browser(url)

    now = time.time()
    session = {
        "state": state,
        "code_verifier": pkce.verifier,
        "code_challenge_method": pkce.method,
        "callback_url": callback_url,
        "redirect_uri": receiver.redirect_uri,
        "started_at": now,
        "expires_at": now + OPENROUTER_CODE_TTL_SECONDS,
        "connection_id": str(connection_id or "").strip(),
        "label": str(label or "").strip(),
    }
    instructions = (
        "Complete the OpenRouter sign-in in the browser window that just opened."
        if launched
        else f"Open this URL in your browser to authorize OpalaTex with OpenRouter:\n{url}"
    )
    return AuthChallenge(
        authorization_url=url,
        session=session,
        instructions=instructions,
    )


def cancel_openrouter_authorization(session: dict) -> None:
    """Cancel a pending OpenRouter OAuth loopback listener."""
    if isinstance(session, dict):
        oauth.cancel_authorization(session)


def _resolve_target_connection_payload(
    *,
    api_key: str,
    connection_id: str = "",
    label: str = "",
) -> Dict[str, Any]:
    """Determine the connection ID, label, and API base to save for OpenRouter."""
    clean_id = str(connection_id or "").strip()
    clean_label = str(label or "").strip()

    if clean_id:
        existing = get_connection(clean_id)
        if existing:
            return {
                "id": existing["id"],
                "label": clean_label or existing.get("label") or OPENROUTER_DEFAULT_CONNECTION_LABEL,
                "provider": "openrouter",
                "api_key": api_key,
                "api_base": existing.get("api_base") or OPENROUTER_API_BASE,
            }
        return {
            "id": clean_id,
            "label": clean_label or OPENROUTER_DEFAULT_CONNECTION_LABEL,
            "provider": "openrouter",
            "api_key": api_key,
            "api_base": OPENROUTER_API_BASE,
        }

    if (
        not clean_label
        or clean_label.lower() == OPENROUTER_DEFAULT_CONNECTION_LABEL.lower()
    ):
        existing_default = get_connection(OPENROUTER_DEFAULT_CONNECTION_ID)
        if existing_default and str(existing_default.get("provider") or "").strip().lower() == "openrouter":
            return {
                "id": existing_default["id"],
                "label": clean_label or existing_default.get("label") or OPENROUTER_DEFAULT_CONNECTION_LABEL,
                "provider": "openrouter",
                "api_key": api_key,
                "api_base": existing_default.get("api_base") or OPENROUTER_API_BASE,
            }

    effective_label = clean_label or OPENROUTER_DEFAULT_CONNECTION_LABEL
    return {
        "id": suggest_connection_id(effective_label),
        "label": effective_label,
        "provider": "openrouter",
        "api_key": api_key,
        "api_base": OPENROUTER_API_BASE,
    }


def complete_openrouter_authorization(
    session: dict,
    *,
    connection_id: str = "",
    label: str = "",
    response: Optional[dict] = None,
    timeout: float = OPENROUTER_CODE_TTL_SECONDS,
) -> Dict[str, Any]:
    """Wait for the OpenRouter redirect, exchange the code, and save the connection."""
    if not isinstance(session, dict):
        raise CloudAuthError("Invalid authorization session. Start the connection again.")

    state = str(session.get("state") or "").strip()
    verifier = str(session.get("code_verifier") or "").strip()
    method = str(session.get("code_challenge_method") or "S256").strip()
    if not state or not verifier:
        raise CloudAuthError("No authorization is pending. Start the connection again.")

    try:
        wait_timeout = _remaining_session_ttl(session, timeout)
    except CloudAuthError:
        oauth.cancel_authorization(session)
        raise

    params = dict(response or {})
    if not params:
        receiver = oauth.pop_pending_receiver(state)
        if receiver is None:
            raise CloudAuthError(
                "No authorization is pending. Start the connection again."
            )
        try:
            params = receiver.wait(wait_timeout)
        finally:
            receiver.close()
    else:
        oauth.cancel_authorization(session)

    if not _mark_state_consumed(state):
        raise CloudAuthError(
            "This authorization session has already been used. Start a new connection."
        )

    if params.get("error"):
        raise CloudAuthError(f"Authorization was denied: {params['error']}")
    if params.get("state") != state:
        raise CloudAuthError("Authorization state mismatch; the response was discarded.")

    code = str(params.get("code") or "").strip()
    if not code:
        raise CloudAuthError("The authorization response carried no code.")

    # Verify the session has not expired while waiting before exchanging the code.
    _remaining_session_ttl(session, timeout)

    token_data = oauth.post_json(
        OPENROUTER_KEYS_ENDPOINT,
        {
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": method,
        },
    )
    api_key = str((token_data or {}).get("key") or "").strip()
    if not api_key:
        raise CloudAuthError("OpenRouter did not return an API key.")

    conn_payload = _resolve_target_connection_payload(
        api_key=api_key,
        connection_id=connection_id or str(session.get("connection_id") or ""),
        label=label or str(session.get("label") or ""),
    )
    add_or_update_connection(conn_payload)

    saved_conn_id = conn_payload["id"]
    added_model = ensure_default_openrouter_free_model(saved_conn_id)

    free_model = added_model
    if free_model is None:
        for existing_model in load_models():
            if (
                existing_model.get("connection_id") == saved_conn_id
                and str(existing_model.get("name") or "").strip() == OPENROUTER_FREE_MODEL_NAME
            ):
                free_model = existing_model
                break

    if free_model and free_model.get("id"):
        try:
            ensure_provider_context_window(free_model["id"])
        except Exception:
            pass
        free_model = get_model(free_model["id"]) or free_model

    return {
        "connection": get_connection(saved_conn_id) or conn_payload,
        "default_model": free_model,
        "added_default_model": added_model is not None,
    }

