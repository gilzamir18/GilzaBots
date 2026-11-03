"""Agent reasoning: measured in tokens, stored whole, summarized for resumption.

Reasoning is never discarded. Every chunk is streamed to the chat and persisted
as activity. The size configured in the editor settings
(``thought_context_tokens``) governs only two views of it:

- the chat panel shows the most recent reasoning up to that size;
- resuming an interrupted turn replays the turn's reasoning in full when it fits
  that size, and a summary written by the worker model when it does not.

Tokens are counted with the model's own tokenizer through agenticblocks.
"""

from __future__ import annotations

import math
from typing import Awaitable, Callable, Optional

from agenticblocks.blocks.llm.tokens import count_text_tokens

# Reasoning events of a turn, in the order they were streamed.
REASONING_EVENTS = ("thought",)
VISIBLE_EVENTS = ("stream_chunk",)

# Exact counts are taken on batches of this many characters, so the tokenizer
# runs a handful of times per turn instead of once per streamed token. Between
# batches the running total carries an estimate for the uncounted tail.
_COUNT_BATCH_CHARS = 2000

# A summary pass that still does not fit after this many rounds is reported as a
# failure instead of being cut to size: truncating it would hand the model a
# partial account presented as a complete one.
MAX_SUMMARY_ROUNDS = 4


class ThoughtSummaryError(RuntimeError):
    """The reasoning could not be summarized within the configured size."""


def thought_context_tokens() -> int:
    from gilzabots.ui_settings import clamp_thought_context_tokens, load_ui_settings

    return clamp_thought_context_tokens(load_ui_settings().get("thought_context_tokens"))


class ThoughtTokenMeter:
    """Running token count of one turn's reasoning."""

    def __init__(self, model: str):
        self.model = model or ""
        self._counted = 0
        self._pending = ""

    def add(self, text: str) -> int:
        self._pending += text or ""
        if len(self._pending) >= _COUNT_BATCH_CHARS:
            self._counted += count_text_tokens(self.model, self._pending)
            self._pending = ""
        return self.total

    @property
    def total(self) -> int:
        return self._counted + math.ceil(len(self._pending) / 4)


def collect_interrupted_turn(store, project_name: str, chat_id: str, history: list[dict]) -> tuple[str, str]:
    """Return the reasoning and the visible text of the most recent turn.

    The turn is everything recorded after the last user message: the message
    that opened the turn being resumed.
    """
    started_at = ""
    for message in reversed(history or []):
        if message.get("role") == "user":
            started_at = str(message.get("timestamp") or "")
            break
    if not started_at:
        return "", ""

    activity = store.list_activity(project_name, chat_id, limit=None, since=started_at)
    reasoning: list[str] = []
    visible: list[str] = []
    for item in activity:
        if str(item.get("timestamp") or "") < started_at:
            continue
        content = str(item.get("content") or "")
        if item.get("event") in REASONING_EVENTS:
            reasoning.append(content)
        elif item.get("event") in VISIBLE_EVENTS:
            visible.append(content)
    return "".join(reasoning), "".join(visible)


