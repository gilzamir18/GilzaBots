"""Compatibility helpers for LiteLLM/OpenAI chat requests."""
from __future__ import annotations

import json
from typing import Any, Callable

from gilzabots.config import model_requires_single_system_message, sanitize_litellm_kwargs_for_model


def sanitize_tool_call_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return messages with OpenAI tool-call adjacency repaired.

    OpenAI-compatible endpoints reject a request when an assistant message with
    ``tool_calls`` is not immediately followed by one ``tool`` message for each
    call id. Long-running agent histories can become malformed after an aborted
    turn, context eviction, or provider/tool parsing failure. This function keeps
    valid tool-call/result pairs intact, but downgrades incomplete tool-call
    records to plain assistant text so the provider never receives an orphaned
    function call id.

    The only invariant enforced is the provider one: every ``tool_calls`` id must
    have its matching ``tool`` result. What comes *before* the assistant message is
    not checked — ``assistant`` followed by ``assistant`` is valid everywhere, and
    the agent loop legitimately produces it (an empty-response correction, for
    example, inserts a ``system`` alert between two assistant turns). A ``system``
    alert that lands inside an otherwise complete tool block is re-emitted after
    the block instead of invalidating it: role and content are preserved, only the
    position changes, which is a transport-shape repair (PROJECT_DESIGN 2.6).
    """
    cleaned: list[dict[str, Any]] = []
    idx = 0

    while idx < len(messages):
        msg = dict(messages[idx] or {})

        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            if msg.get("role") == "tool":
                cleaned.append(_orphan_tool_as_system_message(msg))
            else:
                cleaned.append(msg)
            idx += 1
            continue

        tool_calls = [
            tc for tc in msg.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        ]
        if not tool_calls:
            msg.pop("tool_calls", None)
            cleaned.append(msg)
            idx += 1
            continue

        expected_ids = [tc["id"] for tc in tool_calls]
        expected = set(expected_ids)
        seen: set[str] = set()
        buffered_tool_messages: list[dict[str, Any]] = []
        orphan_tool_messages: list[dict[str, Any]] = []
        deferred_system_messages: list[dict[str, Any]] = []
        idx += 1

        while idx < len(messages):
            candidate = dict(messages[idx] or {})
            role = candidate.get("role")
            if role == "tool":
                tool_call_id = candidate.get("tool_call_id")
                if tool_call_id in expected and tool_call_id not in seen:
                    buffered_tool_messages.append(candidate)
                    seen.add(tool_call_id)
                else:
                    orphan_tool_messages.append(candidate)
                idx += 1
                continue
            if role == "system" and expected - seen:
                # A corrective alert raised while the tool batch was still being
                # processed. Keep it, but move it past the tool block.
                deferred_system_messages.append(candidate)
                idx += 1
                continue
            break

        by_id = {m.get("tool_call_id"): m for m in buffered_tool_messages}
        if not expected <= set(by_id):
            cleaned.append(_assistant_tool_calls_as_text(msg, tool_calls))
            for tool_msg in buffered_tool_messages + orphan_tool_messages:
                cleaned.append(_orphan_tool_as_system_message(tool_msg))
            cleaned.extend(deferred_system_messages)
            continue

        cleaned.append(msg)
        for tool_call in tool_calls:
            tool_call_id = tool_call["id"]
            cleaned.append(by_id[tool_call_id])
        for orphan in orphan_tool_messages:
            cleaned.append(_orphan_tool_as_system_message(orphan))
        cleaned.extend(deferred_system_messages)

    return cleaned


def consolidate_leading_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge every ``system`` message into a single leading message.

    Some chat templates reject a request outright with ``system message must
    be at the beginning`` as soon as more than one ``system``-role message is
    present, even when both are consecutive at the very start of the list.
    This was first observed with an Ollama-served qwen3.8, but it is a
    property of the model's chat template, not of the transport: the same
    rejection arrives from OpenAI-compatible servers (vLLM/NIM) hosting the
    same model families, so the behaviour is keyed on the catalog capability
    alone and never on the provider prefix. The chat-orchestrator always sends
    two leading system messages (the system prompt and the recursive summary)
    on every turn, and AgenticBlocks separately injects mid-turn corrective
    alerts with the ``system`` role (see PROJECT_DESIGN.md 2.6) so local models
    do not treat them as user-authored content. Merging their content into a
    single message satisfies that "one system message, first position"
    requirement without changing role or semantics. Callers should only invoke
    this for models whose catalog entry sets
    ``requires_single_system_message`` (see
    ``gilzabots.config.model_requires_single_system_message``) so every other
    model's request shape is unaffected.
    """
    system_msgs = [m for m in messages if (m or {}).get("role") == "system"]
    if len(system_msgs) <= 1:
        return messages
    merged_content = "\n\n".join(_system_message_text(m) for m in system_msgs)
    merged = {"role": "system", "content": merged_content}
    rest = [m for m in messages if (m or {}).get("role") != "system"]
    return [merged] + rest


