"""TD-5015: lanes, the control link and what a viewer sees through them.

A real :class:`Bridge` dials a scripted other computer
(:mod:`tests.fake_remote`) over 127.0.0.1. Link backoff sleeps are cut
to 50 ms and the hub's clocks are shrunk with :class:`Timings`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.fake_remote import FakeRemote, row
from tests.machine_helpers import LOCAL, Clock, FakeKeychain, eventually
from tstd.bridge import Bridge
from tstd.bridge_link import LOCKED_MESSAGE, Sleep
from tstd.bridge_machine import Timings
from tstd.machines import MachineRecord, MachineRegistry

MCH = "mch_0123456789ab"
KEY = "k" * 64
S1 = f"s1:{MCH}"
FAST = Timings(linger_seconds=0.3, queue_seconds=0.3, queue_limit=3, debounce_seconds=0.05)


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(min(seconds, 0.05))


class _Viewer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def next(self, within: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self.frames.get(), within)

    async def nothing(self, within: float = 0.3) -> None:
        with pytest.raises(TimeoutError):
            await self.next(within)


class _Hub:
    """The Bridge with the Hub's callbacks recorded."""

    def __init__(
        self,
        tmp: Path,
        remote: FakeRemote,
        timings: Timings,
        clock: Any,
        link_sleep: Sleep = _fast_sleep,
    ) -> None:
        self.registry = MachineRegistry(tmp)
        self.registry.load()
        self.registry.put(
            MachineRecord(
                id=MCH,
                name="ezer",
                host="127.0.0.1",
                port=remote.port,
                device_id="dev_000000000001",
                paired_at="2026-10-14T00:00:00Z",
                machine_id=remote.machine_id,
            )
        )
        self.rows: list[tuple[bool, Any]] = []
        self.bridge = Bridge(
            self.registry,
            machine_id=lambda: "mid_ffffffffffffffff",
            deliver=self._deliver,
            on_rows=self._on_rows,
            on_machines=self._on_machines,
            policy=LOCAL,
            clock=clock,
            link_sleep=link_sleep,
            timings=timings,
        )

    async def _deliver(self, viewer: _Viewer, payload: str) -> None:
        viewer.frames.put_nowait(json.loads(payload))

    async def _on_rows(self, changed: bool, requester: Any) -> None:
        self.rows.append((changed, requester))

    async def _on_machines(self) -> None:
        return None

    def forward(self, viewer: _Viewer, **frame: Any) -> dict[str, Any] | None:
        reply = self.bridge.forward(viewer, frame)
        return None if reply is None else json.loads(reply)


def _chunk_read(sid: str) -> dict[str, Any]:
    return {"type": "read_artifact_chunk", "session_id": sid, "artifact_id": "a" * 32, "offset": 0}


@pytest.fixture
async def remote(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeRemote]:
    FakeKeychain().install(monkeypatch).keys[MCH] = KEY
    srv = FakeRemote(KEY)
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


@pytest.fixture
async def hub(tmp_path: Path, remote: FakeRemote) -> AsyncIterator[_Hub]:
    made = _Hub(tmp_path, remote, FAST, time.monotonic)
    try:
        yield made
    finally:
        await made.bridge.stop()


async def _connected(hub: _Hub, viewer: _Viewer) -> None:
    hub.bridge.viewer_connected(viewer)
    await eventually(lambda: hub.bridge.status_of(MCH) == "connected")
    await eventually(lambda: bool(hub.bridge.cached_rows()))


# ── the control link and rows ────────────────────────────────────────


async def test_rows_come_back_bridged_tagged_and_one_hop(hub: _Hub, remote: FakeRemote) -> None:
    remote.rows = [
        row("s1"),
        row("s2:mch_ba9876543210", machine="mch_ba9876543210"),
        row("s3", machine="mch_ba9876543210"),
    ]
    await _connected(hub, _Viewer())
    assert [r["session_id"] for r in hub.bridge.cached_rows()] == [S1, f"s3:{MCH}"]
    assert {r["machine"] for r in hub.bridge.cached_rows()} == {MCH}
    assert hub.rows[0] == (True, None)
    record = hub.registry.get(MCH)
    assert record is not None and record.last_seen is not None


