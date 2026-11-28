"""A damaged or unwritable audit.db never stops the daemon (ERR2B-06)."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, recv_until, window
from tstd.audit import audit_db_path
from tstd.audit_open import open_audit_store


def test_a_good_log_opens_as_before(tmp_path: Path) -> None:
    store, recovery = open_audit_store(tmp_path)
    assert recovery is None
    assert store.db_path == audit_db_path(tmp_path)


@pytest.mark.parametrize("damage", ["garbage", "cut"])
def test_a_damaged_log_is_kept_aside_and_a_new_one_started(tmp_path: Path, damage: str) -> None:
    path = audit_db_path(tmp_path)
    if damage == "garbage":
        path.write_bytes(os.urandom(8192))
    else:
        store, _ = open_audit_store(tmp_path)
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store._conn.close()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    before = path.read_bytes()
    store, recovery = open_audit_store(tmp_path)
    assert recovery is not None and recovery.store == "audit" and not recovery.memory_only
    kept = tmp_path / recovery.kept_as
    assert kept.name.startswith("audit.db.damaged-")
    assert kept.read_bytes() == before
    assert store.schema_version > 0  # a working, migrated log


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX modes, not root")
def test_a_read_only_folder_keeps_this_run_in_memory(tmp_path: Path) -> None:
    folder = tmp_path / "data"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        store, recovery = open_audit_store(folder)
        assert recovery is not None and recovery.memory_only
        assert store.schema_version > 0
        assert not audit_db_path(folder).exists()
    finally:
        folder.chmod(0o700)


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_the_daemon_starts_on_a_garbage_log_and_says_so(tmp_path: Path, fleet: Fleet) -> None:
    data = tmp_path / "d"
    data.mkdir()
    audit_db_path(data).write_bytes(os.urandom(8192))
    daemon = await fleet.start(data)
    async with window(daemon) as ws:
        event = await recv_until(ws, lambda f: f.get("type") == "store_recovered")
    assert event["store"] == "audit"
    assert event["kept_as"].startswith("audit.db.damaged-")
    assert event["memory_only"] is False
