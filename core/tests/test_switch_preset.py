"""One switch: this chat, the model new chats use, or both (TD-5235).

The picker's click. Everything is checked before anything changes; a
switch asked for while a turn runs waits for that turn and no longer; a
chat with no loop just has the name recorded; and ``new_session`` can open
a chat on a named entry, which is how the picker reaches an entry on
another engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _ask, _start, _stop
from tests.test_preset_engine import _grok_stub
from tests.test_preset_engine import _text as _catalog_with_grok
from tstd.config import load_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.peer_policy import MACHINE_PEER_VERBS
from tstd.protocol import TierState
from tstd.session_store import SessionStore

EFFORT = """\
    overrides:
      session:
        default_effort: high
"""


def _text() -> str:
    """The test catalog plus a Grok entry; ``budget`` names an effort."""
    text = _catalog_with_grok()
    header = "\n  budget:\n"
    assert text.count(header) == 1
    return text.replace(header, header + EFFORT, 1)


async def _seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: MockProvider | None = None
) -> tuple[Daemon, Any, Any, Path]:
    monkeypatch.setattr("tstd.daemon.grok_loop", _grok_stub)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(_text(), encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    mock = provider or MockProvider(default=Script(kind="stream", content="done"))
    daemon, task, ws = await _start(data, mock)
    return daemon, task, ws, workspace


async def _open(ws: Any, workspace: Path) -> str:
    opened = await _ask(ws, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    return str(opened["session_id"])


def _tier_states(sess: Any) -> list[TierState]:
    return [e for e in sess.event_log.all_events if isinstance(e, TierState)]


async def _read_all(ws: Any) -> None:
    with contextlib.suppress(Exception):
        async for _message in ws:
            pass


async def _until(condition: Any, what: str) -> None:
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def _brain(daemon: Daemon, name: str) -> str:
    brain = daemon.config.presets[name].brain
    assert brain is not None and brain.slug is not None
    return brain.slug


class TestTheThreeSwitches:
    async def test_this_chat_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            sess = daemon.session_registry.get(sid)
            assert sess is not None and sess.router is not None
            reply = await _ask(
                ws, {"type": "switch_preset", "name": "budget", "session_id": sid}, "session_list"
            )
            assert reply["type"] == "session_list"
            assert sess.preset == "budget"
            assert daemon.config.active_preset == "tst-default"
            assert load_config(daemon.config_path).active_preset == "tst-default"
            # TD-5234: the entry names an effort, and the click sets it.
            assert sess.router.effort == "high"
            assert _tier_states(sess)[-1].preset == "budget"
            assert _tier_states(sess)[-1].pending_preset is None
        finally:
            await _stop(task, ws)

    async def test_the_model_new_chats_use_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            sess = daemon.session_registry.get(sid)
            assert sess is not None
            snapshot = sess.config
            reply = await _ask(
                ws,
                {"type": "switch_preset", "name": "budget", "make_default": True},
                "setup_state",
            )
            assert reply["type"] == "setup_state" and reply["active_preset"] == "budget"
            assert load_config(daemon.config_path).active_preset == "budget"
            # The open chat is not rewritten.
            assert sess.preset == "tst-default" and sess.config is snapshot
            second = daemon.session_registry.get(await _open(ws, workspace))
            assert second is not None and second.preset == "budget"
        finally:
            await _stop(task, ws)

    async def test_both_in_one_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            other = await _open(ws, workspace)
            reply = await _ask(
                ws,
                {
                    "type": "switch_preset",
                    "name": "budget",
                    "session_id": sid,
                    "make_default": True,
                },
                "setup_state",
            )
            assert reply["active_preset"] == "budget"
            switched = daemon.session_registry.get(sid)
            untouched = daemon.session_registry.get(other)
            assert switched is not None and switched.preset == "budget"
            assert untouched is not None and untouched.preset == "tst-default"
            record = daemon._session_store.get(sid)
            assert record is not None and record.preset == "budget"
        finally:
            await _stop(task, ws)

    async def test_the_same_model_again_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            sess = daemon.session_registry.get(sid)
            assert sess is not None
            snapshot, text = sess.config, daemon.config_path.read_text(encoding="utf-8")
            states = len(_tier_states(sess))
            await _ask(
                ws,
                {
                    "type": "switch_preset",
                    "name": "tst-default",
                    "session_id": sid,
                    "make_default": True,
                },
                "setup_state",
            )
            assert sess.config is snapshot and len(_tier_states(sess)) == states
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await _stop(task, ws)


class TestNothingHalfApplies:
    @pytest.mark.parametrize(
        ("patch", "code"),
        [
            ({"name": "nope", "make_default": True}, "unknown_preset"),
            ({"name": "budget"}, "bad_request"),
            (
                {"name": "budget", "session_id": "no-such-session", "make_default": True},
                "session_not_found",
            ),
            ({"name": "grok", "session_id": "<chat>", "make_default": True}, "engine_mismatch"),
        ],
    )
    async def test_a_refusal_changes_neither_the_chat_nor_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: dict[str, Any], code: str
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            sess = daemon.session_registry.get(sid)
            assert sess is not None
            snapshot, text = sess.config, daemon.config_path.read_text(encoding="utf-8")
            msg = {"type": "switch_preset", **patch}
            if msg.get("session_id") == "<chat>":
                msg["session_id"] = sid
            reply = await _ask(ws, msg, "setup_state")
            assert reply["type"] == "error" and reply["code"] == code
            assert sess.preset == "tst-default" and sess.config is snapshot
            assert sess.pending_preset is None
            assert daemon.config.active_preset == "tst-default"
            assert daemon.config_path.read_text(encoding="utf-8") == text
        finally:
            await _stop(task, ws)


class TestWhileATurnRuns:
    """The switch waits for the turn, and the turn keeps the model it started on."""

    async def _turn_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Daemon, Any, Any, str, Any, MockProvider]:
        slow = MockProvider(default=Script(kind="stream", content="a b c d", chunk_delay=0.02))
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch, slow)
        sid = await _open(ws, workspace)
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "one"}))
        await _until(lambda: sess.turn_in_flight and len(slow.calls) > 0, "the turn started")
        # These tests watch the daemon, not the socket. Unread, the client's
        # queue fills, it stops reading, and closing waits out its timeout.
        self._reader = asyncio.create_task(_read_all(ws))
        return daemon, task, ws, sid, sess, slow

    async def _quiet(self) -> None:
        """Stop reading, so a test can ask and read a reply itself."""
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader

    async def test_it_is_held_then_applied_when_the_turn_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, sid, sess, slow = await self._turn_running(tmp_path, monkeypatch)
        try:
            old, new = _brain(daemon, "tst-default"), _brain(daemon, "budget")
            assert old != new
            snapshot = sess.config
            await ws.send(
                json.dumps({"type": "switch_preset", "name": "budget", "session_id": sid})
            )
            await _until(lambda: sess.pending_preset == "budget", "the switch is pending")
            # Held: the chat is still on its own model, and says what is waiting.
            assert sess.preset == "tst-default" and sess.config is snapshot
            held = _tier_states(sess)[-1]
            assert held.preset == "tst-default" and held.pending_preset == "budget"

            await _until(lambda: not sess.turn_in_flight, "the turn completed")
            # The bind comes first and its tier_state a moment later: wait for the event.
            await _until(
                lambda: _tier_states(sess)[-1].preset == "budget", "the pending switch applied"
            )
            assert sess.preset == "budget" and sess.pending_preset is None
            applied = _tier_states(sess)[-1]
            assert applied.preset == "budget" and applied.pending_preset is None
            assert sess.router.effort == "high"
            record = daemon._session_store.get(sid)
            assert record is not None and record.preset == "budget"
            # The turn that was running asked only its own model.
            assert {call.model for call in slow.calls} == {old}

            before = len(slow.calls)
            await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "two"}))
            await _until(lambda: len(slow.calls) > before, "the next turn asked a model")
            assert slow.calls[before].model == new
            await _until(lambda: not sess.turn_in_flight, "the second turn completed")
        finally:
            await _stop(task, ws)

    async def test_a_message_queued_behind_the_turn_runs_on_the_new_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, sid, sess, slow = await self._turn_running(tmp_path, monkeypatch)
        try:
            old, new = _brain(daemon, "tst-default"), _brain(daemon, "budget")
            await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "two"}))
            await ws.send(
                json.dumps({"type": "switch_preset", "name": "budget", "session_id": sid})
            )
            await _until(
                lambda: sess.preset == "budget" and not sess.turn_in_flight, "both turns ran"
            )
            models = [call.model for call in slow.calls]
            assert models[0] == old
            assert new in models
            assert models.index(new) == len([m for m in models if m == old])
        finally:
            await _stop(task, ws)

    async def test_a_later_switch_replaces_it_and_one_back_cancels_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _daemon, task, ws, sid, sess, _slow = await self._turn_running(tmp_path, monkeypatch)
        try:
            for name in ("budget", "local"):
                await ws.send(
                    json.dumps({"type": "switch_preset", "name": name, "session_id": sid})
                )
            await _until(lambda: sess.pending_preset == "local", "the later switch replaced it")
            await ws.send(
                json.dumps({"type": "switch_preset", "name": "tst-default", "session_id": sid})
            )
            await _until(lambda: sess.pending_preset is None, "switching back cancelled it")
            assert _tier_states(sess)[-1].pending_preset is None
            await _until(lambda: not sess.turn_in_flight, "the turn completed")
            assert sess.preset == "tst-default"
        finally:
            await _stop(task, ws)

    async def test_an_entry_removed_meanwhile_is_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _daemon, task, ws, sid, sess, _slow = await self._turn_running(tmp_path, monkeypatch)
        try:
            await ws.send(
                json.dumps({"type": "switch_preset", "name": "budget", "session_id": sid})
            )
            await _until(lambda: sess.pending_preset == "budget", "the switch is pending")
            await ws.send(json.dumps({"type": "delete_preset", "name": "budget"}))
            await _until(lambda: not sess.turn_in_flight, "the turn completed")
            await _until(lambda: sess.pending_preset is None, "the pending switch was dropped")
            assert sess.preset == "tst-default"
            assert _tier_states(sess)[-1].pending_preset is None
        finally:
            await _stop(task, ws)

    async def test_the_old_verb_still_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _daemon, task, ws, sid, sess, _slow = await self._turn_running(tmp_path, monkeypatch)
        await self._quiet()
        try:
            reply = await _ask(
                ws,
                {"type": "set_session_preset", "session_id": sid, "name": "budget"},
                "session_list",
            )
            assert reply["type"] == "error" and reply["code"] == "session_busy"
            assert sess.pending_preset is None and sess.preset == "tst-default"
        finally:
            await _stop(task, ws)


async def test_a_chat_with_no_loop_has_the_name_recorded(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    data.mkdir()
    workspace.mkdir()
    (data / "config.yaml").write_text(_text(), encoding="utf-8")
    # No conversation snapshot: restored as a row that never gets a loop.
    await SessionStore(data).upsert("sid-1", str(workspace), "complete", preset="tst-default")
    daemon, task, ws = await _start(data, MockProvider(default=Script(kind="text")))
    try:
        sess = daemon.session_registry.get("sid-1")
        assert sess is not None and sess.router is None
        reply = await _ask(
            ws, {"type": "switch_preset", "name": "budget", "session_id": "sid-1"}, "session_list"
        )
        assert reply["type"] == "session_list"
        row = next(r for r in reply["sessions"] if r["session_id"] == "sid-1")
        assert row["preset"] == "budget" and sess.preset == "budget"
        record = daemon._session_store.get("sid-1")
        assert record is not None and record.preset == "budget"
        # The old verb still says why it cannot.
        old = await _ask(
            ws,
            {"type": "set_session_preset", "session_id": "sid-1", "name": "local"},
            "session_list",
        )
        assert old["type"] == "error" and old["code"] == "session_not_live"
    finally:
        await _stop(task, ws)


class TestANewChatOnAnEntry:
    async def test_it_opens_on_the_entry_with_the_entrys_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            grok = await _ask(
                ws, {"type": "new_session", "session_id": sid, "preset": "grok"}, "session_state"
            )
            assert grok["type"] == "session_state" and grok["engine"] == "grok"
            made = daemon.session_registry.get(grok["session_id"])
            assert made is not None and made.preset == "grok" and made.engine == "grok"
            native = await _ask(
                ws,
                {"type": "new_session", "session_id": sid, "preset": "budget"},
                "session_state",
            )
            other = daemon.session_registry.get(native["session_id"])
            assert other is not None and other.preset == "budget" and other.engine == "native"
            # Neither changed the model new chats use, nor the chat it came from.
            assert daemon.config.active_preset == "tst-default"
            first = daemon.session_registry.get(sid)
            assert first is not None and first.preset == "tst-default"
        finally:
            await _stop(task, ws)

    async def test_without_an_entry_it_is_the_default_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            plain = await _ask(ws, {"type": "new_session", "session_id": sid}, "session_state")
            made = daemon.session_registry.get(plain["session_id"])
            assert made is not None and made.preset == "tst-default"
        finally:
            await _stop(task, ws)

    async def test_an_unknown_entry_starts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        daemon, task, ws, workspace = await _seeded(tmp_path, monkeypatch)
        try:
            sid = await _open(ws, workspace)
            before = len(await daemon.session_registry.list_sessions())
            reply = await _ask(
                ws, {"type": "new_session", "session_id": sid, "preset": "nope"}, "session_state"
            )
            assert reply["type"] == "error" and reply["code"] == "unknown_preset"
            assert len(await daemon.session_registry.list_sessions()) == before
        finally:
            await _stop(task, ws)


def test_another_computers_hub_may_not_send_it() -> None:
    assert "switch_preset" not in MACHINE_PEER_VERBS
    assert "set_session_preset" in MACHINE_PEER_VERBS
