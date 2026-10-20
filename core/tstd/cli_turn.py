"""One turn of ``tst run``, followed to its end (TD-3101, TD-5111).

:func:`stream_turn` reads a session's events until its ``turn_complete``
and returns a :class:`TurnOutcome`: the reply, the turn's cost, and how
it ended. The output mode decides what reaches stdout on the way:

- ``text`` writes assistant text as it arrives and asks for approvals on
  a TTY, as ``tst run`` always has.
- ``json`` writes nothing during the turn; the caller prints the outcome.
- ``ndjson`` writes each of the session's protocol events, one per line,
  as the daemon sent it.

Errors, refusals and notices go to stderr in every mode, so stdout holds
only what the mode promises.

The machine modes never prompt: an approval request ends the run with
``approval_needed`` and the turn waits for the window, the same as a
text run without a TTY. Under ``--on-ask deny-and-continue`` the daemon
refuses instead of asking (TD-5118), so no request reaches the run.

A run that sent ``run_permissions`` checks that its ``user_turn`` carries
them back. A daemon that does not (one older than the CLI) would run the
turn without them, a ``--deny`` included, so the run stops that turn and
fails with ``run_permissions_unsupported``.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .cli import (
    RETRACT_NOTICE,
    _print_error,
    _recv_event,
    _send,
    _turn_timeout_secs,
    config_trust_notice,
)
from .cli_approvals import prompt_approval
from .logging import REDACTED

Status = Literal["success", "refused", "approval_needed", "error"]
Output = Literal["text", "json", "ndjson"]
OUTPUTS: tuple[Output, ...] = ("text", "json", "ndjson")

EXIT_OK = 0
EXIT_ERROR = 1
#: argparse's own code for a bad command line; ``tst run`` uses it for a
#: missing message too.
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_APPROVAL_NEEDED = 4

EXIT_CODES: dict[Status, int] = {
    "success": EXIT_OK,
    "error": EXIT_ERROR,
    "refused": EXIT_REFUSED,
    "approval_needed": EXIT_APPROVAL_NEEDED,
}

#: Tool-result codes for a call a gate refused: the workspace boundary, a
#: ``never`` policy rule, or a person answering Deny. A handler's own
#: failure is not a refusal.
REFUSAL_CODES = frozenset({"boundary_refusal", "policy_denied", "approval_denied"})

APPROVAL_NEEDED = "approval_needed"

RUN_PERMISSIONS_UNSUPPORTED = "run_permissions_unsupported"
RUN_PERMISSIONS_UNSUPPORTED_MESSAGE = (
    "The TST Desk daemon did not apply --allow, --deny or --on-ask to this turn "
    "(it is older than this tst), so the turn was stopped. Restart TST Desk, then run again."
)


@dataclass
class TurnOutcome:
    """How one ``tst run`` turn ended, and what it produced."""

    status: Status
    session_id: str | None = None
    reply: str = ""
    cost: float = 0.0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def as_json(self) -> dict[str, Any]:
        """The ``--output json`` object: reply, session id, cost, status, error."""
        error = None
        if self.error_code is not None:
            error = {"code": self.error_code, "message": self.error_message or self.error_code}
        return {
            "session_id": self.session_id,
            "status": self.status,
            "reply": self.reply,
            "cost": self.cost,
            "error": error,
        }


def is_sent_message(logged: str, sent: str) -> bool:
    """Whether a ``user_turn``'s *logged* content is the message *sent*.

    The daemon redacts every event before it is stored or broadcast, so a
    message with secret-shaped text comes back with ``[REDACTED]`` in its
    place. Each one stands for at least one character of *sent*: matching
    against the marker, rather than redacting *sent* here, holds when the
    daemon's patterns differ from this install's.
    """
    if logged == sent:
        return True
    if REDACTED not in logged:
        return False
    head, *middle, tail = logged.split(REDACTED)
    if not sent.startswith(head):
        return False
    pos = len(head)
    # Earliest placement of each literal piece leaves the most room for the
    # rest; the +1 keeps every redacted run non-empty.
    for piece in middle:
        found = sent.find(piece, pos + 1)
        if found < 0:
            return False
        pos = found + len(piece)
    return len(sent) - len(tail) >= pos + 1 and sent.endswith(tail)


@dataclass
class _Refusal:
    tool: str
    code: str


@dataclass
class _TurnReader:
    """Folds one session's events into a :class:`TurnOutcome`."""

    session_id: str
    output: Output
    # False until this run's own ``user_turn`` is seen: in an existing
    # session, a turn already queued ahead of ours is not ours to report.
    ours: bool
    content: str | None
    # TD-5118: this run sent ``run_permissions``; its user_turn must echo them.
    permissions_sent: bool = False
    turn_checked: bool = False
    # ``(response_id, text)`` per delta, so a withdrawn response can be
    # taken back out of the reply (TD-5159).
    reply: list[tuple[str | None, str]] = field(default_factory=list)
    cost: float = 0.0
    refusals: list[_Refusal] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    line_open: bool = False

    def outcome(
        self, status: Status, code: str | None = None, message: str | None = None
    ) -> TurnOutcome:
        return TurnOutcome(
            status=status,
            session_id=self.session_id,
            reply="".join(text for _, text in self.reply),
            cost=self.cost,
            error_code=code,
            error_message=message,
        )

    def end_line(self) -> None:
        """Close an open line of assistant text before anything else prints."""
        if self.line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.line_open = False

    def emit(self, event: dict[str, Any]) -> None:
        if self.output == "ndjson":
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()

    async def feed(self, ws: Any, event: dict[str, Any]) -> TurnOutcome | None:
        """Take one event. Returns the outcome once the turn has ended."""
        typ = event.get("type")
        if typ == "error":
            self.emit(event)
            self.end_line()
            _print_error(event)
            code = str(event.get("code") or "error")
            return self.outcome("error", code, str(event.get("message") or code))
        if event.get("session_id") != self.session_id:
            return None
        if not self.ours:
            if typ != "user_turn" or self.content is None:
                return None
            if not is_sent_message(str(event.get("content") or ""), self.content):
                return None
            self.ours = True
        self.emit(event)
        if typ == "user_turn" and not self.turn_checked:
            self.turn_checked = True
            if self.permissions_sent and event.get("run_permissions") is None:
                return await self._unsupported(ws)
        if typ == "boundary_update":
            notice = config_trust_notice(event)
            if notice is not None:
                self.end_line()
                print(notice, file=sys.stderr)
            return None
        if typ == "approval_request":
            return await self._approval(ws, event)
        if typ == "tool_call":
            self.tool_names[str(event.get("tool_call_id"))] = str(event.get("name") or "tool")
            return None
        if typ == "tool_result":
            self._note_refusal(event)
            return None
        if typ == "cost_update":
            self.cost = float(event.get("turn_cost") or 0.0)
            return None
        if typ == "assistant_retract":
            self._withdraw(event)
            return None
        if typ == "assistant_delta":
            delta = str(event.get("delta") or "")
            rid = event.get("response_id")
            self.reply.append((rid if isinstance(rid, str) else None, delta))
            if self.output == "text":
                sys.stdout.write(delta)
                sys.stdout.flush()
                self.line_open = True
            return None
        if typ == "turn_complete":
            return self._complete(event)
        return None

    async def _unsupported(self, ws: Any) -> TurnOutcome:
        """The daemon ignored this run's permissions: stop the turn, don't trust it."""
        self.end_line()
        await _send(ws, {"type": "stop_turn", "session_id": self.session_id})
        print(
            f"{RUN_PERMISSIONS_UNSUPPORTED}: {RUN_PERMISSIONS_UNSUPPORTED_MESSAGE}", file=sys.stderr
        )
        return self.outcome(
            "error", RUN_PERMISSIONS_UNSUPPORTED, RUN_PERMISSIONS_UNSUPPORTED_MESSAGE
        )

    def _withdraw(self, event: dict[str, Any]) -> None:
        """The provider stalled partway through a response; the daemon asks again.

        The response leaves the reply. Text mode already wrote it, and a
        pipe cannot take it back, so stderr says the retry follows.
        """
        rid = event.get("response_id")
        self.reply = [(r, text) for r, text in self.reply if r is None or r != rid]
        if self.output == "text":
            self.end_line()
            print(RETRACT_NOTICE, file=sys.stderr)

    async def _approval(self, ws: Any, event: dict[str, Any]) -> TurnOutcome | None:
        self.end_line()
        summary = str(event.get("summary") or event.get("tool_name") or "a tool call")
        waiting = f"{summary} is waiting for approval; answer it in the TST Desk window"
        if self.output != "text":
            print(f"{APPROVAL_NEEDED}: {waiting}", file=sys.stderr)
            return self.outcome("approval_needed", APPROVAL_NEEDED, waiting)
        reply = await prompt_approval(event, self.session_id)
        if reply is None:
            return self.outcome("approval_needed", APPROVAL_NEEDED, waiting)
        await _send(ws, reply)
        return None

    def _note_refusal(self, event: dict[str, Any]) -> None:
        code = event.get("error_code")
        if event.get("status") != "error" or code not in REFUSAL_CODES:
            return
        tool = self.tool_names.get(str(event.get("tool_call_id")), "tool")
        self.refusals.append(_Refusal(tool=tool, code=str(code)))
        self.end_line()
        print(f"refused: {tool} ({code})", file=sys.stderr)

    def _complete(self, event: dict[str, Any]) -> TurnOutcome:
        self.end_line()
        self.cost = float(event.get("cost") or 0.0)
        if event.get("failed"):
            code = str(event.get("error_code") or "turn_failed")
            print(code, file=sys.stderr)
            return self.outcome("error", code, code)
        if self.refusals:
            listed = ", ".join(f"{r.tool} ({r.code})" for r in self.refusals)
            return self.outcome("refused", self.refusals[0].code, f"refused: {listed}")
        return self.outcome("success")


async def stream_turn(
    ws: Any,
    session_id: str,
    data_dir: Path | None = None,
    budget: float | None = None,
    *,
    output: Output = "text",
    content: str | None = None,
    run_permissions: bool = False,
) -> TurnOutcome:
    """Follow *session_id* until its turn completes, fails or needs approval.

    *content* is the message this run sent into a session that already
    existed: events before the ``user_turn`` carrying it (redacted as the
    daemon logs it; see :func:`is_sent_message`) belong to an earlier turn
    and are skipped. ``None`` (a session this run opened) takes every
    event of the session. *run_permissions* says this run sent some, so
    its ``user_turn`` must carry them back.
    """
    reader = _TurnReader(
        session_id,
        output,
        ours=content is None,
        content=content,
        permissions_sent=run_permissions,
    )
    deadline = time.monotonic() + _turn_timeout_secs(data_dir, budget)
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            event = await _recv_event(ws, remaining)
        except TimeoutError:
            break
        outcome = await reader.feed(ws, event)
        if outcome is not None:
            return outcome
    reader.end_line()
    message = "timed out waiting for turn_complete"
    print(message, file=sys.stderr)
    return reader.outcome("error", "timeout", message)
