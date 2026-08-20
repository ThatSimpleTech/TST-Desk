"""JSON durability for the session registry.

The registry list (session id, workspace path, state, timestamps) lives
here. The transcript and model conversation live in ``session_persist``.
Together they are what a restart uses: a row without a conversation
snapshot is still a tombstone; a row with one is revived.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .logging import get_logger

log = get_logger("tstd.session_store")

_STORE_FILE = "sessions.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        self._load()

    async def upsert(
        self,
        session_id: str,
        workspace_path: str,
        state: str,
        created_at: str | None = None,
    ) -> None:
        """Insert or refresh a session record and persist the store.

        ``created_at`` is only set on first insert, so later state refreshes
        preserve the original creation timestamp.
        """
        existing = self._records.get(session_id)
        record = SessionRecord(
            session_id=session_id,
            workspace_path=workspace_path,
            state=state,
            created_at=existing.created_at if existing else (created_at or _now_iso()),
            archived=existing.archived if existing else False,
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
            self._records[record.session_id] = record
        log.info(
            "session store loaded",
            extra={"extra_fields": {"count": len(self._records)}},
        )

    async def _persist(self) -> None:
        """Write records atomically off the event loop."""
        await asyncio.to_thread(self._persist_sync)

    def _persist_sync(self) -> None:
        payload = [asdict(r) for r in self._records.values()]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{_STORE_FILE}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            # Owner-only before the rename. POSIX mode bits only: on Windows
            # os.chmod can merely toggle the read-only flag, so this is a
            # silent no-op there — ACL-based restriction is a separate
            # hardening story (TD-1406).
            tmp.chmod(0o600)
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning(
                "failed to write session store",
                extra={"extra_fields": {"path": str(self._path), "error": str(e)}},
            )
