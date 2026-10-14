"""A session whose conversation holds a picture revives after a restart (TD-5017).

The save path writes list content (text plus image_url parts) as-is; the
loader used to refuse anything but a string, so every restart turned the
session into an ``interrupted`` tombstone that would not take a message.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.test_loop import wait_for_turn
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, TurnComplete, UserTurn
from tstd.provider import ChatMessage
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore

_PICTURE = "data:image/png;base64,iVBORw0KGgo="


async def _stop_runners(daemon: Daemon) -> None:
    for sess in await daemon.session_registry.list_sessions():
        runner = daemon.session_registry.get_runner(sess.id)
        if runner is not None:
            await runner.cancel()


# "interrupted" is the row a tombstoning restart already left behind: the
# snapshot, not the row's state, decides revive.
@pytest.mark.parametrize("stored_state", ["running", "interrupted"])
async def test_image_turn_revives_not_tombstone(
    tmp_path: Path, stored_state: str, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data_dir.mkdir()

    store = SessionStore(data_dir)
    persist = SessionPersist(data_dir)
    await store.upsert("img-1", str(workspace), stored_state)
    persist.prepare("img-1")
    persist.append_event(
        "img-1",
        UserTurn(session_id="img-1", turn_id="t1", content="What is in this picture?", seq=1),
    )
    persist.append_event("img-1", AssistantDelta(session_id="img-1", delta="A pixel.", seq=2))
    # The picture turn finished before the restart, so the revive seal
    # (TD-5018) leaves it alone and turn 2 below is the follow-up.
    persist.append_event(
        "img-1",
        TurnComplete(session_id="img-1", tokens=3, cost=0, tier="brain", duration=0.1, seq=3),
    )
    picture_turn: list[dict[str, object]] = [
        {"type": "text", "text": "What is in this picture?"},
        {"type": "image_url", "image_url": {"url": _PICTURE}},
    ]
    persist.save_conversation(
        "img-1",
        [
            ChatMessage(role="user", content=picture_turn),
            ChatMessage(role="assistant", content="A pixel."),
        ],
    )

    mock = MockProvider(default=Script(kind="stream", content="Still a pixel."))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    try:
        with caplog.at_level(logging.INFO, logger="tstd"):
            await daemon._restore_sessions()
        messages = [r.getMessage() for r in caplog.records]
        assert "session conversation malformed" not in messages
        assert "revived session" in messages

        sess = daemon.session_registry.get("img-1")
        assert sess is not None
        assert sess.state != "interrupted"
        runner = daemon.session_registry.get_runner("img-1")
        assert runner is not None
        assert runner.is_running
        assert sess.conversation[0].content == picture_turn

        # Live, not a tombstone: it takes a message and the model sees the picture.
        await sess.add_user_message("And now?")
        await wait_for_turn(sess, 2)
        assert sess.conversation[-1].content == "Still a pixel."
        assert mock.calls
        assert picture_turn in [m.content for m in mock.calls[0].messages]
        record = daemon._session_store.get("img-1")
        assert record is not None
        assert record.state != "interrupted"
    finally:
        await _stop_runners(daemon)
