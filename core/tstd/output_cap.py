"""Replies the provider cut off at the tier's output cap (TD-5008).

Every turn request carries ``max_tokens`` from the tier's
``max_output_tokens``, so a reply that runs long stops mid-stream with
``finish_reason: "length"``. Text cut there is still an answer: it is
kept and the cap is logged. A tool call cut there is not. Its arguments
are the front of a JSON object, so running it would act on half a
command, and replaying it would put invalid JSON into the transcript,
which some chat templates refuse to render. The call stays in the
assistant message with empty arguments, is never dispatched, and is
answered with a note that tells the model to send smaller pieces.

A reasoning model can spend the whole cap thinking: on most providers
thinking tokens count against ``max_tokens``, so the stream ends at
``"length"`` with no answer and no tool call. The turn asks once more with
a larger cap (TD-5233, reply_retry.py), sized here, and when that also ends
with no answer the chat says which setting to raise.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .logging import get_logger
from .provider import ChatMessage, FunctionCall, ToolCall

log = get_logger("tstd.output_cap")

LENGTH_FINISH = "length"
# The assistant_retract reason for the retry after a reply that spent the
# cap thinking, and the turn's error code when it still ends with no answer
# at the cap (TD-5233).
OUTPUT_CAP_THINKING = "output_cap_thinking"
# A reply with no answer that did not end at the cap.
EMPTY_COMPLETION = "empty_completion"
# The retry's cap: this many times the tier's, up to the ceiling (or the
# tier's own cap when that is larger), and never more than the window has
# room for. A raise to less than double is not worth a second call.
THINKING_RETRY_FACTOR = 4
THINKING_RETRY_CEILING = 32_768
THINKING_RETRY_MIN_GAIN = 2
# Kept free in the window on top of the prompt estimate, which can be low.
THINKING_RETRY_MARGIN = 1024
THINKING_RETRY_MARGIN_FRACTION = 20
# What a cut call is replayed with: valid JSON, and no claim about what
# the model meant to send.
CUT_CALL_ARGUMENTS = "{}"


def arguments_complete(arguments: str) -> bool:
    """True when *arguments* is a whole JSON object.

    A prefix of a JSON object never parses as one, so this tells a cut
    call from a finished one.
    """
    try:
        return isinstance(json.loads(arguments), dict)
    except json.JSONDecodeError:
        return False


def cut_call_note(max_output_tokens: int) -> str:
    """The tool result a cut call gets in place of running."""
    return (
        f"Your output hit the max_output_tokens limit ({max_output_tokens}) before this "
        "tool call was complete, so it was not run. Split large writes into smaller "
        "pieces (fs_write a skeleton, then fs_edit), and keep shell commands short."
    )


def stub_call(call_id: str, name: str) -> ToolCall:
    """A cut call as the transcript replays it."""
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments=CUT_CALL_ARGUMENTS),
    )


def cut_call_reply(call_id: str, max_output_tokens: int) -> ChatMessage:
    """The tool message that answers a cut call."""
    return ChatMessage(role="tool", content=cut_call_note(max_output_tokens), tool_call_id=call_id)


def split_cut_calls(calls: Sequence[ToolCall]) -> tuple[list[ToolCall], list[ToolCall]]:
    """``(complete, cut)`` for a non-streamed reply that hit the cap."""
    complete: list[ToolCall] = []
    cut: list[ToolCall] = []
    for call in calls:
        arguments = call.function.arguments if call.function is not None else ""
        (complete if arguments_complete(arguments) else cut).append(call)
    return complete, cut


def log_output_cap(
    *,
    session_id: str,
    tier: str,
    model: str,
    max_output_tokens: int,
    cut_calls: int,
) -> None:
    """One warning per reply that stopped at the cap."""
    log.warning(
        "reply hit the output cap",
        extra={
            "extra_fields": {
                "session_id": session_id,
                "tier": tier,
                "model": model,
                "max_output_tokens": max_output_tokens,
                "cut_tool_calls": cut_calls,
            }
        },
    )


def raised_output_cap(cap: int, context_window: int, prompt_tokens: int) -> int | None:
    """The larger ``max_tokens`` to ask again with, or ``None`` (TD-5233).

    *cap* is the tier's ``max_output_tokens``; *prompt_tokens* the
    estimate of the prompt the call sends. ``None`` means the window has
    no room for at least double the cap, or the cap is already at the
    ceiling, so a second call would most likely end the same way.
    """
    margin = max(THINKING_RETRY_MARGIN, context_window // THINKING_RETRY_MARGIN_FRACTION)
    room = context_window - prompt_tokens - margin
    raised = min(cap * THINKING_RETRY_FACTOR, max(THINKING_RETRY_CEILING, cap), room)
    return raised if raised >= THINKING_RETRY_MIN_GAIN * cap else None


def cap_setting(tier: str, preset: str, fallback: int = 0) -> str:
    """Which ``max_output_tokens`` the call used, as the chat names it.

    *fallback* is the backup position (0 is the tier's own model, ``n`` is
    ``fallbacks[n - 1]``). The path is the one Settings → Configuration
    and config.yaml use, so a backup or a remapped worker names its own
    entry rather than the tier's, whose value did not apply.
    """
    path = f"presets.{preset}.{tier}"
    if fallback:
        return f"the {tier} tier's backup ({path}.fallbacks[{fallback - 1}])"
    return f"the {tier} tier ({path})"


def empty_reply_failure(
    finish_reason: str | None,
    *,
    setting: str,
    cap: int,
    sent: int,
    near_full: bool,
) -> tuple[str, str]:
    """``(error_code, chat text)`` for a turn whose reply had no answer.

    *setting* names the config entry (:func:`cap_setting`), *cap* is its
    ``max_output_tokens`` and *sent* the ``max_tokens``
    the last call carried, which is larger when the retry ran. The text
    is replayed to the model on the next turn, so it stays short and only
    states what happened and what to change. *near_full* adds the one case
    where a new chat helps: the prompt is at the compaction budget.
    """
    if finish_reason == LENGTH_FINISH:
        code = OUTPUT_CAP_THINKING
        if sent > cap:
            text = (
                "No answer: the model used its whole output limit thinking, including one "
                f"retry at {sent:,} tokens. Raise max_output_tokens for {setting}, now "
                f"{cap:,}, in Settings → Configuration. New chats use the new limit."
            )
        else:
            text = (
                f"No answer: the model used its whole output limit ({sent:,} tokens) "
                f"thinking. Raise max_output_tokens for {setting} in Settings → "
                "Configuration. New chats use the new limit."
            )
    else:
        code = EMPTY_COMPLETION
        text = "No answer: the model ended its turn without writing anything. Resend to try again."
    if near_full:
        text += " The conversation is also close to its context limit, so a new chat may help."
    return code, text
