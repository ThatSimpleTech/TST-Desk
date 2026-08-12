"""Tests for crash resilience (TD-207).

A crash in one session must never take down other sessions, must produce
a readable failure event, and must leave no state that prevents the daemon
from restarting cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import cast

import pytest

from tstd.daemon import Daemon
from tstd.protocol import SessionState as SessionStateEvent
from tstd.session import Session, SessionRegistry, SessionRunner
from tstd.ws import WebSocketServer


class TestExceptionIsolation:
    async def test_one_session_failure_others_continue(self) -> None:
        """An exception inside one session's loop fails only that session."""
        reg = SessionRegistry()
        s1 = await reg.create("/tmp/a")
        s2 = await reg.create("/tmp/b")

        async def failing_loop(sess: Session) -> None:
            raise RuntimeError("boom")

        async def running_loop(sess: Session) -> None:
            await sess.wait_for_cancel()

        r1 = SessionRunner(s1, loop_factory=failing_loop)
        r2 = SessionRunner(s2, loop_factory=running_loop)

        await r1.start()
        await r2.start()
        await asyncio.sleep(0.1)

        # s1 failed, s2 still running
        assert s1.state == "failed"
        assert s2.state == "running"
        assert r1.is_running is False
        assert r2.is_running is True

        # Cleanup
        await r2.cancel()

    async def test_failure_emits_session_state_with_reason(self) -> None:
        """A loop failure emits a session_state event with a readable reason."""
        s = Session("/tmp/test")

        async def failing_loop(sess: Session) -> None:
            raise ValueError("disk is full")

        runner = SessionRunner(s, loop_factory=failing_loop)
        await runner.start()
        await asyncio.sleep(0.05)

        events = s.event_log.all_events
        # events: [running, failed]
        assert len(events) == 2
        failed = cast(SessionStateEvent, events[-1])
        assert failed.type == "session_state"
        assert failed.state == "failed"
        assert failed.reason == "disk is full"

    async def test_failure_traceback_in_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """The full traceback of a loop failure lands in the logs."""
        s = Session("/tmp/test")

        async def failing_loop(sess: Session) -> None:
            raise RuntimeError("traceback-me")

        with caplog.at_level(logging.ERROR, logger="tstd.session"):
            runner = SessionRunner(s, loop_factory=failing_loop)
            await runner.start()
            await asyncio.sleep(0.05)

        # The log record should contain the traceback via exc_info
        records = [r for r in caplog.records if r.getMessage() == "session loop failed"]
        assert records, "expected a 'session loop failed' log record"
        record = records[0]
        assert record.exc_info is not None, "log.exception must capture exc_info"
        # The exception info is processed by the formatter — verify the
        # formatted text contains the traceback
        assert record.exc_info[1] is not None
        assert "traceback-me" in str(record.exc_info[1])


class TestStalePortFile:
    async def test_restart_replaces_port_file(self) -> None:
        """A restart detects and replaces the stale port file from the
        previous daemon instance."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            # First daemon instance
            d1 = Daemon(data_dir=data_dir)
            await d1.ws_server.start()
            token1 = d1.ws_server.token
            port_file = data_dir / "port.json"
            assert port_file.exists()
            old_content = port_file.read_text()
            await d1.ws_server.stop()

            # Second daemon instance — must detect and replace the stale file
            d2 = Daemon(data_dir=data_dir)
            await d2.ws_server.start()
            port2 = d2.ws_server.port
            token2 = d2.ws_server.token

            # The port file was replaced with fresh content
            assert token1 != token2, "token must rotate on restart"
            content = json.loads(port_file.read_text())
            assert content["token"] == token2
            assert content["port"] == port2
            assert port_file.read_text() != old_content

            await d2.ws_server.stop()

    async def test_stale_port_file_does_not_block_startup(self) -> None:
        """A stale port file from a dead daemon does not prevent startup."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            # Simulate a stale port file left by a dead daemon
            stale = data_dir / "port.json"
            stale.write_text(json.dumps({"port": 99999, "token": "stale-token"}))

            # Startup must succeed and replace the file
            server = WebSocketServer(data_dir)
            await server.start()
            assert server.port > 0
            content = json.loads(stale.read_text())
            assert content["token"] == server.token
            assert content["port"] == server.port
            assert content["token"] != "stale-token"
            await server.stop()


class TestDaemonRestart:
    async def test_restart_after_stop_works(self) -> None:
        """The daemon restarts cleanly after a previous run — no corrupt
        state prevents a fresh start."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            async def run_daemon(d: Daemon) -> None:
                task = asyncio.create_task(d.run())
                for _ in range(50):
                    if d.ws_server.port:
                        break
                    await asyncio.sleep(0.05)
                assert d.ws_server.port > 0
                d._shutdown_event.set()
                await asyncio.gather(task, return_exceptions=True)

            d1 = Daemon(data_dir=data_dir)
            await run_daemon(d1)

            # Second run in the same directory
            d2 = Daemon(data_dir=data_dir)
            await run_daemon(d2)
            assert d2.ws_server.port > 0
            assert d2.ws_server.token != d1.ws_server.token

    async def test_daemon_health_after_restart(self) -> None:
        """The health endpoint reports correctly after restart."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            d1 = Daemon(data_dir=data_dir)
            d1_task = asyncio.create_task(d1.run())
            for _ in range(50):
                if d1.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            d1._shutdown_event.set()
            await asyncio.gather(d1_task, return_exceptions=True)

            d2 = Daemon(data_dir=data_dir)
            d2_task = asyncio.create_task(d2.run())
            for _ in range(50):
                if d2.ws_server.port:
                    break
                await asyncio.sleep(0.05)
            h = d2.health()
            assert h["version"] == "0.1.0"
            assert h["active_sessions"] == 0
            assert h["ws_port"] == d2.ws_server.port

            d2._shutdown_event.set()
            await asyncio.gather(d2_task, return_exceptions=True)
