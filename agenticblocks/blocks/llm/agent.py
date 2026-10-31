import asyncio
import json
import sys
import uuid
import time
import inspect
import re
from datetime import datetime
from collections import defaultdict
from pydantic import BaseModel, Field, model_validator
from typing import List, Dict, Any, Optional, Callable
import litellm
from litellm.integrations.custom_logger import CustomLogger as _LiteLLMCustomLogger
from agenticblocks.core.agent import AgentBlock
from agenticblocks.core.block import Block
from agenticblocks.tools.a2a_bridge import block_to_tool_schema
from agenticblocks.runtime.state import TokenUsage, _current_ctx
from agenticblocks.utils.parsers import split_inline_reasoning
from agenticblocks.utils.tool_calls import (
    OLLAMA_TOOL_CALL_JSON_ALERT,
    is_ollama_tool_call_json_error,
    unknown_tool_message,
)
from agenticblocks.utils.messages import build_user_content, history_accepts_user_message
from agenticblocks.blocks.llm.inbox import InboxItem, MessageInbox
from agenticblocks.blocks.llm.response_wait import ResponseWait, ResponseWaitWatch


class _DummyFunction(BaseModel):
    name: str
    arguments: str


class _DummyToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:10]}")
    type: str = "function"
    function: _DummyFunction

    def __init__(self, name: str, arguments: str, **data):
        super().__init__(function=_DummyFunction(name=name, arguments=arguments), **data)


class _DummyMessage:
    def __init__(self, tool_calls: list):
        self.content = ""
        self.tool_calls = tool_calls


def _infer_tool_from_keys(keys: set, available_tools: dict) -> str | None:
    """Infer which registered tool a bare param dict targets, from its key shape.

    A tool is a candidate when the provided `keys` are a subset of its declared
    parameters AND all of that tool's required parameters are present. The match is
    only accepted when EXACTLY ONE tool qualifies — zero or multiple candidates
    return None, so the caller never invents a tool name on ambiguity.

    `available_tools` maps tool_name -> (all_params, required_params).
    """
    candidates = [
        name
        for name, (params, required) in available_tools.items()
        if keys <= params and required <= keys
    ]
    return candidates[0] if len(candidates) == 1 else None


def _json_to_tool_calls(data: dict, available_tools: dict) -> list | None:
    """Convert a hallucinated JSON dict into _DummyToolCall objects.

    Handles the formats a model emits when it ignores the function-calling API.
    All tool names are validated against `available_tools` (the tools actually
    registered on the block) — names are never hardcoded, and key-shape inference
    (Formats C/E) maps onto the real tools' parameter schemas.

    `available_tools` maps tool_name -> (all_params: set, required_params: set).

    Format A — explicit wrapper:
        {"tool_name": "read_file", "tool_args": {"file_path": "index.html"}}

    Format B — name + params flat:
        {"name": "read_file", "file_path": "index.html"}

    Format C — bare params only (tool inferred from the registered tools' schemas):
        {"file_path": "style.css"}   → whichever single tool declares "file_path"

    Format D — tool_calls list wrapper (OpenAI-style hallucination):
        {"tool_calls": [{"function": {"name": "read_file", "arguments": {"file_path": "x.js"}}}]}
        {"tool_calls": [{"name": "read_file", "arguments": {"file_path": "x.js"}}]}

    Format E — fs_operations list, where each op's "type" names a registered tool.
    """
    available_names = set(available_tools.keys())

    # Format E — fs_operations list: each op's "type" must name a registered tool.
    fs_ops = data.get("fs_operations")
    if isinstance(fs_ops, list) and fs_ops:
        results = []
        for op in fs_ops:
            if not isinstance(op, dict):
                continue
            op_type = op.get("type", "")
            if op_type not in available_names:
                continue
            params, _required = available_tools[op_type]
            args = {k: v for k, v in op.items() if k != "type" and k in params}
            if args:
                results.append(_DummyToolCall(op_type, json.dumps(args)))
        if results:
            return results

    # Format D: tool_calls wrapper — unpack first item and recurse
    tool_calls_list = data.get("tool_calls")
    if isinstance(tool_calls_list, list) and tool_calls_list:
        first = tool_calls_list[0]
        if isinstance(first, dict):
            # {"function": {"name": ..., "arguments": ...}} or {"name": ..., "arguments": ...}
            inner = first.get("function") or first
            if isinstance(inner, dict):
                return _json_to_tool_calls(inner, available_tools)
            # Format D variant: {"function": "<name>", "args"/"arguments": {...}}
            # the model emits "function" as a plain string instead of a nested dict.
            if isinstance(inner, str) and inner in available_names:
                raw = first.get("args") or first.get("arguments") or first.get("parameters") or {}
                if isinstance(raw, dict):
                    return [_DummyToolCall(inner, json.dumps(raw))]
        return None

    tool_name = data.get("tool_name") or data.get("name") or data.get("function") or data.get("function_name")
    # "function" can be a dict (e.g. {"name": "edit_file", "arguments": {...}}) when the
    # model emits OpenAI-style tool_calls without the outer wrapper. If so, recurse into it
    # rather than using the dict as a tool name — which would raise TypeError on set lookup.
    if isinstance(tool_name, dict):
        return _json_to_tool_calls(tool_name, available_tools)
    raw_args = data.get("tool_args") or data.get("parameters") or data.get("arguments")

    # Format A/B: explicit tool name present
    if tool_name and tool_name in available_names:
        if raw_args is None:
            raw_args = {k: v for k, v in data.items() if k not in {"tool_name", "name"}}
        if isinstance(raw_args, dict):
            return [_DummyToolCall(tool_name, json.dumps(raw_args))]

    if tool_name and tool_name not in available_names:
        return None

    # Format C: infer the target tool from the bare-param key shape, matched
    # against the registered tools' schemas. Accepted only on a unique match.
    keys = set(data.keys())
    inferred = _infer_tool_from_keys(keys, available_tools)
    if inferred is not None:
        return [_DummyToolCall(inferred, json.dumps(data))]

    return None


