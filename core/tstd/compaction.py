"""Context window management — compaction of older turns (TD-405).

Budget model: a send may proceed only while the estimated prompt stays
below the tier's compaction threshold — ``THRESHOLD_FRACTION`` of the
context window, minus the ``max_output_tokens`` reservation (the model
must still be able to answer). Exceeding it compacts older turns into a
deterministic extractive summary; no model call is spent to save tokens
(see DECISIONS.md).

Cuts happen only at user-message boundaries: an assistant ``tool_calls``
message and its ``tool`` results can never be split, and the in-flight
turn's user message is always kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TierConfig
from .context.tokens import TokenCounter
from .provider import ChatMessage, content_as_text

# Trigger compaction at this fraction of the usable window.
THRESHOLD_FRACTION = 0.8
# Whole user turns kept verbatim after compaction; older ones summarize.
KEEP_USER_TURNS = 2
# Summary shape: each dropped message contributes at most this many
# chars; the whole summary is capped at _SUMMARY_MAX_CHARS.
_SUMMARY_CHARS_PER_MESSAGE = 200
_SUMMARY_MAX_CHARS = 2_000


@dataclass(frozen=True)
class CompactionStats:
    """What one compaction did — carried on the ContextCompacted event."""

    dropped_messages: int
    kept_messages: int
    tokens_before: int
    tokens_after: int
    counter_method: str


def budget_threshold(tier: TierConfig) -> int:
    """Estimated prompt-token budget before compaction triggers."""
    return int((tier.context_window - tier.max_output_tokens) * THRESHOLD_FRACTION)


def estimate_tokens(messages: list[ChatMessage], counter: TokenCounter) -> tuple[int, str]:
    """Sum per-message token estimates; returns (total, counter method)."""
    total = 0
    methods: set[str] = set()
    for message in messages:
        text = content_as_text(message.content)
        if message.tool_calls:
            text += "".join(
                tc.function.arguments for tc in message.tool_calls if tc.function is not None
            )
        if text:
            count = counter.count(text)
            total += count.count
            methods.add(count.method)
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


def _truncate(text: str) -> str:
    if len(text) <= _SUMMARY_CHARS_PER_MESSAGE:
        return text
    return text[:_SUMMARY_CHARS_PER_MESSAGE] + "…"


def _render_summary(dropped: list[ChatMessage]) -> str:
    """Deterministic extractive summary of the dropped span.

    A *prior* summary (from an earlier compaction) can fall inside the
    span — its lines fold into the new one so the oldest context degrades
    gradually instead of vanishing on the second compaction.
    """
    lines = ["[Earlier conversation compacted]"]
    for message in dropped:
        if message.role == "user":
            lines.append(f"User: {_truncate(content_as_text(message.content))}")
        elif message.role == "assistant":
            if message.tool_calls:
                names = ", ".join(
                    tc.function.name for tc in message.tool_calls if tc.function is not None
                )
                lines.append(f"Assistant called tools: {names}")
            else:
                lines.append(f"Assistant: {_truncate(content_as_text(message.content))}")
        elif message.role == "tool":
            lines.append(f"Tool result: {_truncate(content_as_text(message.content))}")
        else:  # system: only a prior summary can land here — fold it in
            lines.append(content_as_text(message.content))
    body = "\n".join(lines)
    if len(body) > _SUMMARY_MAX_CHARS:
        body = body[:_SUMMARY_MAX_CHARS] + "…"
    return body


def maybe_compact(
    messages: list[ChatMessage], tier: TierConfig, counter: TokenCounter
) -> tuple[list[ChatMessage], CompactionStats | None]:
    """Compact older turns when the estimated prompt exceeds the tier's
    budget. Returns the (possibly unchanged) message list and stats when
    compaction happened (None otherwise)."""
    if not messages:
        return messages, None
    tokens_before, method = estimate_tokens(messages, counter)
    if tokens_before <= budget_threshold(tier):
        return messages, None
    cut = find_compaction_point(messages)
    if cut <= 1:
        # Fewer than keep_user_turns + 1 user turns: the whole
        # conversation is recent. Nothing older exists to drop.
        return messages, None
    summary = ChatMessage(role="system", content=_render_summary(messages[1:cut]))
    compacted = [messages[0], summary, *messages[cut:]]
    tokens_after, _ = estimate_tokens(compacted, counter)
    return compacted, CompactionStats(
        dropped_messages=cut - 1,
        kept_messages=len(compacted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
    )
