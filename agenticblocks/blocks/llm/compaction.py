"""Conversation compaction: replace a long working context with a structured summary.

Compaction is a different operation from MemGPT's FIFO eviction. Eviction drops
the *oldest* quarter of the history and folds it into a short rolling summary, a
little at a time, every time the window overflows. Compaction stops the whole
conversation at once, asks the model for a detailed, sectioned account of it --
every request the user made, the files and decisions involved, the errors and how
they were fixed, the work in progress and the next step -- and starts the context
over from that account alone.

The summary prompt and the continuation message follow the shape popularised by
Claude Code's ``/compact``: the model first reasons inside ``<analysis>`` tags,
which are discarded, then writes the summary inside ``<summary>`` tags; the new
context opens with a user message stating that the session continues from a
previous conversation, followed by that summary. When the compaction happens in
the middle of a turn, the message also tells the model to carry on with the task
instead of asking the user what to do.

Everything here is provider-neutral and has no knowledge of any host
application. :class:`agenticblocks.blocks.llm.memgpt_agent.MemGPTAgentBlock`
uses it through ``compact()`` and ``auto_compact_threshold``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


COMPACTION_TRIGGER_MANUAL = "manual"
COMPACTION_TRIGGER_AUTO = "auto"

COMPACTION_SYSTEM_PROMPT = "You are a helpful AI assistant tasked with summarizing conversations."

DEFAULT_COMPACTION_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools. Tool calls will be rejected and you will fail the task.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, file contents, decisions and work in progress that would be essential for continuing the work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and patterns
   - Specific details like:
     - file names
     - full code snippets or document excerpts
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail.
2. Key Technical Concepts: List all important technical concepts, technologies and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code or document sections examined, modified or created. Pay special attention to the most recent messages and include full snippets where applicable, with a note on why each read or edit is important.
4. Errors and Fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All User Messages: List ALL user messages that are not tool results. These are critical for understanding the user's feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the user's request. Do not start on tangential requests or old requests that were already completed without confirming with the user first.
   If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important snippet]

4. Errors and Fixes:
   - [Detailed description of error 1]:
     - [How you fixed the error]
     - [User feedback on the error, if any]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All User Messages:
   - [Detailed non tool use user message]

7. Pending Tasks:
   - [Task 1]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional next step to take]
</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response. Write the summary in the language the user has been writing in."""

CUSTOM_INSTRUCTIONS_HEADER = "Additional Instructions:"

CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out of context. "
    "The summary below covers the earlier portion of the conversation."
)

CONTINUATION_RESUME_INSTRUCTION = (
    "Please continue the conversation from where we left it off without asking the user any "
    "further questions. Continue with the last task that you were asked to work on."
)

_ANALYSIS_RE = re.compile(r"<analysis>.*?</analysis>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_ANALYSIS_RE = re.compile(r"<analysis>.*\Z", re.DOTALL | re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary>(.*?)(?:</summary>|\Z)", re.DOTALL | re.IGNORECASE)


class CompactionError(RuntimeError):
    """Compaction could not produce a summary; the context was left unchanged."""


@dataclass
class CompactionEvent:
    """One step of a compaction, as reported to ``on_compaction``.

    ``phase`` is ``"started"`` when the summary request is about to be sent,
    ``"completed"`` once the context has been replaced, and ``"failed"`` when
    the context was left as it was (``error`` says why). ``trigger`` is
    ``"manual"`` for an explicit ``compact()`` call and ``"auto"`` when the
    window crossed ``auto_compact_threshold`` during a run.
    """

    phase: str
    trigger: str
    pre_tokens: int = 0
    post_tokens: int = 0
    context_window: Optional[int] = None
    messages_compacted: int = 0
    summary: str = ""
    custom_instructions: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "phase": self.phase,
            "trigger": self.trigger,
            "pre_tokens": self.pre_tokens,
            "post_tokens": self.post_tokens,
            "context_window": self.context_window,
            "messages_compacted": self.messages_compacted,
            "summary": self.summary,
            "custom_instructions": self.custom_instructions,
            "error": self.error,
        }
        data.update(self.extra)
        return data


@dataclass
class CompactionRequest:
    """A compaction about to be requested, handed to ``on_compaction_request``.

    The host may edit ``custom_instructions`` (to add its own focus) or set
    ``blocked_reason`` to stop the compaction; a blocked compaction fails with
    that reason and leaves the context untouched. ``history`` is a copy of the
    messages about to be summarized, for reading only. ``metadata`` travels with
    this compaction into the ``completed`` or ``failed`` event (``extra``), so a
    host can report what its callbacks did.
    """

    trigger: str
    custom_instructions: str
    history: List[Dict[str, Any]]
    blocked_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompactionContext:
    """A summary obtained, before it replaces the context: ``on_compacted_context``.

    The host may append text to ``extra_context``; each entry is placed after the
    summary in the message the new context starts from (for example the current
    contents of the files the conversation was working on). ``history`` is the
    conversation that was summarized. ``metadata`` is the same dictionary the
    request carried.
    """

    trigger: str
    summary: str
    custom_instructions: str
    history: List[Dict[str, Any]]
    extra_context: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


