"""Background ``delegate`` workers a session owns, for one turn (TD-5299).

``delegate`` with ``background: true`` starts the child as a task and
returns at once; the parent keeps working. A worker belongs to the turn
that started it: the loop does not end the turn while one is alive, and
when one settles its summary reaches the parent model as a note before
its next call. Stop and cancel end every worker, as they end a
foreground child (TD-5195).

This module only keeps the records. The tools package starts the tasks,
so the session can import it without pulling the tools in. Every change
logs one ``worker_jobs`` event with the whole list, which the window
shows; a settled worker also logs ``worker_result`` with its steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from .protocol import WorkerJobInfo, WorkerResult, WorkerState, WorkerStep

#: Settled workers kept in the list beside the live ones.
MAX_SETTLED = 16
#: Snapshot events for progress (calls, spend, last step) are at most this often.
_PROGRESS_SECS = 1.0
_LIVE: frozenset[WorkerState] = frozenset({"queued", "running", "awaiting_approval"})

Emit = Callable[[list[WorkerJobInfo]], Awaitable[None]]
EmitResult = Callable[[WorkerResult], Awaitable[None]]


def _cut(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass
class WorkerJob:
    """One background worker."""

    id: str
    call_id: str
    label: str
    kind: str
    task: str
    agent: str | None = None
    state: WorkerState = "queued"
    stopped: str | None = None
    calls: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    branch: str | None = None
    worktree_path: str | None = None
    summary: str = ""
    steps: list[WorkerStep] = field(default_factory=list)
    delivered: bool = False
    handle: asyncio.Task[None] | None = None
    _shown_at: float = 0.0

    @property
    def live(self) -> bool:
        return self.state in _LIVE

    def info(self) -> WorkerJobInfo:
        last = self.steps[-1].text if self.steps else ""
        return WorkerJobInfo(
            worker_id=self.id,
            label=_cut(self.label, 80),
            kind=self.kind,
            agent=self.agent,
            task=_cut(self.task, 300),
            state=self.state,
            stopped=self.stopped,
            calls=self.calls,
            cost_usd=round(self.cost_usd, 6),
            started_at=self.started_at,
            ended_at=self.ended_at,
            branch=self.branch,
            worktree_path=self.worktree_path,
            last_step=_cut(last, 200),
        )

    def note(self) -> str:
        """What the parent model is told when this worker settles."""
        head = f"[Worker {self.id} ({_cut(self.label, 60)})"
        if self.state == "done":
            body = f"{head} finished. Summary:\n{self.summary}]"
        elif self.state == "interrupted":
            tidy = f" {self.summary}" if self.summary else ""
            body = f"{head} was interrupted by a restart. Nothing was re-run.{tidy}]"
        else:
            why = self.stopped or self.state
            body = f"{head} {self.state}: {why}. What it had:\n{self.summary}]"
        return body


@dataclass
class WorkerJobs:
    """A session's background workers and the turn's spawn count."""

    emit: Emit
    emit_result: EmitResult
    _jobs: dict[str, WorkerJob] = field(default_factory=dict)
    _next: int = 1
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    spawned_this_turn: int = 0

    def new(
        self, *, call_id: str, label: str, kind: str, task: str, agent: str | None
    ) -> WorkerJob:
        job = WorkerJob(
            id=f"w-{self._next}",
            call_id=call_id,
            label=label or _cut(task, 60),
            kind=kind,
            task=task,
            agent=agent,
        )
        self._next += 1
        self._jobs[job.id] = job
        self.spawned_this_turn += 1
        self._trim()
        return job

    def get(self, worker_id: str) -> WorkerJob | None:
        return self._jobs.get(worker_id)

    def by_call(self, call_id: str) -> WorkerJob | None:
        return next((j for j in self._jobs.values() if j.call_id == call_id), None)

    def all(self) -> list[WorkerJob]:
        return list(self._jobs.values())

    def live(self) -> list[WorkerJob]:
        return [j for j in self._jobs.values() if j.live]

    def running_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in ("running", "awaiting_approval"))

    def owed(self) -> bool:
        """Some worker is alive, or settled and its note not yet given."""
        return any(j.live or not j.delivered for j in self._jobs.values())

    def take_notes(self) -> list[str]:
        """The notes for every settled worker not yet told, marking them told."""
        notes: list[str] = []
        for job in self._jobs.values():
            if not job.live and not job.delivered:
                job.delivered = True
                notes.append(job.note())
        return notes

    def begin_turn(self) -> None:
        self.spawned_this_turn = 0

    async def changed(self, job: WorkerJob, *, progress: bool = False) -> None:
        """Log the list; a progress tick is throttled per worker."""
        now = time.monotonic()
        if progress and now - job._shown_at < _PROGRESS_SECS:
            return
        job._shown_at = now
        await self.emit([j.info() for j in self._jobs.values()])
        self._changed.set()

    async def settle(self, job: WorkerJob, state: WorkerState, summary: str) -> None:
        """Mark *job* ended, log the list and its result, and wake the loop."""
        job.state = state
        job.summary = summary
        job.ended_at = time.time()
        await self.changed(job)
        await self.emit_result(
            WorkerResult(
                session_id="",
                worker_id=job.id,
                state=state,
                summary=summary,
                transcript=list(job.steps),
                branch=job.branch,
                stopped=job.stopped,
                seq=1,
            )
        )

    async def asking(self, call_id: str, waiting: bool) -> None:
        """A worker's approval card opened (or closed)."""
        job = self.by_call(call_id)
        if job is None or not job.live:
            return
        job.state = "awaiting_approval" if waiting else "running"
        await self.changed(job)

    async def wait_change(self) -> None:
        """Until some worker changes. Cleared on return."""
        await self._changed.wait()
        self._changed.clear()

    def clear_change(self) -> None:
        self._changed.clear()

    async def stop(self, worker_id: str, why: str = "stopped") -> bool:
        """Stop one live worker and wait for it to settle. False when none."""
        job = self._jobs.get(worker_id)
        if job is None or not job.live:
            return False
        await self._end(job, why)
        return True

    async def stop_all(self, why: str = "the turn ended") -> None:
        for job in self.live():
            await self._end(job, why)

    async def _end(self, job: WorkerJob, why: str) -> None:
        job.stopped = job.stopped or why
        task = job.handle
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if job.live:
            # It never started, or its task ended without settling.
            await self.settle(job, "stopped", job.summary or f"Worker stopped: {why}.")

    def restore(self, last: Sequence[WorkerJobInfo]) -> list[WorkerJob]:
        """After a restart: the last list, with every live worker interrupted.

        Returns the interrupted ones, for the caller to settle their
        worktrees and log. Nothing is re-run.
        """
        cut: list[WorkerJob] = []
        for info in last:
            job = WorkerJob(
                id=info.worker_id,
                call_id="",
                label=info.label,
                kind=info.kind,
                task=info.task,
                agent=info.agent,
                state=info.state,
                stopped=info.stopped,
                calls=info.calls,
                cost_usd=info.cost_usd,
                started_at=info.started_at,
                ended_at=info.ended_at,
                branch=info.branch,
                worktree_path=info.worktree_path,
                delivered=True,
            )
            if job.live:
                job.state = "interrupted"
                job.stopped = "the daemon restarted"
                job.ended_at = time.time()
                job.delivered = False
                cut.append(job)
            self._jobs[job.id] = job
            number = info.worker_id.removeprefix("w-")
            if number.isdigit():
                self._next = max(self._next, int(number) + 1)
        return cut

    def _trim(self) -> None:
        settled = [j for j in self._jobs.values() if not j.live and j.delivered]
        for job in settled[: max(0, len(settled) - MAX_SETTLED)]:
            del self._jobs[job.id]
