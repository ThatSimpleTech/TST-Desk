"""The plan that ends read-only plan mode (TD-5123).

``exit_plan_mode`` parks its turn on the plan the way ``ask_user`` parks
on a question (TD-5121): the session goes to ``awaiting_approval``, the
``plan_review`` event puts the plan on every attached window and phone,
and the call waits on a session-owned future (no viewer owns it, prime
directive §2.5) until ``review_plan``, Stop, cancel or the approval
timeout. ``plan_reviewed`` then logs how it ended.

Approving ends plan mode and pins the plan: the session keeps it, the
``plan_pinned`` event shows it in the Plan pane, and every compaction
carries it into its summary. Rejecting keeps plan mode on and hands the
person's feedback to the model. Nothing here decides for the person:
skip-all releases approvals, never a plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .protocol import (
    DaemonEvent,
    PlanOutcomeKind,
    PlanPinned,
    PlanReview,
    PlanReviewed,
    ReviewPlan,
    build_error,
)
from .task_list import carry_text as task_carry_text

if TYPE_CHECKING:
    from .session import Session

WAITING_REASON = "waiting for your plan review"


@dataclass(frozen=True)
class PlanReply:
    """How a parked plan review was resolved."""

    outcome: PlanOutcomeKind
    plan: str = ""
    edited: bool = False
    feedback: str = ""


@dataclass
class PendingPlan:
    """One ``exit_plan_mode`` call waiting for the person."""

    future: asyncio.Future[PlanReply]
    plan: str


async def review(session: Session, tool_call_id: str, plan: str) -> PlanReply:
    """Park *plan* on *session* and wait for the person's review.

    Logs ``plan_review``, then ``plan_reviewed`` however it ends. On
    approval, plan mode is off and the plan pinned before that event, so
    a window that hears of it already has the new ``tier_state``.
    """
    fut: asyncio.Future[PlanReply] = asyncio.get_running_loop().create_future()
    # Stop or cancel may land before the review parks; it ends at once.
    if session.cancel_requested:
        fut.set_result(PlanReply("cancelled"))
    elif session.stop_turn_requested:
        fut.set_result(PlanReply("stopped"))
    session.pending_plans[tool_call_id] = PendingPlan(fut, plan)
    try:
        await session.event_log.add(
            PlanReview(session_id=session.id, tool_call_id=tool_call_id, plan=plan, seq=1)
        )
        await session.enter_awaiting(WAITING_REASON)
        reply = await _wait(fut, session.policy.approval_timeout_seconds)
    finally:
        session.pending_plans.pop(tool_call_id, None)
        await session.leave_awaiting("plan reviewed")
    if reply.outcome == "approved":
        await end_plan_mode(session)
        await pin_plan(session, reply.plan)
    await session.event_log.add(
        PlanReviewed(
            session_id=session.id,
            tool_call_id=tool_call_id,
            outcome=reply.outcome,
            plan=reply.plan,
            edited=reply.edited,
            feedback=reply.feedback,
            seq=1,
        )
    )
    return reply


async def _wait(fut: asyncio.Future[PlanReply], limit: float | None) -> PlanReply:
    if limit is None:
        return await fut
    try:
        # Shielded so expiry leaves the future alone: a late review then
        # finds it gone from the map and is refused, never half-applied.
        return await asyncio.wait_for(asyncio.shield(fut), limit)
    except TimeoutError:
        return PlanReply("timed_out")


def plan_review_text(event: PlanReview) -> str:
    """One line for a notification: the plan's first line, markup trimmed."""
    first = next((line.strip() for line in event.plan.splitlines() if line.strip()), "")
    first = first.lstrip("#*- ").strip()
    return first[:117] + "..." if len(first) > 120 else first


def resolve_all(session: Session, outcome: PlanOutcomeKind) -> None:
    """End every parked plan review with *outcome* (Stop, cancel)."""
    for pending in session.pending_plans.values():
        if not pending.future.done():
            pending.future.set_result(PlanReply(outcome))


