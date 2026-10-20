"""TD-5155: a message the daemon accepted outlives a restart, held for the person.

2026-10-19: a message sent while the progress guard had paused the turn sat
in the session's in-memory queue, and the restart for a deploy dropped it.
Real daemons on loopback (the machine helpers' fleet) and the mock provider
for the restart; seeded data folders for the restore rules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection

from tests.machine_helpers import Fleet, ask, recv_until, window
from tests.test_loop import make_config, start_loop, wait_for_turn
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import QueuedMessages, TurnComplete, UserTurn
from tstd.queued_messages import (
    dropped_note,
    keep_held_visible,
    messages_from_rows,
    queue_rows,
)
from tstd.router import TierRouter
from tstd.session import QueuedUserMessage, Session
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore
from tstd.turn_seal import INTERRUPTED_BY_RESTART


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


def _same_read(work: Path) -> MockProvider:
    """Every call reads the same file: the progress guard pauses on the third."""
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    arguments = json.dumps({"path": str(work / "a.txt")})
    return MockProvider(
        default=Script(kind="tool_call", tool_name="fs_read", tool_arguments=arguments)
    )


async def _send(ws: ClientConnection, sid: str, content: str) -> None:
    await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": content}))


async def paused_session(ws: ClientConnection, work: Path) -> str:
    """Open *work*, send a turn, and wait for the guard to pause it."""
    opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
    sid = str(opened["session_id"])
    await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
    await _send(ws, sid, "read a")
    paused = await recv_until(
        ws,
        lambda f: f.get("type") == "session_state" and f.get("state") == "paused",
        within=20,
    )
    assert str(paused.get("reason", "")).startswith("progress guard:")
    return sid


def _frames(kind: str, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("type") == kind]


async def test_a_message_sent_while_paused_survives_a_restart(tmp_path: Path, fleet: Fleet) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    data_dir = tmp_path / "d"
    first = await fleet.start(data_dir, provider=_same_read(work))
    async with window(first) as ws:
        sid = await paused_session(ws, work)
        await _send(ws, sid, "also check b.txt")
        # Answered after the send's handler returned: the message is on disk.
        await ask(ws, {"type": "list_sessions"}, "session_list")
        rows = await asyncio.to_thread(SessionPersist(data_dir).load_queue, sid)
        assert [r["display"] for r in rows] == ["also check b.txt"]
        # The guard pause is not a running turn: drain names it apart.
        result = await ask(ws, {"type": "drain", "timeout_s": 1, "hold_s": 0}, "drain_result")
        assert result["running"] == []
        assert result["paused"] == [sid]
        assert result["awaiting_approval"] == []
        assert result["drained"] is False
    await fleet.stop(first)

    mock = MockProvider(default=Script(kind="stream", content="b is fine"))
    second = await fleet.start(data_dir, provider=mock)
    async with window(second) as ws:
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        held = await recv_until(ws, lambda f: f.get("type") == "queued_messages")
        assert [m["content"] for m in held["messages"]] == ["also check b.txt"]
        assert held["messages"][0]["images_dropped"] == 0
        queued_id = held["messages"][0]["queued_id"]
        # Held, not sent: the cut turn is sealed and nothing asks the model.
        sess = second.session_registry.get(sid)
        assert sess is not None
        assert any(
            isinstance(e, TurnComplete) and e.error_code == INTERRUPTED_BY_RESTART
            for e in sess.event_log.all_events
        )
        await asyncio.sleep(0.3)
        assert mock.calls == []

        await ws.send(
            json.dumps({"type": "send_queued", "session_id": sid, "queued_id": queued_id})
        )
        cleared = await recv_until(ws, lambda f: f.get("type") == "queued_messages")
        assert cleared["messages"] == []
        turn = await recv_until(ws, lambda f: f.get("type") == "user_turn")
        assert (turn["content"], turn["turn_id"]) == ("also check b.txt", queued_id)
        done = await recv_until(ws, lambda f: f.get("type") == "turn_complete", within=10)
        assert done.get("error_code") is None
    assert mock.calls, "the held message became a turn"
    assert await asyncio.to_thread(SessionPersist(data_dir).load_queue, sid) == []


# ── Restore rules, on a seeded data folder ───────────────────────────

SID = "sid-held"


async def _seed(
    data_dir: Path, workspace: Path, rows: list[dict[str, Any]], events: list[Any]
) -> None:
    store = SessionStore(data_dir)
    persist = SessionPersist(data_dir)
    await store.upsert(SID, str(workspace), "running")
    persist.prepare(SID)
    for event in events:
        persist.append_event(SID, event)
    persist.save_queue(SID, rows)


def _row(ident: str, text: str) -> dict[str, Any]:
    return {"id": ident, "display": text, "provider": text, "images_dropped": 0}


async def _restored(tmp_path: Path, rows: list[dict[str, Any]], events: list[Any]) -> Daemon:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    await _seed(data_dir, workspace, rows, events)
    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    await daemon._restore_sessions()
    return daemon


async def _stop(daemon: Daemon) -> None:
    for sess in await daemon.session_registry.list_sessions():
        runner = daemon.session_registry.get_runner(sess.id)
        if runner is not None:
            await runner.cancel()


def _lists(sess: Session) -> list[QueuedMessages]:
    return [e for e in sess.event_log.all_events if isinstance(e, QueuedMessages)]


async def test_a_message_whose_turn_started_is_not_held_again(tmp_path: Path) -> None:
    """A stop between the user_turn and the queue write: the log wins."""
    events = [
        UserTurn(session_id=SID, turn_id="q-ran", content="ran", seq=1),
        TurnComplete(session_id=SID, tokens=1, cost=0, tier="brain", duration=1, seq=2),
    ]
    daemon = await _restored(tmp_path, [_row("q-ran", "ran"), _row("q-wait", "wait")], events)
    try:
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        assert [m.id for m in sess.held_messages] == ["q-wait"]
        assert [[m.queued_id for m in e.messages] for e in _lists(sess)] == [["q-wait"]]
        rows = daemon._session_persist.load_queue(SID)
        assert [r["id"] for r in rows] == ["q-wait"]
    finally:
        await _stop(daemon)


async def test_remove_queued_drops_it_for_good(tmp_path: Path) -> None:
    daemon = await _restored(tmp_path, [_row("q-1", "first"), _row("q-2", "second")], [])
    try:
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        frame = json.dumps({"type": "remove_queued", "session_id": SID, "queued_id": "q-1"})
        assert await daemon._dispatch_message(frame, None) is None
        assert [m.id for m in sess.held_messages] == ["q-2"]
        assert [[m.queued_id for m in e.messages] for e in _lists(sess)][-1] == ["q-2"]
        assert [r["id"] for r in daemon._session_persist.load_queue(SID)] == ["q-2"]

        again = await daemon._dispatch_message(frame, None)
        assert again is not None
        assert json.loads(again)["code"] == "queued_not_found"
    finally:
        await _stop(daemon)


async def test_send_queued_waits_out_a_drain_hold(tmp_path: Path) -> None:
    daemon = await _restored(tmp_path, [_row("q-1", "first")], [])
    try:
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        daemon._drain.hold_for(60)
        frame = json.dumps({"type": "send_queued", "session_id": SID, "queued_id": "q-1"})
        refused = await daemon._dispatch_message(frame, None)
        assert refused is not None
        assert json.loads(refused)["code"] == "daemon_draining"
        assert json.loads(refused)["in_reply_to"] == "send_queued"
        assert [m.id for m in sess.held_messages] == ["q-1"]
        assert not sess.turn_in_flight
    finally:
        await _stop(daemon)


async def test_a_restart_with_nothing_queued_logs_no_list(tmp_path: Path) -> None:
    daemon = await _restored(tmp_path, [], [])
    try:
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        assert sess.held_messages == []
        assert _lists(sess) == []
    finally:
        await _stop(daemon)


async def test_a_stale_list_in_the_log_is_cleared(tmp_path: Path) -> None:
    """The log's last list named a message the queue file no longer has."""
    stale = QueuedMessages(session_id=SID, messages=[{"queued_id": "q-x", "content": "x"}], seq=1)
    daemon = await _restored(tmp_path, [], [stale])
    try:
        sess = daemon.session_registry.get(SID)
        assert sess is not None
        assert _lists(sess)[-1].messages == []
    finally:
        await _stop(daemon)


