"""Archive, delete, move, and rename for sessions (TD-1715, TD-3002).

The verbs the rail's row actions send. They live here rather than in
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

from .boundary_config import (
    BoundaryConfig,
    CapsSection,
    boundary_from_mapping,
    boundary_source,
    configured_spend_from_mapping,
)
from .config import ConfigError
from .logging import get_logger
from .policy import PolicyConfig, policy_from_mapping
from .protocol import BoundaryUpdate as BoundaryUpdateEvent
from .protocol import ConfigTrust, LoosenedSetting, build_error
from .session import Session, SessionRegistry
from .session_persist import SessionPersist
from .session_store import SessionStore
from .workspace_trust import WorkspaceConfigView, read_view

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


async def rename_session(
    store: SessionStore,
    session_id: str,
    title: str,
) -> str | None:
    """Set or restore a session's display title (TD-3002).

    Allowed in every state, including mid-turn: the title is metadata,
    it touches neither the loop nor the log.
    """
    if not await store.set_title(session_id, title):
        return _not_found(session_id)
    log.info("session renamed", extra={"extra_fields": {"session_id": session_id}})
    return None


async def delete_session(
    registry: SessionRegistry,
    store: SessionStore,
    session_id: str,
    release: Callable[[str], None],
    persist: SessionPersist | None = None,
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
    if persist is not None:
        persist.remove(session_id)
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


def _config_trust(session: Session) -> ConfigTrust | None:
    """The trust card's payload: only when the daemon gates and there is something to ask."""
    view = session.config_view
    if session.trust_store is None or view is None or view.sha256 is None:
        return None
    if not view.loosened or view.state == "none":
        return None
    return ConfigTrust(
        state=view.state,
        sha256=view.sha256,
        loosened=[
            LoosenedSetting(key=item.key, value=item.value, default=item.default)
            for item in view.loosened
        ],
    )


def _spend_configured(session: Session) -> bool:
    """A charter's cap, or ``caps.spend_usd`` in a config that is in force."""
    if session.charter is not None:
        return True
    view = session.config_view
    return view is not None and configured_spend_from_mapping(view.effective) is not None


def config_source(session: Session) -> str:
    """Where the session's wall came from, for ``boundary_update.source``.

    An untrusted config says so instead of naming a file whose settings
    are not in force (TD-5113).
    """
    view = session.config_view
    if view is not None and not view.applies:
        why = "declined" if view.state == "declined" else "not trusted yet"
        return f"defaults — workspace config {why}"
    return boundary_source(session.workspace_path)


def boundary_update_event(session: Session, source: str) -> BoundaryUpdateEvent:
    """The wall event for *session*, including whether a spend budget is set."""
    cfg = session.boundary_config
    configured = _spend_configured(session)
    return BoundaryUpdateEvent(
        session_id=session.id,
        writable_paths=list(cfg.boundary.writable_paths),
        allowed_commands=list(cfg.boundary.allowed_commands),
        network=cfg.boundary.network,
        spend_usd=cfg.caps.spend_usd,
        wall_clock_hours=cfg.caps.wall_clock_hours,
        max_iterations=cfg.caps.max_iterations,
        source=source,
        attachments=cfg.attachments,
        spend_configured=configured,
        config_trust=_config_trust(session),
        seq=1,
    )


def _charter_caps(charter: CapsSection, workspace: CapsSection) -> CapsSection:
    """A charter run's caps: the charter's, with the file's memory cap when it sets none."""
    if charter.shell_memory_mb is not None or workspace.shell_memory_mb is None:
        return charter
    return charter.model_copy(update={"shell_memory_mb": workspace.shell_memory_mb})


