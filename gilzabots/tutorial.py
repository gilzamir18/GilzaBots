"""Built-in usage tutorial for GilzaBots.

The tutorial lives in ``gilzabots/guides/tutorial.<lang>.md`` and those files are the
single source of truth: the intro shown as the tutorial chat's first message, the
pre-written answer behind each menu entry, and the guide block injected into the
chat-orchestrator system prompt are all read from them. Nothing here duplicates the
prose into Python constants or into the front-end translation bundles, so the tutorial
cannot drift between the menu and the model's memory.

Guide format: free Markdown up to the first topic heading (the intro), then one section
per topic introduced by::

    ## <topic-id> :: <question shown in the menu>

"""

from __future__ import annotations

import os
import re
from typing import Optional

GUIDES_DIR = os.path.join(os.path.dirname(__file__), "guides")

#: ``## overview :: How does GilzaBots work?``
_TOPIC_HEADING = re.compile(r"^##[ \t]+([a-z0-9][a-z0-9-]*)[ \t]*::[ \t]*(.+?)[ \t]*$")

SUPPORTED_LANGS = ("en", "pt")


def normalize_lang(lang: Optional[str]) -> str:
    """Map a UI language tag onto one of the guide files.

    The front-end uses ``en``/``pt-BR`` and the Python side uses ``en``/``pt``; both
    reach the same two files. Anything unrecognised falls back to ``pt``, matching the
    default already used by :func:`gilzabots.ui_settings.load_ui_settings`.
    """
    tag = str(lang or "").strip().lower()
    return "en" if tag.startswith("en") else "pt"


def guide_path(lang: Optional[str] = None) -> str:
    """Absolute path of the guide file for ``lang``."""
    return os.path.join(GUIDES_DIR, f"tutorial.{normalize_lang(lang)}.md")


def load_guide(lang: Optional[str] = None) -> str:
    """Return the raw Markdown of the guide.

    Raises ``FileNotFoundError`` if the guide is missing from the installation rather
    than returning an empty tutorial that would look like a working one.
    """
    with open(guide_path(lang), "r", encoding="utf-8") as handle:
        return handle.read()


def _parse(text: str) -> tuple[str, list[dict]]:
    """Split raw guide Markdown into ``(intro, topics)``."""
    intro_lines: list[str] = []
    topics: list[dict] = []
    current: Optional[dict] = None
    body: list[str] = []

    for line in text.splitlines():
        match = _TOPIC_HEADING.match(line)
        if match:
            if current is not None:
                current["answer"] = "\n".join(body).strip()
                topics.append(current)
            current = {"id": match.group(1), "question": match.group(2).strip()}
            body = []
            continue
        if current is None:
            intro_lines.append(line)
        else:
            body.append(line)

    if current is not None:
        current["answer"] = "\n".join(body).strip()
        topics.append(current)

    intro = "\n".join(intro_lines).strip()
    # The guide opens with a `# Title` heading that is useful in the file but redundant
    # as the first line of a chat message, where the chat header already names the chat.
    if intro.startswith("# "):
        intro = intro.split("\n", 1)[1].strip() if "\n" in intro else ""

    return intro, topics


def load_intro(lang: Optional[str] = None) -> str:
    """The welcome text shown as the tutorial chat's first assistant message."""
    return _parse(load_guide(lang))[0]


def load_topics(lang: Optional[str] = None) -> list[dict]:
    """Every topic as ``{"id", "question", "answer"}``, in guide order."""
    return _parse(load_guide(lang))[1]


def topic_menu(lang: Optional[str] = None) -> list[dict]:
    """The menu payload sent to the front-end: ids and questions, without answers."""
    return [{"id": t["id"], "question": t["question"]} for t in load_topics(lang)]


def get_topic(topic_id: str, lang: Optional[str] = None) -> Optional[dict]:
    """Return one topic, or ``None`` when ``topic_id`` names no topic.

    Callers must surface the ``None`` as an error. Substituting the closest match would
    answer a question the user did not ask, which is the kind of silent semantic
    fallback the project forbids.
    """
    wanted = str(topic_id or "").strip()
    for topic in load_topics(lang):
        if topic["id"] == wanted:
            return topic
    return None


def tutorial_system_block(lang: Optional[str] = None) -> str:
    """The guide, framed for injection into the chat-orchestrator system prompt.

    Only the tutorial chat pays for this block, so no other conversation loses context
    window to it.
    """
    guide = load_guide(lang).strip()
    return (
        "## GilzaBots Tutorial (this chat only)\n"
        "**IMPORTANT**: This conversation is the built-in GilzaBots tutorial. The user "
        "opened it to learn how to use the application. The complete usage guide is "
        "reproduced below and is the authoritative source for anything about GilzaBots "
        "itself — provider and model registration, recommended settings, projects, "
        "local models, skills, and when to prefer cloud models. Answer from this guide "
        "instead of guessing, and say so plainly when a question is not covered here. "
        "Keep answers short and point the user at the relevant menu topic when one "
        "applies.\n\n"
        f"{guide}\n"
    )
