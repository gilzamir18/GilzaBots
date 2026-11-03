"""Provider connections and catalog models as slash commands.

Registering a model has two halves, and both were reachable only through the
desktop Settings dialog:

* a **provider connection** — a label, a LiteLLM provider, an API base and a
  key. Credentials live here, so rotating a key updates every model that uses
  the connection.
* a **catalog model** — a name under a connection, plus the capabilities and
  inference parameters that describe *that* model (`supports_thinking`,
  `num_ctx`, `temperature`, …).

``/set-main-model`` only points a project at an id. Without a catalog entry
behind that id, `config.get_agent_llm_kwargs` finds no `api_base`, no key and no
capabilities, so the run reaches the provider unauthenticated and every
per-model setting reads as unset. Registering is what makes an id usable.

These commands go in the shared registry, so they work in the REPL, in a
one-shot ``gilzabots -p``, over the stdin protocol and in the desktop chat's
command box: they change a global store, not terminal state.

Arguments are ``key=value`` pairs rather than positionals. Values may be quoted.
An unrecognised key is refused with the list of known fields instead of being
forwarded to the provider as an inference parameter -- a typo would otherwise
become a silent, invisible setting. Deliberate pass-through parameters are
written ``extra.<name>=<value>``.
"""

from __future__ import annotations

import shlex
from typing import Any

from . import terminal as T
from .cli_commands import REPLState, _parse_model_param_value, _registry
from .i18n import _
from rich.markup import escape as _escape

#: Catalog fields a model command accepts, with the way each value is read.
_MODEL_FIELDS: dict[str, str] = {
    "name": "str",
    "id": "str",
    "connection_id": "str",
    "supports_thinking": "bool",
    "requires_single_system_message": "bool",
    "prompt_profile": "prompt_profile",
    "orchestrator_policy": "orchestrator_policy",
    "num_ctx": "int",
    "temperature": "float",
    "max_tokens": "int",
    "seed": "int",
    "top_p": "float",
    "top_k": "int",
    "min_p": "float",
    "frequency_penalty": "float",
    "presence_penalty": "float",
    "repetition_penalty": "float",
    "reasoning_effort": "str",
    "supports_image_generation": "bool",
    "image_route": "str",
    "supports_speech_synthesis": "bool",
    "speech_route": "str",
}

#: Spellings accepted for a field, so the command reads the way it is spoken.
_MODEL_ALIASES = {
    "connection": "connection_id",
    "thinking": "supports_thinking",
    "profile": "prompt_profile",
    "policy": "orchestrator_policy",
    "context": "num_ctx",
}

_CONNECTION_FIELDS: dict[str, str] = {
    "id": "str",
    "label": "str",
    "provider": "str",
    "api_key": "str",
    "api_base": "str",
}

_CONNECTION_ALIASES = {"key": "api_key", "base": "api_base", "url": "api_base"}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


class ArgumentError(ValueError):
    """A key=value argument the command refuses to guess at."""


