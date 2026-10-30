"""Pause an interactive turn that stops making progress.

Skip-all (TD-806) skips the spend, wall-clock, and iteration caps.
Autonomy runs already have circuit breakers (TD-4203) and this guard
does not run for them. A delegate child does not run it either: the
child is not a session the user can resume. An interactive turn is
different: it can call tools forever, say nothing, and write nothing.

Three stops, all of them a pause the user can resume:

- 12 tool calls since the last user-visible reply or file write
  ask one tool-free question. ``CONTINUE`` with a new next step opens
  another window, at most three times. Any other answer is the reply
  to the user: ``DONE``, ``BLOCKED``, or text with no label at all
  ends the turn like a normal tool-free reply (TD-5198). An empty
  answer, a tool call, or a repeated next step pauses.
- The same tool batch three times in a row pauses immediately.
- 52 model calls since the last successful ``fs_write`` or ``fs_edit``
  pause even when the model keeps narrating. A reply resets the tool
  window only. A write resets both counters.

Resume clears the window, the identical-call streak, and the 52-call
budget, then the same turn continues.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

GuardGate = Literal["proceed", "check", "pause"]
CheckKind = Literal["continue", "reply", "pause"]

# A file write is the sign that the turn changed the workspace.
# Shell output is not: the stuck sessions were successful shell calls.
PROGRESS_WRITE_TOOLS = frozenset({"fs_write", "fs_edit"})

TOOL_WINDOW = 12
MAX_AUTO_CONTINUES = 3
MODEL_CALL_BUDGET = 52
IDENTICAL_ROUNDS = 3

# Sent after the "Next: ..." note so the request ends on a user turn (TD-5177).
CONTINUE_INSTRUCTION = "Carry on with that next step."

PROGRESS_CHECK_INSTRUCTION = (
    "Progress check. Tools are off for this reply. "
    "Start your reply with one of these labels and make no tool call:\n"
    "DONE: <the full reply the user is waiting for>\n"
    "CONTINUE: <the single next edit or command, on one line>\n"
    "BLOCKED: <what you need from the user>\n"
    "CONTINUE must name a different next step than the previous check."
)

# One label line, as models really write it: "DONE: x", "**Done:** x",
# "- CONTINUE - x", "BLOCKED" on a line of its own.
_LABEL_LINE = re.compile(
    r"^[\s>*_`#-]*(DONE|CONTINUE|BLOCKED)[*_`]*\s*(?:[:\u2013\u2014-][*_`]*\s*(.*))?$",
    re.IGNORECASE,
)

ToolNote = tuple[str, str, bool]


def tool_note(name: str, arguments: Mapping[str, Any], *, status: str) -> ToolNote:
    """One dispatched call: name, canonical args, and whether it wrote a file."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    wrote = status == "success" and name in PROGRESS_WRITE_TOOLS
    return (name, canonical, wrote)


def pause_reason_for(detail: str) -> str:
    """A session-state reason. The UI matches the ``progress guard:`` prefix."""
    text = " ".join(detail.split()) or "the turn stopped making progress"
    if len(text) > 360:
        text = text[:357] + "..."
    return f"progress guard: {text}"


def interpret_check(text: str, previous_continue: str) -> tuple[CheckKind, str]:
    """Read a tool-free check.

    ``continue`` carries the next step. ``reply`` carries the text the
    user should see: a ``DONE`` or ``BLOCKED`` answer with its label
    removed, or the whole answer when the model wrote no label. Models
    often answer the check with the finished reply or a question and
    skip the label; pausing on that hid a good answer in a banner.
    """
    body = text.strip()
    if not body:
        return "pause", "The progress check returned no answer."
    lines = body.splitlines()
    found = _find_label(lines)
    if found is None:
        return "reply", body
    index, key, tail = found
    after = "\n".join(lines[index + 1 :]).strip()
    if key == "CONTINUE":
        step = f"{tail}\n{after}".strip()
        if not step:
            return "pause", "The progress check named no next step."
        if _same(step, previous_continue):
            return "pause", "The progress check repeated the same next step."
        return "continue", step
    before = "\n".join(line for line in lines[:index] if not _is_echo(line)).strip()
    reply = "\n".join(part for part in (before, tail, after) if part).strip()
    if not reply:
        if key == "BLOCKED":
            return "pause", "The progress check says it is blocked."
        return "pause", "The progress check returned no answer."
    return "reply", reply


