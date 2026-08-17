"""Archive, delete, and move-to-project (TD-1715).

Three verbs with durability consequences, so these tests go through the
real wire and the real store wherever they can: a live daemon on a real
data directory, shut down and started again on the same directory to prove
the archive flag is metadata and not memory.

The refusal rules are driven at the ``session_lifecycle`` layer with a bare
session, because "a turn is in flight" has to be arranged deterministically
and a live loop with a provider behind it cannot be held mid-turn on demand.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_loop import make_config
from tests.test_usage_recording import ScriptedProvider, _factory, _finish, _text
from tstd.daemon import Daemon
from tstd.loop import agent_loop
from tstd.protocol import PROTOCOL_VERSION, TurnComplete
from tstd.provider import ChatCompletionRequest, ProviderError, StreamChunk
from tstd.router import TierRouter
from tstd.session import Session, SessionRegistry, SessionRunner
from tstd.session_lifecycle import archive_session, delete_session, move_session
from tstd.session_store import SessionStore


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws: Any, path: str) -> dict[str, Any]:
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    return dict(json.loads(await ws.recv()))


async def _request(ws: Any, payload: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps(payload))
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))


class _RunningDaemon:
    """Spin a daemon up on a data dir and tear it down.

    ``data_dir`` is optional so a test can start a second daemon over the
    first one's directory — the restart case.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory() if data_dir is None else None
        self.data_dir = data_dir if data_dir is not None else Path(self._tmp.name)  # type: ignore[union-attr]
        self.daemon = Daemon(data_dir=self.data_dir)

    async def __aenter__(self) -> Daemon:
        self._task = asyncio.create_task(self.daemon.run())
        for _ in range(50):
            if self.daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        assert self.daemon.ws_server.port > 0
        return self.daemon

    async def __aexit__(self, *_exc: Any) -> None:
        self.daemon._shutdown_event.set()
        await asyncio.gather(self._task, return_exceptions=True)
        if self._tmp is not None:
            self._tmp.cleanup()


