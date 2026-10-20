"""Restore files to a turn over the wire (TD-5125).

A real agent loop writes through the dispatcher (so the pre-image and the
``Log-Seq`` come from the shipped wiring), and the daemon's handlers answer
``preview_restore_files`` and ``restore_files``. The last test goes through
a real hub to a session on another computer, the way the phone reaches one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.machine_helpers import FakeKeychain, Fleet, window
from tests.test_checkpoint import _git, make_repo
from tests.test_checkpoint_wiring import _write_handler, make_dispatcher, make_write_registry
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_peer_policy import BRIDGED, MACHINE, PHONE_VIEWER, VIEWER, _msg
from tests.test_two_daemons import VIEWER as VIEWER_FEATURES
from tests.test_two_daemons import _for, _open, _pair, _Steps, _until, _writes
from tstd.config import WorktreeConfig
from tstd.mock import Script
from tstd.peer_policy import FORWARDED_VERBS, MACHINE_PEER_VERBS, check
from tstd.protocol import FilesRestored, PreviewRestoreFiles, RestoreFiles, UserTurn
from tstd.rewind_handlers import preview_restore, restore_files
from tstd.router import TierRouter
from tstd.session import Session


def _write(path: str, content: str) -> Script:
    return Script(
        kind="tool_call",
        tool_name="fs_write",
        tool_arguments=json.dumps({"path": path, "content": content}),
    )


def _turn_ids(session: Session) -> list[str]:
    return [e.turn_id for e in session.event_log.all_events if isinstance(e, UserTurn)]


async def _two_turns(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, between: str | None = None
) -> tuple[Session, object]:
    """Turn one writes a.txt, turn two rewrites it and makes b.txt.

    *between* is what the person types into a.txt by hand between the turns.
    """
    session = Session(str(repo))
    dispatcher = make_dispatcher(repo, make_write_registry())
    dispatcher.register_handler("fs_write", _write_handler)
    monkeypatch.chdir(repo)
    mock = _Steps(
        [
            _write("a.txt", "turn one\n"),
            Script(kind="stream", content="Done"),
            _write("a.txt", "turn two\n"),
            _write("b.txt", "new in turn two\n"),
            Script(kind="stream", content="Done"),
        ]
    )
    runner = await start_loop(
        session, TierRouter(lead_turns=3), mock, make_config(), make_write_registry(), dispatcher
    )
    await session.add_user_message("first")
    await wait_for_turn(session, 1)
    if between is not None:
        (repo / "a.txt").write_text(between)
    await session.add_user_message("second")
    await wait_for_turn(session, 2)
    return session, runner


def test_the_verbs_reach_a_phone_and_another_computers_session() -> None:
    for verb in ("preview_restore_files", "restore_files"):
        assert verb in MACHINE_PEER_VERBS
        assert verb in FORWARDED_VERBS
    fields = {"turn_id": "t1", "plan_id": "p1"}
    for kind in ("preview_restore_files", "restore_files"):
        own = _msg(kind, session_id="s1", **fields)
        bridged = _msg(kind, session_id=BRIDGED, **fields)
        assert check(own, PHONE_VIEWER, bridged=False) is None
        for viewer in (VIEWER, PHONE_VIEWER):
            assert check(bridged, viewer, bridged=True) is None
        assert check(own, MACHINE, bridged=False) is None


async def test_a_real_turn_is_previewed_then_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    session, runner = await _two_turns(repo, monkeypatch)
    try:
        second = _turn_ids(session)[1]
        reply = json.loads(
            await preview_restore(
                session, PreviewRestoreFiles(session_id=session.id, turn_id=second)
            )
        )
        assert reply["type"] == "files_restore_preview", reply
        assert {f["path"]: f["change"] for f in reply["files"]} == {
            "a.txt": "modify",
            "b.txt": "delete",
        }
        assert "-turn one" in reply["diff"] and "+turn two" in reply["diff"]

        answer = await restore_files(
            session,
            RestoreFiles(session_id=session.id, turn_id=second, plan_id=reply["plan_id"]),
        )
        assert answer is None
        assert (repo / "a.txt").read_text() == "turn one\n"
        assert not (repo / "b.txt").exists()
        logged = [e for e in session.event_log.all_events if isinstance(e, FilesRestored)]
        assert [(e.turn_id, e.restored, e.removed) for e in logged] == [
            (second, ["a.txt"], ["b.txt"])
        ]
    finally:
        await runner.cancel()


async def test_a_hand_edit_the_agent_wrote_over_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dispatcher's pre-image keeps the person's edit on the branch
    # before the agent's write buries it, so restoring turn two gives it back.
    repo = make_repo(tmp_path)
    session, runner = await _two_turns(repo, monkeypatch, between="mine\n")
    try:
        second = _turn_ids(session)[1]
        reply = json.loads(
            await preview_restore(
                session, PreviewRestoreFiles(session_id=session.id, turn_id=second)
            )
        )
        assert "-mine" in reply["diff"]
        await restore_files(
            session,
            RestoreFiles(session_id=session.id, turn_id=second, plan_id=reply["plan_id"]),
        )
        assert (repo / "a.txt").read_text() == "mine\n"
    finally:
        await runner.cancel()


async def test_refused_mid_turn_on_grok_and_for_an_unknown_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    session, runner = await _two_turns(repo, monkeypatch)
    try:
        unknown = json.loads(
            await preview_restore(session, PreviewRestoreFiles(session_id=session.id, turn_id="x"))
        )
        assert (unknown["code"], unknown["in_reply_to"]) == (
            "turn_not_found",
            "preview_restore_files",
        )
        first = _turn_ids(session)[0]
        session.engine = "grok"  # Grok writes with its own tools: nothing on the branch
        grok = json.loads(
            await preview_restore(
                session, PreviewRestoreFiles(session_id=session.id, turn_id=first)
            )
        )
        assert grok["code"] == "rewind_grok"
        session.engine = "native"
        session._open_turns = 1  # a turn is running
        busy = await restore_files(
            session, RestoreFiles(session_id=session.id, turn_id=first, plan_id="p")
        )
        assert busy is not None
        assert json.loads(busy)["code"] == "turn_in_progress"
        assert (repo / "a.txt").read_text() == "turn two\n"
    finally:
        session._open_turns = 0
        await runner.cancel()


async def test_restore_through_a_hub_like_a_phone_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeKeychain().install(monkeypatch)
    fleet = Fleet(monkeypatch)
    try:
        work = make_repo(tmp_path, "ws-b")
        b = await fleet.start(tmp_path / "b", provider=_writes(work, "one.txt"))
        # The fake model writes into ``work`` itself; a TD-5126 worktree would
        # put the session elsewhere. This test is about the hub, not worktrees.
        b.config = b.config.model_copy(update={"worktree": WorktreeConfig(enabled=False)})
        a = await fleet.start(tmp_path / "a")
        async with window(b) as b_screen, window(a, VIEWER_FEATURES) as hub:
            sid = await _open(b_screen, work)
            mch = await _pair(hub, b)
            remote = f"{sid}:{mch}"
            await hub.send(json.dumps({"type": "attach", "session_id": remote, "from_seq": 1}))
            await hub.send(
                json.dumps({"type": "user_message", "session_id": remote, "content": "go"})
            )
            turn = await _until(hub, _for(remote, "user_turn"))
            request = await _until(hub, _for(remote, "approval_request"))
            await hub.send(
                json.dumps(
                    {
                        "type": "approve",
                        "session_id": remote,
                        "tool_call_id": request["tool_call_id"],
                    }
                )
            )
            await _until(hub, _for(remote, "turn_complete"))
            assert (work / "one.txt").exists()

            ask = {"type": "preview_restore_files", "session_id": remote}
            await hub.send(json.dumps({**ask, "turn_id": turn["turn_id"]}))
            preview = await _until(hub, _for(remote, "files_restore_preview"))
            assert preview["type"] == "files_restore_preview", preview
            assert preview["files"] == [
                {"path": "one.txt", "change": "delete", "hand_edited": False}
            ]
            await hub.send(
                json.dumps(
                    {
                        "type": "restore_files",
                        "session_id": remote,
                        "turn_id": turn["turn_id"],
                        "plan_id": preview["plan_id"],
                    }
                )
            )
            done = await _until(hub, _for(remote, "files_restored"))
            assert done["type"] == "files_restored", done
            assert done["removed"] == ["one.txt"]
            assert not (work / "one.txt").exists()
            # Back to the commit it started from; the daemon's own .tst/ aside.
            status = await asyncio.to_thread(
                _git, work, "status", "--porcelain", "--untracked-files=no"
            )
            assert status == ""
            assert not (work / "one.txt").exists()
    finally:
        await fleet.close()