def has_label(text: str) -> bool:
    """True when the check answer used one of the three labels."""
    return _find_label(text.strip().splitlines()) is not None


def _find_label(lines: Sequence[str]) -> tuple[int, str, str] | None:
    """The first label line: its index, the label, and the rest of the line."""
    for index, line in enumerate(lines):
        match = _LABEL_LINE.match(line)
        if match:
            return index, match.group(1).upper(), (match.group(2) or "").strip()
    return None


@dataclass
class ProgressGuard:
    """Per-turn counters. One instance per user message."""

    tools_since_progress: int = 0
    model_calls_since_progress: int = 0
    auto_continues: int = 0
    last_continue: str = ""
    last_fingerprint: tuple[tuple[str, str], ...] | None = None
    identical_streak: int = 0
    pause_reason: str = ""

    def note_reply(self) -> None:
        """User-visible text resets the tool window, not the model-call budget.

        A short preamble on every tool call would otherwise keep the
        window at zero forever. The 52-call budget still ends that orbit.
        """
        self.tools_since_progress = 0

    def note_tools(self, calls: Sequence[ToolNote]) -> None:
        """Record one dispatched batch. A successful write resets the budget."""
        if not calls:
            return
        if any(wrote for _name, _args, wrote in calls):
            self.tools_since_progress = 0
            self.model_calls_since_progress = 0
            self.auto_continues = 0
            self.last_continue = ""
            self.last_fingerprint = None
            self.identical_streak = 0
            return
        self.tools_since_progress += len(calls)
        fingerprint = tuple((name, args) for name, args, _wrote in calls)
        if fingerprint == self.last_fingerprint:
            self.identical_streak += 1
        else:
            self.identical_streak = 1
        self.last_fingerprint = fingerprint

    def note_model_call(self) -> None:
        """Count a real model call, not the tool-free progress check."""
        self.model_calls_since_progress += 1

    def note_auto_continue(self, action: str) -> None:
        """A check named a new next step. Open one more tool window."""
        self.auto_continues += 1
        self.last_continue = _norm(action)
        self.tools_since_progress = 0
        self.identical_streak = 0
        self.last_fingerprint = None

    def on_resume(self) -> None:
        """The user let the same turn continue. Start the counters over."""
        self.tools_since_progress = 0
        self.model_calls_since_progress = 0
        self.auto_continues = 0
        self.last_continue = ""
        self.last_fingerprint = None
        self.identical_streak = 0
        self.pause_reason = ""

    def before_model_call(self) -> GuardGate:
        """What to do before the next model call."""
        if self.identical_streak >= IDENTICAL_ROUNDS:
            self.pause_reason = (
                f"progress guard: the same tool call repeated {self.identical_streak} times"
            )
            return "pause"
        if self.model_calls_since_progress >= MODEL_CALL_BUDGET:
            self.pause_reason = (
                f"progress guard: {MODEL_CALL_BUDGET} model calls without a file write"
            )
            return "pause"
        if self.tools_since_progress >= TOOL_WINDOW:
            if self.auto_continues >= MAX_AUTO_CONTINUES:
                self.pause_reason = "progress guard: three checks did not finish the turn"
                return "pause"
            return "check"
        return "proceed"


def _is_echo(line: str) -> bool:
    """A preamble line that only repeats the check's own header."""
    return line.strip(" \t>*_`#-").casefold().startswith("progress check")


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _same(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    return bool(a) and a == b