def _summary(listing: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    for entry in listing["sessions"]:
        if entry["session_id"] == session_id:
            return dict(entry)
    return None


async def _busy_session(workspace: str) -> tuple[SessionRegistry, SessionStore, str, Any]:
    """A registered, live session that owes the user a turn.

    Built without a runner on purpose: the point is the in-flight-turn rule,
    not the loop, and an enqueued message the loop never dequeues is exactly
    the state the rule has to refuse in.
    """
    registry = SessionRegistry()
    session = await registry.restore("busy-1", workspace, "running")
    store = SessionStore(Path(workspace))
    await store.upsert(session.id, workspace, "running")
    await session.add_user_message("what is in this repo?")
    assert session.turn_in_flight
    return registry, store, session.id, session


class TestTurnInFlight:
    """The signal the refusals are built on, measured against a real loop.

    ``session_state`` cannot answer this — "running" is set once at open and
    spans the session's whole life (TD-1714) — so the flag has its own
    tracking, and the tracking is only worth anything if a real turn moves it.
    """

    @pytest.mark.asyncio
    async def test_a_real_turn_raises_and_lowers_the_flag(self, tmp_path: Path) -> None:
        released = asyncio.Event()
        entered = asyncio.Event()

        class HeldProvider(ScriptedProvider):
            """Parks inside the provider call, which is where a turn lives."""

            async def chat_completion_stream(
                self, request: ChatCompletionRequest
            ) -> AsyncIterator[StreamChunk | ProviderError]:
                entered.set()
                await released.wait()
                async for chunk in super().chat_completion_stream(request):
                    yield chunk

        session = Session(str(tmp_path))
        provider = HeldProvider([_text("hi"), _finish("stop")])
        runner = SessionRunner(
            session,
            loop_factory=lambda s: agent_loop(s, TierRouter(), _factory(provider), make_config()),
        )
        await runner.start()
        assert session.turn_in_flight is False, "an idle loop owes nothing"

        await session.add_user_message("hello")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert session.turn_in_flight is True

        released.set()
        for _ in range(200):
            if not session.turn_in_flight:
                break
            await asyncio.sleep(0.02)
        assert session.turn_in_flight is False, "turn_complete lowers it again"
        await runner.cancel()


class TestArchive:
    @pytest.mark.asyncio
    async def test_archive_flag_rides_the_session_list(self, tmp_path: Path) -> None:
        """A session reports archived once filed, and reports it again on restore."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            opened = await _open_workspace(ws, str(tmp_path))
            sid = opened["session_id"]

            listing = await _request(ws, {"type": "list_sessions"})
            assert _summary(listing, sid) is not None
            assert _summary(listing, sid)["archived"] is False

            listing = await _request(ws, {"type": "archive_session", "session_id": sid})
            assert listing["type"] == "session_list"
            assert _summary(listing, sid)["archived"] is True

            listing = await _request(
                ws, {"type": "archive_session", "session_id": sid, "archived": False}
            )
            assert _summary(listing, sid)["archived"] is False
            await ws.close()

    @pytest.mark.asyncio
    async def test_archive_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        """The flag is durable metadata, not a live-process fact.

        The proof is a second daemon over the same data directory: it never
        saw the archive_session message, so anything it reports came off
        disk.
        """
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async with _RunningDaemon(data_dir) as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(workspace)))["session_id"]
            await _request(ws, {"type": "archive_session", "session_id": sid})
            await ws.close()

        async with _RunningDaemon(data_dir) as restarted:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{restarted.ws_server.port}", restarted.ws_server.token
            )
            listing = await _request(ws, {"type": "list_sessions"})
            summary = _summary(listing, sid)
            assert summary is not None, "the archived session must still be listed"
            assert summary["archived"] is True
            # Archived, not deleted: the session is still a real row with its
            # workspace intact — the rail's Archived section can restore it.
            assert summary["workspace_path"] == str(workspace)
            await ws.close()

    @pytest.mark.asyncio
    async def test_archived_session_is_never_a_live_auto_bind_candidate(
        self, tmp_path: Path
    ) -> None:
        """Every archived row on the wire is marked, in every state.

        Auto-bind picks the newest *live, unarchived* summary; the daemon's
        half of that contract is that the flag is on every row it lists,
        including a session that is still running.
        """
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            listing = await _request(ws, {"type": "archive_session", "session_id": sid})

            summary = _summary(listing, sid)
            assert summary is not None
            assert summary["state"] == "running", "archiving must not kill the session"
            assert summary["archived"] is True
            live_unarchived = [
                s for s in listing["sessions"] if not s["archived"] and s["state"] == "running"
            ]
            assert live_unarchived == []
            await ws.close()

    @pytest.mark.asyncio
    async def test_archive_is_allowed_mid_turn_and_leaves_the_turn_running(
        self, tmp_path: Path
    ) -> None:
        """Archive is filing, not killing: the in-flight turn is untouched."""
        _registry, store, sid, session = await _busy_session(str(tmp_path))

        assert await archive_session(store, sid, True) is None

        record = store.get(sid)
        assert record is not None
        assert record.archived is True
        assert session.turn_in_flight is True
        assert session.state == "running"
        assert session.cancel_requested is False

    @pytest.mark.asyncio
    async def test_archive_unknown_session_is_typed(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        refusal = await archive_session(store, "no-such-id", True)
        assert refusal is not None
        assert json.loads(refusal)["code"] == "session_not_found"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_drops_the_session_and_its_event_log(self, tmp_path: Path) -> None:
        """The row leaves session_list and the event log goes with it."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            keep = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            doomed = (await _request(ws, {"type": "new_session", "session_id": keep}))["session_id"]

            session = daemon.session_registry.get(doomed)
            assert session is not None
            assert session.event_log.last_seq > 0, "the session had a log to lose"

            listing = await _request(ws, {"type": "delete_session", "session_id": doomed})
            assert listing["type"] == "session_list"
            assert _summary(listing, doomed) is None
            assert _summary(listing, keep) is not None

            # Gone from the registry, the durable store, and the runner table:
            # nothing is left holding the log.
            assert daemon.session_registry.get(doomed) is None
            assert daemon.session_registry.get_runner(doomed) is None
            assert doomed not in {r.session_id for r in daemon._session_store.records()}
            await ws.close()

    @pytest.mark.asyncio
    async def test_delete_releases_an_attached_client(self, tmp_path: Path) -> None:
        """No streaming task is left parked on a log nothing can append to."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            keep = (await _open_workspace(ws, str(tmp_path)))["session_id"]
            doomed = (await _request(ws, {"type": "new_session", "session_id": keep}))["session_id"]

            await ws.send(json.dumps({"type": "attach", "session_id": doomed, "from_seq": 1}))
            for _ in range(50):
                if doomed in daemon._attached_clients:
                    break
                await asyncio.sleep(0.02)
            assert doomed in daemon._attached_clients

            # The attach replay is still arriving, so read past it to the
            # delete's own answer rather than assuming the next frame is it.
            await ws.send(json.dumps({"type": "delete_session", "session_id": doomed}))
            while True:
                frame = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
                if frame["type"] == "session_list":
                    break
            assert _summary(frame, doomed) is None
            assert doomed not in daemon._attached_clients
            assert not [key for key in daemon._streaming_tasks if key[1] == doomed]
            await ws.close()

    @pytest.mark.asyncio
    async def test_delete_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        """A deleted session does not come back when the store reloads."""
        data_dir = tmp_path / "data"
        workspace = tmp_path / "ws"
        workspace.mkdir()

        async with _RunningDaemon(data_dir) as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(workspace)))["session_id"]
            await _request(ws, {"type": "delete_session", "session_id": sid})
            await ws.close()

        async with _RunningDaemon(data_dir) as restarted:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{restarted.ws_server.port}", restarted.ws_server.token
            )
            listing = await _request(ws, {"type": "list_sessions"})
            assert _summary(listing, sid) is None
            await ws.close()

    @pytest.mark.asyncio
    async def test_delete_is_refused_mid_turn(self, tmp_path: Path) -> None:
        """The negative case: a session owing the user a turn refuses Delete."""
        registry, store, sid, session = await _busy_session(str(tmp_path))
        released: list[str] = []

        refusal = await delete_session(registry, store, sid, released.append)

        assert refusal is not None
        payload = json.loads(refusal)
        assert payload["code"] == "session_busy"
        assert payload["session_id"] == sid
        # Copy has to name the way out, not just the state.
        assert "archiv" in payload["message"].lower()
        # And nothing was torn down on the way to refusing.
        assert released == []
        assert registry.get(sid) is not None
        assert store.get(sid) is not None
        assert session.cancel_requested is False

    @pytest.mark.asyncio
    async def test_delete_allowed_once_the_turn_completes(self, tmp_path: Path) -> None:
        """The refusal lifts on the event that proves the turn ended."""
        registry, store, sid, session = await _busy_session(str(tmp_path))
        await session.event_log.add(
            TurnComplete(
                session_id=sid,
                tier="brain",
                tokens=10,
                cost=0.0,
                duration=0.2,
                seq=1,
            )
        )
        assert session.turn_in_flight is False

        assert await delete_session(registry, store, sid, lambda _sid: None) is None
        assert registry.get(sid) is None
        assert store.get(sid) is None

    @pytest.mark.asyncio
    async def test_delete_unknown_session_is_typed(self, tmp_path: Path) -> None:
        registry = SessionRegistry()
        store = SessionStore(tmp_path)
        refusal = await delete_session(registry, store, "no-such-id", lambda _sid: None)
        assert refusal is not None
        assert json.loads(refusal)["code"] == "session_not_found"


class TestMove:
    @pytest.mark.asyncio
    async def test_move_reassigns_the_workspace_and_the_log_comes_along(
        self, tmp_path: Path
    ) -> None:
        """The session keeps its id and its event log; only the root moves."""
        origin = tmp_path / "origin"
        target = tmp_path / "target"
        origin.mkdir()
        target.mkdir()

        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(origin)))["session_id"]
            session = daemon.session_registry.get(sid)
            assert session is not None
            before = [e.seq for e in session.event_log.events_from(1)]

            listing = await _request(
                ws,
                {"type": "move_session", "session_id": sid, "workspace_path": str(target)},
            )
            assert listing["type"] == "session_list"
            summary = _summary(listing, sid)
            assert summary is not None
            assert summary["workspace_path"] == str(target)

            # Same session object, same history — plus the fresh wall for the
            # new root, so the client is never shown the old boundary.
            assert session.workspace_path == str(target)
            after = [e.seq for e in session.event_log.events_from(1)]
            assert after[: len(before)] == before
            assert session.event_log.events_from(1)[-1].type == "boundary_update"
            await ws.close()

    @pytest.mark.asyncio
    async def test_move_survives_a_daemon_restart(self, tmp_path: Path) -> None:
        """The reassignment is durable metadata, like the archive flag."""
        data_dir = tmp_path / "data"
        origin = tmp_path / "origin"
        target = tmp_path / "target"
        origin.mkdir()
        target.mkdir()

        async with _RunningDaemon(data_dir) as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(origin)))["session_id"]
            await _request(
                ws,
                {"type": "move_session", "session_id": sid, "workspace_path": str(target)},
            )
            await ws.close()

        async with _RunningDaemon(data_dir) as restarted:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{restarted.ws_server.port}", restarted.ws_server.token
            )
            listing = await _request(ws, {"type": "list_sessions"})
            summary = _summary(listing, sid)
            assert summary is not None
            assert summary["workspace_path"] == str(target)
            await ws.close()

    @pytest.mark.asyncio
    async def test_move_validates_the_target(self, tmp_path: Path) -> None:
        """A path that is not a directory is refused before anything changes."""
        origin = tmp_path / "origin"
        origin.mkdir()
        not_a_dir = tmp_path / "notes.txt"
        not_a_dir.write_text("hi")

        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            sid = (await _open_workspace(ws, str(origin)))["session_id"]

            reply = await _request(
                ws,
                {"type": "move_session", "session_id": sid, "workspace_path": str(not_a_dir)},
            )
            assert reply["type"] == "error"
            assert reply["code"] == "workspace_not_found"

            session = daemon.session_registry.get(sid)
            assert session is not None
            assert session.workspace_path == str(origin)
            await ws.close()

    @pytest.mark.asyncio
    async def test_move_is_refused_mid_turn(self, tmp_path: Path) -> None:
        """The other half of the negative case: Move waits for the turn too."""
        origin = tmp_path / "origin"
        target = tmp_path / "target"
        origin.mkdir()
        target.mkdir()
        registry, store, sid, session = await _busy_session(str(origin))

        refusal = await move_session(registry, store, sid, str(target))

        assert refusal is not None
        assert json.loads(refusal)["code"] == "session_busy"
        assert session.workspace_path == str(origin)
        record = store.get(sid)
        assert record is not None
        assert record.workspace_path == str(origin)

    @pytest.mark.asyncio
    async def test_move_unknown_session_is_typed(self, tmp_path: Path) -> None:
        registry = SessionRegistry()
        store = SessionStore(tmp_path)
        refusal = await move_session(registry, store, "no-such-id", str(tmp_path))
        assert refusal is not None
        assert json.loads(refusal)["code"] == "session_not_found"