class AgentInput(BaseModel):
    prompt: str
    attachments: list = Field(default_factory=list)
    """Optional list of attachment descriptors [{type, data, mime, name}] for multimodal messages."""
    role: str = "user"
    """Conversation role this prompt enters the history as.

    Defaults to "user", which is the normal case: a person is talking to the agent.
    Blocks that keep a conversation history (MemGPTAgentBlock) also accept "system"
    so runtime/framework corrections are injected as system feedback instead of
    impersonating the user. Blocks without a persistent history reject anything
    other than "user".
    """

def compose_visible_response(parts: List[str], final: str = "") -> str:
    """Join everything the model addressed to the user, in order and verbatim.

    One rule, shared by every agent block, so the two cannot drift into treating
    the same output differently. Each part is kept exactly as the model wrote it:
    leading whitespace is content, not padding, and stripping it turned a
    Markdown indented code block into ordinary prose. Parts are separated by a
    blank line, and only a wholly blank part is dropped.
    """
    chunks = [p for p in [*parts, final] if p and p.strip()]
    return "\n\n".join(chunks)


class ToolCallRecord(BaseModel):
    """What happened to one tool call the model issued, in issue order.

    ``ok`` is False when the call never produced a result: the tool raised, the
    name matched no tool, or loop detection refused it before execution. A tool
    that *returns* text describing a problem still counts as ``ok`` -- the block
    cannot read meaning into a successful return value, and does not try.
    """
    index: int
    """1-based position of the call within the run."""
    tool: str
    ok: bool
    error: str = ""
    """The error the model was shown, without the block's corrective suffix."""


class AgentOutput(BaseModel):
    response: str
    tool_calls_made: int = 0
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    """Every tool call of the run and whether it produced a result, in order.

    A run's final text is the model's own account, and a model told to correct
    tool errors silently will usually leave a recovered error out of it. A host
    that delegates to this block and has to know what actually happened -- an
    error, and what the model did after it -- reads it here instead of from the
    prose. Empty when the block does not report one."""
    structured_output: Optional[Any] = None
    termination_reason: str = ""
    """Why the run loop stopped, in the loop's own words.

    Every agent block already computes this to build its debug report, and it is
    the only thing that distinguishes a turn the model chose to end from one the
    runtime cut short (an exhausted guardrail, a narration fallback, a breaker).
    Discarding it left a host with no way to tell those apart after the fact, so
    it is reported rather than printed only under ``debug``. Empty when the block
    does not report one."""
    narration: List[str] = Field(default_factory=list)
    """User-facing text the model wrote *while still working*, in order.

    A model that speaks beside a tool call is reporting progress, not answering.
    ``response`` already contains this text -- it is the whole answer, joined and
    lossless, so a caller that ignores this field keeps every word. The split is
    offered on top of that, for a host that wants to present the progress and the
    deliverable differently, and never as the only place the text exists: making
    presentation the difference between shown and lost is the defect this whole
    protocol was rewritten to remove. Empty when the block does not report one."""
    final_text: str = ""
    """The text that ended the turn: the answer, without the progress above it.

    Empty when the block does not report one; ``response`` is always complete."""
    budget_exhausted: bool = False
    """True when the run ended with its step budget spent.

    The budget is the block's own guardrail: ``max_heartbeats`` for
    ``MemGPTAgentBlock``, ``max_iterations`` / ``max_tool_calls`` for
    ``LLMAgentBlock``. It is reported whether or not a final answer was still
    obtained, so ``final_text`` says whether the model answered and this says
    whether a larger budget could have changed how the run ended. A host that
    reads the human-readable ``termination_reason`` for that decision is parsing
    prose; this is the structured form of the same fact."""
    truncated: bool = False
    """True when the completion that produced ``final_text`` stopped at the
    provider's output-token limit (``finish_reason == "length"``).

    A reply cut off mid-sentence reads exactly like a finished one, so a host
    that presents the answer as complete would be presenting a partial result as
    a whole. This says which of the two it is. False when the provider reported
    any other finish reason, or none."""


def _print_debug_report(
    *,
    agent_name: str,
    model: str,
    iteration_count: int,
    tool_call_count: int,
    tool_usage: Dict[str, int],
    termination_reason: str,
    elapsed_seconds: float,
) -> None:
    """Print a structured debug report in English after an agent run."""
    sep = "─" * 56
    print(f"\n{'═' * 56}")
    print(f"  [DEBUG] Agent Run Report — {agent_name}")
    print(f"{'═' * 56}")
    print(f"  Model              : {model}")
    print(f"  Total iterations   : {iteration_count}")
    print(f"  Total tool calls   : {tool_call_count}")
    print(f"  Elapsed time       : {elapsed_seconds:.3f}s")
    print(f"  Termination reason : {termination_reason}")
    print(sep)
    if tool_usage:
        print("  Tool usage breakdown:")
        for tool_name, count in sorted(tool_usage.items(), key=lambda x: -x[1]):
            print(f"    • {tool_name:<30} {count:>3} call(s)")
    else:
        print("  No tools were used — response based entirely on the model.")
    #print(f"{'═' * 56}\n")


