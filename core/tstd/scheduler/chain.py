"""Start one job after another ends ok (TD-3817).

``then`` is another job's id. A scheduled run that ends ok, including one
that succeeded on a later try, and Run now, start that job once. The
child's cadence and next run stay where they are: this fire is not its
slot. A failure, a skipped slot, and a run parked on an approval card do
not start it. Approving that card later does not start it either: the
park settles on its own path, and only a runner success walks ``then``.
A chain may be longer than one link. The save refuses a cycle. A run
stops after five follow-ons so a long chain cannot hold the tick open.

The child's own retry and grace apply to its slot, not to this fire.
That matches Run now. This module imports the runner at load time. The
runner imports this module only from the function that runs after an ok
result, so the two modules do not load each other.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..logging import get_logger
from .history import append_run
from .models import Job, JobValidationError
from .park import ParkHook, park_run
from .runner import Deliver, InFlight, TurnFn, TurnResult, _turn_result
from .schedule import record_run
from .store import get_job, save_job

log = get_logger("tstd.scheduler.chain")

# Five follow-on starts from the run that succeeded. The sixth is not
# started and is not recorded: the link is still valid, it is just past
# what one fire will walk.
CHAIN_DEPTH = 5
MISSED_PAUSED = "paused"
MISSED_RUNNING = "already running"


def chain_note(parent_id: str) -> str:
    """History note that names the job whose ok run started this one."""
    return f"after {parent_id}"


def require_chain(jobs: list[Job], job: Job) -> None:
    """Refuse a missing follow-on and a cycle. Does not write.

    Called when the user sets ``then``. A receipt stamp does not call
    this: the runner must be able to save a row whose follow-on was
    removed in the same moment. Pause omits the field and does not call
    this either, so a hand-edited loop can still be paused.
    """
    target = job.then
    if target is None:
        return
    links: dict[str, str | None] = {item.id: item.then for item in jobs}
    links[job.id] = target
    if target not in links:
        raise JobValidationError(f"Then: no job {target!r}")
    path = _cycle(links, job.id)
    if path is not None:
        raise JobValidationError(" → ".join(path))


def _cycle(links: Mapping[str, str | None], start: str) -> list[str] | None:
    seen: list[str] = []
    current: str | None = start
    while current is not None:
        if current in seen:
            return [*seen[seen.index(current) :], current]
        seen.append(current)
        current = links.get(current)
    return None


async def follow_chain(
    data_dir: Path,
    parent: Job,
    now: datetime,
    *,
    depth: int,
    run_turn: TurnFn,
    deliver: Deliver,
    in_flight: InFlight | None,
    on_parked: ParkHook | None,
) -> None:
    """Start ``parent.then`` once, then its follow-on, up to the depth cap.

    ``depth`` is how many follow-ons have already started from the run
    that succeeded. Zero is that run itself.
    """
    if depth >= CHAIN_DEPTH or not parent.then:
        return
    child = await asyncio.to_thread(get_job, data_dir, parent.then)
    if child is None:
        return
    note = chain_note(parent.id)
    if child.paused:
        await _record_miss(data_dir, child, now, MISSED_PAUSED, note, deliver)
        return
    if in_flight is not None and not in_flight.claim(child.id):
        await _record_miss(data_dir, child, now, MISSED_RUNNING, note, deliver)
        return
    try:
        await _run_linked(
            data_dir,
            child,
            now,
            note=note,
            depth=depth + 1,
            run_turn=run_turn,
            deliver=deliver,
            in_flight=in_flight,
            on_parked=on_parked,
        )
    finally:
        if in_flight is not None:
            in_flight.release(child.id)


async def _run_linked(
    data_dir: Path,
    child: Job,
    now: datetime,
    *,
    note: str,
    depth: int,
    run_turn: TurnFn,
    deliver: Deliver,
    in_flight: InFlight | None,
    on_parked: ParkHook | None,
) -> None:
    """One chained fire. The schedule is not moved. Ok continues the chain."""
    result = await _turn_result(child, run_turn)
    fresh = await asyncio.to_thread(get_job, data_dir, child.id)
    if fresh is None:
        # Deleted during the turn. Do not write the row back.
        return
    if result.waiting and result.session_id:
        # A parked follow-on is not an ok end, so its own ``then`` waits.
        started = await park_run(
            data_dir,
            fresh,
            now,
            session_id=result.session_id,
            summary=result.summary,
            advance=False,
            trigger="chained",
            scheduled_for=None,
            deliver=deliver,
            note=note,
        )
        if on_parked is not None:
            on_parked(fresh.id, result.session_id, started)
        return
    if result.waiting:
        result = TurnResult(summary=result.summary or "a tool call", ok=False)
    stamped = record_run(
        fresh,
        now,
        status="ok" if result.ok else "failed",
        summary=result.summary,
        session_id=result.session_id,
    )
    await asyncio.to_thread(save_job, data_dir, stamped)
    await asyncio.to_thread(
        append_run,
        data_dir,
        fresh.id,
        started_at=now,
        scheduled_for=None,
        trigger="chained",
        status="ok" if result.ok else "failed",
        summary=result.summary,
        session_id=result.session_id,
        note=note,
    )
    await deliver(fresh.deliver_to, result.summary)
    if result.ok:
        log.info(
            "scheduled chain continued",
            extra={"extra_fields": {"job_id": fresh.id, "depth": depth}},
        )
        await follow_chain(
            data_dir,
            stamped,
            now,
            depth=depth,
            run_turn=run_turn,
            deliver=deliver,
            in_flight=in_flight,
            on_parked=on_parked,
        )


async def _record_miss(
    data_dir: Path,
    child: Job,
    now: datetime,
    reason: str,
    note: str,
    deliver: Deliver,
) -> None:
    """One missed chained start. The child's slot stays where it is."""
    fresh = await asyncio.to_thread(get_job, data_dir, child.id)
    if fresh is None:
        return
    stamped = record_run(fresh, now, status="missed", summary=reason, session_id=None)
    await asyncio.to_thread(save_job, data_dir, stamped)
    await asyncio.to_thread(
        append_run,
        data_dir,
        fresh.id,
        started_at=now,
        scheduled_for=None,
        trigger="chained",
        status="missed",
        summary=reason,
        session_id=None,
        note=note,
    )
    log.info(
        "scheduled chain missed",
        extra={"extra_fields": {"job_id": fresh.id, "reason": reason}},
    )
    await deliver(fresh.deliver_to, reason)