async def test_dialing_stops_after_the_linger_and_a_return_cancels_it(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    await hub.bridge.viewer_closed(viewer)
    again = _Viewer()
    hub.bridge.viewer_connected(again)
    await asyncio.sleep(FAST.linger_seconds + 0.2)
    assert remote.conns, "a viewer came back inside the linger"
    await hub.bridge.viewer_closed(again)
    await eventually(lambda: not remote.conns, within=3)
    assert hub.bridge.cached_rows(), "last-known rows stay"
    dials = len(remote.got("list_sessions"))
    await asyncio.sleep(0.3)
    assert len(remote.got("list_sessions")) == dials


# ── lanes ────────────────────────────────────────────────────────────


async def test_attach_replays_with_bridged_ids_and_seq_untouched(
    hub: _Hub, remote: FakeRemote
) -> None:
    for _ in range(3):
        await remote.emit("s1")
    viewer = _Viewer()
    await _connected(hub, viewer)
    assert hub.forward(viewer, type="attach", session_id=S1, from_seq=2) is None
    got = [await viewer.next(), await viewer.next()]
    assert [(f["session_id"], f["seq"]) for f in got] == [(S1, 2), (S1, 3)]
    ((_, attach),) = remote.got("attach")
    assert attach == {"type": "attach", "session_id": "s1", "from_seq": 2}


async def test_seq_continues_across_an_upstream_drop_with_no_gap_or_repeat(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    hub.forward(viewer, type="attach", session_id=S1, from_seq=1)
    await eventually(lambda: bool(remote.got("attach")))
    await remote.emit("s1")
    await remote.emit("s1")
    assert [(await viewer.next())["seq"] for _ in range(2)] == [1, 2]
    await remote.drop()
    await eventually(lambda: not hub.bridge.lane(viewer, MCH).is_open)  # type: ignore[union-attr]
    await remote.emit("s1")  # while the lane is down
    await eventually(lambda: len(remote.got("attach")) == 2, within=5)
    assert remote.got("attach")[1][1]["from_seq"] == 3
    await remote.emit("s1")
    assert [(await viewer.next())["seq"] for _ in range(2)] == [3, 4]
    await viewer.nothing()


async def test_verbs_wait_for_the_lane_then_go_in_order(hub: _Hub, remote: FakeRemote) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    assert hub.forward(viewer, type="attach", session_id=S1, from_seq=1) is None
    assert hub.forward(viewer, type="user_message", session_id=S1, content="hi") is None
    assert (await viewer.next())["delta"] == "hi"
    kinds = [f["type"] for i, f in remote.received if f.get("session_id") == "s1"]
    assert kinds == ["attach", "user_message"]


async def test_a_full_or_expired_queue_answers_machine_unreachable(
    hub: _Hub, remote: FakeRemote
) -> None:
    remote.stall = True
    viewer = _Viewer()
    hub.bridge.viewer_connected(viewer)
    await remote.stop()  # nothing answers now: the lane stays connecting
    for _ in range(FAST.queue_limit - 1):
        assert hub.forward(viewer, type="cancel", session_id=S1) is None
    assert hub.forward(viewer, **_chunk_read(S1)) is None
    full = hub.forward(viewer, **_chunk_read(S1))
    assert full is not None and full["code"] == "machine_unreachable"
    assert full["session_id"] == S1 and full["machine"] == MCH
    assert full["in_reply_to"] == "read_artifact_chunk"  # TD-5144
    expired = [await viewer.next(2) for _ in range(FAST.queue_limit)]
    assert {(e["code"], e["session_id"]) for e in expired} == {("machine_unreachable", S1)}
    assert sorted(e["in_reply_to"] for e in expired) == ["cancel", "cancel", "read_artifact_chunk"]


async def test_an_unreachable_computer_answers_at_once(tmp_path: Path, remote: FakeRemote) -> None:
    clock = Clock()
    made = _Hub(tmp_path, remote, FAST, clock)
    await remote.stop()
    try:
        made.bridge.viewer_connected(_Viewer())
        control = made.bridge._machines[MCH].link
        assert control is not None
        # The 60 s count starts with the link; then time passes.
        await eventually(lambda: bool(control.delays))
        assert made.bridge.status_of(MCH) == "connecting"
        clock.advance(61)
        await eventually(lambda: made.bridge.status_of(MCH) == "unreachable", within=3)
        reply = made.forward(_Viewer(), type="user_message", session_id=S1, content="x")
        assert reply is not None and reply["code"] == "machine_unreachable"
        assert reply["message"] == "ezer isn't reachable right now. Try again when it is."
        assert reply["in_reply_to"] == "user_message"  # TD-5144
        chunk = made.forward(_Viewer(), **_chunk_read(S1))
        assert chunk is not None and chunk["code"] == "machine_unreachable"
        assert chunk["in_reply_to"] == "read_artifact_chunk"
    finally:
        await made.bridge.stop()


async def test_a_slow_computer_never_blocks_the_viewer(hub: _Hub, remote: FakeRemote) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    remote.stall = True
    started = time.monotonic()
    for _ in range(50):
        hub.forward(viewer, type="user_message", session_id=S1, content="x")
        hub.forward(viewer, type="rename_session", session_id=S1, title="t")
        hub.bridge.cached_rows()
    assert time.monotonic() - started < 0.2
    assert [r["session_id"] for r in hub.bridge.cached_rows()] == [S1]


async def test_a_lifecycle_reply_becomes_rows_for_that_viewer(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    remote.rows = [row("s1", title="renamed")]
    hub.forward(viewer, type="rename_session", session_id=S1, title="renamed")
    await eventually(lambda: (True, viewer) in hub.rows)
    assert hub.bridge.cached_rows()[0]["title"] == "renamed"
    await viewer.nothing()  # the hub, not the lane, sends the merged list


async def test_a_viewer_closing_closes_its_lanes_only(hub: _Hub, remote: FakeRemote) -> None:
    one, two = _Viewer(), _Viewer()
    await _connected(hub, one)
    hub.bridge.viewer_connected(two)
    hub.forward(one, type="attach", session_id=S1, from_seq=1)
    hub.forward(two, type="attach", session_id=S1, from_seq=1)
    await eventually(lambda: len(remote.conns) == 3)
    await hub.bridge.viewer_closed(one)
    await eventually(lambda: len(remote.conns) == 2)
    assert hub.bridge.lane(one, MCH) is None and hub.bridge.lane(two, MCH) is not None
    await remote.emit("s1")
    assert (await two.next())["seq"] == 1
    await one.nothing()


# ── approvals are never queued ───────────────────────────────────────


async def test_an_approval_with_no_lane_goes_over_the_control_link(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    remote.approve_error = True
    reply = hub.forward(viewer, type="approve", session_id=S1, tool_call_id="tc-1")
    assert reply is None
    error = await viewer.next()
    assert error["code"] == "no_pending_approval" and error["session_id"] == S1
    assert error["machine"] == MCH
    ((conn, approve),) = remote.got("approve")
    control = remote.got("list_sessions")[0][0]
    assert conn == control and approve["session_id"] == "s1"
    # The lane still opened for what comes next.
    await eventually(lambda: hub.bridge.lane(viewer, MCH).is_open)  # type: ignore[union-attr]


async def test_an_approval_with_the_lane_open_goes_on_the_lane(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    hub.forward(viewer, type="attach", session_id=S1, from_seq=1)
    await eventually(lambda: bool(remote.got("attach")))
    hub.forward(viewer, type="deny", session_id=S1, tool_call_id="tc-1")
    await eventually(lambda: bool(remote.got("deny")))
    assert remote.got("deny")[0][0] == remote.got("attach")[0][0]


async def test_an_approval_with_nothing_open_fails_at_once(
    tmp_path: Path, remote: FakeRemote
) -> None:
    made = _Hub(tmp_path, remote, FAST, time.monotonic)
    await remote.stop()
    try:
        viewer = _Viewer()
        made.bridge.viewer_connected(viewer)
        reply = made.forward(viewer, type="always_allow", session_id=S1, tool_call_id="tc-1")
        assert reply is not None and reply["code"] == "machine_unreachable"
        assert reply["session_id"] == S1
        await viewer.nothing()  # and nothing arrives later
    finally:
        await made.bridge.stop()


# ── what a lane drops ────────────────────────────────────────────────


async def test_second_hop_and_machine_wide_frames_never_reach_the_viewer(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    hub.forward(viewer, type="attach", session_id=S1, from_seq=1)
    await eventually(lambda: bool(remote.got("attach")))
    lane_ws = remote.conns[remote.got("attach")[0][0]]
    hop = {"type": "assistant_delta", "session_id": "s9:mch_ba9876543210", "delta": "x", "seq": 1}
    await lane_ws.send(json.dumps(hop))
    await lane_ws.send(json.dumps({"type": "job_list", "jobs": [], "seq": 1}))
    await remote.emit("s1")
    assert (await viewer.next())["session_id"] == S1
    await viewer.nothing()


# ── re-pairing and parked computers ──────────────────────────────────


async def test_re_pairing_keeps_an_open_lane_streaming_from_where_it_was(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    await _connected(hub, viewer)
    hub.forward(viewer, type="attach", session_id=S1, from_seq=1)
    await eventually(lambda: bool(remote.got("attach")))
    await remote.emit("s1")
    assert (await viewer.next())["seq"] == 1
    # Re-paired: a new key, and the other computer dropped the old device.
    remote.key = "n" * 64
    hub.bridge.keys.put(MCH, remote.key)
    await remote.drop()
    record = hub.registry.get(MCH)
    assert record is not None
    await hub.bridge._install(record)
    assert hub.forward(viewer, type="user_message", session_id=S1, content="again") is None
    await eventually(lambda: len(remote.got("attach")) == 2)
    assert remote.got("attach")[1][1]["from_seq"] == 2
    got = await viewer.next()
    assert (got["session_id"], got["seq"], got["delta"]) == (S1, 2, "again")
    await viewer.nothing()


async def test_removing_a_computer_answers_the_verbs_still_waiting(
    hub: _Hub, remote: FakeRemote
) -> None:
    viewer = _Viewer()
    hub.bridge.viewer_connected(viewer)
    await remote.stop()  # the lane stays connecting
    assert hub.forward(viewer, type="cancel", session_id=S1) is None
    assert await hub.bridge.remove(MCH) is None
    answer = await viewer.next(1)
    assert (answer["code"], answer["session_id"]) == ("machine_unreachable", S1)


async def _park_long(seconds: float) -> None:
    # Backoff and the 60 s count stay quick; the 5-minute parked retry never comes.
    await asyncio.sleep(0.05 if seconds < 100 else 3600)


async def test_a_parked_computer_is_not_woken_by_viewers_or_verbs(
    tmp_path: Path, remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain = FakeKeychain().install(monkeypatch)
    keychain.keys[MCH] = KEY
    keychain.locked = True
    made = _Hub(tmp_path, remote, FAST, time.monotonic, link_sleep=_park_long)
    try:
        first = _Viewer()
        made.bridge.viewer_connected(first)
        await eventually(lambda: made.bridge.status_of(MCH) == "locked")
        for _ in range(4):
            made.bridge.viewer_connected(_Viewer())  # windows and phones come and go
            await asyncio.sleep(0.02)
        sent = made.forward(first, type="user_message", session_id=S1, content="x")
        assert sent is not None and sent["code"] == "machine_unreachable"
        assert sent["session_id"] == S1 and sent["message"] == LOCKED_MESSAGE.format(name="ezer")
        approve = made.forward(first, type="approve", session_id=S1, tool_call_id="tc-1")
        assert approve is not None and approve["code"] == "machine_unreachable"
        assert made.forward(first, type="attach", session_id=S1, from_seq=1) is None
        await asyncio.sleep(0.2)
        assert keychain.reads == 1  # one prompt at most, not one per viewer or verb
        assert not remote.conns
        # "Try now" with the keychain open: both links dial and the attach goes.
        keychain.locked = False
        assert made.bridge.reconnect(MCH) is None
        await eventually(lambda: made.bridge.status_of(MCH) == "connected")
        await eventually(lambda: bool(remote.got("attach")))
        await remote.emit("s1")
        assert (await first.next())["seq"] == 1
    finally:
        await made.bridge.stop()


async def test_refusals_for_an_unknown_computer_or_id_name_the_verb(
    hub: _Hub, remote: FakeRemote
) -> None:
    """TD-5144: the viewer can tie a hub refusal to the request it made."""
    viewer = _Viewer()
    gone = "s1:mch_ffffffffffff"
    for frame in (_chunk_read(gone), {"type": "user_message", "session_id": gone, "content": "x"}):
        reply = hub.forward(viewer, **frame)
        assert reply is not None and reply["code"] == "machine_not_found", reply
        assert (reply["session_id"], reply["in_reply_to"]) == (gone, frame["type"])
    for frame in (_chunk_read("s1"), {"type": "user_message", "session_id": "s1", "content": "x"}):
        reply = hub.forward(viewer, **frame)
        assert reply is not None and reply["code"] == "bad_request", reply
        assert reply["in_reply_to"] == frame["type"]