# ── The queue file itself ────────────────────────────────────────────


def test_images_are_not_kept_and_the_model_is_told() -> None:
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": "what is in this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    message = QueuedUserMessage(
        display="what is in this?\n\n[Images attached: a.png]", provider=parts
    )
    (row,) = queue_rows([message])
    assert "base64" not in json.dumps(row)
    assert row["images_dropped"] == 1
    assert row["provider"] == f"what is in this?\n\n{dropped_note(1)}"
    (back,) = messages_from_rows([row])
    assert (back.id, back.display, back.images_dropped) == (message.id, message.display, 1)
    # An engine sent the display text (Grok) gets the note too.
    assert message.prompt_text == message.display
    assert back.prompt_text == row["provider"]
    # Written again (a second restart), the note is not doubled.
    assert queue_rows([back]) == [row]


def test_a_malformed_row_is_skipped() -> None:
    rows: list[dict[str, Any]] = [{"id": "q", "display": 3}, _row("q-ok", "fine")]
    assert [m.id for m in messages_from_rows(rows)] == ["q-ok"]


async def test_the_queue_is_saved_before_the_loop_can_take_it(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    # Each save, with how many messages the loop could take at that moment.
    saved: list[tuple[list[str], int]] = []

    async def hook(messages: list[QueuedUserMessage]) -> None:
        saved.append(([m.display for m in messages], session.pending_user_messages))

    session._queue_hook = hook
    mock = MockProvider(default=Script(kind="stream", content="ok"))
    runner = await start_loop(session, TierRouter(), mock, make_config())
    try:
        await session.add_user_message("one")
        turn = await wait_for_turn(session, 1)
        assert turn.failed is False
        await asyncio.sleep(0.05)
        # On disk before the loop could take it, and gone once its turn was.
        assert saved == [(["one"], 0), ([], 0)]
        (user_turn,) = [e for e in session.event_log.all_events if isinstance(e, UserTurn)]
        assert user_turn.content == "one"
    finally:
        await runner.cancel()


async def test_held_messages_trimmed_from_the_log_are_logged_again(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.held_messages = [QueuedUserMessage.plain("kept")]
    await keep_held_visible(session)
    await keep_held_visible(session)
    lists = _lists(session)
    assert len(lists) == 1
    assert [m.content for m in lists[0].messages] == ["kept"]