def _system_message_text(msg: dict[str, Any] | None) -> str:
    """Return a system message's content as plain text.

    OpenAI-compatible providers accept content parts (a list of
    ``{"type": "text", "text": ...}`` dicts) as well as a bare string, so the
    merge must not stringify a list into its Python repr.
    """
    content = (msg or {}).get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if text:
                    parts.append(str(text))
            elif part:
                parts.append(str(part))
        return "\n\n".join(parts)
    return str(content or "")


def wrap_agent_litellm_compat(agent: Any) -> Any:
    """Patch an AgenticBlocks agent instance to sanitize messages before LiteLLM."""
    if getattr(agent, "_gilzabots_litellm_compat_wrapped", False):
        return agent

    agent_name = str(getattr(agent, "name", "") or "")
    if agent_name == "worker" or agent_name.startswith("skill_"):
        object.__setattr__(agent, "use_shared_router", False)

    original: Callable[..., Any] = agent._acompletion
    original_run: Callable[..., Any] | None = getattr(agent, "run", None)

    async def _acompletion_with_compat(messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        if agent_name == "worker" or agent_name.startswith("skill_"):
            object.__setattr__(agent, "use_shared_router", False)
        model = getattr(agent, "model", "") or kwargs.get("model", "")
        kwargs = sanitize_litellm_kwargs_for_model(model, kwargs)
        kwargs.setdefault("drop_params", True)
        sanitize_agent_state(agent)
        cleaned_messages = sanitize_tool_call_messages(messages)
        invalid_tools = _repeated_tool_validation_error_names(cleaned_messages)
        if invalid_tools:
            names = ", ".join(sorted(invalid_tools))
            # Disable only the tool whose schema the model repeatedly violated.
            # Keeping the remaining schemas lets it recover with a real read tool
            # and still reach create_plan. This is a loop breaker, not a semantic
            # fallback: the rejected call is never rewritten or executed.
            kwargs["tools"] = [
                schema for schema in kwargs.get("tools", [])
                if _litellm_tool_name(schema) not in invalid_tools
            ]
            # Name only tools this request still declares: an agent with its own
            # read-only tool set (the snippet query, say) has no read_file or
            # create_plan, and pointing a small model at a tool it does not hold
            # sends it calling that tool instead of recovering.
            remaining = {_litellm_tool_name(schema) for schema in kwargs["tools"]}
            hints = []
            if "read_file" in remaining:
                hints.append("For whole-file content use read_file(path).")
            if "search_code" in remaining:
                hints.append("search_code always requires query.")
            if "create_plan" in remaining:
                hints.append("In plan mode, finish by calling create_plan when ready.")
            cleaned_messages = cleaned_messages + [{
                "role": "system",
                "content": " ".join([
                    "SYSTEM ALERT: Repeated invalid arguments were rejected for these tools: "
                    f"{names}. They are unavailable for the rest of this turn. Continue with a "
                    "different available tool whose declared schema matches the task. Do not "
                    "repeat or rename the rejected call, and do not claim that all tools are "
                    "unavailable.",
                    *hints,
                ]),
            }]
        if model_requires_single_system_message(model):
            cleaned_messages = consolidate_leading_system_messages(cleaned_messages)
        try:
            res = await original(cleaned_messages, **kwargs)
            # If it's a ChatCompletion object or model response
            if hasattr(res, "choices") and res.choices:
                choice = res.choices[0]
                msg = getattr(choice, "message", None)
                if msg:
                    tcalls = getattr(msg, "tool_calls", None)
                    if tcalls and isinstance(tcalls, list):
                        new_tcalls = []
                        has_split = False
                        for tc in tcalls:
                            fn = getattr(tc, "function", None)
                            if not fn:
                                new_tcalls.append(tc)
                                continue
                            args_str = getattr(fn, "arguments", "")
                            objs = split_concatenated_json(args_str)
                            if len(objs) > 1:
                                has_split = True
                                import uuid
                                from litellm import ChatCompletionMessageToolCall, Function
                                for obj in objs:
                                    original_name = getattr(fn, "name", "") or ""
                                    agent_tools = getattr(agent, "tools", [])
                                    if tool_accepts_args(original_name, obj, agent_tools):
                                        matched_name = original_name
                                    else:
                                        matched_name = find_tool_for_args(obj, agent_tools) or original_name or "unknown"
                                    new_tc = ChatCompletionMessageToolCall(
                                        id=str(uuid.uuid4()),
                                        type="function",
                                        function=Function(
                                            name=matched_name,
                                            arguments=json.dumps(obj)
                                        )
                                    )
                                    new_tcalls.append(new_tc)
                            else:
                                new_tcalls.append(tc)
                        if has_split:
                            msg.tool_calls = new_tcalls
            return res
        except Exception as e:
            normalized = _normalize_ollama_unexpected_response_error(e)
            if normalized is not e:
                raise normalized from e
            raise

    async def _run_with_compat(*args: Any, **kwargs: Any) -> Any:
        sanitize_agent_state(agent)
        return await original_run(*args, **kwargs)

    object.__setattr__(agent, "_acompletion", _acompletion_with_compat)
    if original_run is not None:
        object.__setattr__(agent, "run", _run_with_compat)
    object.__setattr__(agent, "_gilzabots_litellm_compat_wrapped", True)
    return agent


def _normalize_ollama_unexpected_response_error(exc: Exception) -> Exception:
    """Replace noisy Ollama/LiteLLM response-shape errors with a useful hint."""
    text = str(exc)
    low = text.lower()
    # Ollama words the same rejection differently across versions: older builds
    # say "error parsing tool call", llama-server-backed ones "llama-server
    # returned invalid tool call arguments for \"<tool>\"".
    if "error parsing tool call" in low or "returned invalid tool call arguments" in low:
        message = (
            "GILZABOTS_OLLAMA_TOOL_CALL_JSON_ESCAPE: Ollama rejected a model "
            "tool-call argument because its tool-call JSON was invalid. "
            "Original LiteLLM error: "
            f"{_truncate_for_error(text, 500)}"
        )
        try:
            return exc.__class__(message)
        except Exception:
            return RuntimeError(message)
    if (
        "ollama" not in low
        or "keyerror" not in low
        or "'message'" not in text
        or "unexpected response from ollama" not in low
    ):
        return exc

    detail = "Ollama returned an unexpected response instead of a chat message."
    if "internal server error" in low:
        detail = (
            "Ollama returned HTTP 500 (Internal Server Error) instead of a chat "
            "message."
        )

    message = (
        f"{detail} Check that the Ollama server is healthy, the selected model "
        "supports the requested chat/tool/thinking features, and the model "
        "parameters are supported. Original LiteLLM error: "
        f"{_truncate_for_error(text, 500)}"
    )
    try:
        return exc.__class__(message)
    except Exception:
        return RuntimeError(message)


def _truncate_for_error(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def sanitize_agent_state(agent: Any) -> None:
    """Repair persisted agent state before a provider request."""
    _sanitize_agent_history_in_place(agent)


def _sanitize_agent_history_in_place(agent: Any) -> None:
    history = getattr(agent, "internal_history", None)
    if isinstance(history, list):
        sanitized = sanitize_tool_call_messages(history)
        if sanitized != history:
            history[:] = sanitized


def _orphan_tool_as_system_message(msg: dict[str, Any]) -> dict[str, str]:
    """Re-role a tool result that lost its assistant tool call.

    The result cannot keep the ``tool`` role without a matching ``tool_call_id``,
    and it must not become a ``user`` message either: it is runtime output, not
    something the person said (PROJECT_DESIGN 2.6, runtime correction role
    isolation). ``system`` is the only role that preserves that distinction.
    """
    name = msg.get("name") or "unknown"
    content = msg.get("content") or ""
    return {
        "role": "system",
        "content": f"[Recovered orphan tool result from '{name}']\n{content}",
    }


def _assistant_tool_calls_as_text(
    msg: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, str]:
    content = msg.get("content") or ""
    summaries = []
    for tool_call in tool_calls:
        fn = tool_call.get("function") or {}
        name = fn.get("name") or "unknown"
        args = fn.get("arguments") or ""
        summaries.append(f"- {name}: {args}")
    recovered = "[Recovered incomplete assistant tool call; removed before provider request]"
    if summaries:
        recovered += "\n" + "\n".join(summaries)
    if content:
        recovered = f"{content}\n\n{recovered}"
    return {"role": "assistant", "content": recovered}


def split_concatenated_json(s: str) -> list[dict]:
    import json
    decoder = json.JSONDecoder()
    objs = []
    s = s.strip()
    while s:
        try:
            obj, idx = decoder.raw_decode(s)
            objs.append(obj)
            s = s[idx:].strip()
        except json.JSONDecodeError:
            break
    return objs


def find_tool_for_args(args_dict: dict, tools: list) -> str | None:
    keys = set(args_dict.keys())
    candidates = []
    for tool in tools:
        tool_name = getattr(tool, "name", None)
        if not tool_name:
            continue
        schema = _tool_schema_fields(tool)
        if schema is None:
            continue
        fields, required = schema
        if keys and keys <= fields and required <= keys:
            candidates.append(tool_name)
    return candidates[0] if len(candidates) == 1 else None


def tool_accepts_args(tool_name: str, args_dict: dict, tools: list) -> bool:
    if not tool_name:
        return False
    keys = set(args_dict.keys())
    if not keys:
        return False
    for tool in tools:
        if getattr(tool, "name", None) != tool_name:
            continue
        schema = _tool_schema_fields(tool)
        if schema is None:
            return False
        fields, required = schema
        return keys <= fields and required <= keys
    return False


def _repeated_tool_validation_error_names(
    messages: list[dict[str, Any]], *, threshold: int = 2
) -> set[str]:
    seen: dict[str, int] = {}
    repeated: set[str] = set()
    for msg in reversed(messages[-12:]):
        # A new user message starts a new opportunity to call the tool. Without
        # this boundary, two mistakes near the end of one turn could keep a read
        # tool hidden at the start of the next turn as well.
        if msg.get("role") == "user":
            break
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if "validation error" not in content and "validation errors" not in content:
            continue
        if "Field required" not in content and "Input should be" not in content:
            continue
        name = str(msg.get("name") or "unknown")
        seen[name] = seen.get(name, 0) + 1
        if seen[name] >= threshold:
            repeated.add(name)
    return repeated


def _has_repeated_tool_validation_errors(messages: list[dict[str, Any]], *, threshold: int = 2) -> bool:
    """Compatibility predicate retained for callers that only need yes/no."""
    return bool(_repeated_tool_validation_error_names(messages, threshold=threshold))


def _litellm_tool_name(schema: Any) -> str:
    """Return a function name from a LiteLLM/OpenAI tool schema."""
    if not isinstance(schema, dict):
        return ""
    function = schema.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("name") or "")


def _tool_name_available(tools: list, name: str) -> bool:
    return any(getattr(tool, "name", None) == name for tool in tools)


def _tool_schema_fields(tool: Any) -> tuple[set[str], set[str]] | None:
    input_schema_fn = getattr(tool, "input_schema", None)
    if not input_schema_fn:
        return None
    try:
        schema_cls = input_schema_fn()
        if hasattr(schema_cls, "model_json_schema"):
            schema = schema_cls.model_json_schema()
            return set(schema.get("properties", {}).keys()), set(schema.get("required", []))
        if hasattr(schema_cls, "model_fields"):
            fields = set(schema_cls.model_fields.keys())
            required = {
                name for name, field in schema_cls.model_fields.items()
                if getattr(field, "is_required", lambda: False)()
            }
            return fields, required
        if hasattr(schema_cls, "__fields__"):
            fields = set(schema_cls.__fields__.keys())
            required = {
                name for name, field in schema_cls.__fields__.items()
                if getattr(field, "required", False)
            }
            return fields, required
    except Exception:
        return None
    return None
