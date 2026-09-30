"""Fit an in-flight turn into the tier's context budget (TD-4839).

Compaction cuts only at user-message boundaries and always keeps the
turn that is running, so a first-turn burst of tool results never
reaches it. Two guards live here instead:

* The per-result character cap scales with the tokens still free, so
  one read cannot fill a small window. Large windows stay at the
  historical 50,000-character ceiling.
* Before each provider call, the middles of this turn's tool results
  are elided until the same budget compaction uses is met. Messages
  are not removed: an assistant ``tool_calls`` message stays next to
  its ``tool`` results, and the user's message stays.

The window and the output reservation come from the tier config. No
context length is hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .compaction import budget_threshold, estimate_tokens
from .config import TierConfig
from .context.tokens import CHARS_PER_TOKEN, TokenCounter
from .provider import ChatMessage, content_as_text
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


@dataclass(frozen=True)
class InTurnFit:
    """What one in-turn elision did — carried on ContextCompacted.

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


def tool_result_char_cap(context_window: int, max_output_tokens: int, prefix_tokens: int) -> int:
    """Largest single tool result that should not fill the tier alone.

    A quarter of the tokens still free, converted with the heuristic
    counter's characters-per-token ratio, then clamped to
    ``[RESULT_CHAR_FLOOR, DEFAULT_MAX_RESULT_CHARS]``. The ``// 4 * 4``
    is that quarter and the conversion back to characters: they cancel
    when the remainder divides evenly, and the floor still applies when
    the prompt is already over the window.

    ``prefix_tokens`` is the estimate of the prompt the results are
    about to join (system, history, the user message, the assistant
    tool calls) — not only the cache prefix. A negative estimate must
    not widen the cap.
    """
    prefix = max(0, prefix_tokens)
    remaining = context_window - max_output_tokens - prefix
    quarter_chars = (remaining // 4) * CHARS_PER_TOKEN
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


def _inflight_tool_indexes(messages: list[ChatMessage]) -> list[int]:
    """Tool results that belong to the turn still in flight.

    Everything before the last user message is an earlier turn.
    Compaction owns that span; elision must not touch it.
    """
    last_user = -1
    for index, message in enumerate(messages):
        if message.role == "user":
            last_user = index
    if last_user < 0:
        return []
    return [
        index for index in range(last_user + 1, len(messages)) if messages[index].role == "tool"
    ]


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


def fit_inflight_turn(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    budget: int | None = None,
) -> tuple[list[ChatMessage], InTurnFit | None]:
    """Elide in-flight tool results until the prompt fits *budget*.

    *budget* defaults to the compaction threshold. The overflow retry
    passes half of that. Returns the original list and ``None`` when
    nothing changed — including when the prompt already fits, and when
    the overage is not in this turn's tool results — so a text-only
    compaction still emits a single ContextCompacted.

    The largest result absorbs the whole excess first, which lands near
    the budget instead of at the floor. A later, tighter budget can
    then still shrink. Messages are never deleted.
    """
    budget_tokens = budget_threshold(tier) if budget is None else budget
    tokens_before, method = estimate_tokens(messages, counter)
    if tokens_before <= budget_tokens:
        return messages, None
    indexes = _inflight_tool_indexes(messages)
    if not indexes:
        return messages, None

    fitted = list(messages)
    elided: set[int] = set()
    stuck: set[int] = set()
    for _ in range(_MAX_ELISION_PASSES):
        tokens, _ = estimate_tokens(fitted, counter)
        if tokens <= budget_tokens:
            break
        candidates = [
            index
            for index in indexes
            if index not in stuck and len(content_as_text(fitted[index].content)) > _MIN_KEEP_CHARS
        ]
        if not candidates:
            break
        largest = max(candidates, key=lambda index: len(content_as_text(fitted[index].content)))
        text = content_as_text(fitted[largest].content)
        excess_chars = (tokens - budget_tokens) * CHARS_PER_TOKEN
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

    if not elided:
        return messages, None
    tokens_after, _ = estimate_tokens(fitted, counter)
    return fitted, InTurnFit(
        elided_results=len(elided),
        kept_messages=len(fitted),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
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
