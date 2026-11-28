"""Open the audit log so that a damaged one never stops the daemon (ERR2B-06).

The audit log records the work; nothing depends on it. A file that is not
a database or is cut short is kept aside as ``audit.db.damaged-<stamp>``
(with its WAL files) and a new log is started. A data folder the daemon
cannot write keeps this run's records in memory only. Either way the
daemon starts, and its windows are told (``store_recovered``, store
``audit``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .audit import AuditStore, audit_db_path
from .logging import get_logger
from .store_recovery import Recovery, set_aside

log = get_logger("tstd.audit_open")

#: The path sqlite reads as a database held in memory only.
MEMORY = Path(":memory:")


def _cannot_write(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "readonly" in text or "read-only" in text or "unable to open" in text


def open_audit_store(data_dir: Path) -> tuple[AuditStore, Recovery | None]:
    """The daemon's audit store, and what was done to open it, if anything."""
    path = audit_db_path(data_dir)
    try:
        return AuditStore(path), None
    except (sqlite3.Error, OSError) as exc:
        first = exc
    error = str(first)
    if isinstance(first, OSError) or (isinstance(first, sqlite3.Error) and _cannot_write(first)):
        return _in_memory(error)
    kept = set_aside(path)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            set_aside(side)
    try:
        store = AuditStore(path)
    except (sqlite3.Error, OSError) as exc:
        return _in_memory(f"{error}; then {exc}")
    fields = {"error": error}
    log.warning("audit log damaged; a new one was started", extra={"extra_fields": fields})
    return store, Recovery(
        store="audit",
        kept_as=kept.name if kept is not None else path.name,
        from_backup=0,
        rebuilt=0,
        error=error,
    )


def _in_memory(error: str) -> tuple[AuditStore, Recovery]:
    log.warning(
        "audit log cannot be opened; this run's records stay in memory",
        extra={"extra_fields": {"error": error}},
    )
    return AuditStore(MEMORY), Recovery(
        store="audit", kept_as="", from_backup=0, rebuilt=0, error=error, memory_only=True
    )
