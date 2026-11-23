"""``schedule_propose`` and ``schedule_list`` (TD-5263).

The agent can draft a scheduled job and can read this workspace's jobs. It
cannot create, change or delete one: ``schedule_propose`` writes nothing,
and the person's Save on the card is the only thing that does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .registry import Tool, ToolRegistry
from .results import HandlerRefusal

SCHEDULE_PROPOSE_TOOL = Tool(
    name="schedule_propose",
    description=(
        "Propose a scheduled job in this workspace. Saves nothing: the user sees a card "
        "and only their Save creates the job. Use only when the user asked for something "
        "to run later or repeatedly. Don't retry or ask again while they decide."
    ),
    parameters={
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": (
                    "What the scheduled run should do, written so it makes sense with no "
                    "chat history."
                ),
            },
            "cadence": {
                "type": "string",
                "description": (
                    "Repeat rule: 'every 2 hours', 'weekdays at 7:45', or cron like "
                    "'45 7 * * 1-5'. Omit for a one-off."
                ),
            },
            "run_at": {
                "type": "string",
                "description": (
                    "ISO date and time for a one-off run. Give cadence or run_at, not both."
                ),
            },
            "timezone": {"type": "string", "description": "IANA zone, e.g. Europe/London."},
        },
        "required": ["instruction"],
        "additionalProperties": False,
    },
    side_effect_class="auto",
    parallel_safe=False,
    mutates=False,
)

SCHEDULE_LIST_TOOL = Tool(
    name="schedule_list",
    description=(
        "List this workspace's scheduled jobs: id, when it runs, next run, whether it is "
        "paused, and the last result."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    side_effect_class="auto",
    parallel_safe=True,
    mutates=False,
)

LIST_CAP = 50
_INSTRUCTION_SHOWN = 200
_UNAVAILABLE = "Scheduling is not available in this session."


def register_schedule_tools(registry: ToolRegistry) -> None:
    registry.register(SCHEDULE_PROPOSE_TOOL)
    registry.register(SCHEDULE_LIST_TOOL)


async def schedule_propose(
    *,
    session: object,
    tool_call_id: str = "",
    instruction: str,
    cadence: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
) -> str:
    """Park a draft and emit ``schedule_proposal``. Writes nothing."""
    # Imported here: scheduler.proposals -> session -> tools would cycle.
    from ..scheduler.proposals import ScheduleRefusal, draft_from, propose
    from ..session import Session

    if not isinstance(session, Session) or not session.workspace_path:
        raise HandlerRefusal("schedule_unavailable", _UNAVAILABLE)
    try:
        parked = draft_from(
            session,
            instruction=instruction,
            cadence=cadence,
            run_at=run_at,
            timezone=timezone,
            tool_call_id=tool_call_id,
        )
    except ScheduleRefusal as refusal:
        raise HandlerRefusal(refusal.code, refusal.message) from None
    await propose(session, parked)
    return (
        f"Proposed (proposal {parked.proposal_id}). Nothing was scheduled. The user saves "
        "or dismisses it on the card. Carry on."
    )


def _rows(data_dir: Path, project: str) -> list[str]:
    from ..scheduler.store import list_jobs

    here = Path(project).resolve()
    rows: list[str] = []
    for job in list_jobs(data_dir):
        try:
            same = Path(job.workspace).resolve() == here
        except OSError:
            same = False
        if not same:
            continue
        when = job.cadence or (f"once at {job.next_run}" if job.next_run else "not set")
        text = " ".join(job.instruction.split())
        if len(text) > _INSTRUCTION_SHOWN:
            text = text[: _INSTRUCTION_SHOWN - 1] + "…"
        rows.append(
            f"- {job.id}: {when}; next {job.next_run or 'none'}; "
            f"{'paused' if job.paused else 'on'}; last {job.last_status or 'never run'}: {text}"
        )
        if len(rows) == LIST_CAP:
            break
    return rows


async def schedule_list(*, session: object, tool_call_id: str = "", data_dir: object = None) -> str:
    """This workspace's jobs, from the daemon's own store. Never another workspace's."""
    from ..session import Session

    if not isinstance(session, Session) or not isinstance(data_dir, Path):
        raise HandlerRefusal("schedule_unavailable", _UNAVAILABLE)
    rows = await asyncio.to_thread(_rows, data_dir, session.project_path)
    if not rows:
        return "No scheduled jobs in this workspace."
    return "\n".join(rows)
