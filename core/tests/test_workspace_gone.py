"""A chat whose folder is gone runs nothing and recreates nothing (ERRORS-02).

The folder was moved or deleted under a chat. The next turn used to run,
and fs_write recreated the old path, so the agent's work went into an
empty ghost folder while the person's real folder sat elsewhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, window
from tests.test_security_suite import make_dispatcher
from tstd.mock import MockProvider, Script


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_a_write_into_a_gone_folder_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    dispatcher = make_dispatcher(root)
    root.rename(tmp_path / "proj.moved")
    result = await dispatcher.dispatch(
        "tc-1", "fs_write", {"path": str(root / "note.txt"), "content": "hi\n"}
    )
    assert result.status == "error"
    assert result.error_code == "workspace_missing"
    assert not root.exists()
    assert not (tmp_path / "proj.moved" / "note.txt").exists()


async def test_a_message_to_a_chat_whose_folder_is_gone_is_refused(
    tmp_path: Path, fleet: Fleet
) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    mock = MockProvider(default=Script(kind="stream", content="done"))
    daemon = await fleet.start(tmp_path / "d", provider=mock)
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        work.rename(tmp_path / "ws.moved")
        frame = {"type": "user_message", "session_id": sid, "content": "write a note"}
        error = await ask(ws, frame, "error")
    assert error["code"] == "workspace_missing"
    assert error["in_reply_to"] == "user_message"
    assert "gone" in error["message"]
    assert not work.exists()
