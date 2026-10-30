"""JSON durability for the session registry.

The registry list (session id, workspace path, state, timestamps) lives
here. The transcript and model conversation live in ``session_persist``.
Together they are what a restart uses: a row without a conversation
snapshot is still a tombstone; a row with one is revived.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging import get_logger

log = get_logger("tstd.session_store")

_STORE_FILE = "sessions.json"

# First-line auto-title cap (TD-3001). Not a model call — deterministic
# so a clone and a restart agree. Documented in DECISIONS.md.
SESSION_TITLE_MAX_LEN = 60


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def title_from_user_message(content: str) -> str | None:
    """One-line title from a user message, or None if there is no text.

    First line only, whitespace collapsed, then length-capped. Empty,
    whitespace-only, and attachment-only (no text) stay untitled so the
    rail keeps the short id.
    """
    first = content.splitlines()[0] if content else ""
    collapsed = " ".join(first.split())
    if not collapsed:
        return None
    return collapsed[:SESSION_TITLE_MAX_LEN]


@dataclass
class SessionRecord:
    """A minimal, durable description of a session."""

    session_id: str
    workspace_path: str
    state: str
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    # Filed away rather than deleted (TD-1715). Durable, so the rail's
    # default list stays hidden across a restart. Defaulted so a store
    # written before this field loads unchanged instead of being dropped
    # as malformed.
    archived: bool = False
    # Display title (TD-3001 / TD-3002). A user rename, or a copy of
    # auto_title until then. Null → the rail falls back to the short id.
    title: str | None = None
    # First-message title, set once by maybe_set_title. Rename writes
    # `title` only; empty rename restores this (TD-3002). Defaulted so a
    # store written before this field loads unchanged.
    auto_title: str | None = None
    # Loop this session started with (`native` | `grok`). None on a store
    # written before the field existed — revive then follows current
    # config.engine.kind, which is what those sessions already did.
    engine: str | None = None
    # Catalog preset name this session opened with (TD-1721). The name
    # only — not a forked config tree. Defaulted so a store written
    # before this field loads unchanged instead of being dropped.
    preset: str = ""
    # The session's git worktree (TD-5126), as ``SessionWorktree.to_record``;
    # None in the folder itself. ``workspace_path`` stays the folder the
    # user opened, so the rail groups a worktree session with its project.
    worktree: dict[str, Any] | None = None
    # Read-only plan mode (TD-5123). Plan mode is a safety boundary, so a
    # restart must not end it: revive reads it back from here. None on a
    # row written before the field; revive then takes the newest
    # ``tier_state`` in the log.
    plan_mode: bool | None = None
    # Reasoning effort the picker chose, with "+ultracode" when the Ultracode
    # switch is on ("high+ultracode", TD-5195; effort.effort_record). The
    # switch has no key of its own because _load drops a whole row with a key
    # it does not know, so an older build would lose every session; it reads
    # this string as an unknown level instead and keeps the row. A legacy
    # "ultracode" means Max with the switch on. None on a row written before
    # the field (revive then uses the configured defaults).
    effort: str | None = None


class SessionStore:
    """Durable registry snapshot with atomic writes and 0o600 perms.

    Usage:
        store = SessionStore(data_dir)
        await store.upsert(session_id, workspace_path, "idle")
        await store.update_state(session_id, "running")
        records = store.records()
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _STORE_FILE
        self._records: dict[str, SessionRecord] = {}
        self._loaded = False
        # One write at a time (TD-5016). Overlapping writers used to share
        # one temp name, and the second rename found it already moved.
        self._write_lock = asyncio.Lock()
        # In-flight writes. The loop holds tasks weakly, and one whose
        # caller was cancelled has no other owner until it lands.
        self._writes: set[asyncio.Task[None]] = set()
        self._load()

    async def upsert(
        self,
        session_id: str,
        workspace_path: str,
        state: str,
        created_at: str | None = None,
        engine: str | None = None,
        preset: str | None = None,
        worktree: dict[str, Any] | None = None,
    ) -> None:
        """Insert or refresh a session record and persist the store.

        ``created_at`` is only set on first insert, so later state refreshes
        preserve the original creation timestamp. ``engine`` is the loop
        this session started with; omitted on a refresh keeps the stored
        value so a later ``set_engine`` cannot rewrite history. ``preset``
        is the same: omitted keeps whatever the row already had (TD-1721).
        """
        existing = self._records.get(session_id)
        if preset is not None:
            chosen_preset = preset
        elif existing is not None:
            chosen_preset = existing.preset
        else:
            chosen_preset = ""
        chosen_engine: str | None
        if engine is not None:
            chosen_engine = engine
        elif existing is not None:
            chosen_engine = existing.engine
        else:
            chosen_engine = None
        record = SessionRecord(
            session_id=session_id,
            workspace_path=workspace_path,
            state=state,
            created_at=existing.created_at if existing else (created_at or _now_iso()),
            archived=existing.archived if existing else False,
            title=existing.title if existing else None,
            auto_title=existing.auto_title if existing else None,
            engine=chosen_engine,
            preset=chosen_preset,
            worktree=worktree if worktree is not None or existing is None else existing.worktree,
            plan_mode=existing.plan_mode if existing else False,
            effort=existing.effort if existing else None,
        )
        self._records[session_id] = record
        await self._persist()

    async def update_state(self, session_id: str, state: str) -> None:
        """Refresh a session's state, keeping created_at unchanged."""
        record = self._records.get(session_id)
        if record is None:
            return
        record.state = state
        record.updated_at = _now_iso()
        await self._persist()

    async def touch(self, session_id: str) -> None:
        """Mark a session used now: a turn started in it (TD-5111)."""
        record = self._records.get(session_id)
        if record is None:
            return
        record.updated_at = _now_iso()
        await self._persist()

    async def set_archived(self, session_id: str, archived: bool) -> bool:
        """File a session away, or restore it (TD-1715).

        Returns False when the id is unknown, so the caller can answer with
        a typed error rather than pretending the write landed.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        record.archived = archived
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def maybe_set_title(self, session_id: str, content: str) -> bool:
        """Title from the first non-empty user message. Never overwrites.

        Sets ``auto_title`` once. Also fills ``title`` when the display
        title is still empty, so a rename that landed first is kept.
        Returns True when ``auto_title`` was written. False when the id
        is unknown, ``auto_title`` is already set, or ``content`` is
        empty after trim (attachment-only / whitespace).
        """
        record = self._records.get(session_id)
        if record is None or record.auto_title is not None:
            return False
        title = title_from_user_message(content)
        if title is None:
            return False
        record.auto_title = title
        if record.title is None:
            record.title = title
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def set_title(self, session_id: str, title: str | None) -> bool:
        """Set the display title, or restore auto_title when empty (TD-3002).

        A non-empty value is trimmed, first-lined, and capped the same way
        as the first-message title. Empty, whitespace-only, or ``None``
        restores ``auto_title`` (itself ``None`` when there was never a
        first-message title — the rail then falls back to the short id).
        Does not touch ``auto_title``. Returns False when the id is unknown.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        normalized = title_from_user_message(title) if title is not None else None
        record.title = normalized if normalized is not None else record.auto_title
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def set_preset(self, session_id: str, preset: str) -> bool:
        """Record the catalog preset this session is using (TD-1721).

        Name only — the catalog in Settings is unchanged. Returns False
        when the id is unknown.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        record.preset = preset
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def set_plan_mode(self, session_id: str, on: bool) -> bool:
        """Record whether this session is in read-only plan mode (TD-5123).

        Writes only on a change: every ``tier_state`` comes through here.
        Returns False when the id is unknown.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        if record.plan_mode is on:
            return True
        record.plan_mode = on
        await self._persist()
        return True

    async def set_effort(self, session_id: str, effort: str) -> bool:
        """Record the session's reasoning effort so a restart keeps it.

        Writes only on a change: every ``tier_state`` comes through here.
        Returns False when the id is unknown.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        if record.effort == effort:
            return True
        record.effort = effort
        await self._persist()
        return True

    async def set_workspace(self, session_id: str, workspace_path: str) -> bool:
        """Reassign a session to another workspace (TD-1715 move to project).

        Durable half of the move: the record keeps its id, creation time,
        and archived flag, so nothing about the session's history is
        rewritten — only where it now lives.
        """
        record = self._records.get(session_id)
        if record is None:
            return False
        record.workspace_path = workspace_path
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def set_worktree(self, session_id: str, worktree: dict[str, Any] | None) -> bool:
        """Record the session's worktree, or None once it is closed (TD-5126)."""
        record = self._records.get(session_id)
        if record is None:
            return False
        record.worktree = worktree
        record.updated_at = _now_iso()
        await self._persist()
        return True

    async def remove(self, session_id: str) -> None:
        """Drop a session from the store and persist."""
        if session_id in self._records:
            del self._records[session_id]
            await self._persist()

    def records(self) -> list[SessionRecord]:
        """Return the current in-memory records (read-mostly cheap)."""
        return list(self._records.values())

    def get(self, session_id: str) -> SessionRecord | None:
        return self._records.get(session_id)

    def _load(self) -> None:
        """Load existing records from disk (called once at construction).

        A missing or corrupt store is non-fatal: the daemon starts with an
        empty list rather than crashing on a bad snapshot.
        """
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "session store unreadable, starting empty",
                extra={"extra_fields": {"path": str(self._path), "error": str(e)}},
            )
            return
        if not isinstance(raw, list):
            log.warning("session store malformed, starting empty")
            return
        for entry in raw:
            try:
                record = SessionRecord(**entry)
            except TypeError:
                log.warning(
                    "dropping malformed session record",
                    extra={"extra_fields": {"entry": entry}},
                )
                continue
            # Stores written before auto_title treated title as the
            # first-message title. Copy it so empty-rename still restores.
            if isinstance(entry, dict) and "auto_title" not in entry and record.title is not None:
                record.auto_title = record.title
            self._records[record.session_id] = record
        log.info(
            "session store loaded",
            extra={"extra_fields": {"count": len(self._records)}},
        )

    async def _persist(self) -> None:
        """Write records atomically off the event loop, one write at a time.

        The write runs as its own task and the caller only waits on it. A
        cancelled caller cannot stop the thread, so the lock must stay held
        until the thread is done; otherwise a later write could land first
        and then be overwritten by this older payload.
        """
        write = asyncio.create_task(self._write_in_order())
        self._writes.add(write)
        write.add_done_callback(self._writes.discard)
        await asyncio.shield(write)

    async def _write_in_order(self) -> None:
        # The payload is built on the loop under the lock, so the thread
        # never reads ``_records`` while the loop changes it, and the last
        # write to land holds every change made before it took the lock.
        async with self._write_lock:
            payload = [asdict(r) for r in self._records.values()]
            await asyncio.to_thread(self._persist_sync, payload)

    def _persist_sync(self, payload: list[dict[str, object]]) -> None:
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # A unique temp name per write, created 0600 by mkstemp.
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{_STORE_FILE}.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, indent=2))
                f.flush()
                os.fsync(f.fileno())
            # Owner-only before the rename. POSIX mode bits only: on Windows
            # os.chmod can merely toggle the read-only flag, so this is a
            # silent no-op there — ACL-based restriction is a separate
            # hardening story (TD-1406).
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
            tmp = None
        except OSError as e:
            log.warning(
                "failed to write session store",
                extra={"extra_fields": {"path": str(self._path), "error": str(e)}},
            )
        finally:
            # Set only while this write's own temp file is still there.
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