def _stopped_at_length(response: Any) -> bool:
    """Whether a completion ended at the output-token limit.

    LiteLLM normalizes every provider's stop reason to the OpenAI vocabulary
    (Ollama's ``done_reason``, Anthropic's ``max_tokens``), so ``"length"`` is the
    one value that means "cut off"."""
    try:
        return response.choices[0].finish_reason == "length"
    except (AttributeError, IndexError, TypeError):
        return False


def _tool_call_signature(function_name: str, arguments: Any) -> str:
    """Return a stable identity for a tool call, used by loop detection.

    Arguments are normalised through JSON when possible so that semantically
    identical calls emitted with different key ordering or spacing collapse to
    the same signature.
    """
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
    try:
        parsed = json.loads(raw)
        normalised = json.dumps(parsed, sort_keys=True, default=str)
    except Exception:
        normalised = (raw or "").strip()
    return f"{function_name}:{normalised}"


def _loop_block_message(function_name: str, limit: int) -> str:
    """Corrective tool result returned when loop detection blocks a repeat call."""
    return json.dumps({
        "error": (
            f"SYSTEM ALERT: Loop detected. You already called '{function_name}' "
            f"{limit} times with these exact arguments and it did not resolve the task. "
            "This call was BLOCKED and not executed. Do NOT repeat it. Change the "
            "arguments, use a different tool, or stop and report what is blocking you."
        )
    })


# TEMPORARY DIAGNOSTIC — remove once the source of the warning below is found.
# A streaming call cancelled before LiteLLM returns its response leaves LiteLLM's
# stream coroutine unawaited, which Python reports as "coroutine
# 'BaseLLMHTTPHandler.acompletion_stream_function' was never awaited". This
# reports which call was cancelled, when, and how long after it started.
async def _await_reporting_cancellation(awaitable, agent_name: str, model: str, started: float):
    try:
        return await awaitable
    except asyncio.CancelledError:
        elapsed_ms = (time.monotonic() - started) * 1000
        print(
            f"[LLM CALL CANCELLED {datetime.now():%H:%M:%S.%f}] agent={agent_name} "
            f"model={model} cancelled {elapsed_ms:.0f} ms after the call started, "
            "before the response began",
            file=sys.stderr,
            flush=True,
        )
        raise


# Registry of routers shared by model — Flyweight pattern.
# LiteLLM.Router manages connection pooling; the same Router is reused
# for all block instances that target the same model.
_router_registry: Dict[str, litellm.Router] = {}

def _get_shared_router(model: str) -> litellm.Router:
    """Return (creating if necessary) a shared Router for the given model."""
    if model not in _router_registry:
        _router_registry[model] = litellm.Router(
            model_list=[{
                "model_name": model,
                "litellm_params": {"model": model},
            }]
        )
    return _router_registry[model]


