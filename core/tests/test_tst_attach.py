"""Tests for ``tst attach`` (TD-3102).

Attach is a viewer of a session the daemon owns: replay + live text,
the existing ``session_not_found`` error, detach on interrupt.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_tst_run import _start_daemon, _stop_daemon, _workspace
from tstd import cli
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.protocol import ApprovalRequest, AssistantDelta


# Replay of an approval_request must not block pytest's stdin (TD-3103).
class _NonTtyStdin:
    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        raise AssertionError("non-TTY must not read stdin")


async def _wait_attached(daemon: Daemon, session_id: str) -> None:
    for _ in range(100):
        if daemon._attached_clients.get(session_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("cli did not attach")


async def _wait_detached(daemon: Daemon, session_id: str) -> None:
    for _ in range(100):
        if not daemon._attached_clients.get(session_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("cli did not detach")


async def _wait_event_type(session: Any, typ: str) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any(event.type == typ for event in session.event_log.events_from(1)):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {typ}")


async def _wait_stdout(capsys: pytest.CaptureFixture[str], needle: str) -> str:
    buf = ""
    for _ in range(100):
        buf += capsys.readouterr().out
        if needle in buf:
            return buf
        await asyncio.sleep(0.05)
    raise AssertionError(f"{needle!r} not in {buf!r}")


async def _open_session(daemon: Daemon, workspace: Path) -> Any:
    raw = await daemon._start_session(str(workspace))
    assert raw is not None
    session_id = json.loads(raw)["session_id"]
    session = daemon.session_registry.get(session_id)
    assert session is not None
    return session


async def _user_message_via_protocol(
    daemon: Daemon, session_id: str, content: str
) -> dict[str, Any] | None:
    """Send ``user_message``. None means the daemon accepted it (no reply)."""
    async with connect(f"ws://127.0.0.1:{daemon.ws_server.port}") as ws:
        await ws.send(json.dumps(cli.hello_message(str(daemon.ws_server.token))))
        ack = json.loads(await ws.recv())
        assert ack["type"] == "hello_ack"
        await ws.send(
            json.dumps({"type": "user_message", "session_id": session_id, "content": content})
        )
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
            except TimeoutError:
                return None
            event = json.loads(raw)
            if event.get("type") == "ping":
                continue
            return dict(event)
        return None


class TestParser:
    def test_attach_defaults(self, tmp_path: Path) -> None:
        ns = cli.parse_args(["attach", "sess-1", "--data-dir", str(tmp_path)])
        assert ns.command == "attach"
        assert ns.session_id == "sess-1"
        assert ns.from_seq == 1
        assert ns.data_dir == tmp_path

    def test_from_seq_flag(self) -> None:
        ns = cli.parse_args(["attach", "sess-9", "--from-seq", "4"])
        assert ns.from_seq == 4
        assert ns.data_dir is None


class TestAttachAgainstMockDaemon:
    async def test_replays_and_streams_text(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _NonTtyStdin())
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = _workspace(tmp_path)
        daemon, task = await _start_daemon(
            data_dir, Script(kind="stream", content="hello from replay")
        )
        follower: asyncio.Task[int] | None = None
        try:
            session = await _open_session(daemon, workspace)
            await session.add_user_message("say hi")
            await _wait_event_type(session, "turn_complete")

            follower = asyncio.create_task(cli.attach_session(session.id, data_dir, from_seq=1))
            await _wait_attached(daemon, session.id)
            replayed = await _wait_stdout(capsys, "turn complete")
            assert "hello from replay" in replayed
            assert "assistant_delta" not in replayed
            assert '"type"' not in replayed

            await session.event_log.add(
                AssistantDelta(session_id=session.id, delta="live chunk", seq=1)
            )
            await session.event_log.add(
                ApprovalRequest(
                    session_id=session.id,
                    tool_call_id="tc-1",
                    tool_name="shell",
                    arguments={"command": "ls"},
                    decision_class="B",
                    summary="Run `ls`",
                    reason="class B",
                    seq=1,
                )
            )
            live = await _wait_stdout(capsys, "approval: Run `ls`")
            assert "tool: shell" in live
            assert "class: B" in live
            assert "live chunk" in live

            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
        finally:
            if follower is not None and not follower.done():
                follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)
            await _stop_daemon(daemon, task)

    async def test_unknown_id_is_typed_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        daemon, task = await _start_daemon(data_dir, Script(kind="stream", content="unused"))
        try:
            code = await cli.attach_session("no-such-session", data_dir)
        finally:
            await _stop_daemon(daemon, task)
        assert code == 1
        err = capsys.readouterr().err
        assert "session_not_found" in err

    async def test_interrupt_detaches_without_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = _workspace(tmp_path)
        daemon, task = await _start_daemon(data_dir, Script(kind="stream", content="still running"))
        sent: list[dict[str, Any]] = []
        original_send = cli._send

        async def spy_send(ws: Any, message: dict[str, Any]) -> None:
            sent.append(message)
            await original_send(ws, message)

        monkeypatch.setattr(cli, "_send", spy_send)
        follower: asyncio.Task[int] | None = None
        try:
            session = await _open_session(daemon, workspace)
            follower = asyncio.create_task(cli.attach_session(session.id, data_dir))
            await _wait_attached(daemon, session.id)
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
            await _wait_detached(daemon, session.id)

            types = [message.get("type") for message in sent]
            assert "detach" in types
            assert "cancel" not in types
            assert session.state != "cancelled"

            reply = await _user_message_via_protocol(daemon, session.id, "after detach")
            assert reply is None or reply.get("code") != "session_not_running"
            await _wait_event_type(session, "turn_complete")
            contents = [
                getattr(event, "content", None) for event in session.event_log.events_from(1)
            ]
            assert "after detach" in contents
        finally:
            if follower is not None and not follower.done():
                follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)
            await _stop_daemon(daemon, task)


def test_main_dispatches_attach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Path, int]] = []

    async def fake_attach(session_id: str, data_dir: Path, from_seq: int = 1) -> int:
        seen.append((session_id, data_dir, from_seq))
        return 0

    monkeypatch.setattr(cli, "attach_session", fake_attach)
    data = tmp_path / "data"
    data.mkdir()
    code = cli.main(["attach", "sess-42", "--from-seq", "3", "--data-dir", str(data)])
    assert code == 0
    assert seen == [("sess-42", data.resolve(), 3)]
