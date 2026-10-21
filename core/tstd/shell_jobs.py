"""Background shell jobs a session owns (TD-5137).

``shell`` with ``background: true`` (or a foreground command that hits its
timeout with ``on_timeout: "background"``) leaves its process running and
hands it to the session's :class:`ShellJobs`. The model reads what the job
printed since its last read with ``shell_job_output`` and ends it with
``shell_job_stop``. The daemon owns the jobs, as it owns the session (AGENTS
directive 5): a job outlives the window, never the session. Ending the
session (cancel, delete, daemon shutdown) kills every job's process group.

This module knows nothing about processes: the shell tool hands each job a
``kill`` callback and a watcher task, so the session can import it without
pulling in the tools package. Every change logs one ``shell_jobs`` event with
the whole list, which is what the window shows.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from .protocol import DaemonEvent, ShellJobInfo, ShellJobStatus
from .protocol import ShellJobs as ShellJobsEvent

#: Running jobs one session may hold at once. A dev server, a watch build
#: and a log tail fit; a loop that keeps starting jobs is refused.
MAX_RUNNING_JOBS = 8
#: Finished jobs kept in the list (and the event) beside the running ones.
MAX_FINISHED_JOBS = 16
#: Output a job keeps that nothing has read yet; older text is dropped and
#: the next read says how much.
JOB_BUFFER_CHARS = 200_000
#: Most text one ``shell_job_output`` call returns; the rest waits for the next.
READ_MAX_CHARS = 20_000
#: Command text kept for the list.
_COMMAND_CHARS = 500
#: How long ``stop`` waits for the job's readers to finish after the kill.
_STOP_GRACE_SECS = 5.0

Kill = Callable[[], Awaitable[str | None]]
Emit = Callable[[list[ShellJobInfo]], Awaitable[None]]


@dataclass
class JobOutput:
    """The job's stdout and stderr, interleaved, with a read cursor.

    Offsets are absolute character counts since the job started, so a read
    after the buffer dropped its oldest text can say how much it missed.
    """

    cap: int = JOB_BUFFER_CHARS
    _text: str = ""
    _start: int = 0
    _cursor: int = 0

    @property
    def end(self) -> int:
        return self._start + len(self._text)

    def append(self, text: str) -> None:
        self._text += text
        over = len(self._text) - self.cap
        if over > 0:
            self._text = self._text[over:]
            self._start += over

    @property
    def unread(self) -> int:
        return self.end - max(self._cursor, self._start)

    def read(self, max_chars: int = READ_MAX_CHARS) -> tuple[str, int]:
        """Text since the last read (at most *max_chars*), and chars dropped unread."""
        dropped = max(0, self._start - self._cursor)
        begin = max(self._cursor, self._start) - self._start
        chunk = self._text[begin : begin + max_chars]
        self._cursor = self._start + begin + len(chunk)
        return chunk, dropped


@dataclass
class ShellJob:
    """One background command and what it has printed."""

    job_id: str
    command: str
    kill: Kill
    started_at: float = field(default_factory=time.time)
    status: ShellJobStatus = "running"
    exit_code: int | None = None
    ended_at: float | None = None
    output: JobOutput = field(default_factory=JobOutput)
    watcher: asyncio.Task[None] | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def append(self, text: str) -> None:
        self.output.append(text)
        self._changed.set()

    async def wait_for_change(self, timeout: float) -> None:  # noqa: ASYNC109 - a read's bounded wait
        """Return once new output lands or the job ends, or after *timeout*."""
        if self.output.unread or self.status != "running":
            return
        self._changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout=timeout)

    def info(self) -> ShellJobInfo:
        return ShellJobInfo(
            job_id=self.job_id,
            command=self.command[:_COMMAND_CHARS],
            status=self.status,
            exit_code=self.exit_code,
            started_at=self.started_at,
            ended_at=self.ended_at,
        )

    def _end(self, status: ShellJobStatus, exit_code: int | None) -> None:
        self.status = status
        self.exit_code = exit_code
        self.ended_at = time.time()
        self._changed.set()


class ShellJobs:
    """The background jobs of one session. The session holds one."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._jobs: dict[str, ShellJob] = {}
        self._next = 1
        self._closed = False

    def get(self, job_id: str) -> ShellJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[ShellJob]:
        return list(self._jobs.values())

    @property
    def running(self) -> int:
        return sum(1 for job in self._jobs.values() if job.status == "running")

    def refusal(self) -> str | None:
        """Why a new job cannot start now, or None."""
        if self._closed:
            return "this session has ended; it cannot start a background job"
        if self.running >= MAX_RUNNING_JOBS:
            return (
                f"this session already runs {MAX_RUNNING_JOBS} background jobs; "
                "stop one with shell_job_stop first"
            )
        return None

    def new_job(self, command: str, kill: Kill) -> ShellJob:
        """A job, numbered in this session's order. ``add`` lists it."""
        job = ShellJob(job_id=f"job-{self._next}", command=command, kill=kill)
        self._next += 1
        return job

    async def add(self, job: ShellJob) -> None:
        self._jobs[job.job_id] = job
        self._prune()
        await self._changed()

    async def finished(self, job: ShellJob, exit_code: int | None) -> None:
        """The watcher saw the command end by itself."""
        if job.status != "running":
            return
        job._end("exited", exit_code)
        await self._changed()

    async def stop(self, job: ShellJob, status: ShellJobStatus = "stopped") -> str | None:
        """Kill the job's process group. Returns the kill's note, if any."""
        if job.status != "running":
            return None
        job._end(status, None)
        note = await job.kill()
        watcher = job.watcher
        if watcher is not None and not watcher.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(watcher), timeout=_STOP_GRACE_SECS)
            if not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        await self._changed()
        return note

    async def stop_all(self) -> None:
        """The session ended: kill every running job and take no new ones."""
        self._closed = True
        for job in self.list():
            if job.status == "running":
                await self.stop(job)

    async def expire_restored(self, events: Sequence[DaemonEvent]) -> None:
        """After a restart, log the old daemon's running jobs as ``lost``.

        They belonged to a process that is gone, so this daemon can neither
        read nor stop them; the window must not keep showing them running.
        """
        lost = lost_jobs_after_restart(events)
        if lost is not None:
            await self._emit(lost)

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.status != "running"]
        for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            del self._jobs[job.job_id]

    async def _changed(self) -> None:
        self._prune()
        await self._emit([job.info() for job in self._jobs.values()])


def lost_jobs_after_restart(events: Sequence[DaemonEvent]) -> list[ShellJobInfo] | None:
    """On revive: the newest list with every ``running`` job marked ``lost``.

    None when nothing was running. A revived session has a new daemon, and
    the old daemon's jobs are not this one's to read or stop.
    """
    newest = next((e for e in reversed(events) if isinstance(e, ShellJobsEvent)), None)
    if newest is None or not any(j.status == "running" for j in newest.jobs):
        return None
    return [
        j.model_copy(update={"status": "lost"}) if j.status == "running" else j for j in newest.jobs
    ]
