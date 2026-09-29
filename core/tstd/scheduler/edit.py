"""Merge a ``save_job`` onto an existing scheduled job (TD-3810).

Create validates a whole draft. An edit is a patch: fields the client left
out stay as stored, because Pause is a save that only flips ``paused`` and
must not wipe the run receipt or the armed slot. A blank cadence or next
run is the one explicit "remove this" — without it a recurring job could
never become a one-shot, and a one-shot could never become recurring.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from .models import (
    DeliverTo,
    Job,
    JobValidationError,
    describe_validation_error,
    normalize_workspace,
)
from .workspace import require_folder, resolve_workspace_name

#: What an edit reports when it removes the last schedule. A spent one-shot
#: already has neither field; that shape is allowed only when this edit did
#: not take a schedule away.
_SCHEDULE_REQUIRED = "Cadence or next run is required"


def apply_job_edit(
    existing: Job,
    *,
    workspace: str | None,
    instruction: str | None,
    cadence: str | None,
    next_run: str | None,
    deliver_to: DeliverTo | None,
    paused: bool,
    timezone: str | None,
    known_workspaces: Iterable[str],
) -> Job:
    """Return the job to persist. Raises ``JobValidationError``; does not write."""
    new_workspace = _workspace_for_edit(existing, workspace, known_workspaces)
    new_cadence = _merge_cleared(cadence, existing.cadence)
    new_timezone = existing.timezone if timezone is None else timezone
    new_next = _merge_cleared(next_run, existing.next_run)
    # The stored slot was computed from the old cadence and zone. When either
    # changes and the client did not send a replacement time, drop it so the
    # runner re-arms instead of firing the stale instant.
    if next_run is None and (new_cadence, new_timezone) != (existing.cadence, existing.timezone):
        new_next = None
    if new_cadence is None and new_next is None and _had_schedule(existing):
        raise JobValidationError(_SCHEDULE_REQUIRED)
    try:
        return Job(
            id=existing.id,
            workspace=new_workspace,
            instruction=instruction or existing.instruction,
            cadence=new_cadence,
            next_run=new_next,
            deliver_to=deliver_to or existing.deliver_to,
            paused=paused,
            timezone=new_timezone,
            # The receipt belongs to the run, not to this edit.
            last_run=existing.last_run,
            last_status=existing.last_status,
            last_summary=existing.last_summary,
            last_session_id=existing.last_session_id,
        )
    except ValidationError as exc:
        raise JobValidationError(describe_validation_error(exc)) from exc


def _had_schedule(job: Job) -> bool:
    return job.cadence is not None or job.next_run is not None


def _merge_cleared(sent: str | None, current: str | None) -> str | None:
    """``None`` keeps the stored value. A blank string clears it."""
    if sent is None:
        return current
    if not sent.strip():
        return None
    return sent


def _workspace_for_edit(existing: Job, raw: str | None, known: Iterable[str]) -> str:
    """Resolve and check a workspace only when the text changed.

    The model validator stays lenient so a job whose folder has moved still
    loads, and Pause sends no workspace at all. Typing a different folder is
    the same act as create, so it gets the same resolution and the same
    refusal. The sentence matches create because both go through ``Job``.
    """
    if raw is None or not raw.strip():
        return existing.workspace
    resolved = resolve_workspace_name(raw, known)
    try:
        # Same normalizer Job runs on create, so a bad path is the same
        # sentence once it wears the "Workspace:" label.
        normalized = normalize_workspace(resolved)
    except JobValidationError as exc:
        raise JobValidationError(_label_workspace(str(exc))) from exc
    if normalized == existing.workspace:
        return existing.workspace
    require_folder(normalized)
    return normalized


def _label_workspace(message: str) -> str:
    """Prefix a workspace failure the way ``describe_validation_error`` does."""
    if message.lower().startswith("workspace "):
        message = message[len("workspace ") :]
    return f"Workspace: {message}"
