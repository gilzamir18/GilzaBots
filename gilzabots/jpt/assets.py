"""Legacy data-URI helpers for presentation assets.

Canonical `.jpt` files now use `package.py`: local pictures and films live as
content-addressed ZIP members rather than base64 in `deck.json`. These helpers
remain for export/staging compatibility and for callers that explicitly need a
data URI; they do not define the current on-disk package contract.

Two limits, both deliberate and both reported rather than silent:

  * **A picture larger than `MAX_EMBED_BYTES` keeps its path.** Base64 costs a
    third more than the bytes it carries, and a `.jpt` is a document a human
    diffs, a checkpoint stores and the cloud mirror uploads — a 40 MB JSON is a
    worse outcome than a reference. The caller is told which files stayed out.
  * **A picture that cannot be read keeps its path.** A missing figure is
    already an error the linter raises by name; failing the embed step on it
    would report the same problem twice and in a less useful place.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from .model import background_of

# Roughly 5.5 MB once base64-encoded. Above this the reference is kept.
MAX_EMBED_BYTES = 4 * 1024 * 1024

MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
}

# Sources that already travel with the file, or that resolve anywhere.
PORTABLE_PREFIXES = ("data:", "jpt:", "http:", "https:", "blob:")


def is_portable(src: str | None) -> bool:
    return not src or src.startswith(PORTABLE_PREFIXES)


def to_data_uri(path: str) -> str:
    """A file as a data URI. Raises OSError if it cannot be read."""
    with open(path, "rb") as handle:
        payload = base64.b64encode(handle.read()).decode("ascii")
    mime = MIME_BY_EXTENSION.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    return f"data:{mime};base64,{payload}"


def embed_images(deck: dict[str, Any], project_root: str, *,
                 max_bytes: int = MAX_EMBED_BYTES) -> dict[str, Any]:
    """Replace every project-relative picture in `deck` with a data URI.

    Mutates the deck it is given — unlike the editor's export-time inlining,
    which copies, because here the embedded deck *is* what gets written. Returns
    a report: `{"embedded": n, "bytes": n, "skipped": [(src, reason), ...]}`.
    """
    cache: dict[str, str] = {}
    report: dict[str, Any] = {"embedded": 0, "bytes": 0, "skipped": []}

    def resolve(src: str | None) -> str | None:
        if is_portable(src):
            return src
        if src in cache:
            return cache[src]

        path = src if os.path.isabs(src) else os.path.join(project_root, src)
        try:
            size = os.path.getsize(path)
        except OSError:
            # Missing: left alone, and the linter reports it by name.
            report["skipped"].append((src, "not found"))
            cache[src] = src
            return src
        if size > max_bytes:
            report["skipped"].append((src, f"{size // 1024 // 1024} MB, over the embed limit"))
            cache[src] = src
            return src
        try:
            uri = to_data_uri(path)
        except OSError as error:
            report["skipped"].append((src, str(error)))
            cache[src] = src
            return src

        cache[src] = uri
        report["embedded"] += 1
        report["bytes"] += size
        return uri

    theme = deck.get("theme") or {}
    if theme.get("backgroundImage"):
        theme["backgroundImage"] = resolve(theme["backgroundImage"])

    for slide in deck.get("slides") or []:
        if slide.get("backgroundImage"):
            slide["backgroundImage"] = resolve(slide["backgroundImage"])
        for element in slide.get("elements") or []:
            if element.get("type") == "image" and element.get("src"):
                element["src"] = resolve(element["src"])
    return report


def describe(report: dict[str, Any]) -> str:
    """The one line a tool adds to its result, or nothing when there is nothing
    to say."""
    parts = []
    if report["embedded"]:
        size = report["bytes"]
        human = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size // 1024} kB"
        parts.append(
            f"Embedded {report['embedded']} picture"
            f"{'s' if report['embedded'] != 1 else ''} ({human}) so the deck is one "
            "self-contained file."
        )
    for src, reason in report["skipped"]:
        parts.append(f"{src} was left as a reference ({reason}).")
    return " ".join(parts)


def used_sources(deck: dict[str, Any]) -> list[str]:
    """Every picture a deck refers to, portable or not. Useful to a caller that
    wants to know what a deck depends on before moving it."""
    sources: list[str] = []
    for slide in deck.get("slides") or []:
        background = background_of(deck, slide)
        if background["image"]:
            sources.append(background["image"])
        for element in slide.get("elements") or []:
            if element.get("type") == "image" and element.get("src"):
                sources.append(element["src"])
            elif element.get("type") == "video":
                if element.get("src"):
                    sources.append(element["src"])
                if element.get("poster"):
                    sources.append(element["poster"])
    return sources