def answer(session: Session, msg: ReviewPlan) -> str | None:
    """Apply the person's ``review_plan``. An error frame, or ``None``."""
    pending = session.pending_plans.get(msg.tool_call_id)
    if pending is None or pending.future.done():
        return build_error(
            "no_pending_plan",
            f"No plan {msg.tool_call_id!r} is waiting in session {msg.session_id!r}",
            session_id=msg.session_id,
            in_reply_to=msg.type,
        )
    feedback = msg.feedback.strip()
    if msg.decision == "reject":
        pending.future.set_result(PlanReply("rejected", feedback=feedback))
        return None
    edited = msg.plan is not None and msg.plan.strip() != pending.plan.strip()
    plan = msg.plan.strip() if msg.plan is not None and edited else pending.plan
    if not plan:
        return build_error(
            "bad_plan",
            "An approved plan can't be empty. Edit it back in, or reject it.",
            session_id=msg.session_id,
            in_reply_to=msg.type,
        )
    pending.future.set_result(PlanReply("approved", plan=plan, edited=edited, feedback=feedback))
    return None


async def end_plan_mode(session: Session) -> None:
    """Turn plan mode off and tell every window (``tier_state``)."""
    # Imported here: local_worker imports session, which imports this module.
    from .local_worker import tier_state_event

    router = session.router
    if router is None:
        return
    router.set_plan(False)
    if session.config is not None:
        await session.event_log.add(tier_state_event(session, session.config))


async def pin_plan(session: Session, plan: str) -> None:
    """Make *plan* the session's pinned plan and log it."""
    session.pinned_plan = plan
    await session.event_log.add(PlanPinned(session_id=session.id, plan=plan, seq=1))


def restore_pinned_plan(session: Session, events: Sequence[DaemonEvent]) -> None:
    """On revive, the pinned plan is the newest ``plan_pinned`` event's."""
    newest = next((e for e in reversed(events) if isinstance(e, PlanPinned)), None)
    if newest is not None:
        session.pinned_plan = newest.plan


async def keep_pinned_plan_visible(session: Session) -> None:
    """Log the pinned plan again when the log window no longer has it.

    The newest ``plan_pinned`` event is the plan's only copy on disk, as
    the newest ``task_list`` is the list's (TD-5122): a restart reads it
    back and the next compaction carries it, so a rotation that drops it
    puts it back.
    """
    if not session.pinned_plan:
        return
    if any(isinstance(e, PlanPinned) for e in session.event_log.all_events):
        return
    await session.event_log.add(PlanPinned(session_id=session.id, plan=session.pinned_plan, seq=1))


def plan_carry_text(plan: str) -> str:
    """What a compaction carries for the pinned *plan*; empty when there is none."""
    if not plan:
        return ""
    return (
        "The plan the user approved (pinned; carry it out unless they change it):\n" + plan.strip()
    )


def carry_for(session: Session) -> str:
    """Everything a compaction carries for *session*: the pinned plan, then the task list."""
    parts = (plan_carry_text(session.pinned_plan), task_carry_text(session.task_list))
    return "\n\n".join(p for p in parts if p)


def format_reply(reply: PlanReply, *, still_planning: bool) -> str:
    """The tool result text the model reads for an approved or rejected plan.

    *still_planning* is whether plan mode is on as the call returns: the
    person may have turned it off from the composer while the plan waited.
    """
    said = f"\n\nThe user said: {reply.feedback}" if reply.feedback else ""
    if reply.outcome == "rejected":
        if not still_planning:
            return (
                "The user rejected the plan, so don't carry it out. Plan mode is off now; "
                "go on as the user asks." + said
            )
        return (
            "The user rejected the plan. Plan mode is still on: nothing may change yet. "
            "Revise the plan (research more if you need to) and call exit_plan_mode again." + said
        )
    how = "edited and approved" if reply.edited else "approved"
    return (
        f"The user {how} the plan. Plan mode is off: carry it out now. It is pinned, so it "
        f"stays with you through compaction.{said}\n\nThe approved plan:\n{reply.plan}"
    )


UNREVIEWED: dict[PlanOutcomeKind, tuple[str, str]] = {
    "stopped": ("plan_review_stopped", "The user stopped the turn before reviewing the plan."),
    "cancelled": ("plan_review_cancelled", "The session ended before the user reviewed the plan."),
    "timed_out": (
        "plan_review_timed_out",
        "No review came before the approval timeout. Plan mode is still on; end your turn "
        "with the plan in your reply so the user can review it later.",
    ),
}
