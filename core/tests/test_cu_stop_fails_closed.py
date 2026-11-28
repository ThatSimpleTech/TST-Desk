"""Stop holds when its stop-file cannot be written (CU-05).

A failed create was only logged, and a second later the stop-file watcher
read "no file, latch engaged" as "cleared outside TST Desk" and let go:
the emergency stop un-pressed itself, silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, window
from tstd import cu_stopfile


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_stop_stays_on_and_says_why(
    tmp_path: Path, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cu_stopfile, "engage", refuse)
    monkeypatch.setattr(cu_stopfile, "stop_engaged", lambda: False)
    monkeypatch.setattr(cu_stopfile, "env_stop_engaged", lambda: False)
    daemon = await fleet.start(tmp_path / "d")
    async with window(daemon) as ws:
        state = await ask(ws, {"type": "set_cu_kill", "killed": True}, "cu_kill_state")
    assert state["killed"] is True
    assert "Permission denied" in (state["file_error"] or "")
    watch = asyncio.create_task(daemon._cu_stop_watch(interval=0.01))
    try:
        await asyncio.sleep(0.2)  # many watcher passes, each seeing no file
    finally:
        watch.cancel()
    assert daemon._cu_killed is True


async def test_resume_clears_the_error(
    tmp_path: Path, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cu_stopfile, "engage", lambda: (_ for _ in ()).throw(OSError(28, "No space left"))
    )
    monkeypatch.setattr(cu_stopfile, "clear", lambda: tmp_path / "STOP")
    monkeypatch.setattr(cu_stopfile, "env_stop_engaged", lambda: False)
    daemon = await fleet.start(tmp_path / "d")
    async with window(daemon) as ws:
        await ask(ws, {"type": "set_cu_kill", "killed": True}, "cu_kill_state")
        state = await ask(ws, {"type": "set_cu_kill", "killed": False}, "cu_kill_state")
    assert state["killed"] is False and state["file_error"] is None
    assert daemon._cu_file_error is None
