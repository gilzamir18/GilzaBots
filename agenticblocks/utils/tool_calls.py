"""Helpers for reasoning about a tool call against the tools an agent advertises.

The schema helpers work on OpenAI-format tool schemas (the output of
``block_to_tool_schema``), so they apply to any tool the agent can see --
native Python blocks and MCP proxies alike -- and never execute anything.
"""
import difflib
import json
from typing import Any, Dict, Iterable, List, Optional


def _schema_name(schema: Dict[str, Any]) -> str:
    function = schema.get("function") if isinstance(schema, dict) else None
    return str((function or {}).get("name") or "")


def _parse_arguments(arguments: Any) -> Optional[Dict[str, Any]]:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return None
    try:
        parsed = json.loads(arguments)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def tools_accepting_arguments(arguments: Any, tool_schemas: Iterable[Dict[str, Any]]) -> List[str]:
    """Return the names of the tools whose parameters exactly fit ``arguments``.

    A tool fits when every key of the argument object is one of its declared
    parameters and every required parameter is present. Tools that declare no
    required parameter never fit: almost any object is a subset of their
    optional keys, so a match would carry no information. ``arguments`` may be
    a dict or a JSON string; anything that is not a non-empty object fits
    nothing.

    This identifies which tool an argument object was written for. It is a
    diagnostic, not a dispatcher: callers must not execute a tool on its answer.
    """
    parsed = _parse_arguments(arguments)
    if not parsed:
        return []
    keys = set(parsed)
    matches: List[str] = []
    for schema in tool_schemas:
        name = _schema_name(schema)
        parameters = (schema.get("function") or {}).get("parameters") or {}
        properties = set((parameters.get("properties") or {}).keys())
        required = set(parameters.get("required") or [])
        if name and required and required <= keys <= properties:
            matches.append(name)
    return matches


def unknown_tool_message(
    function_name: str,
    arguments: Any,
    tool_schemas: Iterable[Dict[str, Any]],
) -> str:
    """Build the corrective error for a call to a tool the agent does not have.

    A bare "not found" leaves a weak model with nothing to correct against, and
    the observed next step is printing the arguments as text. The message
    therefore names the tools that do exist and, when the evidence is there,
    the likely intended one: a close spelling of the name, or a tool whose
    parameters the arguments fit exactly. These are hints for the model to
    act on; the call itself is never redirected.
    """
    schemas = list(tool_schemas)
    names = [name for name in (_schema_name(s) for s in schemas) if name]
    message = f"Tool '{function_name}' not found."
    if names:
        message += f" Available tools: {', '.join(names)}."
    close = difflib.get_close_matches(str(function_name or ""), names, n=1, cutoff=0.6)
    fitting = tools_accepting_arguments(arguments, schemas)
    if close:
        message += f" Did you mean '{close[0]}'?"
    if fitting:
        quoted = ", ".join(f"'{name}'" for name in fitting)
        message += f" The arguments you sent fit the parameters of {quoted}."
    message += (
        " Nothing was executed. Re-issue the call through the native tool-calling "
        "protocol using one of the available tool names; do not write the call as text."
    )
    return message


def is_ollama_tool_call_json_error(exc: BaseException) -> bool:
    """Return whether Ollama refused a model-generated tool call as invalid JSON.

    Ollama parses tool-call arguments server-side, so a model that writes a raw
    newline or an unescaped backslash inside a JSON string never reaches the
    client as a tool call: the request fails instead. That failure is the model's
    malformed output, recoverable by asking it to re-issue the call, not a
    transport or parameter error. Ollama words it differently across versions.
    """
    message = str(exc).lower()
    return (
        "gilzabots_ollama_tool_call_json_escape" in message
        or (
            "ollama" in message
            and (
                "error parsing tool call" in message
                or "tool-call json was invalid" in message
                # llama-server-backed builds: "llama-server returned invalid
                # tool call arguments for \"<tool>\": invalid character ..."
                or "returned invalid tool call arguments" in message
            )
        )
    )


OLLAMA_TOOL_CALL_JSON_ALERT = (
    "SYSTEM ALERT: Ollama rejected the previous native tool call because its arguments "
    "were not valid JSON. Retry the same action through the native tool-calling API "
    "with valid, compact JSON arguments: inside a JSON string, write a line break as "
    "\\n and a backslash as \\\\, never as a raw character."
)
"""Corrective feedback after ``is_ollama_tool_call_json_error``; sent as ``system``."""
