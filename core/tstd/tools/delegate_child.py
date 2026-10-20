"""What a delegated child is: its kind, tools, prompt, summary cap and transcript.

Split out of ``delegate.py`` (TD-5117) so the child loop stays readable.
Two kinds: ``worker`` reads, writes and runs shell (TD-4602); ``research``
only reads files and the web. Either returns a summary capped in tokens.

The child's messages never reach the parent model: the parent gets the
capped summary as the ``delegate`` result. The steps recorded by
:class:`TranscriptRecorder` ride on that call's ``tool_result`` event
instead, so the user can still read what the child did. Each step is cut
to ``STEP_CHAR_CAP`` characters and the whole transcript to about
``TRANSCRIPT_CHAR_BUDGET``, because the event is stored and replayed with
the session.
"""

from __future__ import annotations

from typing import Literal

from ..context.tokens import HeuristicTokenCounter, TokenCounter
from ..protocol import WorkerStep
from .results import HandlerRefusal

# What the parent gets back, in tokens of the worker tier's counter. The
# heuristic counts four characters a token, so this keeps TD-4602's old
# 2000-character cap for local models.
SUMMARY_TOKEN_CAP = 500

STEP_CHAR_CAP = 4000
TRANSCRIPT_CHAR_BUDGET = 60_000
CUT_NOTE = "Transcript cut here: the later steps are not shown."

DelegateKind = Literal["worker", "research"]
DELEGATE_KINDS: tuple[DelegateKind, ...] = ("worker", "research")

WORKER_CHILD_TOOLS: tuple[str, ...] = (
    "fs_read",
    "fs_list",
    "fs_grep",
    "fs_write",
    "fs_edit",
    "shell",
)

#: The read-only research child (TD-5117): file reads and the web, nothing
#: that writes, runs a command or reaches an MCP server.
RESEARCH_CHILD_TOOLS: tuple[str, ...] = (
    "fs_read",
    "fs_list",
    "fs_grep",
    "web_search",
    "web_fetch",
)

#: Added to either kind when config offers ``consult`` to the worker tier
#: (TD-5119). It reads and writes nothing; it asks the brain or validator.
CONSULT_TOOL_NAME = "consult"

CHILD_TOOLS: dict[DelegateKind, tuple[str, ...]] = {
    "worker": WORKER_CHILD_TOOLS,
    "research": RESEARCH_CHILD_TOOLS,
}

StepKind = Literal["task", "reply", "tool_call", "tool_result", "note"]


def resolve_kind(kind: str | None, *, plan_mode: bool) -> DelegateKind:
    """The kind a call runs as: research by default in plan mode.

    In read-only plan mode (TD-5123) the classifier refuses an explicit
    ``kind: worker`` before this runs, and the shared classifier refuses
    any write a child makes, so only the default needs moving here.
    """
    if kind is None:
        return "research" if plan_mode else "worker"
    if kind not in DELEGATE_KINDS:
        raise HandlerRefusal("delegate_kind", f"Error: unknown delegate kind {kind!r}")
    return "research" if kind == "research" else "worker"


def child_preamble(kind: DelegateKind, tools: list[str]) -> str:
    """The instruction ahead of the task, naming the tools the child has."""
    if kind == "research":
        return (
            "You are a read-only research worker. Your tools are "
            f"{', '.join(tools) or 'none'}; you cannot write files, run "
            "commands or call delegate. Investigate the task, then reply "
            "with what you found, citing file paths with line numbers or "
            f"URLs, in under {SUMMARY_TOKEN_CAP} tokens and with no further "
            "tool calls."
        )
    return (
        "You are a one-level worker. You cannot call delegate. "
        "Complete the task, then reply with a concise summary, in under "
        f"{SUMMARY_TOKEN_CAP} tokens, and no further tool calls."
    )


def summary_marker(limit: int) -> str:
    """The note appended to a summary cut at *limit* tokens."""
    return f"\n… [truncated: worker summary capped at {limit} tokens]"


def cap_worker_summary(
    text: str,
    limit: int = SUMMARY_TOKEN_CAP,
    counter: TokenCounter | None = None,
) -> str:
    """Return *text* cut to at most *limit* tokens, marker included.

    *counter* is the worker tier's; the heuristic when not given. The cut
    keeps the longest prefix that still fits beside the marker.
    """
    return cap_tokens(text, limit, summary_marker(limit), counter)


def cap_tokens(
    text: str,
    limit: int,
    marker: str,
    counter: TokenCounter | None = None,
) -> str:
    """Return *text* cut to at most *limit* tokens, *marker* included.

    Shared by the delegate summary and the ``consult`` answer (TD-5119).
    """
    counter = counter if counter is not None else HeuristicTokenCounter()
    if counter.count(text).count <= limit:
        return text
    budget = limit - counter.count(marker).count
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if counter.count(text[:mid]).count <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low] + marker


class TranscriptRecorder:
    """Appends capped :class:`WorkerStep` rows to *sink*; a ``None`` sink records nothing."""

    def __init__(self, sink: list[WorkerStep] | None) -> None:
        self._sink = sink
        self._used = 0
        self._full = False

    def add(
        self,
        kind: StepKind,
        text: str,
        *,
        tool: str | None = None,
        status: Literal["success", "error"] | None = None,
    ) -> None:
        """Record one step, or one closing note once the budget is spent."""
        if self._sink is None or self._full:
            return
        if self._used >= TRANSCRIPT_CHAR_BUDGET:
            self._sink.append(WorkerStep(kind="note", text=CUT_NOTE))
            self._full = True
            return
        kept = text[:STEP_CHAR_CAP]
        self._used += len(kept)
        self._sink.append(
            WorkerStep(
                kind=kind,
                text=kept,
                tool=tool,
                status=status,
                truncated=len(kept) < len(text),
            )
        )
