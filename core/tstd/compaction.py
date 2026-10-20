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

The window numbers user turns from the start of the session, so every
summary's first line says how many user turns it stands for, and
:func:`window_user_turns` maps the window's numbers back onto the
conversation.  The count lives in the summary itself, so it survives a
restart and a fork with no counter to keep in step.
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
# The header line with the user turns the summary stands for (TD-5109).
_SUMMARY_TURNS = re.compile(re.escape(_SUMMARY_HEADER) + r" \((\d+) user turns?\)")


@dataclass(frozen=True)
class CompactionStats:
    """What one compaction did — carried on the ContextCompacted event."""

    dropped_messages: int
    kept_messages: int
    tokens_before: int
    tokens_after: int
    counter_method: str
    #: User turns the new summary stands for, counting those an earlier
    #: summary in the dropped span already stood for.
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
    #: 1-based position of the backup endpoint in use, 0 for the tier's
    #: own model (TD-5120). ``tier_cfg`` is then the backup's.
    fallback: int = 0

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
    the system message at index 0).

    Only a message that started a turn is a cut point. A message sent
    mid-turn that joined the running turn (TD-5112) belongs to that turn,
    so the turns kept are whole, the running one with its request."""
    user_idxs = [i for i, m in enumerate(messages) if m.role == "user" and not m.steer]
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


def _render_summary(dropped: list[ChatMessage], guidance: str = "", turns: int = 0) -> str:
    """Deterministic extractive summary of the dropped span.

    A *prior* summary (from an earlier compaction) can fall inside the
    span — its lines fold into the new one so the oldest context degrades
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

    header = [f"{_SUMMARY_HEADER} ({turns} user turn{'' if turns == 1 else 's'})"]
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


def summary_user_turns(message: ChatMessage) -> int | None:
    """How many user turns *message* stands for if it is a summary, else None.

    A summary written before the header carried the count (TD-5109) is
    read by its ``User:`` lines, which is exact unless its cap cut lines.
    """
    if message.role != "system":
        return None
    text = content_as_text(message.content)
    if not text.startswith(_SUMMARY_HEADER):
        return None
    counted = _SUMMARY_TURNS.match(text)
    if counted is not None:
        return int(counted.group(1))
    return sum(1 for line in text.splitlines() if line.startswith("User: "))


def window_user_turns(messages: Sequence[ChatMessage]) -> list[int | None]:
    """Where each of the window's user turns is in *messages* (TD-5109).

    Entry *n* is the index of the window's user turn *n*, or None when
    that turn is inside a summary.  The window numbers user turns from
    the start of the session; a summary stands for as many of them as
    its header says.  A Grok-engine conversation has no system prompt,
    so its first message can be user turn 0 (TD-5147).
    """
    turns: list[int | None] = []
    for index, message in enumerate(messages):
        if index == 0 and message.role == "system":
            continue  # the system prompt
        if message.role == "user":
            turns.append(index)
            continue
        folded = summary_user_turns(message)
        if folded:
            turns.extend([None] * folded)
    return turns


def _user_turns(messages: Sequence[ChatMessage]) -> int:
    """User turns *messages* stand for, those inside summaries included."""
    total = 0
    for message in messages:
        if message.role == "user":
            total += 1
        else:
            total += summary_user_turns(message) or 0
    return total


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
    turns = _user_turns(messages[1:cut])
    summary = ChatMessage(role="system", content=_render_summary(messages[1:cut], turns=turns))
    compacted = [messages[0], summary, *messages[cut:]]
    tokens_after, _ = estimate_tokens(compacted, counter, tools)
    return compacted, CompactionStats(
        dropped_messages=cut - 1,
        kept_messages=len(compacted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
        dropped_user_turns=turns,
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
    turns = _user_turns(dropped)
    tokens_before, method = estimate_tokens(messages, counter, tools)
    summary = ChatMessage(role="system", content=_render_summary(dropped, guidance, turns))
    compacted = [*messages[:start], summary, *messages[end:]]
    tokens_after, _ = estimate_tokens(compacted, counter, tools)
    return compacted, CompactionStats(
        dropped_messages=len(dropped),
        kept_messages=len(compacted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
        dropped_user_turns=turns,
    )