def reload_session_boundary(
    session: Session,
    workspace_path: str,
    *,
    fallback: BoundaryConfig | None = None,
) -> ConfigError | None:
    """Reload *session*'s boundary from *workspace_path*, a charter's wall on top.

    Every reload of a live session's boundary goes through here (open,
    revive, move, resume), so none can forget the charter: a charter run's
    wall and caps are the charter's, whatever the workspace file says. The
    shell reads the session's allowlist per call, so a reload that dropped
    the charter would widen it on the spot. One exception: a charter that
    leaves ``caps.shell_memory_mb`` unset keeps the file's value (TD-5116),
    so a machine's memory cap does not fall back to half of RAM because a
    run has a charter.

    An invalid file gives the session *fallback*, or leaves its current
    config when that is None; the error is returned for the caller to log
    or announce. Nothing raises.
    """
    error: ConfigError | None = None
    session.config_view = None
    try:
        # One read: the hash judged for trust and the settings applied
        # come from the same bytes (TD-5113). Untrusted → defaults.
        view = read_view(workspace_path, session.trust_store)
        session.config_view = view
        cfg = boundary_from_mapping(view.path, view.effective)
    except ConfigError as e:
        error = e
        cfg = fallback if fallback is not None else session.boundary_config
    if session.charter is not None:
        cfg.boundary = session.charter.boundary
        cfg.caps = _charter_caps(session.charter.caps, cfg.caps)
    session.boundary_config = cfg
    return error


def reload_session_policy(
    session: Session, workspace_path: str, view: WorkspaceConfigView | None = None
) -> ConfigError | None:
    """Reload *session*'s approval policy through the trust gate (TD-5113).

    An untrusted config gives the packaged default policy. An invalid one
    gives the defaults too, and the error is returned for the caller to log.

    *view* is the read ``reload_session_boundary`` just made of the same
    file. Passing it keeps the policy, the wall and the trust card on one
    set of bytes: a second read could see a file changed in between, and
    announce one version while enforcing another (TD-5158).
    """
    try:
        if view is None:
            view = read_view(workspace_path, session.trust_store)
        session.policy = policy_from_mapping(view.path, view.effective)
    except ConfigError as e:
        session.policy = PolicyConfig()
        return e
    return None


async def apply_workspace_config(session: Session) -> None:
    """Re-read the workspace config after a trust decision and announce the wall.

    Same root, so only what the config feeds is rebound (the loop's
    ``on_config_reloaded``), which is safe while a turn runs. Invalid
    config falls back to defaults, as on open.
    """
    error = reload_session_boundary(session, session.workspace_path, fallback=BoundaryConfig())
    policy_error = reload_session_policy(session, session.workspace_path, view=session.config_view)
    for problem in (error, policy_error):
        if problem is not None:
            log.warning(
                "workspace config invalid after trust decision; using defaults",
                extra={"extra_fields": {"session_id": session.id, "error": str(problem)}},
            )
    if session.on_config_reloaded is not None:
        session.on_config_reloaded()
    source = config_source(session) if error is None else f"defaults — invalid config ({error})"
    await session.event_log.add(boundary_update_event(session, source))


async def _reload_working_context(session: Session, workspace_path: str) -> None:
    """Re-resolve boundary + policy for the new root and announce the wall.

    Mirrors the daemon's open path: an invalid config at the target falls
    back to defaults with the reason surfaced rather than failing the move,
    because the session is already living there by the time we get here.
    A restored tombstone has no loop to reconfigure, so it gets the durable
    record only — there is nothing to announce.
    """
    # Defaults on an invalid config, as announced: never the previous
    # project's wall.
    error = reload_session_boundary(session, workspace_path, fallback=BoundaryConfig())
    if error is None:
        source = config_source(session)
    else:
        log.warning(
            "workspace boundary config invalid after move; using defaults",
            extra={"extra_fields": {"workspace_path": workspace_path, "error": str(error)}},
        )
        source = f"defaults — invalid config ({error})"
    policy_error = reload_session_policy(session, workspace_path, view=session.config_view)
    if policy_error is not None:
        log.warning(
            "workspace policy config invalid after move; using defaults",
            extra={"extra_fields": {"workspace_path": workspace_path, "error": str(policy_error)}},
        )
    # The loop built its guard, classifier, checkpointer, ledger, policy
    # gate and prompt assembler from the old root: rebind them before the
    # next turn, so the wall announced below is the one tools run under.
    if session.on_workspace_moved is not None:
        session.on_workspace_moved()

    await session.event_log.add(boundary_update_event(session, source))
