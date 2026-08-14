"""Tests for the durable session registry snapshot (TD-1002)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from tstd.session_store import SessionStore


class TestSessionStore:
    async def test_upsert_then_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws/one", "idle")
            recs = store.records()
            assert len(recs) == 1
            assert recs[0].session_id == "s1"
            assert recs[0].workspace_path == "/ws/one"
            assert recs[0].state == "idle"

    async def test_update_state_preserves_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle", created_at="t0")
            await store.update_state("s1", "running")
            rec = store.get("s1")
            assert rec is not None
            assert rec.state == "running"
            assert rec.created_at == "t0"

    async def test_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            await store.remove("s1")
            assert store.get("s1") is None
            assert store.records() == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="TD-1406: Windows has no POSIX mode bits — os.chmod only toggles "
        "the read-only flag, so 0o600 is a no-op (ACL hardening is a separate story)",
    )
    async def test_restricted_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            await store.upsert("s1", "/ws", "idle")
            path = Path(tmp) / "sessions.json"
            assert path.exists()
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    async def test_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await SessionStore(Path(tmp)).upsert("s1", "/ws", "idle")
            data = json.loads((Path(tmp) / "sessions.json").read_text())
            assert data[0]["session_id"] == "s1"
            assert "created_at" in data[0]
            assert "updated_at" in data[0]

    async def test_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            await SessionStore(Path(tmp)).upsert("s1", "/ws", "running")
            store2 = SessionStore(Path(tmp))
            rec = store2.get("s1")
            assert rec is not None
            assert rec.state == "running"
            assert rec.workspace_path == "/ws"

    async def test_missing_store_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            assert store.records() == []

    async def test_corrupt_store_loads_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sessions.json").write_text("{not valid json")
            store = SessionStore(Path(tmp))
            assert store.records() == []
