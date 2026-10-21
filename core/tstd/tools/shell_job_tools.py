"""``shell_job_output`` and ``shell_job_stop``: read and end background jobs (TD-5137).

Both reach only the calling session's own jobs, which ``shell`` started
under an approval (or the read-only list). Reading touches nothing but the
daemon's buffer, and stopping kills only a process group this session
started, so the classifier's ``shell-job`` rule puts both in Class A.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..shell_jobs import READ_MAX_CHARS, ShellJob
from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

if TYPE_CHECKING:
    from ..session import Session

#: Longest a read may wait for new output, so a turn never parks on a quiet job.
MAX_WAIT_SECS = 30

SHELL_JOB_OUTPUT_TOOL = Tool(
    name="shell_job_output",
    description=(
        "Read a background shell job's output since your last read, and its status. "
        "No job_id lists the jobs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "wait_secs": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_WAIT_SECS,
                "description": "Wait this long for new output first",
            },
        },
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

SHELL_JOB_STOP_TOOL = Tool(
    name="shell_job_stop",
    description="Stop a background shell job and its whole process group.",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
        },
        "required": ["job_id"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

#: The tools only the builtin jobs answer, for the classifier and plan mode.
SHELL_JOB_TOOLS = frozenset({SHELL_JOB_OUTPUT_TOOL.name, SHELL_JOB_STOP_TOOL.name})


def register_shell_job_tools(registry: ToolRegistry) -> None:
    """Register ``shell_job_output`` and ``shell_job_stop``. Call from ``create_registry``."""
    registry.register(SHELL_JOB_OUTPUT_TOOL)
    registry.register(SHELL_JOB_STOP_TOOL)


def _owner(session: object) -> Session:
    # Imported here: session -> tools would cycle.
    from ..session import Session

    if not isinstance(session, Session):
        raise HandlerRefusal("shell_job_unavailable", "Background jobs need an active session.")
    return session


def _job(owner: Session, job_id: str) -> ShellJob:
    job = owner.shell_jobs.get(job_id)
    if job is None:
        known = ", ".join(j.job_id for j in owner.shell_jobs.list()) or "none"
        raise HandlerRefusal(
            "shell_job_not_found", f"No background job {job_id!r} in this session (jobs: {known})."
        )
    return job


def _status_line(job: ShellJob) -> str:
    if job.status == "exited":
        return f"{job.job_id}: exited with code {job.exit_code}"
    return f"{job.job_id}: {job.status}"


def _read_text(job: ShellJob) -> str:
    text, dropped = job.output.read(READ_MAX_CHARS)
    lines = [_status_line(job)]
    if dropped:
        lines.append(f"[{dropped} older characters were dropped before this read]")
    lines.append("── new output ──")
    lines.append(text or "(none)")
    if job.output.unread:
        lines.append(f"[{job.output.unread} more characters; call shell_job_output again]")
    return "\n".join(lines)


async def shell_job_output(
    *, session: object, tool_call_id: str = "", job_id: str | None = None, wait_secs: int = 0
) -> str:
    """New output of *job_id*, or the session's job list when it is omitted."""
    owner = _owner(session)
    if job_id is None:
        jobs = owner.shell_jobs.list()
        if not jobs:
            return "No background jobs in this session."
        return "\n".join(f"{_status_line(j)} — {j.command}" for j in jobs)
    job = _job(owner, job_id)
    wait = max(0, min(int(wait_secs), MAX_WAIT_SECS))
    if wait:
        await job.wait_for_change(float(wait))
    return _read_text(job)


async def shell_job_stop(*, session: object, tool_call_id: str = "", job_id: str) -> str:
    """Kill *job_id*'s process group and return its unread output."""
    owner = _owner(session)
    job = _job(owner, job_id)
    if job.status != "running":
        return _read_text(job)
    note = await owner.shell_jobs.stop(job)
    text = _read_text(job)
    return f"{note}\n{text}" if note else text
