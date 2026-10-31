"""Restarts leave an idle chat's rail time alone, and its log nearly so (TD-5201).

Every start used to log a revived chat's state, wall and tier again, and
every clean stop its cancel, each stamping ``updated_at``. An idle chat's
event window wore down by four events a restart, and the rail, quick entry
and ``tst run --continue``, which all read ``updated_at`` as "used last"
(TD-5111), sorted chats by the order they were revived in.

What a cycle still adds is the pair a replay needs: the revive's
``running``, which tells a window the chat is live (a replay that ends on a
terminal state reads as an ended chat), and the stop's ``cancelled``, which
says the daemon stopped the loop (TD-5018) and keeps the row's state, a
tombstone's and the audit honest. Dropping that pair needs the window to
stop reading the replay's last state, a story of its own.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from tstd.daemon import Daemon, _session_model_config
from tstd.local_worker import tier_state_event
from tstd.mock import MockProvider, Script
from tstd.protocol import (
    PROTOCOL_VERSION,
    AssistantDelta,
    DaemonEvent,
    SessionState,
    TurnComplete,
    UserTurn,
)
from tstd.provider import ChatMessage
from tstd.session_lifecycle import boundary_update_event, config_source
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore
from tstd.shutdown_budget import ShutdownBudget

USED = "sid-used"
OLDER = "sid-older"
TOMBSTONE = "sid-tombstone"


async def _seed(
    data_dir: Path,
    workspace: Path,
    sid: str,
    updated: str,
    *,
    preset: str = "",
    conversation: bool = True,
) -> None:
    store = SessionStore(data_dir)
    await store.upsert(sid, str(workspace), "running", preset=preset)
    store._records[sid].updated_at = updated
    await store._persist()
    persist = SessionPersist(data_dir)
    persist.prepare(sid)
    turn: list[DaemonEvent] = [
        UserTurn(session_id=sid, turn_id="t1", content=f"hello {sid}", seq=1),
        AssistantDelta(session_id=sid, delta="hi", seq=2),
        TurnComplete(session_id=sid, tokens=1, cost=0, tier="brain", duration=0.1, seq=3),
    ]
    for event in turn:
        persist.append_event(sid, event)
    if conversation:
        said = [
            ChatMessage(role="user", content=f"hello {sid}"),
            ChatMessage(role="assistant", content="hi"),
        ]
        persist.save_conversation(sid, said)
    else:
        # prepare() writes an empty one, which a revive would start from.
        (persist.dir_for(sid) / "conversation.json").unlink()


def _daemon(data_dir: Path) -> Daemon:
    return Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content="ok")),
        shutdown_budget=ShutdownBudget(exit_process=lambda _code: None),
    )


async def _life(data_dir: Path) -> None:
    daemon = _daemon(data_dir)
    await daemon._restore_sessions()
    await daemon._shutdown()


def _logged(data_dir: Path, sid: str) -> list[str]:
    loaded = SessionPersist(data_dir).load(sid)
    assert loaded is not None
    return [f"{e.type}:{e.state}" if isinstance(e, SessionState) else e.type for e in loaded.events]


def _rows(data_dir: Path) -> dict[str, str]:
    return {r.session_id: r.updated_at for r in SessionStore(data_dir).records()}


async def _three_lives(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    # Made first but used last, so revive order and use order disagree.
    await _seed(data_dir, workspace, USED, "2026-10-02T10:00:00Z")
    # A preset since removed, which the restore moves the row off.
    await _seed(data_dir, workspace, OLDER, "2026-10-01T10:00:00Z", preset="gone")
    # No saved conversation: restored as a tombstone, its state fixed up.
    await _seed(data_dir, workspace, TOMBSTONE, "2026-09-30T10:00:00Z", conversation=False)
    return data_dir


async def test_restarts_never_move_a_chat_on_the_rail(tmp_path: Path) -> None:
    data_dir = await _three_lives(tmp_path)
    before = _rows(data_dir)
    for _ in range(3):
        await _life(data_dir)
        assert _rows(data_dir) == before
    records = {r.session_id: r for r in SessionStore(data_dir).records()}
    # The state still follows the daemon: what the row says it is.
    assert records[USED].state == "cancelled"
    assert records[TOMBSTONE].state == "interrupted"
    assert records[OLDER].preset != "gone"


async def test_a_restart_adds_only_the_start_and_the_stop(tmp_path: Path) -> None:
    data_dir = await _three_lives(tmp_path)
    counts = []
    for _ in range(3):
        await _life(data_dir)
        counts.append(len(_logged(data_dir, USED)))
    logged = _logged(data_dir, USED)
    # The first life logs the wall and the tier the window never had.
    assert logged[3:] == [
        "session_state:running",
        "boundary_update",
        "tier_state",
        "session_state:cancelled",
        "session_state:running",
        "session_state:cancelled",
        "session_state:running",
        "session_state:cancelled",
    ]
    assert [b - a for a, b in itertools.pairwise(counts)] == [2, 2]
    assert len(_logged(data_dir, TOMBSTONE)) == 3


async def _replay(daemon: Daemon, sid: str) -> list[dict[str, Any]]:
    """Attach from the start and take the whole window."""
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    last = sess.event_log.last_seq
    async with connect(f"ws://127.0.0.1:{daemon.ws_server.port}") as ws:
        hello = {"type": "hello", "token": daemon.ws_server.token, "version": PROTOCOL_VERSION}
        await ws.send(json.dumps(hello))
        await asyncio.wait_for(ws.recv(), timeout=5)
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        frames: list[dict[str, Any]] = []
        while not frames or frames[-1].get("seq") != last:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if frame.get("session_id") == sid:
                frames.append(frame)
    return frames


async def test_attach_after_quiet_restarts_shows_the_live_state_tier_and_wall(
    tmp_path: Path,
) -> None:
    data_dir = await _three_lives(tmp_path)
    for _ in range(3):
        await _life(data_dir)
    daemon = _daemon(data_dir)
    task = asyncio.create_task(daemon.run())
    try:
        frames = await _replay(daemon, USED)
        sess = daemon.session_registry.get(USED)
        assert sess is not None
        tier = tier_state_event(sess, _session_model_config(sess, daemon.config))
        wall = boundary_update_event(sess, config_source(sess))
    finally:
        daemon._shutdown_event.set()
        await asyncio.gather(task, return_exceptions=True)
    states = [f["state"] for f in frames if f["type"] == "session_state"]
    assert states[-1] == "running"
    tiers = [f for f in frames if f["type"] == "tier_state"]
    walls = [f for f in frames if f["type"] == "boundary_update"]
    assert len(tiers) == len(walls) == 1
    assert {**tiers[0], "seq": 0} == {**tier.model_dump(mode="json"), "seq": 0}
    assert {**walls[0], "seq": 0} == {**wall.model_dump(mode="json"), "seq": 0}
