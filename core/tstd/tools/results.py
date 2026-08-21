"""Result types and output truncation for tool dispatch.

Split out of ``dispatch.py`` (TD-705) so the dispatcher stays focused on
the dispatch pipeline.  ``dispatch`` re-imports these names, so existing
import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..autonomy import DecisionClass, Notice

# ── Result types ────────────────────────────────────────────────────────


@dataclass
class ToolResult:
    """The result of executing a tool call.

    Attributes:
        tool_call_id: Matches the tool call's ID from the model.
        name: The tool name.
        status: ``success`` or ``error``.
        output: Text output (or error message).
        truncated: Whether the output was truncated to the cap.
        error_code: For ``error`` status, a machine-readable code.
        decision_class: Class assigned by the decision classifier (TD-702).
        checkpoint_commit: SHA of the checkpoint commit for this call
            (TD-705); ``None`` for reads and non-checkpointed writes.
        checkpoint_notice: One-time degradation notice from the
            checkpointer, surfaced to the user as an event (TD-705).
        memory_notice: One-time notice that a memory write has no
            commit (non-git workspace, TD-2104).
        diff: Unified diff of what the write changed (TD-604), for
            display; ``None`` for reads and non-diffable writes.
    """

    tool_call_id: str
    name: str
    status: Literal["success", "error"]
    output: str
    truncated: bool = False
    error_code: str | None = None
    # Decision class assigned by the classifier chokepoint (TD-702).
    decision_class: DecisionClass | None = None
    checkpoint_commit: str | None = None
    checkpoint_notice: Notice | None = None
    memory_notice: Notice | None = None
    diff: str | None = None


class HandlerRefusal(Exception):
    """The handler refused after classification, with a typed error code.

    Used by desktop computer-use (focus mismatch, kill-switch, E20) so
    dispatch can return ``error_code`` without treating the refusal as a
    crashed handler.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ValidationError:
    """Arguments failed validation against the tool's schema.

    Returned to the model so it can correct itself.
    """

    tool_call_id: str
    name: str
    message: str


# ── Truncation ──────────────────────────────────────────────────────────

_TRUNCATION_MARKER = "\n\n┈─[truncated — results exceed output cap]─┈"


def truncate_output(output: str, max_chars: int) -> tuple[str, bool]:
    """Truncate *output* to *max_chars* with a visible truncation marker.

    Returns the (possibly truncated) text and a ``truncated`` flag.
    """
    if not max_chars or len(output) <= max_chars:
        return output, False
    truncated = output[: max_chars - len(_TRUNCATION_MARKER)]
    return truncated + _TRUNCATION_MARKER, True
