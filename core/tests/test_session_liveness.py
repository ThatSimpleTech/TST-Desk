"""Tests for session liveness honesty (TD-1711).

A terminal session — ``complete``/``failed``/``cancelled``/``interrupted``
— has no consumer for user input.  Before this story the daemon accepted
``user_message`` for such sessions (and for restored tombstones with no
runner at all), enqueued it, logged "user message enqueued", and nothing
ever happened — the UI's Working… state spun forever (2026-08-14).  The
daemon now refuses with a typed, actionable ``session_not_running`` error
that names the dead session.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import PROTOCOL_VERSION


async def _connect_and_handshake(uri: str, token: str) -> Any:
    """Open a connection and perform the hello handshake."""
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _open_workspace(ws: Any, path: str) -> dict[str, Any]:
    """Send open_workspace and return the session_state response."""
    await ws.send(json.dumps({"type": "open_workspace", "path": path}))
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))


async def _send_user_message(ws: Any, session_id: str, content: str = "hello") -> None:
    await ws.send(
        json.dumps({"type": "user_message", "session_id": session_id, "content": content})
    )


class _RunningDaemon:
    """Spin a daemon up on a throwaway data dir and tear it down."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.daemon = Daemon(data_dir=Path(self._tmp.name))

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
        self._tmp.cleanup()


class TestUserMessageLiveness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["complete", "failed"])
    async def test_terminal_session_refuses_message(self, tmp_path: Path, state: str) -> None:
        """A session that ended refuses the send with a typed, session-tagged error."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            opened = await _open_workspace(ws, str(tmp_path))
            session_id = opened["session_id"]
            sess = daemon.session_registry.get(session_id)
            assert sess is not None
            await sess.set_state(state)

            await _send_user_message(ws, session_id)
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))

            assert reply["type"] == "error"
            assert reply["code"] == "session_not_running"
            # The UI attributes the refusal to its bound session.
            assert reply["session_id"] == session_id
            assert "new session" in reply["message"].lower()
            # Nothing was enqueued for the void.
            assert sess._user_message_queue.empty()
            await ws.close()

    @pytest.mark.asyncio
    async def test_cancelled_session_refuses_message(self, tmp_path: Path) -> None:
        """Cancel leaves a live runner record — the state is what gates."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            opened = await _open_workspace(ws, str(tmp_path))
            session_id = opened["session_id"]
            sess = daemon.session_registry.get(session_id)
            assert sess is not None
            await sess.cancel()

            await _send_user_message(ws, session_id)
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))

            assert reply["code"] == "session_not_running"
            assert sess._user_message_queue.empty()
            await ws.close()

    @pytest.mark.asyncio
    async def test_restored_tombstone_refuses_message(self, tmp_path: Path) -> None:
        """A session restored after restart has no runner at all (TD-1002/1711)."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _open_workspace(ws, str(tmp_path))
            # Mimic the restart path: the tombstone exists in the registry
            # with no runner and a fresh, empty event log.
            await daemon.session_registry.restore("tomb", str(tmp_path), "interrupted")

            await _send_user_message(ws, "tomb")
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))

            assert reply["code"] == "session_not_running"
            tomb = daemon.session_registry.get("tomb")
            assert tomb is not None
            assert tomb._user_message_queue.empty()
            await ws.close()

    @pytest.mark.asyncio
    async def test_dead_runner_refuses_even_before_terminal_state(self, tmp_path: Path) -> None:
        """Belt to the state check: no live loop task means no consumer."""
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _open_workspace(ws, str(tmp_path))
            # Registry entry with no runner and a non-terminal state —
            # unreachable through the daemon's own flows, but the gate must
            # not rely on state alone.
            await daemon.session_registry.restore("ghost", str(tmp_path), "paused")

            await _send_user_message(ws, "ghost")
            reply = dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))

            assert reply["code"] == "session_not_running"
            await ws.close()

    @pytest.mark.asyncio
    async def test_live_session_still_accepts(self, tmp_path: Path) -> None:
        """Regression guard: a running session still enqueues (no wire reply).

        Uses the placeholder loop (never dequeues) so the queued message is
        observable — the real loop would consume it within one poll.
        """
        async with _RunningDaemon() as daemon:
            ws = await _connect_and_handshake(
                f"ws://127.0.0.1:{daemon.ws_server.port}", daemon.ws_server.token
            )
            await _open_workspace(ws, str(tmp_path))
            from tstd.session import SessionRunner

            sess = await daemon.session_registry.create(str(tmp_path))
            runner = SessionRunner(sess)  # placeholder loop: waits for cancel only
            await runner.start()
            await daemon.session_registry.register_runner(sess.id, runner)
            assert sess.state == "running"
            assert runner.is_running

            await _send_user_message(ws, sess.id, "still alive")

            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                if not sess._user_message_queue.empty():
                    break
                await asyncio.sleep(0.02)
            assert sess._user_message_queue.get_nowait() == "still alive"
            # And no error frame arrived.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)
            await ws.close()
