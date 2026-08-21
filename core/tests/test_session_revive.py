"""Daemon restart revives a saved conversation and refuses to invent one."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_loop import wait_for_turn
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, UserTurn
from tstd.provider import ChatMessage
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore


async def _stop_runners(daemon: Daemon) -> None:
    for sess in await daemon.session_registry.list_sessions():
        runner = daemon.session_registry.get_runner(sess.id)
        if runner is not None:
            await runner.cancel()


class TestRestoreHonesty:
    async def test_conversation_snapshot_revives_running_loop(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        store = SessionStore(data_dir)
        persist = SessionPersist(data_dir)
        await store.upsert("sid-1", str(workspace), "running")
        persist.prepare("sid-1")
        persist.append_event(
            "sid-1",
            UserTurn(session_id="sid-1", turn_id="t1", content="What is 2+2?", seq=1),
        )
        persist.append_event(
            "sid-1",
            AssistantDelta(session_id="sid-1", delta="4", seq=2),
        )
        persist.save_conversation(
            "sid-1",
            [
                ChatMessage(role="user", content="What is 2+2?"),
                ChatMessage(role="assistant", content="4"),
            ],
        )

        mock = MockProvider(default=Script(kind="stream", content="still 4"))
        daemon = Daemon(data_dir=data_dir, provider=mock)
        try:
            await daemon._restore_sessions()
            sess = daemon.session_registry.get("sid-1")
            assert sess is not None
            assert sess.state == "running"
            runner = daemon.session_registry.get_runner("sid-1")
            assert runner is not None
            assert runner.is_running
            assert [m.content for m in sess.conversation] == ["What is 2+2?", "4"]
            turns = [e for e in sess.event_log.all_events if isinstance(e, UserTurn)]
            assert turns[0].content == "What is 2+2?"

            await sess.add_user_message("Again")
            await wait_for_turn(sess, 1)
            assert sess.conversation[-1].content == "still 4"
            assert mock.calls
            # The model saw the saved turn, not an empty thread.
            roles = [m.role for m in mock.calls[0].messages]
            assert "user" in roles
            assert "What is 2+2?" in [m.content for m in mock.calls[0].messages]
        finally:
            await _stop_runners(daemon)

    async def test_missing_persist_stays_interrupted(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()
        store = SessionStore(data_dir)
        await store.upsert("old-1", str(workspace), "running")

        daemon = Daemon(data_dir=data_dir)
        await daemon._restore_sessions()
        sess = daemon.session_registry.get("old-1")
        assert sess is not None
        assert sess.state == "interrupted"
        assert daemon.session_registry.get_runner("old-1") is None
        record = daemon._session_store.get("old-1")
        assert record is not None
        assert record.state == "interrupted"

    async def test_events_without_conversation_replay_but_do_not_run(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()
        store = SessionStore(data_dir)
        persist = SessionPersist(data_dir)
        await store.upsert("half-1", str(workspace), "running")
        persist.dir_for("half-1").mkdir(parents=True)
        persist.append_event(
            "half-1",
            UserTurn(session_id="half-1", turn_id="t1", content="saved text", seq=1),
        )
        # No conversation.json — a transcript without a model context.

        daemon = Daemon(data_dir=data_dir)
        await daemon._restore_sessions()
        sess = daemon.session_registry.get("half-1")
        assert sess is not None
        assert sess.state == "interrupted"
        assert daemon.session_registry.get_runner("half-1") is None
        turns = [e for e in sess.event_log.all_events if isinstance(e, UserTurn)]
        assert turns[0].content == "saved text"

        reply = await daemon._handle_message(
            json.dumps(
                {
                    "type": "user_message",
                    "session_id": "half-1",
                    "content": "please continue",
                }
            ),
            object(),
        )
        assert reply is not None
        body = json.loads(reply)
        assert body["type"] == "error"
        assert body["code"] == "session_not_running"

    async def test_corrupt_conversation_does_not_invent_messages(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()
        store = SessionStore(data_dir)
        persist = SessionPersist(data_dir)
        await store.upsert("bad-1", str(workspace), "running")
        persist.prepare("bad-1")
        (persist.dir_for("bad-1") / "conversation.json").write_text("{", encoding="utf-8")

        daemon = Daemon(data_dir=data_dir)
        await daemon._restore_sessions()
        sess = daemon.session_registry.get("bad-1")
        assert sess is not None
        assert sess.state == "interrupted"
        assert sess.conversation == []

    async def test_terminal_without_snapshot_stays_terminal(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()
        store = SessionStore(data_dir)
        await store.upsert("done-1", str(workspace), "complete")

        daemon = Daemon(data_dir=data_dir)
        await daemon._restore_sessions()
        sess = daemon.session_registry.get("done-1")
        assert sess is not None
        assert sess.state == "complete"
        assert daemon.session_registry.get_runner("done-1") is None
