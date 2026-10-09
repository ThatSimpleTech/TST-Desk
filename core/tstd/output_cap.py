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
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from .logging import get_logger
from .provider import ChatMessage, FunctionCall, ToolCall

log = get_logger("tstd.output_cap")

LENGTH_FINISH = "length"
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
