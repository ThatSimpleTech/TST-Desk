"""Context window management — compaction of older turns (TD-405).

Budget model: a send may proceed only while the estimated prompt stays
below the tier's compaction threshold — ``THRESHOLD_FRACTION`` of the
context window, minus the ``max_output_tokens`` reservation (the model
must still be able to answer). Exceeding it compacts older turns into
one summary message. These functions stay synchronous and model-free:
the caller may hand in a worker-written structured summary of the span
and the files read again after it (TD-5128, :mod:`tstd.compaction_model`);
without one the summary is the deterministic extractive one.  The text
of a summary lives in :mod:`tstd.compaction_text`.

Cuts happen only at user-message boundaries: an assistant ``tool_calls``
message and its ``tool`` results can never be split, and the in-flight
turn's user message is always kept. In-flight tool results that still
overflow the budget are fit separately (see :mod:`tstd.context_fit`);
compaction will not drop them.

The user can also compact on request (TD-5109): ``/compact [guidance]``
summarizes everything before the last user turn, and "Summarize from
here" summarizes one user turn and everything after it, keeping earlier
turns verbatim.  Guidance heads the summary; the worker is told to keep
it in view, and in the extractive summary dropped messages that mention
its words keep more of their text and are the last to go when the
summary is over its cap.

The window numbers user turns from the start of the session, so every
summary's first line says how many user turns it stands for, and
:func:`window_user_turns` maps the window's numbers back onto the
conversation.  The count lives in the summary itself, so it survives a
restart and a fork with no counter to keep in step.

A compaction can carry text that is still current into its summary
(TD-5122): the session's task list. It goes under ``_CARRY_HEADER``,
after the capped summary, so the cap never cuts it. A later compaction
drops the old copy when it folds that summary in and carries a fresh one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from .compaction_text import (
    _SUMMARY_CHARS_PER_MESSAGE,
    _SUMMARY_HEADER,
    compose,
    guidance_terms,
    is_summary,
    render_extractive,
    render_model_core,
    summary_turn_count,
)
from .config import TierConfig
from .context.calibration import scale_tokens
from .context.tokens import TokenCounter
from .provider import (
    ChatMessage,
    ToolDefinition,
    content_as_text,
    tool_definition_payload,
    turn_start_index,
)

__all__ = [
    "KEEP_USER_TURNS",
    "MAX_GUIDANCE_CHARS",
    "MESSAGE_OVERHEAD_TOKENS",
    "THRESHOLD_FRACTION",
    "_SUMMARY_CHARS_PER_MESSAGE",
    "_SUMMARY_HEADER",
    "CompactionStats",
    "ContextMeter",
    "auto_span",
    "budget_threshold",
    "compact_on_request",
    "estimate_tokens",
    "find_compaction_point",
    "guidance_terms",
    "manual_span",
    "maybe_compact",
    "summary_user_turns",
    "window_user_turns",
]

# Trigger compaction at this fraction of the usable window.
THRESHOLD_FRACTION = 0.8
# Chat templates wrap every message in role markers (``<|im_start|>role``
# … ``<|im_end|>``) that carry no content of ours (TD-5007).
MESSAGE_OVERHEAD_TOKENS = 4
# Whole user turns kept verbatim after compaction; older ones summarize.
KEEP_USER_TURNS = 2
# An earlier turn with more steps than this is a long task (TD-5178):
# it may be folded to make room when the kept turns are over budget.
LONG_TURN_STEPS = 8
# Guidance longer than this is cut; it is a focus, not a second prompt.
MAX_GUIDANCE_CHARS = 500


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

    start = turn_start_index(messages)
    for i, message in enumerate(messages):
        text = content_as_text(message.content)
        if message.reasoning_content and i >= start:
            # Echoed thinking (TD-5174) goes out for the running turn (TD-5177).
            text += message.reasoning_content
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


def summary_user_turns(message: ChatMessage) -> int | None:
    """How many user turns *message* stands for if it is a summary, else None.

    A summary written before the header carried the count (TD-5109) is
    read by its ``User:`` lines, which is exact unless its cap cut lines.
    """
    if not is_summary(message):
        return None
    text = content_as_text(message.content)
    counted = summary_turn_count(text)
    if counted is not None:
        return counted
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
            # A done-check's feedback (TD-5124) is no turn the window shows.
            if not message.check:
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
            total += 0 if message.check else 1
        else:
            total += summary_user_turns(message) or 0
    return total


def _summary_message(
    dropped: list[ChatMessage],
    turns: int,
    *,
    guidance: str,
    body: str | None,
    carry: str,
    files: str,
) -> ChatMessage:
    """The one system message that stands for *dropped* (TD-5128).

    *body* is the worker's structured summary when one was written; None
    falls back to the extractive summary.
    """
    core = (
        render_model_core(body, guidance, turns)
        if body
        else render_extractive(dropped, guidance, turns)
    )
    return ChatMessage(role="system", content=compose(core, carry, files))


def _auto_cut(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
) -> int:
    """Where an automatic compaction cuts (TD-5178).

    Normally the last ``KEEP_USER_TURNS`` turns stay verbatim. A request
    that ran as a long task of many tool steps is one turn, though, and
    keeping it whole beside the running turn can leave the budget full
    with nothing to fold. When the earlier kept turn has more than
    ``LONG_TURN_STEPS`` steps and the kept turns are over the budget,
    only the running turn stays.
    """
    cut = find_compaction_point(messages)
    running = find_compaction_point(messages, keep_user_turns=1)
    if running <= max(cut, 1):
        return cut
    steps = sum(1 for m in messages[max(cut, 1) : running] if m.role == "assistant")
    if steps <= LONG_TURN_STEPS:
        return cut
    kept = [messages[0], *messages[max(cut, 1) :]]
    if estimate_tokens(kept, counter, tools)[0] <= budget_threshold(tier):
        return cut
    return running


def auto_span(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
) -> tuple[int, int] | None:
    """The span ``maybe_compact`` would fold now, or None (TD-5128).

    Lets a caller write the summary of exactly that span before
    compacting: over budget and with turns old enough to drop.
    """
    if not messages:
        return None
    if estimate_tokens(messages, counter, tools)[0] <= budget_threshold(tier):
        return None
    cut = _auto_cut(messages, tier, counter, tools)
    return (1, cut) if cut > 1 else None


def maybe_compact(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
    *,
    carry: str = "",
    body: str | None = None,
    files: str = "",
) -> tuple[list[ChatMessage], CompactionStats | None]:
    """Compact older turns when the estimated prompt exceeds the tier's
    budget. *tools* are the definitions the next request sends; they
    count against the budget but are never compacted. *carry* goes into
    the summary whole, as still current (TD-5122). *body* is a worker's
    summary of the span :func:`auto_span` names, and *files* the files
    read again after it (TD-5128). Returns the (possibly unchanged)
    message list and stats when compaction happened (None otherwise)."""
    if not messages:
        return messages, None
    tokens_before, method = estimate_tokens(messages, counter, tools)
    if tokens_before <= budget_threshold(tier):
        return messages, None
    cut = _auto_cut(messages, tier, counter, tools)
    if cut <= 1:
        # Fewer than keep_user_turns + 1 user turns: the whole
        # conversation is recent. Nothing older exists to drop.
        return messages, None
    dropped = messages[1:cut]
    turns = _user_turns(dropped)
    summary = _summary_message(dropped, turns, guidance="", body=body, carry=carry, files=files)
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


def manual_span(messages: list[ChatMessage], from_position: int | None) -> tuple[int, int] | None:
    """The span a compaction on request folds, or None (TD-5109).

    Without *from_position*: everything between the system message and
    the last user turn.  With it (the index of a user message): that turn
    and everything after it.
    """
    if not messages or messages[0].role != "system":
        return None
    if from_position is None:
        cut = find_compaction_point(messages, keep_user_turns=1)
        return (1, cut) if cut > 1 else None
    if not 1 <= from_position < len(messages) or messages[from_position].role != "user":
        return None
    return from_position, len(messages)


def compact_on_request(
    messages: list[ChatMessage],
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None = None,
    *,
    guidance: str = "",
    from_position: int | None = None,
    carry: str = "",
    body: str | None = None,
    files: str = "",
) -> tuple[list[ChatMessage], CompactionStats] | None:
    """Compact now, whatever the budget says (TD-5109).

    The span is :func:`manual_span`'s; everything outside it stays
    verbatim.  *carry* goes into the summary whole, as still current
    (TD-5122); *body* and *files* are as for :func:`maybe_compact`.
    Returns None when there is nothing to summarize.
    """
    span = manual_span(messages, from_position)
    if span is None:
        return None
    start, end = span
    guidance = guidance.strip()[:MAX_GUIDANCE_CHARS]
    dropped = messages[start:end]
    turns = _user_turns(dropped)
    tokens_before, method = estimate_tokens(messages, counter, tools)
    summary = _summary_message(
        dropped, turns, guidance=guidance, body=body, carry=carry, files=files
    )
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
