"""Streaming splitter for reasoning a model writes into the content channel.

Providers that expose a dedicated reasoning channel (Ollama's native `/api/chat`
with `think` enabled, DeepSeek, Claude) deliver reasoning as
`delta.reasoning_content`, which reaches `on_thinking` and never touches the
visible stream. When that channel is *not* in use — a thinking-capable model
whose project has thinking turned off, for instance — the same model still
reasons, and its reasoning arrives inline in `delta.content` wrapped in
`<think>` … `</think>`.

Two inline shapes exist, and only the first used to be handled:

- **Balanced**: the model emits both tags.
- **Orphan close**: the chat template already seeded `<think>` at the end of the
  prompt, so the model only ever generates the closing tag. The reasoning then
  arrives as ordinary content terminated by a bare `</think>`.

In the orphan case the reasoning has already been streamed as visible text by
the time the closing tag arrives, so recognising it means *retracting* what was
published. `on_retract` receives the exact text to remove from the tail of the
visible stream; the splitter then republishes it through `on_thinking`.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"


def pending_tag_suffix(text: str, tags: Iterable[str]) -> int:
    """Return the length of the trailing run of `text` that may still grow into a tag.

    A chunk boundary can fall inside a tag (`"…<thi"` + `"nk>…"`), so that tail
    must be held back instead of published as visible text.
    """
    tags = tuple(tags)
    if not text or not tags:
        return 0
    longest = max(len(tag) for tag in tags)
    for size in range(min(len(text), longest - 1), 0, -1):
        tail = text[-size:]
        if any(tag.startswith(tail) for tag in tags):
            return size
    return 0


class InlineReasoningStreamSplitter:
    """Route a streamed content channel into visible text and reasoning.

    `on_visible` and `on_thinking` receive text as it is classified. `on_retract`,
    when supplied, is called with visible text that a later orphan `</think>`
    proved to be reasoning; the consumer is expected to drop that exact text from
    the end of what it published, since `on_thinking` is called with it right
    after.

    `begin_response()` marks the start of a new LLM response and bounds the
    retraction window to it, so an orphan close in one response can never retract
    an answer streamed by an earlier one.
    """

    def __init__(
        self,
        on_visible: Callable[[str], None],
        on_thinking: Callable[[str], None],
        on_retract: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_visible = on_visible
        self._on_thinking = on_thinking
        self._on_retract = on_retract
        self._buffer = ""
        self._in_think = False
        self._saw_open_tag = False
        self._retractable = ""

    def begin_response(self) -> None:
        """Start a new retraction window (call before each LLM request).

        Whatever the previous response left buffered — a dangling `<`, or a
        `<think>` it never closed — is published first: a tag cannot span two
        responses, so holding it back any longer would just drop it.
        """
        self.flush()
        self._retractable = ""

    def feed(self, chunk: str) -> None:
        self._buffer += str(chunk or "")
        while True:
            if self._in_think:
                if self._consume_inside_think():
                    continue
                return
            if self._consume_outside_think():
                continue
            return

    def flush(self) -> None:
        """Publish whatever is still buffered at the end of a stream."""
        remainder, self._buffer = self._buffer, ""
        in_think, self._in_think = self._in_think, False
        if not remainder:
            return
        if in_think:
            self._emit_thinking(remainder)
        else:
            self._emit_visible(remainder)

    # -- internals ---------------------------------------------------------

    def _emit_visible(self, text: str) -> None:
        if not text:
            return
        self._retractable += text
        self._on_visible(text)

    def _emit_thinking(self, text: str) -> None:
        if text:
            self._on_thinking(text)

    def _consume_inside_think(self) -> bool:
        """Return True when the buffer should be re-examined."""
        index = self._buffer.find(CLOSE_TAG)
        if index != -1:
            self._emit_thinking(self._buffer[:index])
            self._in_think = False
            self._buffer = self._buffer[index + len(CLOSE_TAG):]
            return True
        held = pending_tag_suffix(self._buffer, (CLOSE_TAG,))
        ready = self._buffer[: len(self._buffer) - held]
        if ready:
            self._emit_thinking(ready)
            self._buffer = self._buffer[len(ready):]
        return False

    def _consume_outside_think(self) -> bool:
        open_index = self._buffer.find(OPEN_TAG)
        close_index = self._buffer.find(CLOSE_TAG)

        orphan_close = (
            close_index != -1
            and not self._saw_open_tag
            and (open_index == -1 or close_index < open_index)
        )
        if orphan_close:
            # Everything published since this response began was reasoning, not
            # an answer. Take it back and re-publish it as thinking.
            reclaimed = self._retractable + self._buffer[:close_index]
            self._retract()
            self._emit_thinking(reclaimed)
            self._buffer = self._buffer[close_index + len(CLOSE_TAG):]
            return True

        if open_index != -1:
            self._emit_visible(self._buffer[:open_index])
            self._saw_open_tag = True
            self._in_think = True
            self._buffer = self._buffer[open_index + len(OPEN_TAG):]
            return True

        # A bare `</think>` is only reasoning when the model never opened one; once
        # it has written a well-formed pair, a later closing tag is ordinary text
        # and must not be held back as a partial tag.
        candidates = (OPEN_TAG,) if self._saw_open_tag else (OPEN_TAG, CLOSE_TAG)
        held = pending_tag_suffix(self._buffer, candidates)
        ready = self._buffer[: len(self._buffer) - held]
        if ready:
            self._emit_visible(ready)
            self._buffer = self._buffer[len(ready):]
        return False

    def _retract(self) -> None:
        reclaimed, self._retractable = self._retractable, ""
        if reclaimed and self._on_retract:
            self._on_retract(reclaimed)