def parse_pairs(
    args: list[str],
    fields: dict[str, str],
    aliases: dict[str, str],
    *,
    allow_extra: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read ``key=value`` arguments into (known fields, pass-through params)."""
    text = " ".join(args).strip()
    if not text:
        return {}, {}
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ArgumentError(f"Could not read the arguments: {exc}") from exc

    values: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise ArgumentError(
                f"'{token}' is not a key=value pair. "
                f"Known keys: {', '.join(sorted(fields))}."
            )
        key, raw = token.split("=", 1)
        key = key.strip()
        raw = raw.strip()

        if key.startswith("extra."):
            if not allow_extra:
                raise ArgumentError("Pass-through parameters are only accepted for models.")
            name = key[len("extra."):].strip()
            if not name:
                raise ArgumentError("'extra.' needs a parameter name after the dot.")
            extra[name] = _parse_model_param_value(raw)
            continue

        key = aliases.get(key, key)
        kind = fields.get(key)
        if kind is None:
            raise ArgumentError(
                f"Unknown field '{key}'. Known fields: {', '.join(sorted(fields))}"
                + (". Use extra.<name>=<value> for a provider parameter." if allow_extra else ".")
            )
        values[key] = _coerce(key, raw, kind)
    return values, extra


def _coerce(key: str, raw: str, kind: str) -> Any:
    if kind == "str":
        return raw
    low = raw.lower()
    if kind == "bool":
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ArgumentError(f"'{key}' takes true or false, not '{raw}'.")
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ArgumentError(f"'{key}' takes a whole number, not '{raw}'.") from None
    if kind == "float":
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise ArgumentError(f"'{key}' takes a number, not '{raw}'.") from None
    if kind == "prompt_profile":
        if low not in ("full", "light"):
            raise ArgumentError(f"'{key}' takes full or light, not '{raw}'.")
        return low
    if kind == "orchestrator_policy":
        if low not in ("direct", "delegate"):
            raise ArgumentError(f"'{key}' takes direct or delegate, not '{raw}'.")
        return low
    return raw


def _mask(secret: str) -> str:
    """Show that a key exists without printing it into a terminal or a log."""
    if not secret:
        return "—"
    return f"{secret[:4]}…{secret[-2:]}" if len(secret) > 8 else "set"


# ─── /providers ───────────────────────────────────────────────────────────────


@_registry.register(
    "/providers", "/provider",
    usage="[list | add key=value... | login [openrouter] [id=<id>] [label=<label>] | set <id> key=value... | remove <id>]",
    description="List, register, edit, or remove a provider connection (credentials)",
    details='A connection holds the credentials that every model under it uses,\nso rotating a key once updates all of them.\n\nExamples:\n  /providers                               list connections and how many models each carries\n  /providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1\n  /providers add label="OpenAI" provider=openai api_key=sk-...\n  /providers login openrouter              sign in via OpenRouter OAuth PKCE and add openrouter/free\n  /providers add id=openai label="OpenAI" provider=openai api_key=       clear the key\n  /providers set openai api_key=sk-new    change one field, keep the rest\n  /providers remove ollama-gil\n\nStep by step instead: /add-provider. One field: /set-provider-field.\n\nFields: label, provider, api_base (=base, =url), api_key (=key), id.\n\nThe id is derived from the label unless you pass id=. Adding with an existing\nid edits that connection, and leaving api_key out keeps the stored one.\nA connection still used by a model is not removed.',
)
async def cmd_providers(state: REPLState, args: list[str]) -> str | None:
    import asyncio
    from .models_store import (
        add_or_update_connection, delete_connection,
        ensure_default_openrouter_free_model, get_connection,
        load_connections, load_models, suggest_connection_id,
    )

    parts = " ".join(args).split()
    action = parts[0].lower() if parts else "list"
    rest = parts[1:]

    if action == "list":
        connections = load_connections()
        if not connections:
            T.info(_("cli_no_providers"))
            return "continue"
        counts: dict[str, int] = {}
        for model in load_models():
            counts[model.get("connection_id", "")] = counts.get(model.get("connection_id", ""), 0) + 1
        T.console.print(f"\n[dim]{_('cli_providers_header')}[/dim]")
        for connection in connections:
            used = counts.get(connection["id"], 0)
            T.console.print(
                f"  [cyan]{_escape(connection['id'])}[/cyan]  "
                f"[bold]{_escape(connection.get('label') or '')}[/bold]  "
                f"[dim]provider={_escape(connection.get('provider') or '—')}  "
                f"base={_escape(connection.get('api_base') or '—')}  "
                f"key={_mask(connection.get('api_key') or '')}  "
                f"models={used}[/dim]"
            )
        T.console.print()
        return "continue"

    if action == "add":
        try:
            values, _unused = parse_pairs(rest, _CONNECTION_FIELDS, _CONNECTION_ALIASES)
        except ArgumentError as exc:
            T.error(str(exc))
            return "continue"
        label = str(values.get("label") or "").strip()
        provider = str(values.get("provider") or "").strip()
        if not label or not provider:
            T.error(_("cli_usage_providers_add"))
            return "continue"
        connection_id = str(values.get("id") or "").strip() or suggest_connection_id(label)
        existing = get_connection(connection_id)
        payload = {
            "id": connection_id,
            "label": label,
            "provider": provider,
            "api_key": values.get("api_key", existing.get("api_key", "") if existing else ""),
            "api_base": values.get("api_base", existing.get("api_base", "") if existing else ""),
        }
        try:
            add_or_update_connection(payload)
        except Exception as exc:
            T.error(str(exc))
            return "continue"
        T.success(_(
            "cli_provider_updated" if existing else "cli_provider_added",
            id=connection_id,
        ))
        if provider.lower() == "openrouter":
            added_model = ensure_default_openrouter_free_model(connection_id)
            if added_model and added_model.get("id"):
                await _discover_saved_model_window(added_model["id"])
                T.info(_(
                    "cli_provider_default_model_added",
                    model_id=added_model["id"],
                    id=connection_id,
                ))
        return "continue"

    if action in ("login", "oauth"):
        from .cloud.base import CloudError
        from .openrouter_oauth import (
            begin_openrouter_authorization,
            cancel_openrouter_authorization,
            complete_openrouter_authorization,
        )

        pair_tokens = list(rest)
        if pair_tokens and "=" not in pair_tokens[0]:
            target_provider = pair_tokens.pop(0).strip().lower()
            if target_provider != "openrouter":
                T.error(_("cli_usage_providers_login"))
                return "continue"
        try:
            values, _unused = parse_pairs(pair_tokens, _CONNECTION_FIELDS, _CONNECTION_ALIASES)
        except ArgumentError as exc:
            T.error(str(exc))
            return "continue"

        provider_arg = str(values.get("provider") or "openrouter").strip().lower()
        if provider_arg != "openrouter":
            T.error(_("cli_usage_providers_login"))
            return "continue"

        conn_id = str(values.get("id") or "").strip()
        label = str(values.get("label") or "").strip()
        try:
            challenge = begin_openrouter_authorization(
                label=label,
                connection_id=conn_id,
            )
        except Exception as exc:
            T.error(str(exc))
            return "continue"

        T.info(_("cli_openrouter_oauth_waiting"))
        T.console.print(
            f"[dim]{_escape(_('cli_openrouter_oauth_url', url=challenge.authorization_url))}[/dim]"
        )
        try:
            result = await asyncio.to_thread(
                complete_openrouter_authorization,
                challenge.session,
                connection_id=conn_id,
                label=label,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            cancel_openrouter_authorization(challenge.session)
            T.warning(_("cli_action_cancelled"))
            return "continue"
        except CloudError as exc:
            T.error(str(exc))
            return "continue"
        except Exception as exc:
            T.error(str(exc))
            return "continue"

        saved_conn = result.get("connection") or {}
        saved_id = str(saved_conn.get("id") or conn_id or "openrouter")
        T.success(_("cli_provider_added", id=saved_id))
        default_model = result.get("default_model")
        if result.get("added_default_model") and isinstance(default_model, dict) and default_model.get("id"):
            T.info(_(
                "cli_provider_default_model_added",
                model_id=default_model["id"],
                id=saved_id,
            ))
        return "continue"

    if action == "set":
        if len(rest) < 2:
            T.error(_("cli_usage_providers_set"))
            return "continue"
        try:
            values, _unused = parse_pairs(rest[1:], _CONNECTION_FIELDS, _CONNECTION_ALIASES)
        except ArgumentError as exc:
            T.error(str(exc))
            return "continue"
        update_connection_fields(rest[0], values)
        return "continue"

    if action == "remove":
        if not rest:
            T.error(_("cli_usage_providers_remove"))
            return "continue"
        connection_id = rest[0]
        try:
            removed = delete_connection(connection_id)
        except ValueError as exc:
            # Refused because models still reference it; say which.
            T.error(str(exc))
            return "continue"
        if removed:
            T.success(_("cli_provider_removed", id=connection_id))
        else:
            T.error(_("cli_provider_not_found", id=connection_id))
        return "continue"

    T.error(_("cli_usage_providers"))
    return "continue"


def update_connection_fields(connection_id: str, values: dict[str, Any]) -> bool:
    """Change some fields of an existing connection, keeping the others.

    The id is what models reference, so it never changes here
    (`models_store.add_or_update_connection` keeps it stable too); a label or
    provider cannot be emptied, since the desktop form refuses that as well.
    """
    from .models_store import add_or_update_connection, get_connection

    existing = get_connection(connection_id)
    if existing is None:
        T.error(_("cli_provider_not_found", id=connection_id))
        return False
    if "id" in values and values["id"] != connection_id:
        T.error(_("cli_provider_id_fixed", id=connection_id))
        return False
    if not values:
        T.error(_("cli_usage_providers_set"))
        return False
    payload = dict(existing)
    payload.update({k: v for k, v in values.items() if k != "id"})
    for required in ("label", "provider"):
        if not str(payload.get(required) or "").strip():
            T.error(_("cli_provider_field_required", field=required))
            return False
    try:
        add_or_update_connection(payload)
    except Exception as exc:
        T.error(str(exc))
        return False
    T.success(_("cli_provider_updated", id=connection_id))
    return True


# ─── /models (catalog half) ───────────────────────────────────────────────────


def _describe_num_ctx(model: dict) -> str:
    """num_ctx as the list and `show` print it, saying what "auto" resolved to."""
    if model.get("num_ctx"):
        return str(model["num_ctx"])
    from .config import auto_num_ctx

    window, source = auto_num_ctx(model.get("id"), allow_network=False)
    if source == "local_default":
        return f"auto ({window}, local default)"
    if source == "provider":
        return f"auto ({window}, reported by the provider)"
    return "auto (unknown: no limit applied)"


async def _discover_saved_model_window(model_id: str) -> None:
    """Let an "Auto" entry learn its window from the provider right after a save."""
    import asyncio
    from .context_discovery import ensure_provider_context_window

    await asyncio.to_thread(ensure_provider_context_window, model_id)


def _print_model(model: dict) -> None:
    T.console.print(f"\n[bold]{_escape(model.get('id') or '')}[/bold]")
    rows = [
        ("name", model.get("name")),
        ("provider", model.get("provider")),
        ("connection", f"{model.get('connection_id') or '—'} ({model.get('connection_label') or '—'})"),
        ("api_base", model.get("api_base") or "—"),
        ("api_key", _mask(model.get("api_key") or "")),
        ("num_ctx", _describe_num_ctx(model)),
        ("supports_thinking", model.get("supports_thinking")),
        ("requires_single_system_message", model.get("requires_single_system_message")),
        ("prompt_profile", model.get("prompt_profile")),
        ("orchestrator_policy", model.get("orchestrator_policy")),
    ]
    for key in ("temperature", "max_tokens", "seed", "top_p", "top_k", "min_p",
                "frequency_penalty", "presence_penalty", "repetition_penalty",
                "reasoning_effort"):
        if model.get(key) is not None:
            rows.append((key, model.get(key)))
    for key, value in rows:
        T.console.print(f"  [cyan]{key:<32}[/cyan] {_escape(str(value))}")
    extra = model.get("extra_model_params") or {}
    for key, value in extra.items():
        T.console.print(f"  [cyan]extra.{key:<26}[/cyan] {_escape(str(value))}")
    T.console.print()


#: Fields a model row shows but does not own: they are joined from its provider
#: connection (`models_store._row_to_model`). Writing them back would leave a
#: copy of the connection's key in the model row, stale the moment it is
#: rotated. The desktop form never sends them either.
_CONNECTION_OWNED = ("provider", "api_key", "api_base", "connection_label")


def closest_model_id(model_id: str) -> str:
    """The catalog id `model_id` most likely meant, or "" when none is close."""
    import difflib
    from .models_store import load_models

    ids = [m["id"] for m in load_models() if m.get("id")]
    matches = difflib.get_close_matches(model_id, ids, n=1, cutoff=0.8)
    return matches[0] if matches else ""


def report_unknown_model(model_id: str, message: str) -> None:
    """Print `message` for an id missing from the catalog, naming a near miss."""
    suggestion = closest_model_id(model_id)
    if suggestion:
        message = f"{message} {_('cli_model_did_you_mean', id=suggestion)}"
    T.error(message)


async def catalog_models(state: REPLState, action: str, rest: list[str]) -> str:
    """The catalog half of `/models`: list, show, add, set, remove."""
    from .models_store import (
        add_or_update_model, delete_model, get_connection, get_model,
        load_models, suggest_model_id,
    )

    if action == "list":
        models = load_models()
        if not models:
            T.info(_("cli_no_catalog_models"))
            return "continue"
        T.console.print(f"\n[dim]{_('cli_catalog_header')}[/dim]")
        for model in models:
            T.console.print(
                f"  [cyan]{_escape(model.get('id') or '')}[/cyan]  "
                f"[dim]connection={_escape(model.get('connection_id') or '—')}  "
                f"ctx={_escape(_describe_num_ctx(model))}  "
                f"thinking={'yes' if model.get('supports_thinking') else 'no'}  "
                f"profile={model.get('prompt_profile')}/{model.get('orchestrator_policy')}[/dim]"
            )
        T.console.print()
        return "continue"

    if action == "show":
        if not rest:
            T.error(_("cli_usage_models_show"))
            return "continue"
        model = get_model(" ".join(rest).strip())
        if model is None:
            report_unknown_model(" ".join(rest).strip(), _("cli_model_not_found", id=" ".join(rest).strip()))
            return "continue"
        _print_model(model)
        return "continue"

    if action == "remove":
        if not rest:
            T.error(_("cli_usage_models_remove"))
            return "continue"
        model_id = " ".join(rest).strip()
        if delete_model(model_id):
            T.success(_("cli_model_removed", id=model_id))
        else:
            report_unknown_model(model_id, _("cli_model_not_found", id=model_id))
        return "continue"

    if action == "add":
        try:
            values, extra = parse_pairs(rest, _MODEL_FIELDS, _MODEL_ALIASES, allow_extra=True)
        except ArgumentError as exc:
            T.error(str(exc))
            return "continue"
        name = str(values.get("name") or "").strip()
        connection_id = str(values.get("connection_id") or "").strip()
        if not name or not connection_id:
            T.error(_("cli_usage_models_add"))
            return "continue"
        connection = get_connection(connection_id)
        if connection is None:
            T.error(_("cli_provider_not_found", id=connection_id))
            return "continue"
        payload = dict(values)
        payload["name"] = name
        payload["connection_id"] = connection_id
        payload["id"] = str(values.get("id") or "").strip() or suggest_model_id(
            connection.get("provider", ""), name, connection_id
        )
        if extra:
            payload["extra_model_params"] = extra
        try:
            add_or_update_model(payload)
        except Exception as exc:
            T.error(str(exc))
            return "continue"
        await _discover_saved_model_window(payload["id"])
        T.success(_("cli_model_added", id=payload["id"]))
        T.console.print(f"[dim]{_('cli_model_added_hint', id=payload['id'])}[/dim]")
        return "continue"

    if action == "set":
        if not rest:
            T.error(_("cli_usage_models_set"))
            return "continue"
        model_id, pairs = rest[0], rest[1:]
        model = get_model(model_id)
        if model is None:
            report_unknown_model(model_id, _("cli_model_not_found", id=model_id))
            return "continue"
        if not pairs:
            T.error(_("cli_usage_models_set"))
            return "continue"
        try:
            values, extra = parse_pairs(pairs, _MODEL_FIELDS, _MODEL_ALIASES, allow_extra=True)
        except ArgumentError as exc:
            T.error(str(exc))
            return "continue"
        payload = {k: v for k, v in model.items() if k not in _CONNECTION_OWNED}
        payload.update(values)
        # `previous_id` is how the store replaces an entry whose id changed
        # instead of appending a second one under the new id.
        payload["previous_id"] = model_id
        if extra:
            merged = dict(model.get("extra_model_params") or {})
            merged.update(extra)
            payload["extra_model_params"] = merged
        if values.get("connection_id") and get_connection(values["connection_id"]) is None:
            T.error(_("cli_provider_not_found", id=values["connection_id"]))
            return "continue"
        try:
            add_or_update_model(payload)
        except Exception as exc:
            T.error(str(exc))
            return "continue"
        await _discover_saved_model_window(payload.get("id") or model_id)
        T.success(_("cli_model_updated", id=payload.get("id") or model_id))
        return "continue"

    T.error(_("cli_usage_models_catalog"))
    return "continue"


# ─── Step-by-step registration and single-field commands ─────────────────────
#
# `/providers add label=... provider=...` and `/models add name=...
# connection=...` need the field names up front, which is fine in a script and
# a wall to someone registering their first model. The wizards below ask for
# the fields the desktop forms ask for, one at a time, and leave the rest at
# the same defaults the form starts from. They read the keyboard, so they are
# terminal-only; the desktop app has its own dialogs.
#
# The single-field commands are the key=value commands spelled for one change
# (`/set-model-field <id> temperature 0.3`), and reuse their validation.

#: Where a local Ollama answers. The desktop New Project form starts from it.
_OLLAMA_DEFAULT_BASE = "http://localhost:11434/v1"
_OLLAMA_PROVIDERS = ("ollama", "ollama_chat")


class _WizardCancelled(Exception):
    """The user left a wizard; nothing was saved."""


def _prompt_line(label: str, default: str = "", *, secret: bool = False) -> str:
    """Ask for one value. An empty answer takes ``default``."""
    shown = f"{label} [{default}]" if default and not secret else label
    T.console.print(f"\n[bold yellow]?[/bold yellow] {_escape(shown)}")
    try:
        if secret:
            import getpass
            raw = getpass.getpass("  → ")
        else:
            raw = input("  → ")
    except EOFError:
        # No keyboard behind stdin (a piped `gilzabots -p`): there is nobody to
        # answer, and waiting or guessing would both be wrong.
        raise _WizardCancelled(_("cli_wizard_needs_terminal")) from None
    raw = raw.strip()
    try:
        T._check_cancel(raw)
    except (T.UserCancelled, T.AppExit):
        raise _WizardCancelled(_("cli_action_cancelled")) from None
    return raw or default


def _prompt_required(label: str, default: str = "") -> str:
    while True:
        value = _prompt_line(label, default)
        if value:
            return value
        T.error(_("cli_wizard_required"))


def _prompt_bool(label: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _prompt_line(f"{label} ({hint})").lower()
        if not raw:
            return default
        if raw in _TRUE or raw in ("y", "s", "sim"):
            return True
        if raw in _FALSE or raw in ("n", "nao", "não"):
            return False
        T.error(_("cli_wizard_yes_no"))


def _prompt_optional_int(label: str, default: int | None) -> int | None:
    shown_default = str(default) if default is not None else _("cli_wizard_auto")
    while True:
        raw = _prompt_line(label, shown_default)
        if raw == shown_default:
            return default
        try:
            value = int(raw)
        except ValueError:
            T.error(_("cli_wizard_whole_number"))
            continue
        if value > 0:
            return value
        T.error(_("cli_wizard_whole_number"))


def _connection_choice_label(connection: dict) -> str:
    return (
        f"{connection.get('label') or connection['id']}  "
        f"({connection.get('provider') or '—'}, id={connection['id']})"
    )


@_registry.register(
    "/add-provider", "/add_provider", cli_only=True,
    description="Register a provider connection step by step (label, provider, key, URL)",
    details="Asks for each field of a provider connection, as the desktop\n"
            "Add Connection form does, and shows the result before saving.\n\n"
            "Afterwards:\n"
            "  /add-model                               register a model under it\n"
            "  /set-provider-field <id> <field> <value> change one field\n"
            "  /remove-provider <id>                    remove it\n\n"
            "In one line instead: /providers add label=... provider=...",
)
async def cmd_add_provider(state: REPLState, args: list[str]) -> str:
    from .models_store import (
        add_or_update_connection,
        ensure_default_openrouter_free_model,
        suggest_connection_id,
    )

    T.info(_("cli_wizard_provider_intro"))
    try:
        label = _prompt_required(_("cli_wizard_provider_label"))
        provider = _prompt_required(_("cli_wizard_provider_provider")).strip().lower()
        api_key = _prompt_line(_("cli_wizard_provider_key"), secret=True)
        api_base = _prompt_line(
            _("cli_wizard_provider_base"),
            _OLLAMA_DEFAULT_BASE if provider in _OLLAMA_PROVIDERS else "",
        )
        payload = {
            "id": suggest_connection_id(label),
            "label": label,
            "provider": provider,
            "api_key": api_key,
            "api_base": api_base,
        }
        T.console.print(f"\n[dim]{_escape(_('cli_wizard_summary'))}[/dim]")
        for key in ("id", "label", "provider", "api_base"):
            T.console.print(f"  [cyan]{key:<10}[/cyan] {_escape(payload[key] or '—')}")
        T.console.print(f"  [cyan]{'api_key':<10}[/cyan] {_mask(api_key)}")
        if not _prompt_bool(_("cli_wizard_save"), True):
            raise _WizardCancelled(_("cli_action_cancelled"))
    except _WizardCancelled as exc:
        T.warning(str(exc))
        return "continue"

    try:
        add_or_update_connection(payload)
    except Exception as exc:
        T.error(str(exc))
        return "continue"
    T.success(_("cli_provider_added", id=payload["id"]))
    if provider == "openrouter":
        added_model = ensure_default_openrouter_free_model(payload["id"])
        if added_model and added_model.get("id"):
            await _discover_saved_model_window(added_model["id"])
            T.info(_(
                "cli_provider_default_model_added",
                model_id=added_model["id"],
                id=payload["id"],
            ))
    T.console.print(f"[dim]{_escape(_('cli_wizard_provider_next', id=payload['id']))}[/dim]")
    return "continue"


@_registry.register(
    "/add-model", "/add_model", cli_only=True,
    description="Register a model step by step (provider, name, context window, thinking)",
    details="Asks for the main fields of a catalog model -- its provider connection,\n"
            "its name, the context window (num_ctx) and whether it supports thinking --\n"
            "and leaves every other field at the default the desktop form starts from.\n"
            "The entry is printed in full once saved.\n\n"
            "Needs a provider connection first: /add-provider.\n\n"
            "Afterwards:\n"
            "  /set-model-field <id> <field> <value>    change one field (temperature, top_p, ...)\n"
            "  /set-main-model <id>                     use it in this project\n"
            "  /remove-model <id>                       remove it\n\n"
            "In one line instead: /models add name=... connection=...",
)
async def cmd_add_model(state: REPLState, args: list[str]) -> str:
    from .models_store import (
        add_or_update_model, get_model, load_connections, normalize_model_entry,
        suggest_model_id,
    )

    connections = load_connections()
    if not connections:
        T.error(_("cli_add_model_needs_provider"))
        return "continue"

    # The values a new entry starts from in the desktop form.
    defaults = normalize_model_entry({})
    try:
        if len(connections) == 1:
            connection = connections[0]
            T.info(_("cli_wizard_model_only_provider", provider=_connection_choice_label(connection)))
        else:
            by_label = {_connection_choice_label(c): c for c in connections}
            picked = T.choose(_("cli_wizard_model_provider"), list(by_label))
            connection = by_label[picked]
        name = _prompt_required(_("cli_wizard_model_name"))
        num_ctx = _prompt_optional_int(_("cli_wizard_model_num_ctx"), defaults["num_ctx"])
        supports_thinking = _prompt_bool(
            _("cli_wizard_model_thinking"), defaults["supports_thinking"]
        )
    except (T.UserCancelled, T.AppExit):
        T.warning(_("cli_action_cancelled"))
        return "continue"
    except EOFError:
        T.warning(_("cli_wizard_needs_terminal"))
        return "continue"
    except _WizardCancelled as exc:
        T.warning(str(exc))
        return "continue"

    model_id = suggest_model_id(connection.get("provider", ""), name, connection["id"])
    payload = {
        "id": model_id,
        "name": name,
        "connection_id": connection["id"],
        "num_ctx": num_ctx,
        "supports_thinking": supports_thinking,
    }
    try:
        add_or_update_model(payload)
    except Exception as exc:
        T.error(str(exc))
        return "continue"
    await _discover_saved_model_window(model_id)
    T.success(_("cli_model_added", id=model_id))
    saved = get_model(model_id)
    if saved is not None:
        _print_model(saved)
    T.console.print(f"[dim]{_escape(_('cli_wizard_model_next', id=model_id))}[/dim]")
    return "continue"


def _split_field_args(args: list[str]) -> list[str] | None:
    """`<id> <field> <value...>` read the shell way; None when unreadable."""
    try:
        return shlex.split(" ".join(args))
    except ValueError as exc:
        T.error(f"Could not read the arguments: {exc}")
        return None


@_registry.register(
    "/set-provider-field", "/set_provider_field", usage="<id> <field> <value>",
    description="Change one field of a provider connection, keeping the others",
    details="Examples:\n"
            "  /set-provider-field openai api_key sk-new\n"
            "  /set-provider-field ollama-gil api_base http://192.168.0.10:11434/v1\n"
            "  /set-provider-field ollama-gil label \"Ollama at home\"\n"
            "  /set-provider-field openai api_key \"\"          clear the key\n\n"
            "Fields: label, provider, api_key (=key), api_base (=base, =url).\n"
            "The id is fixed: the models under the connection refer to it.",
)
async def cmd_set_provider_field(state: REPLState, args: list[str]) -> str:
    tokens = _split_field_args(args)
    if tokens is None:
        return "continue"
    if len(tokens) < 3:
        T.error(_("cli_usage_set_provider_field"))
        return "continue"
    connection_id, field, value = tokens[0], tokens[1], " ".join(tokens[2:])
    try:
        values, _unused = parse_pairs(
            [shlex.quote(f"{field}={value}")], _CONNECTION_FIELDS, _CONNECTION_ALIASES
        )
    except ArgumentError as exc:
        T.error(str(exc))
        return "continue"
    update_connection_fields(connection_id, values)
    return "continue"


@_registry.register(
    "/remove-provider", "/remove_provider", usage="<id>",
    description="Remove a provider connection that no model uses",
)
async def cmd_remove_provider(state: REPLState, args: list[str]) -> str | None:
    tokens = _split_field_args(args)
    if not tokens:
        T.error(_("cli_usage_providers_remove"))
        return "continue"
    return await cmd_providers(state, ["remove", tokens[0]])


@_registry.register(
    "/set-model-field", "/set_model_field", usage="<id> <field> <value>",
    description="Change one field of a catalog model, keeping the others",
    details="Examples:\n"
            "  /set-model-field ollama/gemma4:26b temperature 0.3\n"
            "  /set-model-field ollama/gemma4:26b num_ctx 65536\n"
            "  /set-model-field ollama/gemma4:26b prompt_profile light\n"
            "  /set-model-field ollama/gemma4:26b extra.keep_alive 30m\n\n"
            "Fields: those listed by /help /models. The change applies to every\n"
            "project using the model. A project-only override (num_ctx, stop, ...)\n"
            "is /set-model-param instead.",
)
async def cmd_set_model_field(state: REPLState, args: list[str]) -> str:
    tokens = _split_field_args(args)
    if tokens is None:
        return "continue"
    if len(tokens) < 3:
        T.error(_("cli_usage_set_model_field"))
        return "continue"
    model_id, field, value = tokens[0], tokens[1], " ".join(tokens[2:])
    return await catalog_models(state, "set", [model_id, shlex.quote(f"{field}={value}")])


@_registry.register(
    "/remove-model", "/remove_model", usage="<id>",
    description="Remove a model from the catalog",
)
async def cmd_remove_model(state: REPLState, args: list[str]) -> str:
    tokens = _split_field_args(args)
    if not tokens:
        T.error(_("cli_usage_models_remove"))
        return "continue"
    return await catalog_models(state, "remove", [tokens[0]])
