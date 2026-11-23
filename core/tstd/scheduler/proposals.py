"""A scheduled job the agent proposes and only the person saves (TD-5263).

``schedule_propose`` writes nothing. It parks a draft on the session and
puts a card in the chat; the job exists only once the person presses Save,
which sends the ordinary ``save_job`` from their window. A tool cannot
send that message, so no approval setting can make the agent schedule
itself: skip-all acts on approvals, and there is no approval here to skip.

The draft is narrow on purpose. The model gives the instruction and when;
the workspace is the calling session's project, and the model, the
engine, where the result is delivered and every other field are the
person's to set.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..protocol import ScheduleProposal, ScheduleProposalDecision
from .models import (
    JobValidationError,
    normalize_cadence,
    normalize_next_run,
    normalize_timezone,
    reject_secrets,
)
from .schedule import next_run_after

if TYPE_CHECKING:
    from ..session import Session

MAX_OPEN = 3
MIN_INTERVAL = timedelta(minutes=15)
INSTRUCTION_CAP = 4000
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class ScheduleRefusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ParkedSchedule:
    """One open proposal. Held in memory on its session; gone after a restart."""

    proposal_id: str
    tool_call_id: str
    workspace: str
    instruction: str
    cadence: str | None
    next_run: str | None
    timezone: str | None


def _refuse_unattended(session: Session) -> None:
    # Imported here: tools -> scheduler would cycle at import time.
    from ..tools.ask_user import denies_every_ask

    if session.delegate_depth > 0:
        raise ScheduleRefusal(
            "schedule_unavailable", "A delegated task cannot propose a scheduled job."
        )
    if session.autonomy or session.scheduled_run or denies_every_ask(session):
        raise ScheduleRefusal(
            "schedule_unavailable",
            "An unattended run cannot propose a scheduled job: nobody is there to save it.",
        )


def _too_often(cadence: str, tz: str | None, now: datetime) -> bool:
    """Whether *cadence* ever fires twice within ``MIN_INTERVAL``."""
    when = now
    for _ in range(8):
        following = next_run_after(cadence, when, tz)
        if when is not now and following - when < MIN_INTERVAL:
            return True
        when = following
    return False


def draft_from(
    session: Session,
    *,
    instruction: str,
    cadence: str | None,
    run_at: str | None,
    timezone: str | None,
    tool_call_id: str,
    now: datetime | None = None,
) -> ParkedSchedule:
    """Check the ask and build the proposal. Raises ScheduleRefusal; writes nothing."""
    _refuse_unattended(session)
    if len(session.schedule_proposals) >= MAX_OPEN:
        raise ScheduleRefusal(
            "schedule_limit",
            f"{MAX_OPEN} proposals are already waiting for the user in this chat. "
            "Let them answer those first.",
        )
    text = instruction.strip()
    if not text or len(text) > INSTRUCTION_CAP:
        raise ScheduleRefusal(
            "bad_request", f"The instruction must be 1 to {INSTRUCTION_CAP} characters."
        )
    has_cadence = bool(cadence and cadence.strip())
    has_run_at = bool(run_at and run_at.strip())
    if has_cadence == has_run_at:
        raise ScheduleRefusal("bad_request", "Give cadence or run_at, one of them.")
    moment = now or datetime.now(UTC)
    try:
        reject_secrets(text, "instruction")
        zone = normalize_timezone(timezone)
        rule = normalize_cadence(cadence or "") if has_cadence else None
        once = normalize_next_run(run_at or "") if has_run_at else None
        if rule is not None and _too_often(rule, zone, moment):
            raise ScheduleRefusal(
                "schedule_too_often",
                "A proposed job may run at most every 15 minutes. The user can shorten it "
                "by hand afterwards.",
            )
    except JobValidationError as exc:
        raise ScheduleRefusal("bad_request", str(exc)) from None
    return ParkedSchedule(
        proposal_id=f"sp_{secrets.token_hex(6)}",
        tool_call_id=tool_call_id,
        # The project, not a worktree copy of it, and never the model's choice.
        workspace=session.project_path,
        instruction=text,
        cadence=rule,
        next_run=once,
        timezone=zone,
    )


def next_runs(parked: ParkedSchedule, now: datetime, count: int = 3) -> list[str]:
    """When the job would next run, for the card."""
    if parked.cadence is None:
        return [parked.next_run] if parked.next_run else []
    out: list[str] = []
    when = now
    for _ in range(count):
        when = next_run_after(parked.cadence, when, parked.timezone)
        out.append(when.astimezone(UTC).strftime(_TIME))
    return out


async def propose(session: Session, parked: ParkedSchedule) -> None:
    """Park *parked* and put its card in the session's log."""
    session.schedule_proposals[parked.proposal_id] = parked
    await session.event_log.add(
        ScheduleProposal(
            session_id=session.id,
            seq=1,
            proposal_id=parked.proposal_id,
            tool_call_id=parked.tool_call_id,
            workspace=parked.workspace,
            instruction=parked.instruction,
            cadence=parked.cadence,
            next_run=parked.next_run,
            timezone=parked.timezone,
            next_runs=next_runs(parked, datetime.now(UTC)),
        )
    )


async def settle(
    session: Session, proposal_id: str, decision: str, job_id: str | None = None
) -> ParkedSchedule | None:
    """Close an open proposal and log how. None when it is not open."""
    parked = session.schedule_proposals.pop(proposal_id, None)
    if parked is None:
        return None
    await session.event_log.add(
        ScheduleProposalDecision(
            session_id=session.id,
            seq=1,
            proposal_id=proposal_id,
            decision=decision,  # type: ignore[arg-type]
            job_id=job_id,
        )
    )
    return parked


async def expire_restored(session: Session, events: Sequence[object]) -> int:
    """Log ``expired`` for every proposal a restart dropped. Returns how many.

    Proposals live in memory. Left open in the log, the replay would bring
    their cards back with Save live and every press would find nothing.
    """
    closed = {e.proposal_id for e in events if isinstance(e, ScheduleProposalDecision)}
    dropped = [
        e.proposal_id
        for e in events
        if isinstance(e, ScheduleProposal)
        and e.proposal_id not in closed
        and e.proposal_id not in session.schedule_proposals
    ]
    for proposal_id in dropped:
        await session.event_log.add(
            ScheduleProposalDecision(
                session_id=session.id, seq=1, proposal_id=proposal_id, decision="expired"
            )
        )
    return len(dropped)
