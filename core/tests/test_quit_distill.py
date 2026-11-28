"""Quitting distils only what is new, and only where the chat already went (LIVE2-08).

At every quit the daemon distilled every chat that had ever completed a
turn, so a stored conversation went to the model again and again, also
after a run with no turns at all. Now a chat is distilled only when it has
a turn since its last distill, counted from this run's start, and a hosted
model is used only when one of the chat's turns ran on it this run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tests.test_add_model import _ask, _stop
from tests.test_preset_engine import _seeded
from tstd.config import ModelConfig
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.session import Session


def _watch(daemon: Daemon, monkeypatch: pytest.MonkeyPatch, turns: list[int]) -> list[str]:
    seen: list[str] = []

    async def fake_distill(session: Session, provider: Any, config: ModelConfig) -> None:
        seen.append(config.active_preset)

    monkeypatch.setattr("tstd.daemon.distill_if_due", fake_distill)
    monkeypatch.setattr("tstd.daemon.completed_turn_count", lambda _s: turns[0])
    return seen


async def _chat(daemon: Daemon, ws: Any, workspace: Path) -> Session:
    opened = await _ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    sess = daemon.session_registry.get(opened["session_id"])
    assert sess is not None
    return sess


async def test_turns_from_before_this_run_are_not_sent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        turns = [3]  # the chat already had three turns when this run began
        seen = _watch(daemon, monkeypatch, turns)
        sess = await _chat(daemon, ws, workspace)
        daemon._distilled_turns[sess.id] = 3
        daemon._bind_session_preset(sess, "local")
        await daemon._distill_live_sessions()
        assert seen == []
    finally:
        await _stop(task, ws)


async def test_a_second_distill_with_no_new_turn_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        turns = [1]
        seen = _watch(daemon, monkeypatch, turns)
        sess = await _chat(daemon, ws, workspace)
        daemon._distilled_turns[sess.id] = 0
        daemon._bind_session_preset(sess, "local")
        await daemon._run_distill(sess)
        await daemon._run_distill(sess)  # say, the quit after an End session
        assert seen == ["local"]
        turns[0] = 2
        await daemon._run_distill(sess)
        assert seen == ["local", "local"]
    finally:
        await _stop(task, ws)


async def test_a_hosted_entry_picked_after_the_last_turn_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch, [1])
        sess = await _chat(daemon, ws, workspace)
        daemon._distilled_turns[sess.id] = 0
        assert not daemon.config.entry_is_local("tst-default")
        daemon._turn_entries[sess.id] = {"local"}  # its turn ran on the local entry
        daemon._bind_session_preset(sess, "tst-default")  # then a hosted one was picked
        await daemon._run_distill(sess)
        assert seen == [daemon.config.local_default()]
    finally:
        await _stop(task, ws)


async def test_a_hosted_entry_the_chat_ran_on_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
    try:
        seen = _watch(daemon, monkeypatch, [1])
        sess = await _chat(daemon, ws, workspace)
        daemon._distilled_turns[sess.id] = 0
        daemon._bind_session_preset(sess, "tst-default")
        daemon._turn_entries[sess.id] = {"tst-default"}
        await daemon._run_distill(sess)
        assert seen == ["tst-default"]
    finally:
        await _stop(task, ws)


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_a_turn_that_ran_records_its_entry(tmp_path: Path, fleet: Fleet) -> None:
    work = tmp_path / "ws"
    work.mkdir()
    daemon = await fleet.start(
        tmp_path / "d", provider=MockProvider(default=Script(kind="stream", content="ok"))
    )
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "hi"}))
        await recv_until(ws, lambda f: f.get("type") == "turn_complete")
    sess = daemon.session_registry.get(sid)
    assert sess is not None
    assert daemon._turn_entries[sid] == {sess.preset}
    assert daemon._distilled_turns[sid] == 0  # a new chat: nothing distilled yet