def build_compaction_prompt(
    custom_instructions: str = "",
    base_prompt: str = DEFAULT_COMPACTION_PROMPT,
) -> str:
    """Return the summary request, with the caller's focus appended when given."""
    prompt = base_prompt
    extra = (custom_instructions or "").strip()
    if extra:
        prompt = f"{prompt}\n\n{CUSTOM_INSTRUCTIONS_HEADER}\n{extra}"
    return prompt


def _content_as_text(content: Any) -> str:
    """Render message content, including multimodal parts, as plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                kind = part.get("type")
                if kind == "text":
                    parts.append(str(part.get("text") or ""))
                elif kind in ("image_url", "input_image", "image"):
                    parts.append("[image attachment]")
                elif kind in ("file", "input_file", "document"):
                    parts.append("[file attachment]")
                else:
                    parts.append(f"[{kind or 'unknown'} part]")
            else:
                parts.append(str(part))
        return "\n".join(p for p in parts if p)
    return str(content)


def _tool_call_text(call: Any) -> str:
    if isinstance(call, dict):
        fn = call.get("function") or {}
        name = fn.get("name") or "unknown"
        args = fn.get("arguments")
    else:
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None) or "unknown"
        args = getattr(fn, "arguments", None)
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False, default=str)
    return f"{name}({args or ''})"


def format_transcript(
    messages: List[Dict[str, Any]],
    previous_summary: str = "",
) -> str:
    """Render a working history as a plain-text transcript for the summarizer.

    The conversation is sent as text inside one request rather than replayed as
    structured messages: a replayed history carries tool calls without the tool
    schemas that declared them, which several providers reject, and the
    summarizer must not be able to act on them anyway. Nothing is cut here; a
    transcript that cannot fit the window is reported by the caller.
    """
    lines: List[str] = []
    if previous_summary.strip():
        lines.append("[summary of an even earlier part of the conversation]")
        lines.append(previous_summary.strip())
        lines.append("")
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "unknown")
        text = _content_as_text(msg.get("content")).strip()
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            if text:
                lines.append("[assistant]")
                lines.append(text)
            for call in calls:
                lines.append(f"[assistant tool call] {_tool_call_text(call)}")
            if not text and not calls:
                continue
        elif role == "tool":
            name = msg.get("name") or "tool"
            lines.append(f"[tool result: {name}]")
            lines.append(text)
        else:
            if not text:
                continue
            lines.append(f"[{role}]")
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def build_compaction_request(
    messages: List[Dict[str, Any]],
    previous_summary: str = "",
    custom_instructions: str = "",
    base_prompt: str = DEFAULT_COMPACTION_PROMPT,
) -> List[Dict[str, Any]]:
    """Return the self-contained request that asks for the summary."""
    transcript = format_transcript(messages, previous_summary)
    body = (
        "<conversation>\n"
        f"{transcript}\n"
        "</conversation>\n\n"
        f"{build_compaction_prompt(custom_instructions, base_prompt)}"
    )
    return [
        {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]


def extract_summary(raw: str) -> str:
    """Return the summary the model wrote, without its ``<analysis>`` scratchpad.

    The analysis exists to make the summary better and is not part of it. When
    the model honoured the format, the ``<summary>`` block is returned; when it
    wrote no tags at all, its text (minus any analysis) is the summary. An empty
    result means the model produced nothing usable.
    """
    text = str(raw or "")
    text = _ANALYSIS_RE.sub("", text)
    match = _SUMMARY_RE.search(text)
    if match:
        return match.group(1).strip()
    # An analysis that was never closed swallowed the rest of the answer; there
    # is no summary to return from it.
    text = _UNCLOSED_ANALYSIS_RE.sub("", text)
    return text.strip()


def continuation_message(
    summary: str,
    resume: bool = False,
    extra_context: Optional[List[str]] = None,
) -> str:
    """Return the user message the compacted context starts from.

    ``extra_context`` entries follow the summary, in order; the instruction to
    resume the task, when present, always comes last.
    """
    text = f"{CONTINUATION_PREAMBLE}\n\nSummary:\n{summary.strip()}"
    for block in extra_context or []:
        if block and block.strip():
            text = f"{text}\n\n{block.strip()}"
    if resume:
        text = f"{text}\n\n{CONTINUATION_RESUME_INSTRUCTION}"
    return text
