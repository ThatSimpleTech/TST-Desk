"""TD-5195: Ultracode is a per-session switch beside Fast, not an effort level.

The switch rides in the session row's effort string ("high+ultracode"), an
older client's or config's ``ultracode`` level means Max with the switch on,
and ``ultracode`` never goes out as ``reasoning_effort``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.test_artifacts import _open, _shutdown
from tests.test_effort import _Capture, _tier
from tests.test_loop import wait_for_turn
from tests.test_set_tier import _connect_and_handshake, _open_workspace, _start_daemon
from tstd.config import ModelConfig, SessionConfig, load_config
from tstd.cost import CostTracker
from tstd.daemon import Daemon
from tstd.effort import (
    LEGACY_ULTRACODE,
    ULTRACODE_SUFFIX,
    effort_record,
    read_effort_record,
    split_legacy_effort,
    with_effort,
)
from tstd.loop import _stream_and_parse
from tstd.mock import MockProvider, Script
from tstd.protocol import SetEffort, SetUltracode, TierState, UserTurn, parse_client_message
from tstd.provider import ChatMessage
from tstd.router import TierRouter
from tstd.session import Session
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore

# ── the effort string ───────────────────────────────────────────────────


def test_split_legacy_effort() -> None:
    assert LEGACY_ULTRACODE == "ultracode"
    assert split_legacy_effort("ultracode") == ("max", True)
    assert split_legacy_effort("high") == ("high", False)
    assert split_legacy_effort("extra") == (None, False)
    assert split_legacy_effort(None) == (None, False)
    assert split_legacy_effort(3) == (None, False)


def test_effort_record_writes_the_suffix_only_when_on() -> None:
    assert ULTRACODE_SUFFIX == "+ultracode"
    assert effort_record("high", False) == "high"
    assert effort_record("high", True) == "high+ultracode"
    assert effort_record("max", True) == "max+ultracode"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("high+ultracode", ("high", True)),
        ("max+ultracode", ("max", True)),
        ("low", ("low", False)),
        ("xhigh", ("xhigh", False)),
        ("ultracode", ("max", True)),  # 0.4.1 to 0.4.3
        (None, (None, False)),
        ("", (None, False)),
        ("extra", (None, False)),
        ("extra+ultracode", (None, False)),
        ("ultracode+ultracode", (None, False)),
        ("+ultracode", (None, False)),
        ("high+fast", (None, False)),
    ],
)
def test_read_effort_record(stored: str | None, expected: tuple[str | None, bool]) -> None:
    assert read_effort_record(stored) == expected


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("on", [False, True])
def test_the_record_round_trips(level: Any, on: bool) -> None:
    assert read_effort_record(effort_record(level, on)) == (level, on)


# ── the wire ────────────────────────────────────────────────────────────


def _set_effort(**fields: Any) -> SetEffort:
    msg = parse_client_message(json.dumps({"type": "set_effort", "session_id": "s", **fields}))
    assert isinstance(msg, SetEffort)
    return msg


def test_set_effort_maps_an_old_clients_ultracode_to_max_with_the_switch_on() -> None:
    legacy = _set_effort(effort="ultracode")
    assert (legacy.effort, legacy.ultracode) == ("max", True)
    told = _set_effort(effort="ultracode", ultracode=False)
    assert (told.effort, told.ultracode) == ("max", False)
    plain = _set_effort(effort="high")
    assert (plain.effort, plain.ultracode) == ("high", None)
    assert _set_effort(effort="low", ultracode=True).ultracode is True


def test_set_ultracode_message_round_trips() -> None:
    msg = parse_client_message(json.dumps({"type": "set_ultracode", "session_id": "s", "on": True}))
    assert isinstance(msg, SetUltracode) and msg.on and msg.session_id == "s"


def test_tier_state_reads_an_old_effort_ultracode_as_max_with_the_switch_on() -> None:
    fresh = TierState(session_id="s", tier="brain", seq=1)
    assert (fresh.ultracode, fresh.ultracode_available) == (False, False)
    old = TierState.model_validate(
        {"type": "tier_state", "session_id": "s", "tier": "brain", "effort": "ultracode", "seq": 4}
    )
    assert (old.effort, old.ultracode) == ("max", True)
    line = TierState(session_id="s", tier="brain", effort="high", ultracode=True, seq=2)
    again = TierState.model_validate_json(line.model_dump_json())
    assert (again.effort, again.ultracode) == ("high", True)


# ── config ──────────────────────────────────────────────────────────────


def test_session_config_maps_a_legacy_default_effort() -> None:
    assert (SessionConfig().default_effort, SessionConfig().default_ultracode) == (
        "medium",
        False,
    )
    old = SessionConfig.model_validate({"default_effort": "ultracode"})
    assert (old.default_effort, old.default_ultracode) == ("max", True)
    told = SessionConfig.model_validate({"default_effort": "ultracode", "default_ultracode": False})
    assert (told.default_effort, told.default_ultracode) == ("max", False)
    on = SessionConfig.model_validate({"default_effort": "low", "default_ultracode": True})
    assert (on.default_effort, on.default_ultracode) == ("low", True)


def test_an_old_config_file_with_effort_ultracode_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("presets: {}\nsession:\n  default_effort: ultracode\n", encoding="utf-8")
    config = load_config(path)
    assert (config.session.default_effort, config.session.default_ultracode) == ("max", True)


# ── the router ──────────────────────────────────────────────────────────


def test_router_keeps_the_switch_through_effort_changes_and_resets() -> None:
    router = TierRouter()
    assert router.ultracode is False
    router.set_ultracode(True)
    router.set_effort("low")
    router.reset()
    assert router.ultracode is True and router.effort == "low"
    assert router.summary()["ultracode"] is True
    router.set_ultracode(False)
    assert router.ultracode is False
    # A router built before the field (an old pickle, a test double) reads off.
    assert object.__new__(TierRouter).ultracode is False


async def test_ultracode_never_goes_out_as_reasoning_effort() -> None:
    assert with_effort(None, "max", tier_supports=True) == {"reasoning_effort": "max"}
    session = Session("/ws")
    session.router = TierRouter()
    msg = _set_effort(effort="ultracode")
    session.router.set_effort(msg.effort)
    session.router.set_ultracode(bool(msg.ultracode))
    provider = _Capture()
    await _stream_and_parse(
        provider,  # type: ignore[arg-type]
        "house",
        [ChatMessage(role="user", content="hi")],
        session,
        None,
        CostTracker(ModelConfig(presets={}, active_preset="")),
        "brain",
        _tier("house", reasoning_effort=True),
        emit_events=False,
    )
    body = provider.bodies[0]
    assert body["reasoning_effort"] == "max"
    assert "ultracode" not in json.dumps(body)


# ── over the wire ───────────────────────────────────────────────────────


async def _tier_state_with(ws: Any, on: bool) -> dict[str, Any]:
    """The next ``tier_state`` frame on *ws* whose switch reads *on*."""
    async with asyncio.timeout(3.0):
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("type") == "tier_state" and frame.get("ultracode") is on:
                return dict(frame)


async def test_set_ultracode_over_the_wire_acks_with_tier_state(tmp_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        daemon, task = await _start_daemon(tmp)
        ws = await _connect_and_handshake(
            f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
        )
        try:
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            sess = daemon.session_registry.get(sid)
            assert sess is not None and sess.router is not None
            assert sess.router.ultracode is False
            await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
            # The replayed opening state, then a beat for the live stream to start.
            assert (await _tier_state_with(ws, False))["ultracode_available"] is True
            await asyncio.sleep(0.2)

            await ws.send(json.dumps({"type": "set_ultracode", "session_id": sid, "on": True}))
            frame = await _tier_state_with(ws, True)
            assert frame["ultracode_available"] is True and frame["effort"] == "medium"
            assert sess.router.ultracode is True

            await ws.send(json.dumps({"type": "set_ultracode", "session_id": sid, "on": False}))
            frame = await _tier_state_with(ws, False)
            assert frame["ultracode_available"] is True
            assert sess.router.ultracode is False
        finally:
            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(task, return_exceptions=True)


async def _send(daemon: Daemon, **msg: Any) -> dict[str, Any] | None:
    reply = await daemon._handle_message(json.dumps(msg), None)
    return None if reply is None else dict(json.loads(reply))


def _row_effort(data_dir: Path, sid: str) -> str | None:
    record = SessionStore(data_dir).get(sid)
    assert record is not None
    return record.effort


def _last_tier_state(daemon: Daemon, sid: str) -> TierState:
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    return [e for e in sess.event_log.all_events if isinstance(e, TierState)][-1]


def _mock() -> MockProvider:
    return MockProvider(default=Script(kind="stream", content="ok"))


async def test_unknown_session_is_refused(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data", provider=_mock())
    reply = await _send(daemon, type="set_ultracode", session_id="nope", on=True)
    assert reply is not None and reply["code"] == "session_not_found"


async def test_an_old_clients_ultracode_is_max_with_the_switch_on_and_the_row_keeps_it(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    daemon = Daemon(data_dir=data_dir, provider=_mock())
    sid = await _open(daemon, ws)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.router is not None
        assert await _send(daemon, type="set_effort", session_id=sid, effort="ultracode") is None
        assert (sess.router.effort, sess.router.ultracode) == ("max", True)
        state = _last_tier_state(daemon, sid)
        assert (state.effort, state.ultracode, state.ultracode_available) == ("max", True, True)
        assert _row_effort(data_dir, sid) == "max+ultracode"

        # A plain level (any client) leaves the switch where it is.
        assert await _send(daemon, type="set_effort", session_id=sid, effort="high") is None
        assert (sess.router.effort, sess.router.ultracode) == ("high", True)
        assert _row_effort(data_dir, sid) == "high+ultracode"

        assert await _send(daemon, type="set_ultracode", session_id=sid, on=False) is None
        assert _row_effort(data_dir, sid) == "high"
    finally:
        await _shutdown(daemon, sid)


async def test_a_turns_own_tier_state_carries_the_switch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    daemon = Daemon(data_dir=tmp_path / "data", provider=_mock())
    sid = await _open(daemon, ws)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        assert await _send(daemon, type="set_ultracode", session_id=sid, on=True) is None
        await sess.add_user_message("hi")
        await wait_for_turn(sess, 1)
        events = sess.event_log.all_events
        turn_seq = next(e.seq for e in events if isinstance(e, UserTurn))
        during = [e for e in events if isinstance(e, TierState) and e.seq > turn_seq]
        assert during, "the loop reported no tier_state"
        assert all(e.ultracode and e.ultracode_available for e in during)
    finally:
        await _shutdown(daemon, sid)


async def test_a_restart_keeps_the_level_and_the_switch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    first = Daemon(data_dir=data_dir, provider=_mock())
    sid = await _open(first, ws)
    assert await _send(first, type="set_effort", session_id=sid, effort="high") is None
    assert await _send(first, type="set_ultracode", session_id=sid, on=True) is None
    await _shutdown(first, sid)
    assert _row_effort(data_dir, sid) == "high+ultracode"

    second = Daemon(data_dir=data_dir, provider=_mock())
    await second._restore_sessions()
    try:
        sess = second.session_registry.get(sid)
        assert sess is not None and sess.router is not None
        assert (sess.router.effort, sess.router.ultracode) == ("high", True)
    finally:
        await _shutdown(second, sid)


async def _revive(
    tmp_path: Path, effort: str | None, session: SessionConfig | None = None
) -> TierRouter:
    data_dir = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    data_dir.mkdir()
    store = SessionStore(data_dir)
    await store.upsert("sid-1", str(ws), "idle")
    if effort is not None:
        assert await store.set_effort("sid-1", effort)
    persist = SessionPersist(data_dir)
    persist.prepare("sid-1")
    persist.save_conversation("sid-1", [ChatMessage(role="user", content="hi")])
    daemon = Daemon(data_dir=data_dir, provider=_mock())
    if session is not None:
        daemon.config = daemon.config.model_copy(update={"session": session})
    await daemon._restore_sessions()
    try:
        sess = daemon.session_registry.get("sid-1")
        assert sess is not None and sess.router is not None
        return sess.router
    finally:
        await _shutdown(daemon, "sid-1")


async def test_a_legacy_ultracode_row_revives_as_max_with_the_switch_on(tmp_path: Path) -> None:
    router = await _revive(tmp_path, "ultracode")
    assert (router.effort, router.ultracode) == ("max", True)


@pytest.mark.parametrize("stored", [None, "extra", "extra+ultracode"])
async def test_a_row_without_a_known_level_takes_both_defaults(
    tmp_path: Path, stored: str | None
) -> None:
    session = SessionConfig(default_effort="low", default_ultracode=True)
    router = await _revive(tmp_path, stored, session)
    assert (router.effort, router.ultracode) == ("low", True)


async def test_a_plain_row_revives_with_the_switch_off_whatever_the_default(
    tmp_path: Path,
) -> None:
    session = SessionConfig(default_ultracode=True)
    router = await _revive(tmp_path, "xhigh", session)
    assert (router.effort, router.ultracode) == ("xhigh", False)


@pytest.mark.parametrize("on", [False, True])
async def test_a_new_session_takes_the_configured_default(tmp_path: Path, on: bool) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    daemon = Daemon(data_dir=tmp_path / "data", provider=_mock())
    daemon.config = daemon.config.model_copy(
        update={"session": SessionConfig(default_effort="high", default_ultracode=on)}
    )
    sid = await _open(daemon, ws)
    try:
        sess = daemon.session_registry.get(sid)
        assert sess is not None and sess.router is not None
        assert (sess.router.effort, sess.router.ultracode) == ("high", on)
    finally:
        await _shutdown(daemon, sid)
