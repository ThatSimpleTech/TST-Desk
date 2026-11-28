"""The daemon listens before it revives a single chat (the 0.5.4 start-up fix).

A long-used install (183 chats) failed to start in 0.5.4 and 0.5.5: every
chat was revived, each one walking its folder for the trust card, before
port.json was written, and the host gave up after 30 seconds. Now the port
opens first, chats revive in the background newest first, and a chat used
before its turn is revived alone, on the spot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.machine_helpers import ask, loopback_serve, window
from tstd.daemon import Daemon
from tstd.port_file import port_file_path


async def _seed(data: Path, work: Path, n: int) -> list[str]:
    daemon = Daemon(data_dir=data)
    ids = []
    for _ in range(n):
        raw = await daemon._start_session(str(work))
        assert raw is not None
        ids.append(str(json.loads(raw)["session_id"]))
    await daemon._shutdown()
    return ids


async def test_the_port_opens_before_any_chat_is_revived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    data = tmp_path / "d"
    ids = await _seed(data, work, 3)

    gate = asyncio.Event()
    real = Daemon._restore_record

    async def slow(self: Daemon, record: object) -> None:
        await gate.wait()  # a revive that would take for ever
        await real(self, record)  # type: ignore[arg-type]

    monkeypatch.setattr(Daemon, "_restore_record", slow)
    monkeypatch.setattr("tstd.ws.serve", loopback_serve())
    daemon = Daemon(data_dir=data)
    task = asyncio.create_task(daemon.run())
    try:
        for _ in range(200):
            if port_file_path(data).exists():
                break
            await asyncio.sleep(0.02)
        assert port_file_path(data).exists(), "port.json waited for revive"
        assert daemon.session_registry.get(ids[0]) is None  # nothing revived yet
        gate.set()
        await asyncio.wait_for(daemon._restore_done.wait(), 10)
        assert all(daemon.session_registry.get(i) is not None for i in ids)
    finally:
        daemon._shutdown_event.set()
        gate.set()
        await asyncio.wait_for(task, 10)


async def test_a_chat_used_first_is_revived_first_and_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    data = tmp_path / "d"
    ids = await _seed(data, work, 3)
    wanted = ids[0]

    others = asyncio.Event()
    real = Daemon._restore_record

    async def hold_the_rest(self: Daemon, record: object) -> None:
        if getattr(record, "session_id", None) != wanted:
            await others.wait()
        await real(self, record)  # type: ignore[arg-type]

    monkeypatch.setattr(Daemon, "_restore_record", hold_the_rest)
    monkeypatch.setattr("tstd.ws.serve", loopback_serve())
    daemon = Daemon(data_dir=data)
    task = asyncio.create_task(daemon.run())
    try:
        for _ in range(200):
            if daemon.ws_server.port > 0:
                break
            await asyncio.sleep(0.02)
        async with window(daemon) as ws:
            state = await asyncio.wait_for(
                ask(ws, {"type": "attach", "session_id": wanted, "from_seq": 1}), 5
            )
        assert state.get("session_id") == wanted
        assert daemon.session_registry.get(wanted) is not None
        assert daemon.session_registry.get(ids[1]) is None  # the rest still wait
    finally:
        others.set()
        daemon._shutdown_event.set()
        await asyncio.wait_for(task, 10)
