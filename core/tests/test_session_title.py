"""Auto-title from the first user message (TD-3001)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_session_lifecycle import (
    _connect_and_handshake,
    _open_workspace,
    _request,
    _RunningDaemon,
    _summary,
)


async def _send_user_message(
    ws: Any, session_id: str, content: str, attachments: list[dict[str, str]] | None = None
) -> None:
    payload: dict[str, Any] = {
        "type": "user_message",
        "session_id": session_id,
        "content": content,
    }
    if attachments is not None:
        payload["attachments"] = attachments
    await ws.send(json.dumps(payload))


async def _list_sessions(ws: Any) -> dict[str, Any]:
    await ws.send(json.dumps({"type": "list_sessions"}))
    while True:
        frame = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
        if frame["type"] == "session_list":
            return frame


class TestSessionTitleWire:
    @pytest.mark.asyncio
    async def test_first_message_titles_the_session_list(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]

            listing = await _list_sessions(ws)
            row = _summary(listing, sid)
            assert row is not None
            assert "title" in row
            assert row["title"] is None

            await _send_user_message(ws, sid, "  Fix the rail titles  ")
            listing = await _list_sessions(ws)
            row = _summary(listing, sid)
            assert row is not None
            assert row["title"] == "Fix the rail titles"
            await ws.close()

    @pytest.mark.asyncio
    async def test_later_messages_do_not_retitle(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            await _send_user_message(ws, sid, "First title")
            await _send_user_message(ws, sid, "Second should not win")
            listing = await _list_sessions(ws)
            row = _summary(listing, sid)
            assert row is not None
            assert row["title"] == "First title"
            await ws.close()

    @pytest.mark.asyncio
    async def test_empty_and_attachment_only_keep_the_short_id(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            empty_sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            await _send_user_message(ws, empty_sid, "   ")
            listing = await _list_sessions(ws)
            row = _summary(listing, empty_sid)
            assert row is not None
            assert row["title"] is None

            attach_sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            await _send_user_message(
                ws,
                attach_sid,
                "",
                attachments=[{"name": "notes.md", "content_b64": "IyBUaXRsZQo="}],
            )
            listing = await _list_sessions(ws)
            row = _summary(listing, attach_sid)
            assert row is not None
            assert row["title"] is None
            await ws.close()

    @pytest.mark.asyncio
    async def test_title_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async with _RunningDaemon(data_dir) as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(workspace)))["session_id"]
            await _send_user_message(ws, sid, "Persisted title")
            await _list_sessions(ws)
            await ws.close()

        async with _RunningDaemon(data_dir) as restarted:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{restarted.ws_server.port}", restarted.ws_server.token
            )
            listing = await _request(ws, {"type": "list_sessions"})
            row = _summary(listing, sid)
            assert row is not None
            assert row["title"] == "Persisted title"
            await ws.close()


class TestSessionListBusy:
    @pytest.mark.asyncio
    async def test_session_list_busy_follows_turn_in_flight(self, tmp_path: Path) -> None:
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            listing = await _list_sessions(ws)
            row = _summary(listing, sid)
            assert row is not None
            assert row["busy"] is False
            assert row["state"] == "running"

            session = daemon.session_registry.get(sid)
            assert session is not None
            await session.add_user_message("hello")
            listing = await _list_sessions(ws)
            row = _summary(listing, sid)
            assert row is not None
            assert row["busy"] is True
            assert row["state"] == "running"
            await ws.close()
