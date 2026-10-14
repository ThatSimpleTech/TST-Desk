"""``drain``: let open turns finish, then hold new ones for a restart (TD-5023).

Real daemons on 127.0.0.1 (the machine helpers' fleet) and the mock
provider. A slow stream is a turn that is still working; an ``ask``
policy parks a turn on an approval. The bridged case pairs two daemons
the way test_two_daemons does.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection

from tests.machine_helpers import FakeKeychain, Fleet, ask, eventually, recv_until, window
from tests.test_scheduler_retry import _NOW, _job
from tests.test_scheduler_retry import _daemon as _scheduler_daemon
from tests.test_two_daemons import VIEWER, _for, _open, _pair, _rows_with, _until, _writes
from tstd.daemon import DRAINING_MESSAGE, SHUTDOWN_SETTLE_SECONDS, Daemon, DrainHold
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete, UserTurn
from tstd.scheduler.store import get_job, save_job


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    return FakeKeychain().install(monkeypatch)


def _slow(seconds: float) -> MockProvider:
    """A stream of ten words spread over *seconds*."""
    words = " ".join(f"w{i}" for i in range(10))
    return MockProvider(default=Script(kind="stream", content=words, chunk_delay=seconds / 10))


def _drain(timeout_s: int, hold_s: int) -> dict[str, Any]:
    return {"type": "drain", "timeout_s": timeout_s, "hold_s": hold_s}


async def _send(ws: ClientConnection, sid: str, content: str) -> None:
    await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": content}))


async def _attached(ws: ClientConnection, sid: str) -> None:
    await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
    await recv_until(ws, lambda f: f.get("type") == "session_state" and f["session_id"] == sid)


def _completes(daemon: Daemon, sid: str) -> int:
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    return sum(isinstance(e, TurnComplete) for e in sess.event_log.all_events)


async def test_idle_daemon_drains_at_once(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(0.1))
    async with window(daemon) as ws:
        await _open(ws, tmp_path / "ws")
        started = time.monotonic()
        result = await ask(ws, _drain(30, 0), "drain_result")
        assert time.monotonic() - started < 1.0
    assert result == {
        "type": "drain_result",
        "seq": 1,
        "drained": True,
        "running": [],
        "awaiting_approval": [],
        "hold_until": None,
    }


async def test_drain_returns_after_the_open_turn_completes(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(1.0))
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        await _send(ws, sid, "go")
        await recv_until(ws, lambda f: f.get("type") == "assistant_delta")
        assert _completes(daemon, sid) == 0
        result = await ask(ws, _drain(10, 0), "drain_result")
        assert result["drained"] is True
        assert result["running"] == []
        assert _completes(daemon, sid) == 1


async def test_hold_refuses_a_new_turn_and_queues_nothing(tmp_path: Path, fleet: Fleet) -> None:
    mock = _slow(0.1)
    daemon = await fleet.start(tmp_path / "d", provider=mock)
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        held = await ask(ws, _drain(0, 60), "drain_result")
        assert held["drained"] is True
        assert isinstance(held["hold_until"], str)

        refused = await ask(ws, {"type": "user_message", "session_id": sid, "content": "hi"})
        assert refused == {
            "type": "error",
            "code": "daemon_draining",
            "message": DRAINING_MESSAGE,
            "session_id": sid,
        }
        forked = await ask(
            ws, {"type": "fork_from", "session_id": sid, "user_index": 0, "content": "x"}
        )
        assert forked["code"] == "daemon_draining"
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        assert sess.pending_user_messages == 0
        assert not sess.turn_in_flight
        assert not any(isinstance(e, UserTurn) for e in sess.event_log.all_events)
        assert mock.calls == []


async def test_turn_parked_on_an_approval_is_listed_not_waited_on(
    tmp_path: Path, fleet: Fleet
) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    daemon = await fleet.start(tmp_path / "d", provider=_writes(work, "one.txt"))
    async with window(daemon) as ws:
        sid = await _open(ws, work)
        await _attached(ws, sid)
        await _send(ws, sid, "write it")
        request = await recv_until(ws, lambda f: f.get("type") == "approval_request")
        started = time.monotonic()
        result = await ask(ws, _drain(20, 0), "drain_result")
        assert time.monotonic() - started < 2.0
        assert result["drained"] is False
        assert result["running"] == []
        assert result["awaiting_approval"] == [sid]
        # Approvals still pass, so the turn can finish.
        deny = {"type": "deny", "session_id": sid, "tool_call_id": request["tool_call_id"]}
        await ws.send(json.dumps(deny))
        await recv_until(ws, lambda f: f.get("type") == "turn_complete")


async def test_send_after_the_hold_is_accepted(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(0.1))
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        await ask(ws, _drain(0, 1), "drain_result")
        refused = await ask(ws, {"type": "user_message", "session_id": sid, "content": "early"})
        assert refused["code"] == "daemon_draining"
        await eventually(lambda: not daemon._drain.active, within=3)
        await _send(ws, sid, "now")
        done = await recv_until(ws, lambda f: f.get("type") in ("turn_complete", "error"))
        assert done["type"] == "turn_complete"


async def test_due_job_waits_for_the_hold(tmp_path: Path) -> None:
    daemon, data_dir, workspace = _scheduler_daemon(tmp_path)
    save_job(data_dir, _job(workspace, retries=0))
    try:
        daemon._drain.hold_for(60)
        assert await daemon.run_due_jobs(_NOW) == []
        held = get_job(data_dir, "inbox")
        assert held is not None and held.next_run == _NOW.isoformat()
        daemon._drain.hold_for(0)
        assert await daemon.run_due_jobs(_NOW) == ["inbox"]
    finally:
        await daemon._shutdown()


async def test_shutdown_gives_an_open_turn_a_short_settle(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(30.0))
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        await _send(ws, sid, "go")
        await recv_until(ws, lambda f: f.get("type") == "assistant_delta")
    started = time.monotonic()
    await fleet.stop(daemon)
    elapsed = time.monotonic() - started
    # It waited for the turn, and not for long.
    assert SHUTDOWN_SETTLE_SECONDS - 0.1 <= elapsed < SHUTDOWN_SETTLE_SECONDS + 1.0
    sess = daemon.session_registry.get(sid)
    assert sess is not None and sess.state == "cancelled"


async def test_hold_refuses_every_verb_that_starts_a_turn(tmp_path: Path, fleet: Fleet) -> None:
    """Not only user_message: anything that would start a turn the restart cuts."""
    daemon = await fleet.start(tmp_path / "d", provider=_slow(0.1))
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        await ask(ws, _drain(0, 60), "drain_result")
        for frame, session_id in (
            ({"type": "run_grok_command", "session_id": sid, "name": "compact"}, sid),
            ({"type": "approve_grok_plan", "session_id": sid}, sid),
            ({"type": "start_autonomy", "workspace_path": str(tmp_path / "ws")}, None),
            ({"type": "run_job", "job_id": "inbox"}, None),
        ):
            refused = await ask(ws, frame, "error")
            assert refused["code"] == "daemon_draining", frame
            assert refused.get("session_id") == session_id, frame
        sess = daemon.session_registry.get(sid)
        assert sess is not None and not sess.turn_in_flight
        assert len(await daemon.session_registry.list_sessions()) == 1


async def test_shutdown_during_a_drain_does_not_wait_for_it(tmp_path: Path, fleet: Fleet) -> None:
    """The server's close waits for the drain's handler, so the drain must stop."""
    daemon = await fleet.start(tmp_path / "d", provider=_slow(30.0))
    async with window(daemon) as ws, window(daemon) as cli:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        await _send(ws, sid, "go")
        await recv_until(ws, lambda f: f.get("type") == "assistant_delta")
        await cli.send(json.dumps(_drain(60, 0)))
        await eventually(lambda: daemon._drain.active)
        started = time.monotonic()
        await fleet.stop(daemon)
        elapsed = time.monotonic() - started
    assert elapsed < SHUTDOWN_SETTLE_SECONDS + 1.5
    sess = daemon.session_registry.get(sid)
    assert sess is not None and sess.state == "cancelled"


async def test_a_release_during_a_drain_is_not_undone(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(1.0))
    async with window(daemon) as ws, window(daemon) as cli:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        await _send(ws, sid, "go")
        await recv_until(ws, lambda f: f.get("type") == "assistant_delta")
        await cli.send(json.dumps(_drain(10, 60)))
        await eventually(lambda: daemon._drain.active)
        async with window(daemon) as other:
            released = await ask(other, _drain(0, 0), "drain_result")
        assert released["hold_until"] is None
        # The first drain stops waiting and answers; it never holds again.
        first = await recv_until(cli, lambda f: f.get("type") == "drain_result", within=3)
        assert first["hold_until"] is None
        await recv_until(ws, lambda f: f.get("type") == "turn_complete")
        assert not daemon._drain.active


async def test_release_clears_the_hold(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "d", provider=_slow(0.1))
    async with window(daemon) as ws:
        sid = await _open(ws, tmp_path / "ws")
        await _attached(ws, sid)
        held = await ask(ws, _drain(0, 600), "drain_result")
        assert held["hold_until"] is not None
        released = await ask(ws, _drain(0, 0), "drain_result")
        assert released["drained"] is True
        assert released["hold_until"] is None
        await _send(ws, sid, "go")
        done = await recv_until(ws, lambda f: f.get("type") in ("turn_complete", "error"))
        assert done["type"] == "turn_complete"


async def test_a_bridged_session_is_not_held(
    tmp_path: Path, fleet: Fleet, keychain: FakeKeychain
) -> None:
    """The hub forwards a bridged id before the drain check runs."""
    b = await fleet.start(tmp_path / "b", provider=_slow(0.1))
    a = await fleet.start(tmp_path / "a", provider=_slow(0.1))
    async with window(b) as b_screen, window(a, VIEWER) as hub:
        sid = await _open(b_screen, tmp_path / "ws-b")
        own = await _open(hub, tmp_path / "ws-a")
        mch = await _pair(hub, b)
        remote = f"{sid}:{mch}"
        await _rows_with(hub, remote)
        await ask(hub, _drain(0, 60), "drain_result")

        refused = await ask(hub, {"type": "user_message", "session_id": own, "content": "x"})
        assert refused["code"] == "daemon_draining"

        await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
        await _send(hub, remote, "go")
        done = await _until(hub, _for(remote, "turn_complete"))
        assert done["type"] == "turn_complete", done
        assert a._drain.active


def test_the_hold_ends_by_itself() -> None:
    now = [100.0]
    hold = DrainHold(clock=lambda: now[0])
    assert not hold.active and hold.until is None
    hold.hold_for(5)
    assert hold.active and hold.until is not None
    now[0] += 5
    assert not hold.active and hold.until is None
    # Shutdown closes it for good.
    hold.close()
    hold.hold_for(0)
    assert hold.active and hold.until is None
