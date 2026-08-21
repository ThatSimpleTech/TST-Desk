"""Durable session log: attach after restart, window, redaction (TD-2901)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION, AssistantDelta, ToolResult, UserTurn
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore

SECRET = "sk-PROJEXAMPLEKEYfakefake0000abcd"  # tst-secret-ok


async def _start_daemon(
    data_dir: Path, *, log_max_events: int | None = None
) -> tuple[Daemon, asyncio.Task[None]]:
    daemon = Daemon(data_dir=data_dir)
    if log_max_events is not None:
        daemon._session_persist = SessionPersist(data_dir, log_max_events=log_max_events)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(daemon: Daemon, task: asyncio.Task[None]) -> None:
    daemon._shutdown_event.set()
    await asyncio.gather(task, return_exceptions=True)


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws: Any, path: str) -> dict[str, Any]:
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    return dict(json.loads(await ws.recv()))


async def _recv_json(ws: Any) -> dict[str, Any]:
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))


class TestDurableLogRestart:
    @pytest.mark.asyncio
    async def test_attach_after_kill_replays_every_committed_seq(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        first, first_task = await _start_daemon(data_dir)
        try:
            uri = f"ws://127.0.0.1:{first.ws_server.port}"
            ws = await _connect_and_handshake(uri, first.ws_server.token)
            opened = await _open_workspace(ws, str(workspace))
            session_id = opened["session_id"]
            session = first.session_registry.get(session_id)
            assert session is not None
            for i in range(5):
                await session.event_log.add(
                    AssistantDelta(session_id=session_id, delta=f"pre-{i}", seq=1)
                )
            # Seq-committed means the subscriber has written the line. Read
            # the file before teardown so restart cannot depend on quit flush.
            events_path = first._session_persist.dir_for(session_id) / "events.jsonl"
            committed = [
                int(json.loads(line)["seq"])
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            await ws.close()
        finally:
            await _stop_daemon(first, first_task)

        second, second_task = await _start_daemon(data_dir)
        try:
            revived = second.session_registry.get(session_id)
            assert revived is not None
            expected = [event.seq for event in revived.event_log.events_from(1)]
            assert set(committed) <= set(expected)
            assert committed == expected[: len(committed)]

            uri = f"ws://127.0.0.1:{second.ws_server.port}"
            ws = await _connect_and_handshake(uri, second.ws_server.token)
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            replayed: list[dict[str, Any]] = []
            need = len(expected)
            if revived.event_log.earliest_seq > 1:
                need += 1
            for _ in range(need):
                replayed.append(await _recv_json(ws))
            session_frames = [frame for frame in replayed if frame["type"] != "log_trimmed"]
            seqs = [int(frame["seq"]) for frame in session_frames]
            assert seqs == expected
            assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
            await ws.close()
        finally:
            await _stop_daemon(second, second_task)

    @pytest.mark.asyncio
    async def test_attach_from_rotated_seq_sends_log_trimmed(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        store = SessionStore(data_dir)
        persist = SessionPersist(data_dir, log_max_events=100)
        await store.upsert("sid-trim", str(workspace), "running")
        persist.dir_for("sid-trim").mkdir(parents=True)
        for seq in range(1, 11):
            persist.append_event(
                "sid-trim",
                AssistantDelta(session_id="sid-trim", delta=f"e{seq}", seq=seq),
            )
        assert persist.load("sid-trim") is not None
        oversized = persist.dir_for("sid-trim") / "events.jsonl"
        assert oversized.read_text(encoding="utf-8").count("\n") == 10

        daemon, task = await _start_daemon(data_dir, log_max_events=3)
        try:
            sess = daemon.session_registry.get("sid-trim")
            assert sess is not None
            assert sess.state == "interrupted"
            assert [e.seq for e in sess.event_log.all_events] == [8, 9, 10]

            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            await ws.send(json.dumps({"type": "attach", "session_id": "sid-trim", "from_seq": 1}))
            notice = await _recv_json(ws)
            assert notice["type"] == "log_trimmed"
            assert notice["session_id"] == "sid-trim"
            assert notice["requested_from_seq"] == 1
            assert notice["earliest_seq"] == 8
            replayed = [await _recv_json(ws) for _ in range(3)]
            assert [frame["seq"] for frame in replayed] == [8, 9, 10]
            assert [frame["type"] for frame in replayed] == [
                "assistant_delta",
                "assistant_delta",
                "assistant_delta",
            ]
            await ws.close()
        finally:
            await _stop_daemon(daemon, task)

    @pytest.mark.asyncio
    async def test_live_window_keeps_memory_and_disk_aligned(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        daemon, task = await _start_daemon(data_dir, log_max_events=3)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            opened = await _open_workspace(ws, str(workspace))
            session_id = opened["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None
            for i in range(7):
                await session.event_log.add(
                    AssistantDelta(session_id=session_id, delta=f"w{i}", seq=1)
                )
            memory_seqs = [event.seq for event in session.event_log.all_events]
            loaded = daemon._session_persist.load(session_id)
            assert loaded is not None
            disk_seqs = [event.seq for event in loaded.events]
            assert memory_seqs == disk_seqs == [8, 9, 10]
            assert session.event_log.earliest_seq == 8
            assert session.event_log.last_seq == 10

            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            notice = await _recv_json(ws)
            assert notice["type"] == "log_trimmed"
            assert notice["earliest_seq"] == 8
            replayed = [await _recv_json(ws) for _ in range(3)]
            assert [frame["seq"] for frame in replayed] == [8, 9, 10]
            await ws.close()
        finally:
            await _stop_daemon(daemon, task)

    @pytest.mark.asyncio
    async def test_secrets_are_redacted_on_disk(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        daemon, task = await _start_daemon(data_dir)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            opened = await _open_workspace(ws, str(workspace))
            session_id = opened["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None
            await session.event_log.add(
                UserTurn(
                    session_id=session_id,
                    turn_id="t-secret",
                    content=f"here is {SECRET}",
                    seq=1,
                )
            )
            await session.event_log.add(
                ToolResult(
                    session_id=session_id,
                    tool_call_id="tc-1",
                    status="success",
                    output=f"echo {SECRET}",
                    seq=1,
                )
            )
            events_path = daemon._session_persist.dir_for(session_id) / "events.jsonl"
            raw = events_path.read_bytes()
            assert SECRET.encode() not in raw
            assert b"[REDACTED]" in raw
            await ws.close()
        finally:
            await _stop_daemon(daemon, task)