def _split_by_tokens(text: str, piece_tokens: int, total_tokens: int) -> list[str]:
    """Split *text* into pieces of about *piece_tokens*, preferring paragraph breaks."""
    if total_tokens <= piece_tokens:
        return [text]
    chars_per_token = max(1.0, len(text) / max(1, total_tokens))
    piece_chars = max(1, int(piece_tokens * chars_per_token))
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + piece_chars)
        if end < len(text):
            boundary = text.rfind("\n\n", start + piece_chars // 2, end)
            if boundary != -1:
                end = boundary + 2
        pieces.append(text[start:end])
        start = end
    return pieces


async def summarize_reasoning(
    text: str,
    *,
    model: str,
    limit_tokens: int,
    piece_tokens: int,
    summarize_piece: Callable[[str], Awaitable[str]],
) -> str:
    """Summarize *text* until it fits *limit_tokens*.

    Each round splits the current text into pieces the summarizer can read
    (*piece_tokens*), summarizes every piece, and joins the summaries.
    """
    current = text
    for _ in range(MAX_SUMMARY_ROUNDS):
        tokens = count_text_tokens(model, current)
        if tokens <= limit_tokens:
            return current
        pieces = _split_by_tokens(current, piece_tokens, tokens)
        summaries = [await summarize_piece(piece) for piece in pieces]
        current = "\n\n".join(s.strip() for s in summaries if s and s.strip())
        if not current:
            raise ThoughtSummaryError("The summarizer returned no text for the agent's reasoning.")
    tokens = count_text_tokens(model, current)
    if tokens > limit_tokens:
        raise ThoughtSummaryError(
            f"The agent's reasoning still has {tokens} tokens after {MAX_SUMMARY_ROUNDS} "
            f"summary rounds, above the configured {limit_tokens}. Increase the reasoning "
            "size in the editor settings."
        )
    return current


_SUMMARIZER_SYSTEM_PROMPT = (
    "You summarize an AI agent's own reasoning so the agent can resume an "
    "interrupted task. Keep what the continuation needs: the decisions already "
    "taken and why, facts established, calculations and their results, open "
    "questions, and the next steps the agent intended. Drop repetition and "
    "abandoned lines of thought, but say that they were abandoned when it matters. "
    "Write in the same language as the reasoning. Output only the summary."
)


async def summarize_with_worker(piece: str) -> str:
    """Summarize one piece of reasoning with the project's worker model."""
    import agenticblocks.blocks.llm.agent as agent_mod

    from gilzabots.config import get_agent_llm_kwargs, resolve_agent_model
    from gilzabots.litellm_compat import wrap_agent_litellm_compat

    model = resolve_agent_model("worker")
    kwargs = dict(get_agent_llm_kwargs("worker"))
    kwargs["stream"] = False
    agent = agent_mod.LLMAgentBlock(
        name="thought_summarizer",
        system_prompt=_SUMMARIZER_SYSTEM_PROMPT,
        model=model,
        model_kwargs=kwargs,
    )
    wrap_agent_litellm_compat(agent)
    result = await agent.run(agent_mod.AgentInput(prompt=piece))
    return str(getattr(result, "response", "") or "")


def summary_piece_tokens(limit_tokens: int) -> int:
    """Largest piece the worker model can summarize in one request."""
    from gilzabots.config import resolve_agent_model, resolve_effective_num_ctx

    window = resolve_effective_num_ctx("worker", model=resolve_agent_model("worker"))
    if not window:
        # Unknown window: no GilzaBots-side cap, the provider decides.
        return max(1000, limit_tokens)
    # Half the window for the piece, leaving room for the instructions and the
    # summary itself.
    return max(1000, min(limit_tokens, int(window) // 2))


def build_resume_prompt(reasoning_section: str, visible_text: str) -> str:
    return "\n".join([
        "Continue the task that was interrupted. Do not restart from scratch.",
        "Your working memory from before the interruption is restored: the conversation, "
        "your tool calls and their results. The sections below add what that memory does "
        "not hold. If an action already appears completed, do not repeat it unless "
        "verification is needed.",
        "",
        "## Your reasoning before the interruption",
        reasoning_section or "(no reasoning was recorded)",
        "",
        "## Text you had already shown to the user",
        visible_text.strip() or "(none)",
    ])


async def resume_prompt_for_chat(
    store,
    project,
    *,
    model: str,
    limit_tokens: Optional[int] = None,
    summarize_piece: Callable[[str], Awaitable[str]] = summarize_with_worker,
    piece_tokens: Optional[int] = None,
    on_summarizing: Optional[Callable[[int], None]] = None,
) -> str:
    """Build the prompt that resumes the chat's interrupted turn from stored activity."""
    limit = limit_tokens if limit_tokens is not None else thought_context_tokens()
    reasoning, visible = collect_interrupted_turn(
        store, project.name, project.current_chat_id, getattr(project, "history", []) or []
    )
    tokens = count_text_tokens(model, reasoning)
    if tokens <= limit:
        section = reasoning
    else:
        if on_summarizing is not None:
            on_summarizing(tokens)
        summary = await summarize_reasoning(
            reasoning,
            model=model,
            limit_tokens=limit,
            piece_tokens=piece_tokens if piece_tokens is not None else summary_piece_tokens(limit),
            summarize_piece=summarize_piece,
        )
        section = f"(Summary of {tokens} tokens of reasoning.)\n{summary}"
    return build_resume_prompt(section, visible)
