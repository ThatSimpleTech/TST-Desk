"""Background shell jobs: start one, adopt a timed-out command (TD-5137).

``shell`` with ``background: true`` runs here: the command is checked and
classified exactly like a foreground one (it is the same ``shell`` call), it
starts under the same environment, memory cap and process group, and its
output goes to a :class:`~tstd.shell_jobs.ShellJob` instead of the result.
``on_timeout: "background"`` reaches :func:`adopt_job` from the foreground
path: the running command and its readers move to a job, and the result
returns with what the command printed so far.

A job runs only in an interactive top-level session. A delegated child is
gone when its task returns, so nothing would ever read or stop its job, and
an autonomy run's commands run in a container whose lifetime is the call.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..proc_lifecycle import reap_subprocess, retain_subprocess_transport
from ..session import Session
from ..shell_jobs import ShellJob
from . import shell_memory
from .ask_user import denies_every_ask
from .results import HandlerRefusal
from .shell import (
    _drain,
    _kill_process_group_offloaded,
    _Route,
    _StreamCapture,
    sanitized_env,
    spawn_shell,
)

_REAP_SECS = 5.0


def job_refusal(session: Session) -> str | None:
    """Why *session* cannot hold a background job now, or None."""
    if session.delegate_depth > 0:
        return "a delegated task cannot start a background job; run it in the foreground"
    # TD-5137: every run nobody watches. An autonomy run, a scheduled
    # job's turn, and a ``tst run --on-ask deny-and-continue`` turn all
    # return (or keep looping) with no one to read or stop the job.
    if session.autonomy or session.scheduled_run or denies_every_ask(session):
        return "an unattended run cannot start a background job; run it in the foreground"
    return session.shell_jobs.refusal()


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> str | None:
    """Kill the job's process group and wait for the leader to exit."""
    note = await _kill_process_group_offloaded(proc)
    if proc.returncode is None and note is None:
        await reap_subprocess(proc, timeout=_REAP_SECS)
    return note


async def _watch(
    session: Session,
    job: ShellJob,
    proc: asyncio.subprocess.Process,
    work: asyncio.Future[Any],
) -> None:
    """Wait for the command to end by itself, then mark the job exited."""
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await work
    finally:
        retain_subprocess_transport(proc)
    await session.shell_jobs.finished(job, proc.returncode)


async def start_job(session: Session, command: str) -> str:
    """Start *command* as a background job and return the job id line."""
    refusal = job_refusal(session)
    if refusal is not None:
        raise HandlerRefusal("shell_job_refused", refusal)
    cap = await shell_memory.plan_cap(session, sanitized_env())
    proc = await spawn_shell(session, command, cap)
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:  # pragma: no cover — PIPE guarantees both
        retain_subprocess_transport(proc)
        raise ValueError("shell pipes were not created")
    job = session.shell_jobs.new_job(command, lambda: _kill_and_reap(proc))
    route = _Route(job=job)
    # The captures are never read: the route sends every chunk to the job.
    unused = _StreamCapture(cap=0)
    work = asyncio.gather(
        _drain(stdout, "stdout", session, "", unused, route),
        _drain(stderr, "stderr", session, "", unused, route),
        proc.wait(),
    )
    job.watcher = asyncio.create_task(_watch(session, job, proc, work), name=job.job_id)
    await session.shell_jobs.add(job)
    return (
        f"started background job {job.job_id} (pid {proc.pid}).\n"
        f"Read its output with shell_job_output job_id={job.job_id}; "
        f"end it with shell_job_stop job_id={job.job_id}. "
        "It stops when the session ends."
    )


async def adopt_job(
    session: Session,
    command: str,
    proc: asyncio.subprocess.Process,
    work: asyncio.Task[None],
    route: _Route,
    timeout_secs: int,
) -> str:
    """Move a command that hit its timeout to a job. Returns the result header.

    Sets ``route.job`` only when the job was made; otherwise the caller
    kills the command as before, and the header is empty.
    """
    if job_refusal(session) is not None:
        return ""
    job = session.shell_jobs.new_job(command, lambda: _kill_and_reap(proc))
    route.job = job
    job.watcher = asyncio.create_task(_watch(session, job, proc, work), name=job.job_id)
    await session.shell_jobs.add(job)
    return (
        f"still running after {timeout_secs}s — moved to background job {job.job_id}. "
        f"The output so far is below; shell_job_output job_id={job.job_id} reads what comes "
        f"next, and shell_job_stop job_id={job.job_id} ends it."
    )
