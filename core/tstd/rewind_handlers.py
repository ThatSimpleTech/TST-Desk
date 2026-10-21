"""The daemon's side of "Restore files to here" (TD-5125).

``preview_restore_files`` answers this connection with the files and diff a
restore would undo; ``restore_files`` restores what the person saw and logs
``files_restored`` so every window (and a phone) shows it. Both name the user
turn by its ``user_turn`` id, found in the session's own log: the window never
supplies a seq or a commit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .file_rewind import RewindError, apply_rewind, plan_rewind
from .logging import get_logger, redact_secrets
from .protocol import (
    FilesRestored,
    FilesRestorePreview,
    PreviewRestoreFiles,
    RestoreFileEntry,
    RestoreFiles,
    UserTurn,
    build_error,
)

if TYPE_CHECKING:
    from .session import Session

log = get_logger("tstd.rewind")

# One restore per session at a time: a second click from another window
# waits, then finds the first one's commit and is told the preview is stale.
_locks: dict[str, asyncio.Lock] = {}

_REFUSALS: dict[str, str] = {
    "turn_in_progress": "Files can be restored when the turn ends.",
    "turn_not_found": "That message isn't in this session's history any more.",
    "rewind_autonomy": (
        "Autonomous runs undo on their own branch. Restore files from an interactive session."
    ),
    "rewind_grok": (
        "Grok changes files with its own tools, which TST Desk doesn't checkpoint, "
        "so there is nothing to restore them from."
    ),
}


def _refusal(msg: PreviewRestoreFiles | RestoreFiles, code: str, message: str = "") -> str:
    return build_error(
        code,
        message or _REFUSALS.get(code, code),
        session_id=msg.session_id,
        in_reply_to=msg.type,
    )


def _turn_seq(session: Session, turn_id: str) -> int | None:
    """The seq of the ``user_turn`` the window's row stands for."""
    for event in session.event_log.all_events:
        if isinstance(event, UserTurn) and event.turn_id == turn_id:
            return event.seq
    return None


def _check(session: Session, msg: PreviewRestoreFiles | RestoreFiles) -> str | int:
    """The turn's seq, or the code of why the session can't restore now."""
    if session.turn_in_flight:
        return "turn_in_progress"
    if session.autonomy and session.charter is not None:
        return "rewind_autonomy"
    if session.engine == "grok":
        return "rewind_grok"
    seq = _turn_seq(session, msg.turn_id)
    return "turn_not_found" if seq is None else seq


async def preview_restore(session: Session, msg: PreviewRestoreFiles) -> str:
    """Answer with what restoring to ``msg.turn_id`` would change. Writes nothing."""
    seq = _check(session, msg)
    if isinstance(seq, str):
        return _refusal(msg, seq)
    try:
        plan = await plan_rewind(Path(session.workspace_path), session.id, seq)
    except RewindError as e:
        return _refusal(msg, e.code, e.message)
    except Exception as e:
        log.exception("restore preview failed", extra={"extra_fields": {"session_id": session.id}})
        return _refusal(msg, "rewind_failed", f"Couldn't work out what to restore: {e}")
    # A direct reply skips the event log's scrub (TD-1405): a diff of an
    # .env the agent wrote must not carry the key to the window.
    return FilesRestorePreview(
        session_id=session.id,
        turn_id=msg.turn_id,
        plan_id=plan.plan_id,
        files=[
            RestoreFileEntry(path=f.path, change=f.change, hand_edited=f.hand_edited)
            for f in plan.files
        ],
        diff=redact_secrets(plan.diff),
        diff_truncated=plan.diff_truncated,
    ).model_dump_json()


async def restore_files(session: Session, msg: RestoreFiles) -> str | None:
    """Restore what the preview showed; log ``files_restored``, or answer an error."""
    lock = _locks.setdefault(session.id, asyncio.Lock())
    async with lock:
        seq = _check(session, msg)
        if isinstance(seq, str):
            return _refusal(msg, seq)
        session.folder_busy = "restoring files"
        try:
            result = await apply_rewind(
                Path(session.workspace_path),
                session.id,
                seq,
                turn_id=msg.turn_id,
                plan_id=msg.plan_id,
                include_hand_edited=msg.include_hand_edited,
                log_seq=session.event_log.last_seq,
            )
        except RewindError as e:
            return _refusal(msg, e.code, e.message)
        except Exception as e:
            log.exception("restore failed", extra={"extra_fields": {"session_id": session.id}})
            return _refusal(msg, "rewind_failed", f"Restoring stopped part way: {e}")
        finally:
            session.folder_busy = None
        log.info(
            "files restored to a turn",
            extra={
                "extra_fields": {
                    "session_id": session.id,
                    "turn_seq": seq,
                    "restored": len(result.restored),
                    "removed": len(result.removed),
                    "left_alone": len(result.left_alone),
                }
            },
        )
        await session.event_log.add(
            FilesRestored(
                session_id=session.id,
                turn_id=msg.turn_id,
                restored=list(result.restored),
                removed=list(result.removed),
                left_alone=list(result.left_alone),
                lfs_pointers=list(result.pointers),
                seq=1,
            )
        )
        return None