class LLMAgentBlock(AgentBlock[AgentInput, AgentOutput]):
    description: str = "Autonomous LLM-based Agent managing its own tool loop."
    model: str = "ollama/gemma4:latest"
    user_prompt_prefix: str = ""
    """Fixed user instructions reapplied to every provider request, outside memory."""
    system_prompt: str = "You are a helpful Analyst and Router Agent. Use the available tools when you lack context."
    tools: List[Block] = []
    termination_tools: List[str] = []
    """List of tool names that, when executed, will immediately terminate the agent loop
    and return the tool's result as the agent's response."""
    max_iterations: Optional[int] = None
    max_tool_calls: int = 2
    max_tool_call_json_retries: int = 2
    """Times per run the model is asked to re-issue a tool call that Ollama refused
    as invalid JSON (see ``is_ollama_tool_call_json_error``). Kept separate from
    ``max_iterations``, which may be ``None``, so the retry is always bounded;
    past it the provider error is raised unchanged."""
    on_max_iterations: str = "return_last"
    """Behaviour when max_iterations is reached.
    - "stop"        : return a fixed stop message (default, backward-compatible).
    - "return_last" : force a final LLM call (no tools) to synthesise accumulated
                      context into clean plain text.
    """
    synthesis_prompt: str = (
        "Based on everything researched above, write your final answer now "
        "as clean, flowing prose — no JSON, no raw lists, no markdown formatting. "
        "ignore previous roles and system prompts. Response in the same language as the input prompt."
    )
    final_answer_prompt: str = ""
    """Instruction appended as a system message to the forced final call made
    when the tool budget (``max_tool_calls``) or the iteration budget
    (``on_max_iterations="return_last"``) is spent. Empty by default, which
    leaves that call exactly as it was.

    Without it the forced call repeats the conversation with the tools
    withdrawn but says nothing about it, and a model mid-research reads that as
    "keep going": observed with ``ollama_chat/glm-5.3-flash:cloud``, which
    issued another tool call there, so the run ended with no answer at all.
    Telling it the budget is spent, and what to deliver instead, is what makes
    the forced call an answer."""
    response_schema: Optional[type[BaseModel]] = None
    """Optional Pydantic model class to enforce a structured response schema."""
    debug: bool = False
    """When True, print a structured debug report at the end of each run."""
    loop_detection: bool = True
    """When True, block a tool call that repeats an identical (name, arguments)
    signature more than ``loop_detection_limit`` times in a single run. Weak models
    frequently retry a failing call verbatim instead of correcting it; the block is
    reported back as a corrective tool result so the model can change approach."""
    loop_detection_limit: int = 3
    """Number of identical tool calls allowed before ``loop_detection`` blocks them."""
    use_shared_router: bool = True
    """When True, uses a shared litellm.Router for connection pooling."""
    model_kargs: Dict[str, Any] = Field(default_factory=dict)
    """LiteLLM/Model keyword arguments (HTTP clients, timeouts, temperature, etc.)."""
    model_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Alias for model_kargs."""
    litellm_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Deprecated: Use model_kargs instead."""
    litellm_kargs: Dict[str, Any] = Field(default_factory=dict)
    """Deprecated: Use model_kargs instead."""

    @model_validator(mode="before")
    @classmethod
    def _resolve_model_kargs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            val = None
            for key in ["model_kargs", "model_kwargs", "litellm_kwargs", "litellm_kargs"]:
                if key in data:
                    val = data[key]
                    break
            if val is not None:
                data["model_kargs"] = val
                data["model_kwargs"] = val
                data["litellm_kwargs"] = val
                data["litellm_kargs"] = val
        return data
    on_iteration: Optional[Callable[[int, List[Dict[str, Any]]], Any]] = None
    """Optional callback invoked at the start of each loop iteration for debugging. 
    Signature: `def callback(iteration: int, messages: List[Dict[str, Any]]) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_token_usage: Optional[Callable[["TokenUsage"], Any]] = None
    """Optional callback invoked after each LLM call with token statistics.
    Signature: `def callback(usage: TokenUsage) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_unknown_tool: Optional[Callable[[str, str], Any]] = None
    """Optional callback invoked when the model calls a tool it was not offered.

    The call is not executed; the model receives a corrective tool result that
    lists the available tools. Receives the tool name and the raw arguments string.
    Signature: `def callback(name: str, arguments: str) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_thinking: Optional[Callable[[str], Any]] = None
    """Optional callback invoked after each LLM call with the model's reasoning content.
    Fires once per LLM call (not per-token) with the full reasoning text for that call.
    Supports native reasoning_content (DeepSeek, Claude) and inline <think> tags (Qwen3).
    Signature: `def callback(chunk: str) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_chunk: Optional[Callable[[str], Any]] = None
    """Optional callback invoked after each LLM call with standard content chunks.
    Signature: `def callback(chunk: str) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_response_wait: Optional[Callable[["ResponseWait"], Any]] = None
    """Optional callback invoked when the provider has sent nothing for
    `response_wait_notice_after` seconds during an LLM call (``waiting=True``),
    and again when the silence ends (``waiting=False``): data resumed, or the
    call finished or failed. At most one notice is outstanding at a time; a
    response that stalls again after resuming produces a new pair.
    It only observes: the request, its timeout and any retry are unchanged.
    Signature: `def callback(wait: ResponseWait) -> Any`.
    Can be a synchronous or asynchronous function."""
    response_wait_notice_after: Optional[float] = 60.0
    """Seconds of provider silence before `on_response_wait` is notified.
    ``None`` or a non-positive value disables the watch."""
    inbox: Optional[MessageInbox] = None
    """Optional channel for messages submitted while this run is already in flight.

    Drained at the top of each loop iteration, which is the only point where the
    history is guaranteed not to sit between a tool call and its results. A
    message submitted after the loop has broken out is never delivered here; it
    stays pending and is returned by `MessageInbox.close()` for the caller to
    route somewhere else."""
    on_message_delivery: Optional[Callable[["InboxItem"], Any]] = None
    """Optional callback invoked for each inbox message at the moment it is
    delivered, in delivery order, immediately *before* it enters the conversation.

    The item is mutable: a host that must finalize the message against live run
    state (budgeting an attachment against the current context, annotating it,
    recording it in its own store) does so here, and whatever the item carries
    when the callback returns is what the model receives. An exception
    propagates and ends the run, so a host that prefers to degrade must handle
    its own failures.
    Signature: `def callback(item: InboxItem) -> Any`.
    Can be a synchronous or asynchronous function."""

    model_config = {"arbitrary_types_allowed": True}

    async def _invoke_on_message_delivery(self, item: "InboxItem") -> None:
        if self.on_message_delivery:
            if inspect.iscoroutinefunction(self.on_message_delivery):
                await self.on_message_delivery(item)
            else:
                self.on_message_delivery(item)

    async def _drain_inbox(self, history: List[Dict[str, Any]]) -> List["InboxItem"]:
        """Append every pending inbox message to ``history``, in order.

        Returns without taking anything when the history cannot accept a message
        yet: the items stay queued for the next boundary rather than being
        delivered into a request the provider would reject.
        """
        if not self.inbox or not len(self.inbox):
            return []
        if not history_accepts_user_message(history):
            return []

        delivered: List["InboxItem"] = []
        for item in self.inbox.drain():
            await self._invoke_on_message_delivery(item)
            history.append({
                "role": item.role,
                "content": build_user_content(item.content, item.attachments),
            })
            delivered.append(item)
        return delivered

    async def _invoke_on_unknown_tool(self, name: str, arguments: Any) -> None:
        if self.on_unknown_tool:
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
            if inspect.iscoroutinefunction(self.on_unknown_tool):
                await self.on_unknown_tool(name, raw)
            else:
                self.on_unknown_tool(name, raw)

    async def _invoke_on_iteration(self, iteration: int, messages: List[Dict[str, Any]]) -> None:
        if self.on_iteration:
            if inspect.iscoroutinefunction(self.on_iteration):
                await self.on_iteration(iteration, messages)
            else:
                self.on_iteration(iteration, messages)

    async def _invoke_on_thinking(self, chunk: str) -> None:
        if self.on_thinking and chunk:
            if inspect.iscoroutinefunction(self.on_thinking):
                await self.on_thinking(chunk)
            else:
                self.on_thinking(chunk)

    async def _invoke_on_chunk(self, chunk: str) -> None:
        if self.on_chunk and chunk:
            if inspect.iscoroutinefunction(self.on_chunk):
                await self.on_chunk(chunk)
            else:
                self.on_chunk(chunk)

    async def _acompletion(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """Single call site for LiteLLM completions.

        When stream=True is present in kwargs the response is fully aggregated
        via stream_chunk_builder before returning, so callers always receive a
        plain ModelResponse regardless of streaming mode.
        """
        # Filter reasoning_content from history to prevent models from seeing
        # their own thinking/reflection blocks and getting stuck in loops.
        cleaned_messages = []
        for msg in messages:
            m = msg.copy()
            if "reasoning_content" in m:
                m.pop("reasoning_content")
            cleaned_messages.append(m)
        from agenticblocks.utils.messages import prepend_user_prompt
        messages = prepend_user_prompt(cleaned_messages, self.user_prompt_prefix)

        streaming = kwargs.get("stream", False)
        if streaming:
            kwargs = {**kwargs, "stream_options": {"include_usage": True}}

        # ollama_chat/ uses Ollama's native /api/chat endpoint which does not support
        # OpenAI-style image_url content parts. Fall back to ollama/ (OpenAI-compat)
        # for any call that contains image data so vision works correctly.
        effective_model = self.model
        _has_images = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"])
            for m in messages
        )
        if _has_images and effective_model.startswith("ollama_chat/"):
            effective_model = "ollama/" + effective_model[len("ollama_chat/"):]
        from agenticblocks.utils.messages import match_tool_result_ids_to_route
        messages = match_tool_result_ids_to_route(effective_model, messages)

        started = time.monotonic()
        agent_name = getattr(self, "name", "") or type(self).__name__
        watch = ResponseWaitWatch(
            self.on_response_wait,
            agent=agent_name,
            model=effective_model,
            notice_after=self.response_wait_notice_after,
        ).start()
        try:
            if self.use_shared_router:
                router = _get_shared_router(effective_model)
                response = await _await_reporting_cancellation(
                    router.acompletion(model=effective_model, messages=messages, **kwargs),
                    agent_name, effective_model, started,
                )
            else:
                response = await _await_reporting_cancellation(
                    litellm.acompletion(model=effective_model, messages=messages, **kwargs),
                    agent_name, effective_model, started,
                )

            # LiteLLM 1.90 can return its async stream initializer as the result of
            # acompletion for some OpenAI-compatible providers. Resolve that nested
            # awaitable before consuming the stream so it is not garbage-collected
            # as an unawaited coroutine.
            if inspect.isawaitable(response):
                response = await response

            if streaming:
                chunks = []
                async for chunk in response:
                    await watch.progress()
                    chunks.append(chunk)
                    if self.on_thinking or self.on_chunk:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta:
                            rc = getattr(delta, "reasoning_content", None)
                            if rc and self.on_thinking:
                                await self._invoke_on_thinking(rc)
                            content = getattr(delta, "content", None)
                            if content and self.on_chunk:
                                await self._invoke_on_chunk(content)
                response = litellm.stream_chunk_builder(chunks, messages=messages)
        finally:
            await watch.stop()

        return response

    def _parse_message(self, message: Any) -> Any:
        """Preserve model content exactly; only native tool_calls may execute actions."""
        return message

    async def _emit_token_usage(self, response: Any, step: int) -> None:
        """
        Extracts token usage from a LiteLLM response and emits a TokenUsage record:
          - Appends to ExecutionContext.token_stats (when inside a WorkflowExecutor run).
          - Invokes self.on_token_usage callback (when set).

        Safe to call when no ExecutionContext is active (standalone use).
        """
        usage = getattr(response, "usage", None)
        record = TokenUsage(
            block_name=self.name,
            step=step,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
        )

        # Push to the shared ExecutionContext when running inside WorkflowExecutor
        try:
            ctx = _current_ctx.get()
            await ctx.add_token_usage(record)
        except LookupError:
            pass  # Running standalone, outside a WorkflowExecutor

        # Invoke optional user-supplied callback
        if self.on_token_usage:
            if inspect.iscoroutinefunction(self.on_token_usage):
                await self.on_token_usage(record)
            else:
                self.on_token_usage(record)

    async def run(self, input: AgentInput) -> AgentOutput:
        start_time = time.monotonic()

        # This block builds a fresh [system, user] pair on every run, so there is no
        # history for a system-role prompt to be appended to. Fail fast instead of
        # silently turning it into a user message.
        if input.role != "user":
            raise ValueError(
                f"{type(self).__name__} only accepts AgentInput(role='user'); "
                f"got '{input.role}'."
            )

        _prev_callbacks = list(litellm.callbacks)

        # Transparent A2A Bridging: convert any sub-block into the Tool API format.
        litellm_tools = [block_to_tool_schema(b) for b in self.tools]

        # Build user content — plain string or multimodal list (vision models).
        user_content: str | list = build_user_content(input.prompt, input.attachments)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content}
        ]

        tool_call_count = 0
        self._current_tool_call_count = 0
        iteration_count = 0
        last_response: str = ""
        # Text the model addressed to the user while a tool call was still
        # pending. Composed into every exit below, so no path can return the
        # last thing said and drop the rest.
        visible_texts: List[str] = []
        tool_usage: Dict[str, int] = defaultdict(int)
        tool_call_signatures: Dict[str, int] = defaultdict(int)
        tool_records: List[ToolCallRecord] = []
        json_retries = 0
        termination_reason: str = "unknown"

        try:
          while True:
            # Messages submitted while this run is in flight enter here, before
            # the request is assembled, so the model sees them on its next call.
            await self._drain_inbox(messages)

            await self._invoke_on_iteration(iteration_count, messages)

            if self.max_iterations is not None and iteration_count >= self.max_iterations:
                if self.on_max_iterations == "return_last":
                    # Force a final LLM call without tools so the model synthesises
                    # the accumulated context. Synthesis instructions should live in
                    # the system_prompt — no extra message is injected here.
                    final_kwargs = self.model_kargs.copy()
                    final_kwargs.pop("tools", None)
                    final_kwargs.pop("tool_choice", None)
                    final_kwargs["system_prompt"] = self.synthesis_prompt
                    
                    if self.response_schema:
                        final_kwargs["response_format"] = self.response_schema

                    if self.final_answer_prompt:
                        messages.append({"role": "system", "content": self.final_answer_prompt})
                    final_resp = await self._acompletion(messages, **final_kwargs)

                    await self._emit_token_usage(final_resp, step=iteration_count)

                    termination_reason = "max_iterations reached → synthesised final response"
                    
                    content = final_resp.choices[0].message.content or last_response
                    
                    final_content = content
                    final_reasoning = getattr(final_resp.choices[0].message, "reasoning_content", None)
                    if not final_reasoning and final_content:
                        inline_reasoning, visible_content = split_inline_reasoning(final_content)
                        if inline_reasoning:
                            final_reasoning = inline_reasoning
                            final_content = visible_content
                            content = final_content

                    final_message = {"role": "assistant", "content": final_content}
                    if final_reasoning:
                        final_message["reasoning_content"] = final_reasoning
                    messages.append(final_message)

                    structured_obj = None
                    if self.response_schema and content:
                        try:
                            clean_content = content.strip()
                            if clean_content.startswith("```json"):
                                clean_content = clean_content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
                            elif clean_content.startswith("```"):
                                clean_content = clean_content.split("```", 1)[1].rsplit("```", 1)[0].strip()
                            structured_obj = self.response_schema.model_validate_json(clean_content)
                        except Exception as e:
                            if self.debug:
                                print(f"[DEBUG] Schema validation failed: {e}")
                    
                    output = AgentOutput(
                        response=compose_visible_response(visible_texts, content),
                        tool_calls_made=tool_call_count,
                        tool_calls=list(tool_records),
                        structured_output=structured_obj,
                        narration=list(visible_texts),
                        final_text=content,
                        budget_exhausted=True,
                        truncated=_stopped_at_length(final_resp),
                    )
                else:
                    termination_reason = "max_iterations reached → stopped"
                    output = AgentOutput(
                        response=compose_visible_response(
                            visible_texts, "Agent stopped: Max iterations reached."
                        ),
                        tool_calls_made=tool_call_count,
                        tool_calls=list(tool_records),
                        narration=list(visible_texts),
                        budget_exhausted=True,
                    )

                await self._invoke_on_iteration(iteration_count, messages)

                if self.debug:
                    _print_debug_report(
                        agent_name=self.name,
                        model=self.model,
                        iteration_count=iteration_count,
                        tool_call_count=tool_call_count,
                        tool_usage=dict(tool_usage),
                        termination_reason=termination_reason,
                        elapsed_seconds=time.monotonic() - start_time,
                    )
                return output

            iteration_count += 1

            # Build optional kwargs: include tools when available; block new tool
            # calls once the per-run limit has been reached.
            kwargs = self.model_kargs.copy()
            if litellm_tools:
                kwargs["tools"] = litellm_tools
                kwargs["tool_choice"] = "none" if tool_call_count >= self.max_tool_calls else "auto"

            # Enforce schema on the first call if no tools are present or tools are disabled
            if self.response_schema and (not litellm_tools or kwargs.get("tool_choice") == "none"):
                kwargs["response_format"] = self.response_schema

            # Main LiteLLM call.
            try:
                response = await self._acompletion(messages, **kwargs)
            except Exception as exc:
                # The server refused the model's own tool-call JSON, so nothing
                # reached this block to execute or append. Tell the model and let
                # it re-issue the call, as a system message (runtime feedback,
                # not something the user said).
                if (
                    is_ollama_tool_call_json_error(exc)
                    and json_retries < self.max_tool_call_json_retries
                ):
                    json_retries += 1
                    messages.append({"role": "system", "content": OLLAMA_TOOL_CALL_JSON_ALERT})
                    continue
                raise

            await self._emit_token_usage(response, step=iteration_count)

            message = response.choices[0].message
            message = self._parse_message(message)

            content = message.content or ""
            reasoning = getattr(message, "reasoning_content", None)
            if not reasoning and content:
                inline_reasoning, visible_content = split_inline_reasoning(content)
                if inline_reasoning:
                    reasoning = inline_reasoning
                    content = visible_content

            if not kwargs.get("stream", False):
                await self._invoke_on_thinking(reasoning or "")

            #print(f"[DIAGNOSTIC LLM ITERATION {iteration_count}] AgentBlock: '{self.name}' | Model: {self.model}")
            #if reasoning:
            #    print(f"  -> Thinking preview: {reasoning[:200]}...")
            #if message.tool_calls:
            #    tc_summary = [f"{tc.function.name}({tc.function.arguments[:120]})" for tc in message.tool_calls]
            #    print(f"  -> Native Tool Calls: {tc_summary}")
            #else:
            #    print(f"  -> No Native Tool Calls. Text output preview: {content[:200]}...")
            #    if "{" in content and ("tool_name" in content or "tool_calls" in content or "run_command" in content or "read_file" in content):
            #        print(f"  [!] DIAGNOSTIC WARNING: Model emitted JSON tool call format inside plain text content!")

            # Track the last text produced by the LLM (used by on_max_iterations="return_last").
            if content:
                last_response = content

            # One text channel here too. A worker that writes its report beside a
            # tool call and then says "report delivered above" used to hand the
            # orchestrator the pointer and drop the report: the same discard the
            # chat loop was rewritten to remove, on the delegation path.
            if message.tool_calls and content and content.strip():
                visible_texts.append(content)

            # Build the assistant message dict manually: model_dump() deserialises
            # `arguments` into a dict, corrupting the history (the API requires
            # arguments to be a JSON string).
            assistant_message: Dict[str, Any] = {"role": "assistant", "content": content}
            if message.tool_calls:
                assistant_message["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ]
            if reasoning:
                assistant_message["reasoning_content"] = reasoning
            messages.append(assistant_message)

            # If no tool call was requested, the agent has finished reasoning.
            if not message.tool_calls:
                termination_reason = "model returned a final text response (no tool calls)"

                content = content
                truncated = _stopped_at_length(response)
                structured_obj = None
                if self.response_schema:
                    try:
                        clean_content = content.strip()
                        if clean_content.startswith("```json"):
                            clean_content = clean_content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
                        elif clean_content.startswith("```"):
                            clean_content = clean_content.split("```", 1)[1].rsplit("```", 1)[0].strip()
                        structured_obj = self.response_schema.model_validate_json(clean_content)
                    except Exception:
                        # Fallback synthesis formatting call to force-convert conversations into the schema format
                        final_kwargs = self.model_kargs.copy()
                        final_kwargs.pop("tools", None)
                        final_kwargs.pop("tool_choice", None)
                        final_kwargs["response_format"] = self.response_schema
                        
                        final_resp = await self._acompletion(messages, **final_kwargs)

                        await self._emit_token_usage(final_resp, step=iteration_count)
                        content = final_resp.choices[0].message.content or ""
                        truncated = _stopped_at_length(final_resp)

                        final_content = content
                        final_reasoning = getattr(final_resp.choices[0].message, "reasoning_content", None)
                        if not final_reasoning and final_content:
                            inline_reasoning, visible_content = split_inline_reasoning(final_content)
                            if inline_reasoning:
                                final_reasoning = inline_reasoning
                                final_content = visible_content
                                content = final_content
                        
                        # Update assistant message in messages history
                        messages[-1]["content"] = final_content
                        if final_reasoning:
                            messages[-1]["reasoning_content"] = final_reasoning

                        try:
                            clean_content = content.strip()
                            if clean_content.startswith("```json"):
                                clean_content = clean_content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
                            elif clean_content.startswith("```"):
                                clean_content = clean_content.split("```", 1)[1].rsplit("```", 1)[0].strip()
                            structured_obj = self.response_schema.model_validate_json(clean_content)
                        except Exception as e:
                            if self.debug:
                                print(f"[DEBUG] Schema validation failed on fallback: {e}")
                
                output = AgentOutput(
                    response=compose_visible_response(visible_texts, content),
                    tool_calls_made=tool_call_count,
                    tool_calls=list(tool_records),
                    structured_output=structured_obj,
                    narration=list(visible_texts),
                    final_text=content,
                    truncated=truncated,
                )

                await self._invoke_on_iteration(iteration_count, messages)

                if self.debug:
                    _print_debug_report(
                        agent_name=self.name,
                        model=self.model,
                        iteration_count=iteration_count,
                        tool_call_count=tool_call_count,
                        tool_usage=dict(tool_usage),
                        termination_reason=termination_reason,
                        elapsed_seconds=time.monotonic() - start_time,
                    )
                return output

            # Transparent Execution (A2A and MCP).
            for tool_call in message.tool_calls:
                tool_call_count += 1
                self._current_tool_call_count = tool_call_count
                function_name = tool_call.function.name
                tool_usage[function_name] += 1

                # Break verbatim retry loops before the call is executed: a weak model
                # that keeps re-issuing an identical failing call would otherwise burn
                # the whole tool budget without ever changing approach.
                if self.loop_detection:
                    signature = _tool_call_signature(function_name, tool_call.function.arguments)
                    tool_call_signatures[signature] += 1
                    if tool_call_signatures[signature] > self.loop_detection_limit:
                        loop_message = _loop_block_message(function_name, self.loop_detection_limit)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": loop_message,
                        })
                        tool_records.append(ToolCallRecord(
                            index=tool_call_count, tool=function_name, ok=False, error=loop_message,
                        ))
                        continue

                # Look for the matching native tool (connected blocks).
                matched_block = next((b for b in self.tools if b.name == function_name), None)
                if not matched_block:
                    unknown_message = unknown_tool_message(
                        function_name, tool_call.function.arguments, litellm_tools
                    )
                    tool_records.append(ToolCallRecord(
                        index=tool_call_count, tool=function_name, ok=False, error=unknown_message,
                    ))
                    tool_result_content = json.dumps({"error": (
                        unknown_message
                        + " CRITICAL: Do NOT apologize to the user for this error. Correct it silently."
                    )})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": tool_result_content
                    })
                    await self._invoke_on_unknown_tool(function_name, tool_call.function.arguments)
                    if function_name in self.termination_tools:
                        termination_reason = f"termination tool '{function_name}' not found"
                        await self._invoke_on_iteration(iteration_count, messages)
                        if self.debug:
                            _print_debug_report(
                                agent_name=self.name,
                                model=self.model,
                                iteration_count=iteration_count,
                                tool_call_count=tool_call_count,
                                tool_usage=dict(tool_usage),
                                termination_reason=termination_reason,
                                elapsed_seconds=time.monotonic() - start_time,
                            )
                        return AgentOutput(
                            response=compose_visible_response(visible_texts, tool_result_content),
                            tool_calls_made=tool_call_count,
                            tool_calls=list(tool_records),
                            narration=list(visible_texts),
                            final_text=tool_result_content,
                        )
                    continue

                try:
                    # Parse arguments with the block's Pydantic input model (A2A bridge).
                    args_dict = json.loads(tool_call.function.arguments)
                    input_model = matched_block.input_schema()(**args_dict)

                    # RUN: the main agent transparently triggers a subordinate agent (A2A).
                    result = await matched_block.run(input=input_model)

                    # The typed output is serialised back to JSON for LiteLLM's history.
                    tool_result_content = json.dumps(result.model_dump(exclude_none=True) if hasattr(result, "model_dump") else result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": tool_result_content
                    })
                    tool_records.append(ToolCallRecord(index=tool_call_count, tool=function_name, ok=True))
                except Exception as e:
                    tool_records.append(ToolCallRecord(
                        index=tool_call_count, tool=function_name, ok=False, error=str(e),
                    ))
                    tool_result_content = json.dumps({"error": f"{str(e)}. Please fix your tool call and try again. CRITICAL: Do NOT apologize to the user for this error. Correct it silently."})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": tool_result_content
                    })

                if function_name in self.termination_tools:
                    termination_reason = f"termination tool '{function_name}' executed"
                    await self._invoke_on_iteration(iteration_count, messages)
                    if self.debug:
                        _print_debug_report(
                            agent_name=self.name,
                            model=self.model,
                            iteration_count=iteration_count,
                            tool_call_count=tool_call_count,
                            tool_usage=dict(tool_usage),
                            termination_reason=termination_reason,
                            elapsed_seconds=time.monotonic() - start_time,
                        )
                    return AgentOutput(
                        response=compose_visible_response(visible_texts, tool_result_content),
                        tool_calls_made=tool_call_count,
                        tool_calls=list(tool_records),
                        narration=list(visible_texts),
                        final_text=tool_result_content,
                    )

            # If the tool-call limit was reached, force a final response without tools.
            # This is necessary because some models (e.g. Ollama) ignore
            # tool_choice="none", causing an infinite loop.
            if tool_call_count >= self.max_tool_calls:
                final_kwargs = self.model_kargs.copy()
                final_kwargs.pop("tool_choice", None)
                if self.response_schema:
                    final_kwargs["response_format"] = self.response_schema

                if self.final_answer_prompt:
                    messages.append({"role": "system", "content": self.final_answer_prompt})
                final_response = await self._acompletion(messages, **final_kwargs)

                await self._emit_token_usage(final_response, step=iteration_count)

                termination_reason = f"max_tool_calls ({self.max_tool_calls}) reached → forced final response"
                
                content = final_response.choices[0].message.content or ""
                final_content = content
                final_reasoning = getattr(final_response.choices[0].message, "reasoning_content", None)
                if not final_reasoning and final_content:
                    inline_reasoning, visible_content = split_inline_reasoning(final_content)
                    if inline_reasoning:
                        final_reasoning = inline_reasoning
                        final_content = visible_content
                        content = final_content

                final_message = {"role": "assistant", "content": final_content}
                if final_reasoning:
                    final_message["reasoning_content"] = final_reasoning
                messages.append(final_message)

                structured_obj = None
                if self.response_schema and content:
                    try:
                        clean_content = content.strip()
                        if clean_content.startswith("```json"):
                            clean_content = clean_content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
                        elif clean_content.startswith("```"):
                            clean_content = clean_content.split("```", 1)[1].rsplit("```", 1)[0].strip()
                        structured_obj = self.response_schema.model_validate_json(clean_content)
                    except Exception as e:
                        if self.debug:
                            print(f"[DEBUG] Schema validation failed: {e}")

                output = AgentOutput(
                    response=compose_visible_response(visible_texts, content),
                    tool_calls_made=tool_call_count,
                    tool_calls=list(tool_records),
                    structured_output=structured_obj,
                    narration=list(visible_texts),
                    final_text=content,
                    budget_exhausted=True,
                    truncated=_stopped_at_length(final_response),
                )

                await self._invoke_on_iteration(iteration_count, messages)

                if self.debug:
                    _print_debug_report(
                        agent_name=self.name,
                        model=self.model,
                        iteration_count=iteration_count,
                        tool_call_count=tool_call_count,
                        tool_usage=dict(tool_usage),
                        termination_reason=termination_reason,
                        elapsed_seconds=time.monotonic() - start_time,
                    )
                return output
        finally:
            # Always restore litellm.callbacks to its pre-run state,
            # regardless of how the agent loop exits (return or exception).
            litellm.callbacks = _prev_callbacks
