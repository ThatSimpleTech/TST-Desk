"""Archive, delete, and move-to-project for sessions (TD-1715).

The three verbs the rail's row actions send. They live here rather than in
``daemon.py`` because each is a small, self-contained rule about the durable
registry plus its in-memory twin, and ``daemon.py`` is already the largest
file in the package (AGENTS §6).

Each entry point returns ``None`` on success or a ready-to-send ``error``
envelope on refusal, so the daemon's dispatch stays three lines per verb and
every refusal is typed rather than silent.

The one rule worth stating twice: **archive is filing, not killing.** An
archived session keeps its loop, its event log, and any turn already in
flight. Delete and Move are the destructive pair, and both are refused while
the loop still owes the user a turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from .boundary_config import boundary_source, load_workspace_boundary
from .config import ConfigError
from .logging import get_logger
from .policy import load_policy
from .protocol import BoundaryUpdate as BoundaryUpdateEvent
from .protocol import build_error
from .session import Session, SessionRegistry
from .session_store import SessionStore

log = get_logger("tstd.session_lifecycle")

#: Refusal code for a destructive action aimed at a session mid-turn.
BUSY_CODE = "session_busy"


def _not_found(session_id: str) -> str:
    return build_error(
        "session_not_found",
        f"Session {session_id!r} not found",
        session_id=session_id,
    )


def _busy(session_id: str, action: str) -> str:
    """Refuse a destructive action with copy that names the way out.

    The user is mid-turn and the answer they are waiting on lives in the
    session being aimed at, so the message says what to do instead rather
    than just reporting the state.
    """
    return build_error(
        BUSY_CODE,
        f"This session has a turn in flight, so it can't be {action} yet. "
        "Wait for the turn to finish or stop it first — archiving works "
        "either way and leaves the turn running.",
        session_id=session_id,
    )


async def archive_session(
    store: SessionStore,
    session_id: str,
    archived: bool,
) -> str | None:
    """File a session away, or restore it.

    Allowed in every state, including mid-turn: the flag is presentation
    filing, it touches neither the loop nor the log.
    """
    if store.get(session_id) is None:
        return _not_found(session_id)
    await store.set_archived(session_id, archived)
    log.info(
        "session archived" if archived else "session restored",
        extra={"extra_fields": {"session_id": session_id}},
    )
    return None


async def delete_session(
    registry: SessionRegistry,
    store: SessionStore,
    session_id: str,
    release: Callable[[str], None],
) -> str | None:
    """Destroy a session, its runner, and its event log.

    ``release`` tears down whatever the daemon holds for the id (attached
    clients and their streaming tasks) — those tasks park on the event log
    forever otherwise, waiting on a session nobody can reach.

    The audit database is deliberately untouched: it is the append-only
    forensic record (§2.2), not this session's conversation.
    """
    record = store.get(session_id)
    session = registry.get(session_id)
    if record is None and session is None:
        return _not_found(session_id)
    if session is not None and session.turn_in_flight:
        return _busy(session_id, "deleted")

    # Stop the loop before the session leaves the registry, so no orphaned
    # task keeps writing into a log nothing will ever read.
    runner = registry.get_runner(session_id)
    if runner is not None:
        await runner.cancel()
    release(session_id)
    await registry.remove(session_id)
    await store.remove(session_id)
    log.info("session deleted", extra={"extra_fields": {"session_id": session_id}})
    return None


async def move_session(
    registry: SessionRegistry,
    store: SessionStore,
    session_id: str,
    workspace_path: str,
) -> str | None:
    """Reassign a session to another workspace.

    The session object is kept — id, event log, and conversation included —
    so the history moves with it. What changes is the working context: the
    boundary and approval policy are re-resolved from the new root and a
    fresh ``boundary_update`` is logged, so the wall the client shows is the
    one the next turn will actually run under.
    """
    record = store.get(session_id)
    session = registry.get(session_id)
    if record is None and session is None:
        return _not_found(session_id)
    if session is not None and session.turn_in_flight:
        return _busy(session_id, "moved")

    target = Path(workspace_path)
    # Blocking stat off the event loop, same as open_workspace's own check.
    if not await asyncio.to_thread(target.is_dir):
        return build_error(
            "workspace_not_found",
            f"Workspace path is not a directory: {workspace_path}",
            session_id=session_id,
        )

    await store.set_workspace(session_id, workspace_path)
    if session is not None:
        session.reassign_workspace(workspace_path)
        await _reload_working_context(session, workspace_path)
    log.info(
        "session moved",
        extra={"extra_fields": {"session_id": session_id, "workspace_path": workspace_path}},
    )
    return None


async def _reload_working_context(session: Session, workspace_path: str) -> None:
    """Re-resolve boundary + policy for the new root and announce the wall.

    Mirrors the daemon's open path: an invalid config at the target falls
    back to defaults with the reason surfaced rather than failing the move,
    because the session is already living there by the time we get here.
    A restored tombstone has no loop to reconfigure, so it gets the durable
    record only — there is nothing to announce.
    """
    source = "defaults"
    try:
        session.boundary_config = load_workspace_boundary(workspace_path)
        source = boundary_source(workspace_path)
    except ConfigError as e:
        log.warning(
            "workspace boundary config invalid after move; using defaults",
            extra={"extra_fields": {"workspace_path": workspace_path, "error": str(e)}},
        )
        source = f"defaults — invalid config ({e})"
    try:
        session.policy = load_policy(workspace_path)
    except ConfigError as e:
        log.warning(
            "workspace policy config invalid after move; using defaults",
            extra={"extra_fields": {"workspace_path": workspace_path, "error": str(e)}},
        )

    cfg = session.boundary_config
    await session.event_log.add(
        BoundaryUpdateEvent(
            session_id=session.id,
            writable_paths=list(cfg.boundary.writable_paths),
            allowed_commands=list(cfg.boundary.allowed_commands),
            network=cfg.boundary.network,
            spend_usd=cfg.caps.spend_usd,
            wall_clock_hours=cfg.caps.wall_clock_hours,
            max_iterations=cfg.caps.max_iterations,
            source=source,
            seq=1,
        )
    )
