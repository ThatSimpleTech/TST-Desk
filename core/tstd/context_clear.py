"""Clear stale tool results before whole turns are compacted (TD-5115).

Compaction folds whole earlier turns into a short summary once the
prompt passes the tier's budget. Most of what fills a small local window
by then is old tool output: a file read three turns ago that the model
has already used. Clearing replaces those results with a one-line
placeholder (tool, target, size) first, so the turns themselves, the
user's words and the model's answers, stay verbatim for longer.

The clearing policy is ``clear_tool_results`` in config.yaml:

* Only results from earlier turns are cleared. This turn's results are
  what the model is working from; ``context_fit`` trims their middles
  when they overflow (TD-4839, TD-5010).
* The newest ``keep_recent_tool_results`` results stay, whatever turn
  they are in, and so does every result of a tool in ``exclude_tools``.
* Clearing triggers at the compaction budget, like the in-turn trim,
  and one clear takes every stale result at once. It has to land at or
  under the trim's target, 70% of the budget (the TD-5010 hysteresis),
  and free at least ``clear_at_least_tokens``; otherwise it does not
  happen and the turn compacts as before. Only when compaction has no
  older turn to fold does a clear that lands higher still go ahead.

Every clear rewrites the prompt from the oldest result it touches on,
which costs a local server (llama.cpp on EZER) its cached prefix. So a
clear is a batch, and between clears nothing earlier changes: a cleared
result is never rewritten, and its placeholder depends only on its own
tool call (in the assistant message just before it) and the result it
replaced, so the bytes are the same on every later request and after a
restart. Taking every stale result, rather
than stopping at 70% of the budget as the trim does, is what keeps the
rebuilds few and short: the next clear starts at the results added
since this one, near the end of the prompt, instead of re-reading a
long run of old results it left behind (DECISIONS.md).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .compaction import budget_threshold, estimate_tokens, find_compaction_point
from .config import ClearToolResultsConfig, TierConfig
from .context.tokens import TokenCounter
from .context_fit import ELIDE_TARGET_FRACTION, turn_start_index
from .provider import ChatMessage, ToolDefinition, content_as_text
from .provider import ToolCall as ProviderToolCall

#: Every placeholder starts with this, so a cleared result is never
#: cleared again (and never changes).
CLEARED_PREFIX = "[Earlier tool result cleared to save context"
# The argument that names what a call was about, in the order tried.
# Built-in tools use these names; an MCP tool that uses none of them is
# named without a target.
_TARGET_KEYS = ("path", "file_path", "url", "command", "query", "pattern", "task", "target")
_TARGET_MAX_CHARS = 80
#: Built-in tools that only read, so running one again changes nothing.
#: Every other tool (shell, delegate, a write, any MCP tool) may have done
#: something, and its placeholder does not invite a second run (TD-5160):
#: with approvals off nothing would stop it.
RERUN_SAFE_TOOLS = frozenset(
    {"fs_read", "fs_list", "fs_grep", "web_fetch", "web_search", "load_skill"}
)


@dataclass(frozen=True)
class ClearStats:
    """What one clear did — carried on ContextCompacted with
    ``trigger="clear_stale"``.

    ``cleared_results`` counts tool results replaced by a placeholder.
    No message leaves the conversation.
    """

    cleared_results: int
    kept_messages: int
    tokens_before: int
    tokens_after: int
    counter_method: str


def is_cleared(message: ChatMessage) -> bool:
    """True when *message* is a tool result this module already cleared."""
    return message.role == "tool" and content_as_text(message.content).startswith(CLEARED_PREFIX)


def _target(arguments: str) -> str:
    """What the call was about, on one line and at most 80 characters."""
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for key in _TARGET_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > _TARGET_MAX_CHARS:
                text = text[: _TARGET_MAX_CHARS - 1] + "…"
            return text
    return ""


def _own_call(
    batch: Sequence[ProviderToolCall], call_id: str, position: int
) -> ProviderToolCall | None:
    """The call in *batch* that the *position*-th result after it answers.

    By id when the id names exactly one call in the batch; otherwise (no
    id, or parallel calls sharing one) by position, which holds because
    the loop appends results in call order, provided that call carries
    the same id. ``None`` when neither matches.
    """
    same = [call for call in batch if call.id == call_id]
    if len(same) == 1:
        return same[0]
    if position < len(batch) and batch[position].id == call_id:
        return batch[position]
    return None


def _calls_by_result(messages: Sequence[ChatMessage]) -> dict[int, tuple[str, str]]:
    """Index of each tool result → (tool name, target) of its own call.

    A result answers a call of the nearest assistant message before it.
    Ids are no key across the conversation: the loop keeps whatever id
    the provider sends, MockProvider sends ``call_mock_1`` on every turn
    and some servers send none, so a dictionary of ids would name another
    turn's call and check ``exclude_tools`` against the wrong tool. A
    result with no call of its own in that message is left out, and so
    is never cleared.
    """
    calls: dict[int, tuple[str, str]] = {}
    batch: Sequence[ProviderToolCall] = ()
    position = 0
    for index, message in enumerate(messages):
        if message.role == "assistant":
            batch = message.tool_calls or ()
            position = 0
        elif message.role == "tool":
            call = _own_call(batch, message.tool_call_id or "", position)
            position += 1
            if call is not None and call.function is not None:
                calls[index] = (call.function.name, _target(call.function.arguments))
    return calls


def _image_count(message: ChatMessage) -> int:
    content = message.content
    if not isinstance(content, list):
        return 0
    return sum(1 for part in content if part.get("type") != "text")


def cleared_placeholder(tool: str, target: str, chars: int, images: int = 0) -> str:
    """The line a cleared result becomes. Same inputs, same bytes."""
    what = tool or "tool"
    if target:
        what += f' "{target}"'
    size = f"{chars:,} chars"
    if images:
        size += f" and {images} image{'' if images == 1 else 's'}"
    if tool in RERUN_SAFE_TOOLS:
        advice = "Run the tool again if you need it."
    else:
        advice = "It already ran; do not run it again only to see this output."
    return f"{CLEARED_PREFIX}: {what}, {size}. {advice}]"


def _placeholder_for(message: ChatMessage, call: tuple[str, str]) -> str:
    tool, target = call
    chars = len(content_as_text(message.content))
    return cleared_placeholder(tool, target, chars, _image_count(message))


def stale_tool_indexes(
    messages: Sequence[ChatMessage], policy: ClearToolResultsConfig
) -> list[int]:
    """Tool results the policy lets go, oldest first.

    Earlier turns only (before the user message that started the turn
    in flight; a message sent mid-turn that joined it, TD-5112, starts
    none), outside the newest ``keep_recent_tool_results`` results, not
    already cleared, paired with a call of their own (see
    ``_calls_by_result``), and not from a tool in ``exclude_tools``.
    """
    turn_start = turn_start_index(messages)
    tool_indexes = [i for i, m in enumerate(messages) if m.role == "tool"]
    keep = policy.keep_recent_tool_results
    recent = set(tool_indexes[-keep:]) if keep > 0 else set()
    calls = _calls_by_result(messages)
    excluded = set(policy.exclude_tools)
    return [
        index
        for index in tool_indexes
        if index < turn_start
        and index not in recent
        and not is_cleared(messages[index])
        and index in calls
        and calls[index][0] not in excluded
    ]


def clear_stale_tool_results(
    messages: list[ChatMessage],
    tier: TierConfig,
    counter: TokenCounter,
    policy: ClearToolResultsConfig,
    tools: Sequence[ToolDefinition] | None = None,
) -> tuple[list[ChatMessage], ClearStats | None]:
    """Clear old tool results once the prompt is over the budget.

    Returns the original list and ``None`` when nothing changed: the
    policy is off, the prompt fits, nothing is stale, or the stale
    results cannot free ``clear_at_least_tokens``, or cannot bring the
    prompt down to ``ELIDE_TARGET_FRACTION`` of the budget while
    compaction could fold an older turn instead. Otherwise every stale
    result becomes its placeholder, in one batch. Messages are never removed, so an
    assistant ``tool_calls`` message keeps its results. A prompt still
    over the budget afterwards is compaction's to fix.
    """
    if not policy.enabled or not messages:
        return messages, None
    budget = budget_threshold(tier)
    tokens_before, method = estimate_tokens(messages, counter, tools)
    if tokens_before <= budget:
        return messages, None
    stale = stale_tool_indexes(messages, policy)
    if not stale:
        return messages, None

    calls = _calls_by_result(messages)
    cleared = list(messages)
    changed = 0
    freed = 0
    for index in stale:
        old = content_as_text(cleared[index].content)
        placeholder = _placeholder_for(cleared[index], calls[index])
        # A tool message's estimate is its text alone, so the drop is
        # exact: the per-message overhead does not change.
        saved = counter.count(old).count - counter.count(placeholder).count
        if saved <= 0:
            continue  # a short result costs less than its placeholder
        cleared[index] = replace(cleared[index], content=placeholder)
        changed += 1
        freed += saved

    if changed == 0 or freed < policy.clear_at_least_tokens:
        return messages, None
    tokens_after, _ = estimate_tokens(cleared, counter, tools)
    if tokens_after > budget * ELIDE_TARGET_FRACTION and find_compaction_point(messages) > 1:
        # Too little room bought: the next clear would come a turn or two
        # later, each one a prefix rebuild. Compaction makes the big cut
        # now, and clearing starts again on the turns it keeps.
        return messages, None
    return cleared, ClearStats(
        cleared_results=changed,
        kept_messages=len(cleared),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        counter_method=method,
    )
