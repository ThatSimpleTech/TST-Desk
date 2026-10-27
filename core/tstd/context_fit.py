"""Fit an in-flight turn into the tier's context budget (TD-4839).

Compaction cuts only at user-message boundaries and always keeps the
turn that is running, so a first-turn burst of tool results never
reaches it. Two guards live here instead:

* The per-result character cap scales with the tokens still free, so
  one read cannot fill a small window. Large windows stay at the
  historical 50,000-character ceiling.
* Before each provider call, if the prompt is over the same budget
  compaction uses, the middles of this turn's tool results are elided
  down to ``ELIDE_TARGET_FRACTION`` of it (TD-5010), or less deep
  when the rest of the prompt leaves no room for that. Messages are not
  removed: an assistant ``tool_calls`` message stays next to its
  ``tool`` results, and the user's message stays.
* A long turn also fills the window with its own steps: the thinking
  echoed back for every step and the full text of each file it wrote.
  Before results are touched, steps older than ``KEEP_RECENT_STEPS``
  lose their echoed thinking and the long text in their call
  arguments (TD-5178). The calls themselves stay, so every ``tool``
  result still has its call.

The window and the output reservation come from the tier config. No
context length is hardcoded here.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from .compaction import budget_threshold, estimate_tokens
from .config import TierConfig
from .context.calibration import calibration_ratio
from .context.tokens import CHARS_PER_TOKEN, TokenCounter
from .provider import ChatMessage, ToolDefinition, content_as_text
from .tools.results import DEFAULT_MAX_RESULT_CHARS

# Below this a tool result stops being worth sending. The floor is well
# under a quarter of a 32k window, so a result stays useful on the
# smallest tier we ship and the cap can still fall back to it when the
# prompt is already full.
RESULT_CHAR_FLOOR = 4_000
# A head and a tail shorter than this are not worth splitting around the
# note — the note itself is the useful part.
_MIN_KEEP_CHARS = 1_000
_ELISION_PHRASE = "chars elided — re-read with offset/limit"
_MAX_ELISION_PASSES = 64
# Elision triggers at the budget but aims below it (TD-5010). Landing
# just under the budget meant the next small result tipped it over
# again, and every rewrite of an earlier result changes the prompt
# prefix a local server caches. The gap is room for the rest of the
# turn's results before history is touched again. When the part of the
# prompt elision cannot touch leaves no room for it, the target rises
# (see ``elision_target``).
ELIDE_TARGET_FRACTION = 0.7
# The latest steps of a running turn keep their thinking and call
# arguments whole (TD-5178); older ones are trimmed before any result.
KEEP_RECENT_STEPS = 4
# A call argument longer than this, in a trimmed step, keeps its head.
_ARG_KEEP_CHARS = 400


@dataclass(frozen=True)
class InTurnFit:
    """What one in-turn elision did — carried on ContextCompacted with
    ``trigger="inflight_trim"`` (TD-5150).

    ``elided_results`` counts tool results whose middle was replaced.
    The messages themselves stay, so this is not compaction's
    ``dropped_messages`` (those are removed). A second shrink of the
    same result does not increment the count.
    """

    elided_results: int
    kept_messages: int
    tokens_before: int
    tokens_after: int
    counter_method: str
    #: Older steps of the turn whose thinking or call text was cut (TD-5178).
    trimmed_steps: int = 0


def tool_result_char_cap(
    context_window: int,
    max_output_tokens: int,
    prefix_tokens: int,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> int:
    """Largest single tool result that should not fill the tier alone.

    A quarter of the tokens still free, converted to characters with
    *chars_per_token*, then clamped to
    ``[RESULT_CHAR_FLOOR, DEFAULT_MAX_RESULT_CHARS]``. With the default
    heuristic ratio the ``// 4 * 4`` is that quarter and the conversion
    back to characters: they cancel when the remainder divides evenly,
    and the floor still applies when the prompt is already over the
    window. A calibrated counter (TD-5007) passes fewer characters per
    token, so the same token room holds less text.

    ``prefix_tokens`` is the estimate of the prompt the results are
    about to join (system, history, the user message, the assistant
    tool calls, the tool definitions) — not only the cache prefix. A
    negative estimate must not widen the cap.
    """
    prefix = max(0, prefix_tokens)
    remaining = context_window - max_output_tokens - prefix
    quarter_chars = math.floor((remaining // 4) * chars_per_token)
    return min(DEFAULT_MAX_RESULT_CHARS, max(RESULT_CHAR_FLOOR, quarter_chars))


def _elision_note(removed: int) -> str:
    return f"\n[{removed} {_ELISION_PHRASE}]\n"


def elide_tool_text(text: str, target_chars: int) -> str:
    """Keep a head and a tail; the middle names how many chars went.

    The note is sized from the original length first, so one pass stays
    inside *target_chars* even though the note's digit count depends on
    how much was removed. Overestimating that count is fine.
    """
    if target_chars < 0:
        target_chars = 0
    if len(text) <= target_chars:
        return text
    # Digits of the full length are an upper bound on the real note.
    over = _elision_note(len(text))
    keep = target_chars - len(over)
    if keep < 2:
        bracket = over.strip("\n")
        if len(bracket) <= target_chars:
            return bracket
        return bracket[:target_chars]
    head_len = keep // 2
    tail_len = keep - head_len
    removed = len(text) - head_len - tail_len
    note = _elision_note(removed)
    tail = text[-tail_len:] if tail_len else ""
    body = text[:head_len] + note + tail
    if len(body) <= target_chars:
        return body
    overflow = len(body) - target_chars
    if tail_len > overflow:
        return text[:head_len] + note + text[-(tail_len - overflow) :]
    return (text[:head_len] + note)[:target_chars]


def turn_start_index(messages: Sequence[ChatMessage]) -> int:
    """Index of the user message that started the turn in flight, or -1.

    A message sent mid-turn that joined the turn (TD-5112) does not start
    one. When only such messages are left (a summary stands for the
    turn's start), the last user message starts it, as every user
    message did before.
    """
    last_user = -1
    turn_start = -1
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index
            if not message.steer:
                turn_start = index
    return turn_start if turn_start >= 0 else last_user


def _inflight_tool_indexes(messages: list[ChatMessage]) -> list[int]:
    """Tool results that belong to the turn still in flight.

    Everything before the user message that started it is an earlier
    turn. Compaction owns that span; elision must not touch it. A message
    sent mid-turn that joined the turn (TD-5112) does not start one, so
    the results the turn got before it stay in reach.
    """
    turn_start = turn_start_index(messages)
    if turn_start < 0:
        return []
    return [
        index for index in range(turn_start + 1, len(messages)) if messages[index].role == "tool"
    ]


def _inflight_step_indexes(messages: list[ChatMessage]) -> list[int]:
    """Assistant messages of the running turn, oldest first."""
    turn_start = turn_start_index(messages)
    if turn_start < 0:
        return []
    return [
        index
        for index in range(turn_start + 1, len(messages))
        if messages[index].role == "assistant"
    ]


def _shorten_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str) and len(value) > _ARG_KEEP_CHARS:
        removed = len(value) - _ARG_KEEP_CHARS
        note = f"\n[{removed} chars elided from this call; it already ran]"
        return value[:_ARG_KEEP_CHARS] + note, True
    if isinstance(value, dict):
        changed = False
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], hit = _shorten_value(item)
            changed = changed or hit
        return out, changed
    if isinstance(value, list):
        pairs = [_shorten_value(item) for item in value]
        return [item for item, _ in pairs], any(hit for _, hit in pairs)
    return value, False


def shorten_call_arguments(arguments: str) -> str:
    """*arguments* with each long string cut to its head, still valid JSON.

    Text that is not a JSON object is left alone: the call already ran,
    and a half-parsed body is worse than a long one.
    """
    if len(arguments) <= _ARG_KEEP_CHARS:
        return arguments
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    shortened, changed = _shorten_value(parsed)
    return json.dumps(shortened, ensure_ascii=False) if changed else arguments


def trim_step(message: ChatMessage) -> ChatMessage:
    """An older step without its echoed thinking or long call text."""
    calls = message.tool_calls
    if calls:
        calls = [
            replace(
                call,
                function=replace(
                    call.function, arguments=shorten_call_arguments(call.function.arguments)
                ),
            )
            if call.function is not None
            else call
            for call in calls
        ]
    return replace(message, reasoning_content=None, tool_calls=calls)


def _trim_older_steps(
    fitted: list[ChatMessage],
    target_tokens: int,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
) -> int:
    """Trim the turn's older steps, oldest first, until *target_tokens*.

    Returns how many steps changed. Works in place on *fitted*.
    """
    steps = _inflight_step_indexes(fitted)[:-KEEP_RECENT_STEPS]
    trimmed = 0
    for index in steps:
        if estimate_tokens(fitted, counter, tools)[0] <= target_tokens:
            break
        updated = trim_step(fitted[index])
        if updated != fitted[index]:
            fitted[index] = updated
            trimmed += 1
    return trimmed


def elision_target(budget_tokens: int, floor_tokens: int) -> int:
    """Where elision aims, given *floor_tokens*: the prompt with every
    in-flight result cut to the keep floor.

    Normally ``ELIDE_TARGET_FRACTION`` of the budget (TD-5010). The
    in-flight results always keep at least the gap's share of the budget
    above the floor, though. The system prompt, the tool definitions and
    the earlier turns compaction keeps verbatim can fill most of the
    budget by themselves, and then aiming at 70% would cut every result,
    the one the model just asked for included, to the floor. So the
    target rises with the floor, and at a floor of 70% or more it is the
    budget itself, as before TD-5010.
    """
    hysteresis = int(budget_tokens * ELIDE_TARGET_FRACTION)
    results_share = budget_tokens - hysteresis
    return max(1, min(budget_tokens, max(hysteresis, floor_tokens + results_share)))


def _chars_to_shed(excess_tokens: int, counter: TokenCounter) -> int:
    """Characters to cut so the estimate drops by *excess_tokens* or more.

    A calibrated count is ``ceil(base * ratio)`` per message, so cutting
    ``excess * 4 / ratio`` characters can leave the prompt a token over
    and send the next pass to a second result. Whole base tokens, each
    worth ``ratio`` estimated tokens, cannot fall short under the
    heuristic counter.
    """
    base_tokens = math.ceil(excess_tokens / calibration_ratio(counter))
    return base_tokens * CHARS_PER_TOKEN


def _with_tool_text(message: ChatMessage, new_text: str) -> ChatMessage:
    """Replace the text of a tool message, leaving image parts in place.

    Several text parts collapse into the first. Screenshot bytes
    (TD-1729) ride a non-text part and have to survive elision or the
    model loses the picture while keeping a note about text it can
    re-read.
    """
    content = message.content
    if not isinstance(content, list):
        return replace(message, content=new_text)
    parts: list[dict[str, Any]] = []
    placed = False
    for part in content:
        if part.get("type") == "text":
            if not placed:
                updated = dict(part)
                updated["text"] = new_text
                parts.append(updated)
                placed = True
            continue
        parts.append(part)
    if not placed:
        parts.insert(0, {"type": "text", "text": new_text})
    return replace(message, content=parts)


def _floor_tokens(
    messages: list[ChatMessage],
    indexes: list[int],
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
) -> int:
    """The prompt's estimate with every in-flight result at the keep floor."""
    floored = list(messages)
    for index in indexes:
        text = content_as_text(floored[index].content)
        if len(text) > _MIN_KEEP_CHARS:
            floored[index] = _with_tool_text(floored[index], elide_tool_text(text, _MIN_KEEP_CHARS))
    tokens, _ = estimate_tokens(floored, counter, tools)
    return tokens


def fit_inflight_turn(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    budget: int | None = None,
    *,
    tools: Sequence[ToolDefinition] | None = None,
) -> tuple[list[ChatMessage], InTurnFit | None]:
    """Elide in-flight tool results once the prompt is over *budget*.

    *budget* defaults to the compaction threshold. The overflow retry
    passes half of that. *tools* are the definitions the request sends:
    they count toward the prompt but cannot be elided. Returns the
    original list and ``None`` when nothing changed — including when
    the prompt already fits, and when the overage is not in this turn's
    tool results — so a text-only compaction still emits a single
    ContextCompacted.

    Over the budget, results shrink until the prompt fits
    ``ELIDE_TARGET_FRACTION`` of it, not the budget itself (TD-5010):
    the next few results then fit without rewriting the ones already
    sent. When the rest of the prompt leaves no room for that, the
    target rises toward the budget (``elision_target``) rather than
    cutting every result to the floor. The largest result absorbs the
    whole excess first, which lands near the target instead of at the
    floor; a remainder goes back to a result this call already cut. A
    later, tighter budget can then still shrink. Messages are never
    deleted.
    """
    budget_tokens = budget_threshold(tier) if budget is None else budget
    tokens_before, method = estimate_tokens(messages, counter, tools)
    if tokens_before <= budget_tokens:
        return messages, None

    fitted = list(messages)
    # The turn's own older steps go first (TD-5178): their thinking and
    # written-file text are spent, the results are what the model reads.
    hysteresis = max(1, int(budget_tokens * ELIDE_TARGET_FRACTION))
    trimmed = _trim_older_steps(fitted, hysteresis, counter, tools)
    indexes = _inflight_tool_indexes(fitted)
    if not indexes or estimate_tokens(fitted, counter, tools)[0] <= hysteresis:
        return _fit_result(fitted, messages, 0, trimmed, tokens_before, method, counter, tools)

    target_tokens = elision_target(budget_tokens, _floor_tokens(fitted, indexes, counter, tools))
    elided: set[int] = set()
    stuck: set[int] = set()
    for _ in range(_MAX_ELISION_PASSES):
        tokens, _ = estimate_tokens(fitted, counter, tools)
        if tokens <= target_tokens:
            break
        candidates = [
            index
            for index in indexes
            if index not in stuck and len(content_as_text(fitted[index].content)) > _MIN_KEEP_CHARS
        ]
        if not candidates:
            break
        # A result this call already cut goes first, so a remainder of
        # the excess does not rewrite a second result.
        largest = max(
            candidates,
            key=lambda index: (index in elided, len(content_as_text(fitted[index].content))),
        )
        text = content_as_text(fitted[largest].content)
        excess_chars = _chars_to_shed(tokens - target_tokens, counter)
        target = max(_MIN_KEEP_CHARS, len(text) - excess_chars)
        new_text = elide_tool_text(text, target)
        if len(new_text) >= len(text):
            halved = max(_MIN_KEEP_CHARS, len(text) // 2)
            new_text = elide_tool_text(text, halved)
        if len(new_text) >= len(text):
            stuck.add(largest)
            continue
        fitted[largest] = _with_tool_text(fitted[largest], new_text)
        elided.add(largest)

    return _fit_result(
        fitted, messages, len(elided), trimmed, tokens_before, method, counter, tools
    )


def _fit_result(
    fitted: list[ChatMessage],
    messages: list[ChatMessage],
    elided: int,
    trimmed: int,
    tokens_before: int,
    method: str,
    counter: TokenCounter,
    tools: Sequence[ToolDefinition] | None,
) -> tuple[list[ChatMessage], InTurnFit | None]:
    if not elided and not trimmed:
        return messages, None
    tokens_after, _ = estimate_tokens(fitted, counter, tools)
    return fitted, InTurnFit(
        elided_results=elided,
        kept_messages=len(fitted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
        trimmed_steps=trimmed,
    )


def overflow_user_message(model: str, context_window: int) -> str:
    """Short failure text. The window is the configured one, contiguous digits."""
    return (
        f"{model} is configured for a {context_window}-token context window, "
        "and this turn does not fit. Switch the tier to a larger-context model "
        "in config.yaml, or start a fresh session."
    )


def _looks_like_raw_upstream(text: str) -> bool:
    """True when *text* is a provider body rather than a sentence we wrote.

    Auth and credit failures are prose and must stay. A JSON object or
    array — or a sentence that has one embedded — is the upstream payload.
    """
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return True
    return '{"' in text or '["' in text


def transcript_failure_text(
    error_code: str | None,
    error_msg: str,
    *,
    model: str,
    context_window: int,
) -> str:
    """Assistant text for a failed turn. Never the raw upstream body.

    A context overflow names the configured model and window. Any other
    error whose message is upstream JSON becomes one plain sentence, so
    the error card (keyed by ``error_code``) is what tells the user the
    fix. Prose we already wrote — auth, credits, a missing key — is
    kept and prefixed the way the chat pane has always shown failures.
    """
    if error_code == "context_overflow":
        detail = overflow_user_message(model, context_window)
    elif _looks_like_raw_upstream(error_msg):
        detail = "The provider rejected the request."
    else:
        detail = error_msg
    return f"I encountered an error: {detail}"
