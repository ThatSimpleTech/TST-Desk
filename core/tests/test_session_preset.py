"""Per-session catalog preset (TD-1721)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION, AssistantDelta, TierState, UserTurn
from tstd.provider import ChatMessage
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _ask(ws: Any, msg: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(msg))
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))


async def _start_daemon(tmp: Path) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=tmp)
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(task: asyncio.Task[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _open_session(ws: Any, path: str) -> str:
    resp = await _ask(ws, {"type": "open_workspace", "path": path})
    assert resp["type"] == "session_state"
    return str(resp["session_id"])


def _row(listing: dict[str, Any], session_id: str) -> dict[str, Any]:
    assert listing["type"] == "session_list"
    for row in listing["sessions"]:
        if row["session_id"] == session_id:
            return dict(row)
    raise AssertionError(f"session {session_id} missing from list")


class FakeSaver:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Path:
        self.calls.append(args)
        return Path("/tmp/fake-config.yaml")


class TestSessionPreset:
    @pytest.mark.asyncio
    async def test_new_session_records_the_active_preset(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                listing = await _ask(ws, {"type": "list_sessions"})
                assert _row(listing, session_id)["preset"] == daemon.config.active_preset
                sess = daemon.session_registry.get(session_id)
                assert sess is not None
                assert sess.preset == daemon.config.active_preset
                assert sess.config is not None
                assert sess.config.active_preset == daemon.config.active_preset
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_settings_preset_does_not_rewrite_open_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saver = FakeSaver()
        monkeypatch.setattr("tstd.daemon.save_active_preset", saver)
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                opened = daemon.config.active_preset
                assert opened != "budget"
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                brain_slug = daemon.session_registry.get(session_id)
                assert brain_slug is not None and brain_slug.config is not None
                opened_slug = brain_slug.config.tier("brain").slug

                resp = await _ask(ws, {"type": "set_preset", "name": "budget"})
                assert resp["type"] == "setup_state"
                assert resp["active_preset"] == "budget"
                assert daemon.config.active_preset == "budget"

                listing = await _ask(ws, {"type": "list_sessions"})
                assert _row(listing, session_id)["preset"] == opened
                sess = daemon.session_registry.get(session_id)
                assert sess is not None and sess.config is not None
                assert sess.preset == opened
                assert sess.config.active_preset == opened
                assert sess.config.tier("brain").slug == opened_slug
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_session_preset_switch_does_not_write_the_catalog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        preset_saver = FakeSaver()
        slug_saver = FakeSaver()
        monkeypatch.setattr("tstd.daemon.save_active_preset", preset_saver)
        monkeypatch.setattr("tstd.daemon.save_tier_slug", slug_saver)
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                global_preset = daemon.config.active_preset
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                listing = await _ask(
                    ws, {"type": "set_session_preset", "session_id": session_id, "name": "budget"}
                )
                assert listing["type"] == "session_list"
                assert _row(listing, session_id)["preset"] == "budget"
                assert daemon.config.active_preset == global_preset
                assert preset_saver.calls == []
                assert slug_saver.calls == []

                sess = daemon.session_registry.get(session_id)
                assert sess is not None and sess.config is not None
                assert sess.preset == "budget"
                assert sess.config.active_preset == "budget"
                assert sess.config.tier("brain").slug == "z-ai/glm-5.2"
                events = [e for e in sess.event_log.all_events if isinstance(e, TierState)]
                assert events
                assert events[-1].preset == "budget"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_unknown_preset_is_a_typed_error(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                resp = await _ask(
                    ws, {"type": "set_session_preset", "session_id": session_id, "name": "nope"}
                )
                assert resp["type"] == "error"
                assert resp["code"] == "unknown_preset"
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_refuse_while_a_turn_is_running(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                sess = daemon.session_registry.get(session_id)
                assert sess is not None
                sess._open_turns = 1
                resp = await _ask(
                    ws, {"type": "set_session_preset", "session_id": session_id, "name": "budget"}
                )
                assert resp["type"] == "error"
                assert resp["code"] == "session_busy"
                assert sess.preset == daemon.config.active_preset
                await ws.close()
            finally:
                await _stop_daemon(task)

    @pytest.mark.asyncio
    async def test_catalog_slug_edit_does_not_rewrite_an_open_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Settings slug write updates the catalog, not this session's snapshot."""

        def _save(preset: str, tier: str, slug: str, path: Path | None = None) -> Path:
            return Path("/tmp/fake-config.yaml")

        monkeypatch.setattr("tstd.daemon.save_tier_slug", _save)
        monkeypatch.setattr(
            "tstd.daemon.Daemon._reload_user_config",
            lambda self: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            daemon, task = await _start_daemon(Path(tmp))
            try:
                ws = await _connect_and_handshake(
                    f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
                )
                session_id = await _open_session(ws, str(tmp_path))
                sess = daemon.session_registry.get(session_id)
                assert sess is not None and sess.config is not None
                opened_slug = sess.config.tier("brain").slug
                opened_obj = sess.config

                resp = await _ask(
                    ws,
                    {
                        "type": "set_tier_slug",
                        "preset": daemon.config.active_preset,
                        "tier": "brain",
                        "slug": "some/other-model",
                    },
                )
                assert resp["type"] == "setup_state"
                assert sess.config is opened_obj
                assert sess.config.tier("brain").slug == opened_slug
                await ws.close()
            finally:
                await _stop_daemon(task)


class TestSessionPresetRevive:
    async def test_revive_restores_the_stored_preset(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()
        data_dir.mkdir()

        store = SessionStore(data_dir)
        persist = SessionPersist(data_dir)
        await store.upsert("sid-1", str(workspace), "running", preset="budget")
        persist.prepare("sid-1")
        persist.append_event(
            "sid-1",
            UserTurn(session_id="sid-1", turn_id="t1", content="hello", seq=1),
        )
        persist.append_event(
            "sid-1",
            AssistantDelta(session_id="sid-1", delta="hi", seq=2),
        )
        persist.save_conversation(
            "sid-1",
            [
                ChatMessage(role="user", content="hello"),
                ChatMessage(role="assistant", content="hi"),
            ],
        )

        daemon = Daemon(data_dir=data_dir)
        try:
            assert daemon.config.active_preset != "budget"
            await daemon._restore_sessions()
            sess = daemon.session_registry.get("sid-1")
            assert sess is not None
            assert sess.preset == "budget"
            assert sess.config is not None
            assert sess.config.active_preset == "budget"
            record = daemon._session_store.get("sid-1")
            assert record is not None
            assert record.preset == "budget"
        finally:
            for live in await daemon.session_registry.list_sessions():
                runner = daemon.session_registry.get_runner(live.id)
                if runner is not None:
                    await runner.cancel()
