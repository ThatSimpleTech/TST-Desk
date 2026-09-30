"""Merge a ``save_job`` onto an existing scheduled job.

TD-3810, TD-3812, TD-3813, TD-3814, TD-3815.

Create validates a whole draft. An edit is a patch: fields the client left
out stay as stored, because Pause is a save that only flips ``paused`` and
must not wipe the run receipt, the armed slot, how late is still worth
running, or a retry that is already waiting. A blank cadence, next run,
preset, engine, or grace is the explicit "remove this" — without it a
recurring job could never become a one-shot, a pinned model could never
go back to whatever the window is using, and a grace could never go back
to always running. Retries uses 0 for that, not a blank cadence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal, cast

from pydantic import ValidationError

from .models import (
    DeliverTo,
    Job,
    JobValidationError,
    describe_validation_error,
    normalize_next_run,
    normalize_workspace,
)
from .pin import require_known_preset
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
    preset: str | None,
    engine: str | None,
    known_presets: Mapping[str, object],
    grace: str | int | None,
    retries: int | None,
    retry_delay: str | int | None,
) -> Job:
    """Return the job to persist. Raises ``JobValidationError``; does not write."""
    new_workspace = _workspace_for_edit(existing, workspace, known_workspaces)
    new_cadence = _merge_cleared(cadence, existing.cadence)
    new_timezone = existing.timezone if timezone is None else timezone
    new_next = _merge_cleared(next_run, existing.next_run)
    new_preset = _merge_cleared(preset, existing.preset)
    new_engine = _merge_cleared(engine, existing.engine)
    new_grace = _merge_grace(grace, existing.grace)
    new_retries: int | str | None = existing.retries if retries is None else retries
    new_delay = _merge_delay(retry_delay, existing.retry_delay)
    # The stored slot was computed from the old cadence and zone. When either
    # changes and the client did not send a replacement time, drop it so the
    # runner re-arms instead of firing the stale instant.
    cadence_changed = (new_cadence, new_timezone) != (existing.cadence, existing.timezone)
    if next_run is None and cadence_changed:
        new_next = None
    # An in-progress retry remembers the regular slot. Pause sends the armed
    # retry time back unchanged and must not drop that memory. Changing the
    # cadence, the armed time, or turning retries off does: the remembered
    # slot belonged to the old schedule, and "no retries" means go back to
    # it now instead of firing the extra try.
    attempt = existing.attempt
    resume_at = existing.resume_at
    next_changed = _next_run_changed(next_run, existing.next_run)
    retries_off = _retries_turned_off(retries, existing.attempt)
    if cadence_changed or next_changed or retries_off:
        attempt = 0
        resume_at = None
        if retries_off and not cadence_changed and not next_changed:
            if existing.resume_at is not None:
                new_next = existing.resume_at
            elif existing.cadence is None:
                new_next = None
                paused = True
    if new_retries == 0:
        # A count of 0 does not keep a delay around. The model validator
        # clears it too; doing it here means a phrase sent beside 0 is
        # not what fails the save.
        new_delay = None
    # Turning retries off on a one-shot spends it: there is no later slot
    # to resume, which is the same shape as a one-shot that already ran.
    # That is not "the edit deleted the schedule".
    spending = retries_off and not cadence_changed and not next_changed and existing.cadence is None
    if new_cadence is None and new_next is None and _had_schedule(existing) and not spending:
        raise JobValidationError(_SCHEDULE_REQUIRED)
    try:
        job = Job(
            id=existing.id,
            workspace=new_workspace,
            instruction=instruction or existing.instruction,
            cadence=new_cadence,
            next_run=new_next,
            deliver_to=deliver_to or existing.deliver_to,
            paused=paused,
            timezone=new_timezone,
            preset=new_preset,
            # Same as validate_draft: the annotation is the stored kind.
            engine=cast(Literal["native", "grok"] | None, new_engine),
            # The annotation is seconds. The validator accepts the phrase.
            grace=cast(int | None, new_grace),
            retries=cast(int, new_retries),
            retry_delay=cast(int | None, new_delay),
            attempt=attempt,
            resume_at=resume_at,
            # The receipt belongs to the run, not to this edit.
            last_run=existing.last_run,
            last_status=existing.last_status,
            last_summary=existing.last_summary,
            last_session_id=existing.last_session_id,
            # Pause is a save that only flips ``paused``. Dropping the
            # park link here would leave the approval session with no
            # job to settle into, and the next tick would start another.
            parked_session_id=existing.parked_session_id,
            parked_started_at=existing.parked_started_at,
        )
    except ValidationError as exc:
        raise JobValidationError(describe_validation_error(exc)) from exc
    # Only a name the client just sent. Resending a stale name fails here;
    # omitting it (Pause) keeps a pin whose preset has left the catalog.
    if preset is not None and preset.strip():
        require_known_preset(job.preset, known_presets)
    return job


def _had_schedule(job: Job) -> bool:
    return job.cadence is not None or job.next_run is not None


def _retries_turned_off(sent: int | None, attempt: int) -> bool:
    """True when this edit says "no more retries" while one is still armed.

    Anything that is not a real 0 falls through to ``Job``, which is what
    reports a bad count. A bool is an ``int`` in Python; it is not a count.
    """
    if sent is None or attempt <= 0:
        return False
    if isinstance(sent, bool) or not isinstance(sent, int):
        return False
    return sent == 0


def _next_run_changed(sent: str | None, current: str | None) -> bool:
    """True when the client sent a different armed time. Omitted is not a change.

    Pause sends the stored instant back. That must not look like a new
    schedule, or it would drop the retry the pause is supposed to keep.
    """
    if sent is None:
        return False
    if not sent.strip():
        return current is not None
    try:
        normalized = normalize_next_run(sent)
    except JobValidationError:
        return True
    if current is None:
        return True
    try:
        return normalized != normalize_next_run(current)
    except JobValidationError:
        return True


def _merge_delay(sent: str | int | None, current: int | None) -> str | int | None:
    """``None`` keeps the stored seconds. A blank string means the default."""
    if sent is None:
        return current
    if isinstance(sent, str) and not sent.strip():
        return None
    return sent


def _merge_grace(sent: str | int | None, current: int | None) -> str | int | None:
    """``None`` keeps the stored seconds. A blank string clears. A phrase sets."""
    if sent is None:
        return current
    if isinstance(sent, str) and not sent.strip():
        return None
    return sent


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
