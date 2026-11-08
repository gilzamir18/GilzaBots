import asyncio
import json
import re
import time
import inspect
from collections import defaultdict
from pydantic import Field, BaseModel, model_validator
from typing import List, Dict, Any, Optional, Callable
import litellm

from agenticblocks.core.agent import AgentBlock
from agenticblocks.blocks.llm.agent import (
    AgentInput, AgentOutput, _get_shared_router, _print_debug_report,
    _tool_call_signature, _loop_block_message, compose_visible_response
)
from agenticblocks.blocks.llm.inbox import InboxItem, MessageInbox
from agenticblocks.blocks.llm.agent import _await_reporting_cancellation
from agenticblocks.blocks.llm.response_wait import ResponseWait, ResponseWaitWatch
from agenticblocks.utils.messages import build_user_content, history_accepts_user_message
from agenticblocks.blocks.llm.tokens import count_message_tokens
from agenticblocks.blocks.llm.compaction import (
    COMPACTION_TRIGGER_AUTO,
    COMPACTION_TRIGGER_MANUAL,
    DEFAULT_COMPACTION_PROMPT,
    CompactionContext,
    CompactionError,
    CompactionEvent,
    CompactionRequest,
    build_compaction_request,
    continuation_message,
    extract_summary,
)
from agenticblocks.tools.a2a_bridge import block_to_tool_schema
from agenticblocks.core.block import Block
from agenticblocks.core.function_block import as_tool
from agenticblocks.runtime.state import TokenUsage, _current_ctx
from agenticblocks.utils.parsers import split_inline_reasoning
from agenticblocks.utils.tool_calls import (
    OLLAMA_TOOL_CALL_JSON_ALERT,
    is_ollama_tool_call_json_error,
    unknown_tool_message,
)


DEFAULT_RECURSIVE_SUMMARY = "No history has been evicted yet."

# Marker an older version of this loop wrote into the history in place of an
# assistant turn that produced no visible text. It is no longer written (the empty
# turn is dropped instead), because a model reads its own last turn as an example
# and echoes the marker back as if it were the answer. The string is kept here so
# it can still be recognised when it arrives from a persisted state or a chat log
# saved before that change.
EMPTY_RESPONSE_PLACEHOLDER = "(removed: empty response)"


HEARTBEAT_TOOL_NAME = "new_heartbeat"
"""Name of the tool a model calls to keep its turn open without acting.

The turn protocol has exactly one text channel and one continuation rule:
assistant ``content`` is always the user-facing answer, and the run continues
only while the model has something pending. A native tool call continues it
implicitly -- the result has to be delivered -- so this tool exists solely for
the remaining case: the model wants to think or speak in one step and act in
the next. It is rarely the right call, which is why it is a separate, explicit
request rather than a flag riding on the message that carries the answer."""


def _is_concurrency_safe(block: Any) -> bool:
    """Whether *block* declared it may run alongside other calls of its batch."""
    return bool(getattr(block, "concurrency_safe", False))


async def _execute_tool_call(tool_call: Any, function_name: str, block: Any, sys_msg: str) -> dict:
    """Run one native tool call and return the tool message for the history.

    An exception becomes an error result for that call alone, so one failing
    call of a concurrent group never discards the results of the others.
    Cancellation is not an exception here: it propagates and cancels the group.
    """
    try:
        args_dict = json.loads(tool_call.function.arguments)
        input_model = block.input_schema()(**args_dict)
        result = await block.run(input=input_model)
        if hasattr(result, "model_dump"):
            content_str = json.dumps(result.model_dump(exclude_none=True))
        elif isinstance(result, str):
            content_str = result
        else:
            content_str = json.dumps(result)
        return {"role": "tool", "tool_call_id": tool_call.id, "name": function_name, "content": content_str + sys_msg}
    except Exception as e:
        return {"role": "tool", "tool_call_id": tool_call.id, "name": function_name, "content": json.dumps({"error": str(e)})}


def is_empty_response_placeholder(text: Any) -> bool:
    """Return whether assistant text is this runtime's own empty-response marker."""
    if not isinstance(text, str):
        return False
    return text.strip().lower() == EMPTY_RESPONSE_PLACEHOLDER


# Shared with LLMAgentBlock; the private names are kept for existing callers.
_is_ollama_tool_call_json_escape_error = is_ollama_tool_call_json_error


def _heartbeat_pressure_alert(heartbeats_left: int, max_heartbeats: int) -> str:
    """Return the system warning shown while the step budget is running out."""
    return (
        f"SYSTEM ALERT: Heartbeat Pressure. Only {heartbeats_left} of {max_heartbeats} "
        "heartbeats remain in this turn (one per response that carries tool calls). "
        "When they run out no further tool call will run, "
        "and work still in progress stays unfinished. "
        "Be brief from here on: keep any text short, drop optional checks and "
        "exploration, finish the work already in progress, and deliver your final "
        "user-facing answer while you still have steps to act on anything it needs."
    )


def _idle_allowance_spent_alert(acted_in_run: bool) -> str:
    """Return the request for the answer once the model has stopped acting.

    The allowance counts consecutive responses with no action since the last
    one, so it can run out at the end of a turn full of real work. Telling that
    model "no tool call was ever issued" would invite it to report to the user
    that nothing was done, so the wording depends on whether anything ran.
    """
    if acted_in_run:
        situation = (
            "SYSTEM ALERT: Your turn is ending now. Your last responses asked to continue "
            "without acting, so nothing you only described since your last tool call has "
            "been done; the tool calls before that did run."
        )
    else:
        situation = (
            "SYSTEM ALERT: Your turn is ending now and no tool call was ever issued, so "
            "nothing you described has been done."
        )
    return (
        f"{situation} Do NOT state what you are going to do. Answer the user with what you "
        "actually established; if you could not act, say plainly that you could not and "
        "what you would need."
    )


_EXHAUSTED_BUDGET_ALERT = (
    "SYSTEM ALERT: No action heartbeats remain in this turn, so no tool call will run. "
    "Write your final user-facing response now, as normal text: report what was "
    "actually done and verified, and state plainly anything still unfinished so the "
    "user can ask you to continue. Do NOT describe actions you are about to take."
)
"""The last request of a run whose step budget is spent.

It asks for an honest status rather than a success report: the budget can run
out in the middle of work, and an answer that implies the work is complete when
it is not is the false completion the idle allowance exists to prevent."""


def _ollama_tool_call_json_escape_alert() -> str:
    """Return bounded system feedback for a malformed native Ollama tool call."""
    return OLLAMA_TOOL_CALL_JSON_ALERT


