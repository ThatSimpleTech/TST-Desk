"""Rename a session without touching the loop (TD-3002)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_session_lifecycle import (
    _busy_session,
    _connect_and_handshake,
    _open_workspace,
    _request,
    _RunningDaemon,
    _summary,
)
from tstd.session_lifecycle import rename_session
from tstd.session_store import SessionStore


class TestRenameWire:
    @pytest.mark.asyncio
    async def test_rename_rides_the_session_list(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            await daemon._session_store.maybe_set_title(sid, "First line")

            listing = await _request(
                ws, {"type": "rename_session", "session_id": sid, "title": "Custom"}
            )
            assert listing["type"] == "session_list"
            row = _summary(listing, sid)
            assert row is not None
            assert row["title"] == "Custom"

            listing = await _request(ws, {"type": "rename_session", "session_id": sid, "title": ""})
            restored = _summary(listing, sid)
            assert restored is not None
            assert restored["title"] == "First line"
            await ws.close()

    @pytest.mark.asyncio
    async def test_rename_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async with _RunningDaemon(data_dir) as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(workspace)))["session_id"]
            await _request(ws, {"type": "rename_session", "session_id": sid, "title": "Kept"})
            await ws.close()

        async with _RunningDaemon(data_dir) as restarted:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{restarted.ws_server.port}", restarted.ws_server.token
            )
            listing = await _request(ws, {"type": "list_sessions"})
            row = _summary(listing, sid)
            assert row is not None
            assert row["title"] == "Kept"
            await ws.close()

    @pytest.mark.asyncio
    async def test_rename_unknown_session_is_typed(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        refusal = await rename_session(store, "no-such-id", "x")
        assert refusal is not None
        assert json.loads(refusal)["code"] == "session_not_found"


class TestRenameWhileBusy:
    @pytest.mark.asyncio
    async def test_rename_is_allowed_mid_turn_and_leaves_the_turn_running(
        self, tmp_path: Path
    ) -> None:
        _registry, store, sid, session = await _busy_session(str(tmp_path))
        await store.maybe_set_title(sid, "what is in this repo?")

        assert await rename_session(store, sid, "New name") is None

        record = store.get(sid)
        assert record is not None
        assert record.title == "New name"
        assert record.auto_title == "what is in this repo?"
        assert session.turn_in_flight is True
        assert session.state == "running"
        assert session.cancel_requested is False

    @pytest.mark.asyncio
    async def test_rename_of_a_running_session_is_metadata_only(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            listing = await _request(
                ws, {"type": "rename_session", "session_id": sid, "title": "While running"}
            )
            row = _summary(listing, sid)
            assert row is not None
            assert row["state"] == "running", "renaming must not kill the session"
            assert row["title"] == "While running"
            await ws.close()
