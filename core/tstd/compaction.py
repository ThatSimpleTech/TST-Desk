"""Context window management — compaction of older turns (TD-405).

Budget model: a send may proceed only while the estimated prompt stays
below the tier's compaction threshold — ``THRESHOLD_FRACTION`` of the
context window, minus the ``max_output_tokens`` reservation (the model
must still be able to answer). Exceeding it compacts older turns into a
deterministic extractive summary; no model call is spent to save tokens
(see DECISIONS.md).

Cuts happen only at user-message boundaries: an assistant ``tool_calls``
message and its ``tool`` results can never be split, and the in-flight
turn's user message is always kept. In-flight tool results that still
overflow the budget are fit separately (see :mod:`tstd.context_fit`);
compaction will not drop them.

The user can also compact on request (TD-5109): ``/compact [guidance]``
summarizes everything before the last user turn, and "Summarize from
here" summarizes one user turn and everything after it, keeping earlier
turns verbatim.  Guidance stays deterministic: it heads the summary, and
dropped messages that mention its words keep more of their text and are
the last to go when the summary is over its cap.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import TierConfig
from .context.calibration import scale_tokens
from .context.tokens import TokenCounter
from .provider import ChatMessage, ToolDefinition, content_as_text, tool_definition_payload

# Trigger compaction at this fraction of the usable window.
THRESHOLD_FRACTION = 0.8
# Chat templates wrap every message in role markers (``<|im_start|>role``
# … ``<|im_end|>``) that carry no content of ours (TD-5007).
MESSAGE_OVERHEAD_TOKENS = 4
# Whole user turns kept verbatim after compaction; older ones summarize.
KEEP_USER_TURNS = 2
# Summary shape: each dropped message contributes at most this many
# chars; the whole summary is capped at _SUMMARY_MAX_CHARS.
_SUMMARY_CHARS_PER_MESSAGE = 200
_SUMMARY_MAX_CHARS = 2_000
# A dropped message that mentions the guidance keeps this much (TD-5109).
_SUMMARY_CHARS_ON_TOPIC = 600
# Guidance longer than this is cut; it is a focus, not a second prompt.
MAX_GUIDANCE_CHARS = 500
_SUMMARY_HEADER = "[Earlier conversation compacted]"


@dataclass(frozen=True)
class CompactionStats:
    """What one compaction did — carried on the ContextCompacted event."""

    dropped_messages: int
    kept_messages: int
    tokens_before: int
    tokens_after: int
    counter_method: str
    #: User turns folded into the summary.  The session keeps a running
    #: count so the window's user-turn numbers still find their message.
    dropped_user_turns: int = 0


@dataclass(frozen=True)
class ContextMeter:
    """How the loop last measured the prompt (TD-5109).

    Kept on the session so a compaction asked for between turns, and the
    gauge it updates, count tokens exactly as the next request will: same
    calibrated counter, same tool definitions.
    """

    tier: str
    tier_cfg: TierConfig
    counter: TokenCounter
    tools: Sequence[ToolDefinition] | None

    def measure(self, messages: list[ChatMessage]) -> int:
        return estimate_tokens(messages, self.counter, self.tools)[0]


def budget_threshold(tier: TierConfig) -> int:
    """Estimated prompt-token budget before compaction triggers."""
    return int((tier.context_window - tier.max_output_tokens) * THRESHOLD_FRACTION)


def estimate_tokens(
    messages: list[ChatMessage],
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
) -> tuple[int, str]:
    """Estimate the prompt a request with *messages* and *tools* sends.

    Counts message text and tool-call arguments, each tool definition as
    the request body serializes it, and ``MESSAGE_OVERHEAD_TOKENS`` per
    message (TD-5007). Tool definitions are a fixed few thousand tokens
    on every call; leaving them out let a 32k tier overflow while the
    estimate said it fit. Echoed ``reasoning_details`` (TD-1903) go out
    on every later call too, so they count as the body serializes them.
    Returns (total, counter method).
    """
    total = 0
    methods: set[str] = set()

    def add(text: str) -> None:
        nonlocal total
        if text:
            count = counter.count(text)
            total += count.count
            methods.add(count.method)

    for message in messages:
        text = content_as_text(message.content)
        if message.tool_calls:
            text += "".join(
                tc.function.arguments for tc in message.tool_calls if tc.function is not None
            )
        if message.reasoning_details:
            text += json.dumps(message.reasoning_details)
        add(text)
    for tool in tools or ():
        add(json.dumps(tool_definition_payload(tool)))
    total += scale_tokens(counter, MESSAGE_OVERHEAD_TOKENS * len(messages))
    return total, "+".join(sorted(methods))


def find_compaction_point(
    messages: list[ChatMessage], keep_user_turns: int = KEEP_USER_TURNS
) -> int:
    """Index of the oldest user message to keep; everything from 1 up to
    it is dropped. Returns 0 or 1 when there is nothing to cut (never cut
    the system message at index 0)."""
    user_idxs = [i for i, m in enumerate(messages) if m.role == "user"]
    if len(user_idxs) <= keep_user_turns:
        return 0
    return user_idxs[-keep_user_turns]


def _truncate(text: str, limit: int = _SUMMARY_CHARS_PER_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def guidance_terms(guidance: str) -> frozenset[str]:
    """Words of four or more letters in *guidance*, lowercased."""
    return frozenset(w.lower() for w in re.findall(r"[A-Za-z0-9_][\w.-]{3,}", guidance))


def _render_summary(dropped: list[ChatMessage], guidance: str = "") -> str:
    """Deterministic extractive summary of the dropped span.

    A *prior* summary (from an earlier compaction) can fall inside the
    span — its lines fold into the new one so the oldest context degrades
    gradually instead of vanishing on the second compaction.

    With *guidance* (TD-5109) the summary opens with it, messages that
    mention its words keep up to ``_SUMMARY_CHARS_ON_TOPIC`` characters,
    and when the summary is over its cap the oldest off-topic lines go
    first.
    """
    terms = guidance_terms(guidance)

    def on_topic(text: str) -> bool:
        lowered = text.lower()
        return any(term in lowered for term in terms)

    header = [_SUMMARY_HEADER]
    if guidance:
        header.append(f"Kept in view, as asked: {guidance}")
    lines: list[tuple[str, bool]] = []
    for message in dropped:
        text = content_as_text(message.content)
        hit = bool(terms) and on_topic(text)
        limit = _SUMMARY_CHARS_ON_TOPIC if hit else _SUMMARY_CHARS_PER_MESSAGE
        if message.role == "user":
            lines.append((f"User: {_truncate(text, limit)}", hit))
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
            lines.append((text, True))

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


def _user_turns(messages: Sequence[ChatMessage]) -> int:
    return sum(1 for m in messages if m.role == "user")


def maybe_compact(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
) -> tuple[list[ChatMessage], CompactionStats | None]:
    """Compact older turns when the estimated prompt exceeds the tier's
    budget. *tools* are the definitions the next request sends; they
    count against the budget but are never compacted. Returns the
    (possibly unchanged) message list and stats when compaction happened
    (None otherwise)."""
    if not messages:
        return messages, None
    tokens_before, method = estimate_tokens(messages, counter, tools)
    if tokens_before <= budget_threshold(tier):
        return messages, None
    cut = find_compaction_point(messages)
    if cut <= 1:
        # Fewer than keep_user_turns + 1 user turns: the whole
        # conversation is recent. Nothing older exists to drop.
        return messages, None
    summary = ChatMessage(role="system", content=_render_summary(messages[1:cut]))
    compacted = [messages[0], summary, *messages[cut:]]
    tokens_after, _ = estimate_tokens(compacted, counter, tools)
    return compacted, CompactionStats(
        dropped_messages=cut - 1,
        kept_messages=len(compacted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
        dropped_user_turns=_user_turns(messages[1:cut]),
    )


def compact_on_request(
    messages: list[ChatMessage],
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
    *,
    guidance: str = "",
    from_position: int | None = None,
) -> tuple[list[ChatMessage], CompactionStats] | None:
    """Compact now, whatever the budget says (TD-5109).

    Without *from_position*: summarize everything between the system
    message and the last user turn, which stays verbatim.  With it (the
    index of a user message): summarize that turn and everything after
    it, and keep everything before it verbatim.  Returns None when there
    is nothing to summarize.
    """
    if not messages or messages[0].role != "system":
        return None
    guidance = guidance.strip()[:MAX_GUIDANCE_CHARS]
    if from_position is None:
        cut = find_compaction_point(messages, keep_user_turns=1)
        if cut <= 1:
            return None
        start, end = 1, cut
    else:
        if not 1 <= from_position < len(messages) or messages[from_position].role != "user":
            return None
        start, end = from_position, len(messages)
    dropped = messages[start:end]
    tokens_before, method = estimate_tokens(messages, counter, tools)
    summary = ChatMessage(role="system", content=_render_summary(dropped, guidance))
    compacted = [*messages[:start], summary, *messages[end:]]
    tokens_after, _ = estimate_tokens(compacted, counter, tools)
    return compacted, CompactionStats(
        dropped_messages=len(dropped),
        kept_messages=len(compacted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
        dropped_user_turns=_user_turns(dropped),
    )
