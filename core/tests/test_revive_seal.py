"""A turn a restart cut in half is sealed once, on the next start (TD-5018)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.test_loop import wait_for_turn
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, SessionState, TurnComplete, UserTurn
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.provider import ChatMessage, FunctionCall, ToolCall
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore
from tstd.turn_seal import CUT_BY_RESTART, INTERRUPTED_BY_RESTART

SID = "sid-cut"


async def _seed_cut_turn(data_dir: Path, workspace: Path) -> None:
    """What disk holds after a kill between a tool call and its reply."""
    store = SessionStore(data_dir)
    persist = SessionPersist(data_dir)
    await store.upsert(SID, str(workspace), "running")
    persist.prepare(SID)
    for event in (
        SessionState(session_id=SID, state="running", seq=1),
        UserTurn(session_id=SID, turn_id="t1", content="read a", seq=2),
        AssistantDelta(session_id=SID, delta="Reading", seq=3),
        ToolCallEvent(
            session_id=SID, tool_call_id="c1", name="fs_read", arguments={"path": "a"}, seq=4
        ),
    ):
        persist.append_event(SID, event)
    persist.save_conversation(
        SID,
        [
            ChatMessage(role="user", content="read a"),
            ChatMessage(
                role="assistant",
                content="Reading",
                tool_calls=[
                    ToolCall(id="c1", function=FunctionCall(name="fs_read", arguments="{}"))
                ],
            ),
        ],
    )


def _seals(events: list[object]) -> list[TurnComplete]:
    return [
        e for e in events if isinstance(e, TurnComplete) and e.error_code == INTERRUPTED_BY_RESTART
    ]


async def _stop(daemon: Daemon) -> None:
    for sess in await daemon.session_registry.list_sessions():
        runner = daemon.session_registry.get_runner(sess.id)
        if runner is not None:
            await runner.cancel()


async def test_cut_turn_is_sealed_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    await _seed_cut_turn(data_dir, workspace)

    first = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    try:
        with caplog.at_level(logging.INFO, logger="tstd.daemon"):
            await first._restore_sessions()
        sess = first.session_registry.get(SID)
        assert sess is not None
        seals = _seals(list(sess.event_log.all_events))
        assert len(seals) == 1
        seal = seals[0]
        assert seal.failed is True
        assert (seal.tokens, seal.cost, seal.duration) == (0, 0, 0)
        assert seal.tier == "brain"
        # The seal comes after the cut turn's events, so a replay ends it.
        assert seal.seq > 4
        assert not sess.turn_in_flight
        last = sess.conversation[-1]
        assert last.role == "tool"
        assert last.tool_call_id == "c1"
        assert last.content == CUT_BY_RESTART
        sealed = [r for r in caplog.records if r.getMessage() == "sealed cut turn"]
        assert len(sealed) == 1
        assert sealed[0].__dict__["extra_fields"] == {"session_id": SID}
    finally:
        await _stop(first)

    # Disk has the seal and the repaired conversation.
    loaded = SessionPersist(data_dir).load(SID)
    assert loaded is not None
    assert len(_seals(list(loaded.events))) == 1
    assert loaded.conversation is not None
    assert loaded.conversation[-1].tool_call_id == "c1"

    mock = MockProvider(default=Script(kind="stream", content="checked, a is fine"))
    second = Daemon(data_dir=data_dir, provider=mock)
    try:
        await second._restore_sessions()
        sess = second.session_registry.get(SID)
        assert sess is not None
        assert len(_seals(list(sess.event_log.all_events))) == 1
        assert [m.tool_call_id for m in sess.conversation if m.role == "tool"] == ["c1"]

        # Continue is an ordinary message; the request it makes is whole.
        await sess.add_user_message("Continue")
        await wait_for_turn(sess, 2)
        assert mock.calls
        request = mock.calls[-1].messages
        replies = {m.tool_call_id for m in request if m.role == "tool"}
        for message in request:
            for call in message.tool_calls or []:
                assert call.id in replies
        assert len(_seals(list(sess.event_log.all_events))) == 1
    finally:
        await _stop(second)


async def test_finished_turn_is_not_sealed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    store = SessionStore(data_dir)
    persist = SessionPersist(data_dir)
    await store.upsert(SID, str(workspace), "running")
    persist.prepare(SID)
    persist.append_event(SID, UserTurn(session_id=SID, turn_id="t1", content="hi", seq=1))
    persist.append_event(
        SID, TurnComplete(session_id=SID, tokens=3, cost=0, tier="brain", duration=0.1, seq=2)
    )
    conversation = [
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="hello"),
    ]
    persist.save_conversation(SID, conversation)

    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    try:
        await daemon._restore_sessions()
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        assert _seals(list(sess.event_log.all_events)) == []
        assert [m.content for m in sess.conversation] == ["hi", "hello"]
    finally:
        await _stop(daemon)