class MemGPTAgentBlock(AgentBlock[AgentInput, AgentOutput]):
    """
    An autonomous LLM agent with bounded MemGPT-style heartbeats and memory tools.

    There is one text channel and one continuation rule. Assistant ``content`` is
    always user-facing: every non-empty text the model produces during a run is
    delivered, in order, whether or not tool calls rode along with it. The run
    continues while a native tool call is pending, or while the model asks for
    another heartbeat through ``new_heartbeat``; a response that does neither ends
    the turn. Text is never recovered or interpreted as a tool call.
    """
    description: str = "MemGPT style Agent with strict heartbeat limits and context management."
    model: str = "ollama/gemma4:latest"
    user_prompt_prefix: str = ""
    """Fixed user instructions reapplied to every provider request, outside memory."""
    system_prompt: str = "You are a helpful AI assistant with extended memory capabilities."
    tools: List[Block] = []
    max_heartbeats: int = 10
    """Model calls that may carry tool calls in one run: the runaway guardrail.

    Spending the last one does not end the run on the spot. The results of that
    step have not been read yet, so one more request follows with tool calls off
    and an alert asking for the final answer; whatever it returns ends the run,
    and tool calls it returns anyway are refused, not executed. ``AgentOutput``
    reports ``budget_exhausted`` either way."""
    max_context_tokens: Optional[int] = 4000
    """Context window the history is managed against.

    ``None`` means the window is unknown: nothing is evicted and no memory
    pressure alert is added, so the provider is the only authority on whether a
    request fits. Use it when the caller cannot learn the model's real window,
    rather than inventing a number that would evict or warn at the wrong size."""
    eviction_threshold: float = 1.0
    auto_compact_threshold: Optional[float] = None
    """Fraction of ``max_context_tokens`` past which a run compacts the context.

    Compaction (see :meth:`compact`) replaces the whole working history with one
    structured summary, instead of evicting the oldest messages a quarter at a
    time. It is checked at the top of every heartbeat, before eviction, so a
    threshold below ``eviction_threshold`` makes compaction the first response
    to a filling window and leaves eviction as the mechanism of last resort.

    ``None`` (the default) or a value of ``1.0`` or higher disables it, as does
    an unknown window (``max_context_tokens=None``). Within one run, automatic
    compaction stops after an attempt fails or leaves the context still above
    the threshold, so a summary that cannot help is not requested again on every
    heartbeat."""
    compaction_prompt: str = DEFAULT_COMPACTION_PROMPT
    """Instructions that ask the model for the summary a compaction keeps."""
    memory_pressure_threshold: float = 0.7
    heartbeat_pressure_threshold: float = 0.75
    """Fraction of ``max_heartbeats`` spent past which every request carries a
    step-budget warning.

    Running out of heartbeats stops a turn's work wherever it is, leaving the
    model a single request to report on it, and the model only ever learns how
    much budget is left from the tail of a tool result, which is easy to
    overlook. Past this fraction the remaining budget stops being a footnote:
    each request opens with a SYSTEM ALERT naming the steps left and asking for
    brevity, so the model can converge on its answer while it still has the
    steps to write one. Mirrors ``memory_pressure_threshold``, for the other
    finite resource of a turn.

    A value of ``1.0`` or higher disables the warning -- the exhausted-budget
    alert already covers the last step -- and ``0`` warns from the first."""
    tool_call_limits: Dict[str, int] = Field(default_factory=dict)
    response_schema: Optional[type[BaseModel]] = None
    """Optional Pydantic model class to enforce a structured response schema."""
    debug: bool = False
    loop_detection: bool = True
    """When True, block a tool call that repeats an identical (name, arguments)
    signature more than ``loop_detection_limit`` times in a single run."""
    loop_detection_limit: int = 3
    """Number of identical tool calls allowed before ``loop_detection`` blocks them."""
    empty_response_reasoning_fallback: bool = False
    """When True, a turn that produced only reasoning and no visible text ends the
    run with that reasoning as the final response, instead of spending a heartbeat
    asking the model to repeat itself in the visible channel. Off by default: the
    reasoning channel is a draft space, so publishing it as the user-facing answer
    is a semantic decision the caller has to opt into."""
    max_idle_heartbeats: int = 2
    """Consecutive ``new_heartbeat`` requests granted with no action in between.

    ``new_heartbeat`` is the one way to keep a turn open without doing anything,
    which makes it the one way to spin: a model can ask to continue, say nothing
    it has not already said, and ask again. A model that acts between requests is
    working normally, so the count resets on every other tool call; only an
    unbroken run of idle heartbeats means the model is not going to act.

    Spending the allowance must not deliver the failure it exists to stop. The
    last call is not accepted as the answer -- that hands the user "I will now
    read the file" as a result -- it is answered by asking for the answer, with
    tool calls off the table, so the turn cannot end on one more intention."""
    use_shared_router: bool = True
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
    tools_provider: Optional[Callable[[], List[Block]]] = Field(default=None, exclude=True)
    """Optional capability resolver, evaluated before each model request.

    Each response is dispatched against the tools advertised for that request.
    Hosts can grant or revoke capabilities between requests without restarting
    the conversation. Without a provider, the existing tools list is used.
    """
    on_iteration: Optional[Callable[[int, List[Dict[str, Any]]], Any]] = None
    """Optional callback invoked at the start of each loop iteration for debugging. 
    Signature: `def callback(iteration: int, messages: List[Dict[str, Any]]) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_thinking: Optional[Callable[[str], Any]] = None
    """Optional callback invoked after each LLM heartbeat call with the model's reasoning content.
    Fires once per heartbeat (not per-token) with the full reasoning text for that call.
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
    on_token_usage: Optional[Callable[["TokenUsage"], Any]] = None
    """Optional callback invoked after each LLM call with token statistics.
    Fires once per heartbeat call, carrying the provider-reported prompt,
    completion and total token counts for that call. `prompt_tokens` measures the
    whole assembled request -- system prompt, recursive summary, tool schemas,
    tool calls and tool results included -- so it is the only accurate reading of
    how full the context window is.
    Signature: `def callback(usage: TokenUsage) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_compaction: Optional[Callable[["CompactionEvent"], Any]] = None
    """Optional callback invoked as a compaction starts, completes or fails.

    Fires for both manual (:meth:`compact`) and automatic compactions. The
    ``"completed"`` event carries the summary and the token counts before and
    after, so a host can show where the context was compacted and persist it.
    Signature: `def callback(event: CompactionEvent) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_compaction_request: Optional[Callable[["CompactionRequest"], Any]] = None
    """Optional host step run before a compaction's summary is requested.

    Receives a mutable :class:`CompactionRequest`: the host may change
    ``custom_instructions`` or set ``blocked_reason`` to stop the compaction
    (it then fails with that reason, context untouched). An exception it raises
    fails the compaction too. Anything it stores in ``metadata`` is reported in
    the compaction's ``completed`` or ``failed`` event as ``extra``.
    Signature: `def callback(request: CompactionRequest) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_compacted_context: Optional[Callable[["CompactionContext"], Any]] = None
    """Optional host step run once the summary exists, before it replaces the context.

    Receives a mutable :class:`CompactionContext`; text the host appends to
    ``extra_context`` follows the summary in the message the new context starts
    from -- for instance the current contents of the files the conversation was
    working on. A compacted context that would not fit the known window fails
    the compaction instead of being cut.
    Signature: `def callback(context: CompactionContext) -> Any`.
    Can be a synchronous or asynchronous function."""
    on_unknown_tool: Optional[Callable[[str, str], Any]] = None
    """Optional callback invoked when the model calls a tool it was not offered.

    The call is not executed; the model receives a corrective tool result that
    lists the available tools. This hook lets a host surface the failed call,
    which otherwise leaves no trace outside the conversation because no tool
    ran. Receives the tool name and the raw arguments string.
    Signature: `def callback(name: str, arguments: str) -> Any`.
    Can be a synchronous or asynchronous function."""

    inbox: Optional[MessageInbox] = None
    """Optional channel for messages submitted while this run is already in flight.

    Drained at the top of each heartbeat, before the request is assembled, which
    is the only point where `internal_history` is guaranteed not to sit between
    an assistant tool call and its results. Delivered messages join
    `internal_history`, so they are saved with `dump_state` and survive an
    interrupted turn like any other part of the conversation.

    The loop breaks as soon as the model returns a final text response, so a
    message submitted during that last response is never delivered here: it
    stays pending and `MessageInbox.close()` hands it back to the caller."""
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

    # Memória de estado persistente do agente
    internal_history: List[Dict[str, Any]] = Field(default_factory=list)
    recursive_summary: str = DEFAULT_RECURSIVE_SUMMARY

    model_config = {"arbitrary_types_allowed": True}

    async def _invoke_on_unknown_tool(self, name: str, arguments: Any) -> None:
        if self.on_unknown_tool:
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
            if inspect.iscoroutinefunction(self.on_unknown_tool):
                await self.on_unknown_tool(name, raw)
            else:
                self.on_unknown_tool(name, raw)

    async def _invoke_on_message_delivery(self, item: "InboxItem") -> None:
        if self.on_message_delivery:
            if inspect.iscoroutinefunction(self.on_message_delivery):
                await self.on_message_delivery(item)
            else:
                self.on_message_delivery(item)

    async def _drain_inbox(self) -> List["InboxItem"]:
        """Append every pending inbox message to `internal_history`, in order.

        Returns without taking anything while the history cannot accept a
        message: the items stay queued for the next heartbeat instead of being
        delivered into a request the provider would reject.
        """
        if not self.inbox or not len(self.inbox):
            return []
        if not history_accepts_user_message(self.internal_history):
            return []

        delivered: List["InboxItem"] = []
        for item in self.inbox.drain():
            await self._invoke_on_message_delivery(item)
            self.internal_history.append({
                "role": item.role,
                "content": build_user_content(item.content, item.attachments),
            })
            delivered.append(item)
        return delivered

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

    def _estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return count_message_tokens(self.model, messages)

    def build_request_messages(self) -> List[Dict[str, Any]]:
        """Return the messages a request would carry for the current state.

        The main context is the system prompt, the recursive summary of what was
        evicted, and the internal history — in that order. ``run`` assembles the
        request from here, so a caller that needs to know how full the window is
        (a context indicator, an eviction check, a budget for the next tool
        result) measures the same list the model will actually receive instead of
        re-deriving it and drifting from it.
        """
        return [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "system", "content": f"Recursive Summary of older messages: {self.recursive_summary}"},
        ] + self.internal_history

    def count_context_tokens(self) -> int:
        """Return the token count of the current main context, for this model."""
        from agenticblocks.utils.messages import prepend_user_prompt
        return self._estimate_tokens(prepend_user_prompt(self.build_request_messages(), self.user_prompt_prefix))

    def _get_safe_eviction_index(self, history: List[Dict[str, Any]], target_count: int) -> int:
        """Finds a safe index to evict up to, ensuring we don't split tool calls from their results.

        The last ``user`` message is the turn currently being answered, so eviction
        never crosses it: dropping it would make the agent answer a question that is
        no longer in context. Returning 0 means "nothing can be safely evicted" and
        the caller must leave the history untouched.
        """
        # The clamp comes first: asking to evict the whole history must still stop
        # at the current turn.
        last_user_index = max(
            (i for i, msg in enumerate(history) if (msg or {}).get("role") == "user"),
            default=len(history),
        )
        target_count = min(target_count, last_user_index, len(history))
        if target_count <= 0:
            return 0
        safe_index = target_count
        while safe_index < len(history):
            msg = history[safe_index]
            if msg.get("role") == "tool":
                safe_index += 1
                continue
            if safe_index > 0:
                prev_msg = history[safe_index - 1]
                if prev_msg.get("role") == "assistant" and prev_msg.get("tool_calls"):
                    safe_index += 1
                    continue
            break
        # Adjacency repair may have walked past the current turn (e.g. an orphan
        # tool call left by an interrupted run sits right before it). Keeping the
        # user message always wins; any orphan left behind is repaired downstream.
        if safe_index > last_user_index:
            return last_user_index
        return safe_index

    async def _summarize(self, messages_to_evict: List[Dict[str, Any]]) -> str:
        """Build a new recursive summary from the previous one plus the evicted messages."""
        summary_prompt = (
            f"CURRENT SUMMARY: {self.recursive_summary}\n\n"
            f"NEW MESSAGES EVICTED FROM CONTEXT:\n{json.dumps(messages_to_evict, indent=2)}\n\n"
            "Write a new concise summary that incorporates the key information from the "
            "current summary and from the newly evicted messages."
        )
        try:
            resp = await self._acompletion(
                [{"role": "system", "content": "You are a concise conversation summarizer."},
                 {"role": "user", "content": summary_prompt}],
                **self.model_kargs
            )
            return resp.choices[0].message.content or self.recursive_summary
        except Exception as e:
            if self.debug: print(f"[DEBUG] Erro na sumarização recursiva: {e}")
            return self.recursive_summary

    async def _invoke_on_compaction(self, event: CompactionEvent) -> None:
        if self.on_compaction:
            if inspect.iscoroutinefunction(self.on_compaction):
                await self.on_compaction(event)
            else:
                self.on_compaction(event)

    def _auto_compact_enabled(self) -> bool:
        threshold = self.auto_compact_threshold
        return (
            self.max_context_tokens is not None
            and threshold is not None
            and threshold < 1.0
        )

    def _compaction_kwargs(self) -> Dict[str, Any]:
        """The request options for a summary: the model's own, without actions.

        Streaming is off so the summary never reaches ``on_chunk`` as if it were
        part of the answer, and no tools are declared so the model cannot act.
        """
        kwargs = dict(self.model_kargs)
        for key in ("stream", "stream_options", "tools", "tool_choice",
                    "parallel_tool_calls", "response_format"):
            kwargs.pop(key, None)
        return kwargs

    async def _invoke_compaction_hook(self, hook: Optional[Callable[[Any], Any]], arg: Any) -> None:
        if hook:
            if inspect.iscoroutinefunction(hook):
                await hook(arg)
            else:
                result = hook(arg)
                if inspect.isawaitable(result):
                    await result

    async def compact(
        self,
        custom_instructions: str = "",
        *,
        trigger: str = COMPACTION_TRIGGER_MANUAL,
    ) -> CompactionEvent:
        """Replace the working context with a structured summary of it.

        The recursive summary and the whole ``internal_history`` are sent to the
        model as a transcript, with ``compaction_prompt`` (plus
        ``custom_instructions``, when given) asking for a sectioned summary. On
        success the history becomes a single user message that states the
        conversation continues from a previous one and carries that summary, and
        the recursive summary is reset, since it is folded into the new one. An
        automatic compaction also tells the model to carry on with its task.

        Two host extension points take part: ``on_compaction_request`` before the
        summary is requested (it may add instructions or block the compaction)
        and ``on_compacted_context`` once the summary exists (it may add context
        after it). An exception raised by either fails the compaction.

        Raises :class:`CompactionError`, after reporting a ``"failed"`` event,
        when there is nothing to compact, when the history ends in a tool call
        still waiting for its result, when a host blocks it, when the request or
        the compacted context would not fit the known window, or when the model
        returns no summary. The context is untouched in every one of those cases.
        """
        pre_tokens = self.count_context_tokens()
        history = list(self.internal_history)
        previous_summary = (
            "" if self.recursive_summary == DEFAULT_RECURSIVE_SUMMARY else self.recursive_summary
        )
        state = {"instructions": (custom_instructions or "").strip()}
        metadata: Dict[str, Any] = {}

        async def fail(reason: str, cause: Optional[BaseException] = None) -> None:
            await self._invoke_on_compaction(CompactionEvent(
                phase="failed", trigger=trigger, pre_tokens=pre_tokens,
                context_window=self.max_context_tokens,
                custom_instructions=state["instructions"], error=reason,
                extra=metadata,
            ))
            if cause is not None:
                raise CompactionError(reason) from cause
            raise CompactionError(reason)

        if not history and not previous_summary:
            await fail("There is no conversation to compact yet.")
        if not history_accepts_user_message(history):
            await fail(
                "The conversation ends in a tool call that is still waiting for its "
                "result, so it cannot be compacted at this point."
            )

        compaction_request = CompactionRequest(
            trigger=trigger,
            custom_instructions=state["instructions"],
            history=[dict(m) for m in history if isinstance(m, dict)],
            metadata=metadata,
        )
        try:
            await self._invoke_compaction_hook(self.on_compaction_request, compaction_request)
        except Exception as exc:
            await fail(f"Preparing the compaction failed: {exc}", exc)
        state["instructions"] = (compaction_request.custom_instructions or "").strip()
        if (compaction_request.blocked_reason or "").strip():
            await fail(f"The compaction was blocked: {compaction_request.blocked_reason.strip()}")

        request = build_compaction_request(
            history, previous_summary, state["instructions"], self.compaction_prompt,
        )
        if self.max_context_tokens is not None:
            request_tokens = self._estimate_tokens(request)
            if request_tokens >= self.max_context_tokens:
                await fail(
                    f"The conversation is too long to compact: the summary request needs "
                    f"{request_tokens} tokens and the context window holds "
                    f"{self.max_context_tokens}."
                )

        await self._invoke_on_compaction(CompactionEvent(
            phase="started", trigger=trigger, pre_tokens=pre_tokens,
            context_window=self.max_context_tokens,
            messages_compacted=len(history), custom_instructions=state["instructions"],
            extra=metadata,
        ))

        try:
            response = await self._acompletion(request, **self._compaction_kwargs())
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            await fail(f"The summary request failed: {exc}", exc)

        summary = extract_summary(raw)
        if not summary:
            await fail("The model returned an empty summary.")

        compacted = CompactionContext(
            trigger=trigger,
            summary=summary,
            custom_instructions=state["instructions"],
            history=compaction_request.history,
            metadata=metadata,
        )
        try:
            await self._invoke_compaction_hook(self.on_compacted_context, compacted)
        except Exception as exc:
            await fail(f"Building the compacted context failed: {exc}", exc)

        previous_history = self.internal_history
        previous_recursive = self.recursive_summary
        self.internal_history = [{
            "role": "user",
            "content": continuation_message(
                summary,
                resume=trigger == COMPACTION_TRIGGER_AUTO,
                extra_context=[str(block) for block in compacted.extra_context],
            ),
        }]
        self.recursive_summary = DEFAULT_RECURSIVE_SUMMARY
        post_tokens = self.count_context_tokens()
        if self.max_context_tokens is not None and post_tokens >= self.max_context_tokens:
            self.internal_history = previous_history
            self.recursive_summary = previous_recursive
            await fail(
                f"The compacted context would not fit: it needs {post_tokens} tokens and the "
                f"context window holds {self.max_context_tokens}."
            )

        event = CompactionEvent(
            phase="completed", trigger=trigger, pre_tokens=pre_tokens,
            post_tokens=post_tokens,
            context_window=self.max_context_tokens,
            messages_compacted=len(history), summary=summary,
            custom_instructions=state["instructions"],
            extra=metadata,
        )
        await self._invoke_on_compaction(event)
        return event

    def _build_system_prompt(self) -> str:
        tool_descriptions_list = []
        for t in self.tools:
            desc = f"- **{t.name}**: {getattr(t, 'description', 'No description.')}"
            if t.name in self.tool_call_limits:
                desc += f" [LIMIT: at most {self.tool_call_limits[t.name]} call(s) per turn]"
            tool_descriptions_list.append(desc)
        tool_descriptions = "\n".join(tool_descriptions_list)
        
        heartbeat_line = (
            f"- **{HEARTBEAT_TOOL_NAME}**: Keeps your turn open for one more step without "
            "performing an action. Needed only when you want to speak now and act next: an "
            "ordinary tool call already keeps the turn open on its own."
        )

        memgpt_rules = f"""
\n\n---
# SYSTEM INSTRUCTIONS (MEMGPT ARCHITECTURE)

You are running on an OS-like MemGPT architecture. You have a limited Main Context (working memory) and access to external memory databases via tools.

## AVAILABLE TOOLS
{tool_descriptions}
{heartbeat_line}

## CORE RULES
1. **RESPONSE CONTRACT**: Every piece of normal text you write is delivered to the user exactly as written, including text you write in the same response as a tool call. There is no separate delivery tool and no second channel. Use native provider tool calls for actions; JSON, Markdown, code blocks, examples and questions written as text are never tool calls.
2. **HEARTBEATS (YOUR STEP BUDGET)**: Your turn continues while a tool call is pending. It ends when you reply with text and no tool call, and that reply is your final answer. To speak before acting, call `{HEARTBEAT_TOOL_NAME}` in the same response to hold the turn open. You have a finite number of heartbeats per turn, and a heartbeat is one step: each response of yours that carries tool calls spends exactly one, however many calls it contains. When several actions do not depend on each other's results (reading two files, searching and reading), issue them together in one response. Plan efficiently, avoid repetitive verification and exploratory rabbit holes, and conclude with your final answer well before your heartbeats run out. If you see a SYSTEM ALERT about Heartbeat Pressure, your heartbeats are nearly spent: be brief, stop exploring, wrap up what you are doing and deliver your final answer.
3. **MEMORY PRESSURE**: If you see a SYSTEM ALERT about Memory Pressure, your Main Context is almost full. Be concise and rely on memory tools instead of keeping everything in context.
4. **NO HALLUCINATION**: If the user asks about past interactions or facts you don't know, ALWAYS use your memory tools to retrieve the information before answering.
5. **FINISHING & TERMINATION CONTRACT**: Always bring the turn to a clean conclusion. Never finish by writing that you are about to do something. When the goal is met or when remaining heartbeats are low, stop issuing tool calls and deliver your completed, user-facing answer as normal text. Text with no tool call is read as your finished answer.
"""
        return self.system_prompt + memgpt_rules

    def _heartbeat_pressure_reached(self, heartbeats_used: int) -> bool:
        """Return whether the spent step budget has crossed the warning point."""
        if self.max_heartbeats <= 0 or self.heartbeat_pressure_threshold >= 1.0:
            return False
        threshold = max(0.0, self.heartbeat_pressure_threshold)
        return heartbeats_used >= self.max_heartbeats * threshold

    async def _acompletion(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """Single call site for LiteLLM completions.

        When stream=True is present in kwargs the response is fully aggregated
        via stream_chunk_builder before returning, so callers always receive a
        plain ModelResponse regardless of streaming mode. Thinking chunks are
        forwarded to on_thinking in real time during stream consumption.
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

        # A multimodal request used to be rerouted here from ollama_chat/ to
        # ollama/, because Ollama's native /api/chat endpoint did not accept
        # OpenAI-style image_url parts and the OpenAI-compatible route did. That
        # reroute has since become the more expensive bug: on the ollama/ route a
        # streamed tool call never arrives as tool_calls at all -- the provider
        # emits it as plain text, token by token ('{"', 'name', '":"', ...), so
        # there is nothing for stream_chunk_builder to assemble and the model's
        # action reaches the caller as prose. An agent that attached an image
        # therefore lost its ability to act for the whole turn.
        #
        # LiteLLM converts image parts for the native route now (verified against
        # 1.97 with a vision model: the image is described correctly *and* the
        # tool call arrives native while streaming), so the reroute buys nothing
        # and costs tool calling. Requests go to the model's own route.
        effective_model = self.model
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

    def dump_state(self) -> Dict[str, Any]:
        """Serialize the agent's internal state so it can be paused and persisted."""
        return {
            "internal_history": self.internal_history,
            "recursive_summary": self.recursive_summary,
        }

    def load_state(self, state: Dict[str, Any]) -> None:
        """Restore the agent's internal state from a serialized dictionary.

        Assistant turns holding only ``EMPTY_RESPONSE_PLACEHOLDER`` are dropped:
        a state saved by an older version can still carry them, and they exist for
        the model to imitate while carrying nothing it needs to remember.
        """
        history = state.get("internal_history", [])
        self.internal_history = [
            msg
            for msg in history
            if not (
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and not msg.get("tool_calls")
                and is_empty_response_placeholder(msg.get("content"))
            )
        ]
        self.recursive_summary = state.get("recursive_summary", DEFAULT_RECURSIVE_SUMMARY)

    def append_external_messages(self, messages: List[Dict[str, Any]]) -> int:
        """Append conversation turns that happened outside this agent.

        A conversation can continue somewhere this agent never ran — another
        process, another machine, a different client — and reach the host's
        store without ever entering this agent's saved state. Restoring that
        state alone would leave the agent unaware of those turns. This appends
        them to `internal_history`, in the order given, so the next request
        carries them like any other part of the conversation.

        Only plain ``user`` and ``assistant`` messages with string content are
        accepted: a tool call or a tool result cannot be replayed without the
        other half of its unit, and a ``system`` message mid-history breaks
        chat templates that require the system message first. Anything else
        raises ``ValueError`` and nothing is appended.

        Returns the number of messages appended. Returns 0 without appending
        while the history ends in a tool call that is still unanswered (see
        :func:`history_accepts_user_message`): the caller decides whether to
        repair the history first or to keep the messages for later.
        """
        cleaned: List[Dict[str, Any]] = []
        for index, message in enumerate(messages or []):
            if not isinstance(message, dict):
                raise ValueError(f"External message {index} is not a mapping.")
            role = message.get("role")
            content = message.get("content")
            if role not in ("user", "assistant"):
                raise ValueError(
                    f"External message {index} has role {role!r}; only 'user' and 'assistant' are accepted."
                )
            if not isinstance(content, str):
                raise ValueError(f"External message {index} must carry string content.")
            cleaned.append({"role": role, "content": content})
        if not cleaned:
            return 0
        if not history_accepts_user_message(self.internal_history):
            return 0
        self.internal_history.extend(cleaned)
        return len(cleaned)

    async def run(self, input: AgentInput) -> AgentOutput:
        start_time = time.monotonic()

        @as_tool(
            name=HEARTBEAT_TOOL_NAME,
            description=(
                "Keep your turn open for one more step without performing an action. Use it "
                "only when you want to speak now and act next: any ordinary tool call already "
                "keeps the turn open, and text with no tool call ends the turn as your answer."
            ),
        )
        def new_heartbeat() -> str:
            # Declared for its schema only: the loop answers this call itself, so
            # that granting a heartbeat and counting it as idle stay one decision.
            # It takes no reason argument on purpose -- text is the channel now,
            # so a model with something to say writes it and the user reads it.
            return "Heartbeat granted. Your turn stays open: take the next step."

        agent_tools = [*self.tools, new_heartbeat]
        litellm_tools = [block_to_tool_schema(b) for b in agent_tools]

        # Build user content — plain string or multimodal list (vision models).
        user_content: str | list = build_user_content(input.prompt, input.attachments)

        # Append the incoming prompt to the internal history. Runtime corrections
        # enter as "system" so local models do not read framework feedback as
        # user-authored content.
        input_role = input.role or "user"
        if input_role not in ("user", "system"):
            raise ValueError(
                f"MemGPTAgentBlock accepts AgentInput(role='user'|'system'); got '{input_role}'."
            )
        if input_role != "user" and not isinstance(user_content, str):
            raise ValueError(
                "Attachments require AgentInput(role='user'); a system prompt cannot carry them."
            )
        self.internal_history.append({"role": input_role, "content": user_content})

        heartbeats_used = 0
        tool_call_count = 0
        # Whether any real action ran this turn. The idle allowance resets on every
        # action, so it can run out after a long stretch of real work, and the
        # alert that follows must not tell the model that nothing was done.
        acted_in_run = False
        tool_usage: Dict[str, int] = defaultdict(int)
        tool_call_signatures: Dict[str, int] = defaultdict(int)
        termination_reason = "unknown"
        idle_heartbeats = 0
        force_final_answer = False
        # Every non-empty assistant text of this run, in order. One text channel
        # means nothing the model writes is dropped for having travelled beside a
        # tool call: the answer is what it said, from the first word to the last.
        visible_texts: List[str] = []
        # Automatic compaction stays armed until an attempt fails or does not bring
        # the context back under the threshold; repeating it would spend a summary
        # request on every heartbeat for nothing.
        auto_compact_armed = self._auto_compact_enabled()
        # The same text, split by what the model was doing when it wrote it, for a
        # host that wants to show progress and answer differently. `visible_texts`
        # stays the whole of it: this split is a view, never the only copy.
        narration_texts: List[str] = []
        final_text_only = ""
        
        while True:
            # Capabilities can change after an approval or revocation. Resolve
            # once per request: calls in its response use that exact snapshot.
            if self.tools_provider is not None:
                self.tools = list(self.tools_provider())
                agent_tools = [*self.tools, new_heartbeat]
                litellm_tools = [block_to_tool_schema(b) for b in agent_tools]
            # Messages submitted while this turn is in flight enter the history
            # here, before the request is assembled and before eviction runs, so
            # the model sees them on its next call and the newest of them becomes
            # the turn eviction refuses to cross.
            await self._drain_inbox()

            # --- Gerenciamento de Contexto (FIFO Queue & Summarization) ---
            messages = self.build_request_messages()

            current_tokens = self.count_context_tokens()

            # Compaction comes before eviction: past its threshold the whole
            # context is replaced by a structured summary, and eviction only acts
            # on whatever is still too large afterwards.
            if (
                auto_compact_armed
                and len(self.internal_history) > 1
                and current_tokens > self.max_context_tokens * self.auto_compact_threshold
            ):
                try:
                    await self.compact(trigger=COMPACTION_TRIGGER_AUTO)
                except CompactionError as exc:
                    auto_compact_armed = False
                    if self.debug: print(f"[DEBUG] Automatic compaction failed: {exc}")
                else:
                    messages = self.build_request_messages()
                    current_tokens = self.count_context_tokens()
                    if current_tokens > self.max_context_tokens * self.auto_compact_threshold:
                        auto_compact_armed = False

            # Evictação FIFO
            window_known = self.max_context_tokens is not None
            if window_known and current_tokens > self.max_context_tokens * self.eviction_threshold:
                if self.debug: print(f"[DEBUG] Contexto excedeu limite de evictação ({current_tokens} tokens). Iniciando evictação FIFO...")
                target_evict = max(1, len(self.internal_history) // 4)
                safe_evict_idx = self._get_safe_eviction_index(self.internal_history, target_evict)

                # 0 means nothing can be evicted without dropping the current user
                # turn. Leave the history alone; the memory-pressure alert below
                # still tells the model to be economical.
                if safe_evict_idx > 0 and safe_evict_idx < len(self.internal_history):
                    to_evict = self.internal_history[:safe_evict_idx]
                    self.internal_history = self.internal_history[safe_evict_idx:]
                    
                    self.recursive_summary = await self._summarize(to_evict)
                    
                    messages = self.build_request_messages()
                    current_tokens = self.count_context_tokens()
                else:
                    if self.debug: print("[DEBUG] Falha ao evictar: impossível quebrar o histórico de forma segura.")

            # Alerta de Pressão de Memória após possível evictação
            if window_known and current_tokens > self.max_context_tokens * self.memory_pressure_threshold:
                pct = int(self.memory_pressure_threshold * 100)
                messages.append({
                    "role": "system", 
                    "content": f"SYSTEM ALERT: Memory Pressure (>{pct}% context reached). Move critical facts to archival/working storage if needed."
                })

            # --- Execução do Turno ---
            heartbeats_left = self.max_heartbeats - heartbeats_used
            kwargs = self.model_kargs.copy()
            kwargs["tools"] = litellm_tools
            
            if force_final_answer:
                # The model has spent its idle allowance without acting. Accepting
                # what it last said as the answer would deliver an announcement as
                # if it were the result, so the answer is asked for explicitly
                # instead, with tool calls off the table.
                kwargs["tool_choice"] = "none"
                messages.append({
                    "role": "system",
                    "content": _idle_allowance_spent_alert(acted_in_run),
                })
            elif heartbeats_left <= 0:
                # The budget is spent, but the model has not yet been heard from
                # since its last action: the answer is asked for once, with tool
                # calls off the table. Breaking out as soon as the budget ran out
                # used to skip this request entirely, so a model that finished the
                # work on its last step was cut off before it could say so.
                kwargs["tool_choice"] = "none"
                messages.append({
                    "role": "system",
                    "content": _EXHAUSTED_BUDGET_ALERT,
                })
            else:
                kwargs["tool_choice"] = "auto"
                # Warn while there is still budget to act on the warning. The
                # alert is transient by design: `messages` is rebuilt from
                # `internal_history` every iteration, so the model reads one
                # current warning instead of a pile of stale ones.
                if self._heartbeat_pressure_reached(heartbeats_used):
                    messages.append({
                        "role": "system",
                        "content": _heartbeat_pressure_alert(
                            heartbeats_left, self.max_heartbeats
                        ),
                    })

            await self._invoke_on_iteration(heartbeats_used, messages)

            try:
                response = await self._acompletion(messages, **kwargs)
            except Exception as exc:
                if (
                    _is_ollama_tool_call_json_escape_error(exc)
                    and heartbeats_left > 0
                ):
                    self.internal_history.append({
                        "role": "system",
                        "content": _ollama_tool_call_json_escape_alert(),
                    })
                    heartbeats_used += 1
                    continue
                raise

            await self._emit_token_usage(response, step=heartbeats_used)
            message = response.choices[0].message

            content = message.content or ""
            reasoning = getattr(message, "reasoning_content", None)
            if not reasoning and content:
                # Only replace the visible text when reasoning was actually found:
                # the splitter strips what it returns, so reassigning content
                # unconditionally reshaped every answer that had no <think> tag at
                # all -- a Markdown indented code block arrived as ordinary prose.
                # `LLMAgentBlock` has always guarded this the same way.
                inline_reasoning, visible_content = split_inline_reasoning(content)
                if inline_reasoning:
                    reasoning = inline_reasoning
                    content = visible_content

            if not kwargs.get("stream", False):
                await self._invoke_on_thinking(reasoning or "")

            assistant_msg_raw = {"role": "assistant", "content": content}
            if message.tool_calls:
                assistant_msg_raw["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ]
            if reasoning:
                assistant_msg_raw["reasoning_content"] = reasoning
            
            self.internal_history.append(assistant_msg_raw)
            messages.append(assistant_msg_raw)

            # One text channel: text the model wrote beside a tool call is the
            # same user-facing text as any other, and dropping it here is how a
            # whole answer used to disappear while the run reported success.
            if message.tool_calls and content.strip() and not is_empty_response_placeholder(content):
                visible_texts.append(content)
                # Spoken while still working: progress, not the answer.
                narration_texts.append(content)

            if not message.tool_calls:
                visible_text = content.strip()
                if is_empty_response_placeholder(visible_text):
                    # The model echoed this runtime's own marker back at it, which it
                    # can only have read from the history. Publishing framework
                    # scaffolding as the final answer is worse than having no answer,
                    # so this counts as the empty response it describes.
                    visible_text = ""
                if visible_text:
                    # Text with no tool call is the finished answer: the model was
                    # told exactly that, and it is the convention every provider
                    # already follows. Wanting to speak and keep working is a
                    # different intention, and it has its own tool to express it.
                    visible_texts.append(content)
                    final_text_only = content
                    termination_reason = (
                        f"max_heartbeats ({self.max_heartbeats}) reached; "
                        "final answer given on the last request"
                        if heartbeats_left <= 0
                        else "model returned a final text response (no tool calls)"
                    )
                    break

                # Nothing visible was said: drop the turn instead of describing it in
                # the assistant's own voice. Any stand-in text here is read back by
                # the model as its last utterance and imitated on the next call.
                self.internal_history.pop()
                messages.pop()

                fallback_text = (reasoning or "").strip()
                if is_empty_response_placeholder(fallback_text):
                    fallback_text = ""
                if self.empty_response_reasoning_fallback and fallback_text:
                    visible_texts.append(fallback_text)
                    final_text_only = fallback_text
                    self.internal_history.append(
                        {"role": "assistant", "content": fallback_text}
                    )
                    termination_reason = (
                        "model wrote its answer in the reasoning channel only; "
                        "promoted by empty_response_reasoning_fallback"
                    )
                    break

                if heartbeats_left <= 0 or self.max_heartbeats <= 0:
                    termination_reason = "model returned an empty response with no heartbeats remaining"
                    break

                # A response that is all reasoning and no action spends the same
                # allowance as an idle heartbeat: it is the same model-did-not-act
                # step, and it was once bounded only by the whole guardrail. The
                # branch above drops the message, so the model never reads its own
                # attempt back -- it writes the identical thought again instead.
                if force_final_answer:
                    termination_reason = (
                        "model produced no answer: it stopped acting and returned only reasoning"
                        if acted_in_run
                        else "model produced no answer: only reasoning, and no tool call was ever issued"
                    )
                    break
                idle_heartbeats += 1
                if idle_heartbeats >= self.max_idle_heartbeats:
                    force_final_answer = True
                    heartbeats_used += 1
                    continue

                self.internal_history.append({
                    "role": "system",
                    "content": (
                        "SYSTEM ALERT: You returned no text and no native tool call. "
                        "Return a non-empty final response in normal text, or issue a native "
                        "tool call if an action is still required."
                    ),
                })
                # A correction that spends the last heartbeat still ends in the
                # request for the answer at the top of the loop, never in a break.
                heartbeats_used += 1
                continue

            if heartbeats_left <= 0:
                # The answer was requested with tool_choice="none" and tool calls
                # came back anyway: a provider is free to ignore the parameter.
                # Running them would overrun the guardrail the request exists to
                # close, so they are refused, each with a result -- a call left
                # without one breaks the next request built from this history --
                # and the run ends. Any text beside them was already kept above.
                for tool_call in message.tool_calls:
                    refused = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": json.dumps({
                            "error": (
                                "SYSTEM ALERT: Not executed. No action heartbeats "
                                "remained in this turn."
                            )
                        }),
                    }
                    self.internal_history.append(refused)
                    messages.append(refused)
                termination_reason = (
                    f"max_heartbeats ({self.max_heartbeats}) reached; the final request "
                    "returned tool calls instead of an answer, and they were not executed"
                )
                break

            heartbeats_used += 1
            force_final_answer = False
            # Whether this batch carried real work rather than nothing but a
            # request to keep going. Only the second kind can spin, so only the
            # second kind spends the idle allowance.
            acted_this_round = False

            # Two passes. The first decides, call by call and in order, what
            # each call gets -- a refusal, the heartbeat grant, or execution --
            # exactly as the loop always did. The second runs the executions:
            # consecutive calls to tools that declare `concurrency_safe` run
            # together, every other call runs alone, in order. Results enter the
            # history in the order the model issued the calls, so a batch with
            # no concurrency-safe tool behaves exactly as before.
            planned: list[tuple] = []
            for tool_call in message.tool_calls:
                tool_call_count += 1
                function_name = tool_call.function.name
                tool_usage[function_name] += 1

                if function_name in self.tool_call_limits and tool_usage[function_name] > self.tool_call_limits[function_name]:
                    planned.append(("result", {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": json.dumps({"error": f"SYSTEM ALERT: Execution Blocked. You exceeded the maximum limit of {self.tool_call_limits[function_name]} calls for '{function_name}'."})
                    }))
                    continue

                # Break verbatim retry loops before the call is executed.
                # new_heartbeat is exempt: it is a continuation request handled
                # below, not an action, and its own allowance already bounds it.
                if self.loop_detection and function_name != HEARTBEAT_TOOL_NAME:
                    signature = _tool_call_signature(function_name, tool_call.function.arguments)
                    tool_call_signatures[signature] += 1
                    if tool_call_signatures[signature] > self.loop_detection_limit:
                        planned.append(("result", {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": _loop_block_message(function_name, self.loop_detection_limit),
                        }))
                        continue

                if function_name == HEARTBEAT_TOOL_NAME:
                    # Granted, always: refusing it would strand a model that has
                    # something to say before it acts, which is the case the tool
                    # exists for. It is bounded after the batch instead, by the
                    # idle allowance, because only a run of these with no work in
                    # between means the model is not going to act.
                    hb_left = self.max_heartbeats - heartbeats_used
                    planned.append(("result", {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": (
                            "Heartbeat granted. Your turn stays open: take the next step. "
                            f"[System: You have {hb_left} heartbeats remaining.]"
                        ),
                    }))
                    continue

                acted_this_round = True
                matched_block = next((b for b in agent_tools if b.name == function_name), None)
                if not matched_block:
                    planned.append(("unknown", tool_call, function_name))
                    continue

                # Taken now, at the call's position, so the count a result
                # reports is the one the sequential loop reported.
                hb_left = self.max_heartbeats - heartbeats_used
                sys_msg = f"\n[System: You have {hb_left} heartbeats remaining."
                if function_name in self.tool_call_limits:
                    calls_left = max(0, self.tool_call_limits[function_name] - tool_usage[function_name])
                    sys_msg += f" You have {calls_left} calls remaining for '{function_name}'."
                sys_msg += "]"
                planned.append(("run", tool_call, function_name, matched_block, sys_msg))

            def _record(tool_message: dict) -> None:
                self.internal_history.append(tool_message)
                messages.append(tool_message)

            index = 0
            while index < len(planned):
                entry = planned[index]
                if entry[0] == "result":
                    _record(entry[1])
                    index += 1
                    continue
                if entry[0] == "unknown":
                    _, tool_call, function_name = entry
                    _record({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": json.dumps({"error": unknown_tool_message(
                            function_name, tool_call.function.arguments, litellm_tools
                        )}),
                    })
                    await self._invoke_on_unknown_tool(function_name, tool_call.function.arguments)
                    index += 1
                    continue
                group = [entry]
                if _is_concurrency_safe(entry[3]):
                    while (
                        index + len(group) < len(planned)
                        and planned[index + len(group)][0] == "run"
                        and _is_concurrency_safe(planned[index + len(group)][3])
                    ):
                        group.append(planned[index + len(group)])
                if len(group) == 1:
                    results = [await _execute_tool_call(*group[0][1:])]
                else:
                    results = await asyncio.gather(*(_execute_tool_call(*item[1:]) for item in group))
                for tool_message in results:
                    _record(tool_message)
                index += len(group)

            # A batch that did real work clears the idle count: narrate-act-
            # narrate-act is ordinary work, not a model going in circles. A batch
            # that only asked to keep going spends the allowance, and spending it
            # ends in a request for the answer rather than one more intention.
            if acted_this_round:
                idle_heartbeats = 0
                acted_in_run = True
            else:
                idle_heartbeats += 1
                if idle_heartbeats >= self.max_idle_heartbeats:
                    force_final_answer = True

            # No break when this batch spent the last heartbeat: the model has
            # not read these results yet, and the next request -- tool calls off,
            # the exhausted-budget alert on -- is where it reports on them. That
            # request ends the run whatever comes back.

        # Everything the model said this run, in the order it said it, through the
        # rule both blocks share. Joining is not a formatting choice: any other
        # rule here silently discards or reshapes text addressed to the user.
        final_text = compose_visible_response(visible_texts)
        structured_obj = None

        if self.response_schema and final_text:
            try:
                clean_content = final_text.strip()
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
                
                format_msg = {"role": "system", "content": "Format all the accumulated context into the required structured JSON output schema."}
                format_messages = messages + [format_msg]

                try:
                    final_resp = await self._acompletion(format_messages, **final_kwargs)
                    
                    await self._emit_token_usage(final_resp, step=heartbeats_used)
                    content = final_resp.choices[0].message.content or ""
                    
                    clean_content = content.strip()
                    if clean_content.startswith("```json"):
                        clean_content = clean_content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
                    elif clean_content.startswith("```"):
                        clean_content = clean_content.split("```", 1)[1].rsplit("```", 1)[0].strip()
                    structured_obj = self.response_schema.model_validate_json(clean_content)
                    final_text = content
                except Exception as e:
                    if self.debug:
                        print(f"[DEBUG] Schema validation failed on fallback: {e}")

        output = AgentOutput(
            response=final_text,
            tool_calls_made=tool_call_count,
            termination_reason=termination_reason,
            structured_output=structured_obj,
            narration=list(narration_texts),
            final_text=final_text_only,
            budget_exhausted=heartbeats_used >= self.max_heartbeats,
        )

        if self.debug:
            _print_debug_report(
                agent_name=self.name,
                model=self.model,
                iteration_count=heartbeats_used,
                tool_call_count=tool_call_count,
                tool_usage=dict(tool_usage),
                termination_reason=termination_reason,
                elapsed_seconds=time.monotonic() - start_time,
            )
        await self._invoke_on_iteration(heartbeats_used, messages)
        return output
