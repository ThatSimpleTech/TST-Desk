"""The text of a compaction summary (TD-405, TD-5109, TD-5122, TD-5128).

A summary is one system message in three parts:

1. The core: ``_SUMMARY_HEADER`` with the user turns it stands for, the
   guidance it was given, then either the worker's structured summary
   (TD-5128) or the deterministic extractive one.
2. What was still current when it was written (TD-5122), under
   ``_CARRY_HEADER``.
3. The files read again after the compaction (TD-5128), under
   ``_FILES_HEADER``.

A later compaction that folds this summary in keeps only the core: the
carried text and the files are fresh copies each time, so an old copy
would only repeat them, or be stale.
"""

from __future__ import annotations

import re

from .provider import ChatMessage, content_as_text

# Summary shape: each dropped message contributes at most this many
# chars; the whole summary is capped at _SUMMARY_MAX_CHARS.
_SUMMARY_CHARS_PER_MESSAGE = 200
_SUMMARY_MAX_CHARS = 2_000
# A dropped message that mentions the guidance keeps this much (TD-5109).
_SUMMARY_CHARS_ON_TOPIC = 600
_SUMMARY_HEADER = "[Earlier conversation compacted]"
# The header line with the user turns the summary stands for (TD-5109).
_SUMMARY_TURNS = re.compile(re.escape(_SUMMARY_HEADER) + r" \((\d+) user turns?\)")
# What a summary carries as still current goes under this line (TD-5122).
_CARRY_HEADER = "[Kept across compaction]"
_CARRY_MARK = f"\n\n{_CARRY_HEADER}\n"
# The files read again after compaction go under this line (TD-5128).
_FILES_HEADER = "[Recent files, read again after compaction]"
_FILES_MARK = f"\n\n{_FILES_HEADER}\n"


def guidance_terms(guidance: str) -> frozenset[str]:
    """Words of four or more letters in *guidance*, lowercased."""
    return frozenset(w.lower() for w in re.findall(r"[A-Za-z0-9_][\w.-]{3,}", guidance))


def summary_header(turns: int, guidance: str = "") -> list[str]:
    """The core's first lines: the turns it stands for, and its guidance."""
    header = [f"{_SUMMARY_HEADER} ({turns} user turn{'' if turns == 1 else 's'})"]
    if guidance:
        header.append(f"Kept in view, as asked: {guidance}")
    return header


def is_summary(message: ChatMessage) -> bool:
    """True when *message* is a compaction summary."""
    return message.role == "system" and content_as_text(message.content).startswith(_SUMMARY_HEADER)


def compose(core: str, carry: str = "", files: str = "") -> str:
    """The summary message: *core*, then *carry* and *files* if any."""
    text = core
    carry = carry.strip()
    if carry:
        text += f"{_CARRY_MARK}{carry}"
    files = files.strip()
    if files:
        text += f"{_FILES_MARK}{files}"
    return text


def summary_core(text: str) -> str:
    """A summary without what it carried or the files it read again."""
    cuts = [at for at in (text.find(_CARRY_MARK), text.find(_FILES_MARK)) if at >= 0]
    return text[: min(cuts)] if cuts else text


def summary_turn_count(text: str) -> int | None:
    """The user turns a summary's header says it stands for, if it says."""
    counted = _SUMMARY_TURNS.match(text)
    return int(counted.group(1)) if counted is not None else None


def _truncate(text: str, limit: int = _SUMMARY_CHARS_PER_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def render_extractive(dropped: list[ChatMessage], guidance: str = "", turns: int = 0) -> str:
    """Deterministic extractive summary of the dropped span.

    A *prior* summary (from an earlier compaction) can fall inside the
    span — its core folds into the new one so the oldest context degrades
    gradually instead of vanishing on the second compaction.

    With *guidance* (TD-5109) the summary opens with it, messages that
    mention its words keep up to ``_SUMMARY_CHARS_ON_TOPIC`` characters,
    and when the summary is over its cap the oldest off-topic lines go
    first.

    The header says how many user *turns* the summary stands for, so the
    window's user-turn numbers can be mapped back (TD-5109).
    """
    terms = guidance_terms(guidance)

    def on_topic(text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in terms)

    header = summary_header(turns, guidance)
    lines: list[tuple[str, bool]] = []
    for message in dropped:
        text = content_as_text(message.content)
        hit = bool(terms) and on_topic(text)
        limit = _SUMMARY_CHARS_ON_TOPIC if hit else _SUMMARY_CHARS_PER_MESSAGE
        if message.role == "user":
            who = "Done-check" if message.check else "User"
            lines.append((f"{who}: {_truncate(text, limit)}", hit))
        elif message.role == "assistant":
            if message.tool_calls:
                names = ", ".join(
                    tc.function.name for tc in message.tool_calls if tc.function is not None
                )
                lines.append((f"Assistant called tools: {names}", hit))
            else:
                lines.append((f"Assistant: {_truncate(text, limit)}", hit))
        elif message.role == "tool":
            lines.append((f"Tool result: {_truncate(text, limit)}", hit))
        else:  # system: only a prior summary can land here — fold it in
            lines.append((summary_core(text), True))

    def render(kept: list[tuple[str, bool]]) -> str:
        return "\n".join([*header, *(line for line, _ in kept)])

    if terms:
        # Over the cap: drop the oldest off-topic lines before cutting.
        while len(render(lines)) > _SUMMARY_MAX_CHARS:
            idx = next((i for i, (_, hit) in enumerate(lines) if not hit), None)
            if idx is None:
                break
            del lines[idx]
    body = render(lines)
    if len(body) > _SUMMARY_MAX_CHARS:
        body = body[:_SUMMARY_MAX_CHARS] + "…"
    return body


def render_model_core(body: str, guidance: str = "", turns: int = 0) -> str:
    """The core around the worker's summary *body* (TD-5128).

    Same header as the extractive core, so the window's turn numbers and
    a later fold read it the same way.
    """
    return "\n".join([*summary_header(turns, guidance), body.strip()])
