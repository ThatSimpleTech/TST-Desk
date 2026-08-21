"""End session and graceful quit run distill (TD-2302)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_loop import make_config
from tests.test_usage_recording import ScriptedProvider
from tstd.daemon import Daemon
from tstd.memory_store import memory_dir
from tstd.mock import MockProvider, Script
from tstd.protocol import MemoryProposal, TurnComplete
from tstd.provider import ChatMessage


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _script(payload: object) -> Script:
    return Script(kind="text", content=json.dumps(payload))


async def _open(daemon: Daemon, workspace: Path) -> str:
    reply = await daemon._handle_message(
        json.dumps({"type": "open_workspace", "path": str(workspace)}),
        None,
    )
    assert reply is not None
    return str(json.loads(reply)["session_id"])


async def _seed_turn(daemon: Daemon, session_id: str) -> None:
    session = daemon.session_registry.get(session_id)
    assert session is not None
    session.conversation.extend(
        [
            ChatMessage(role="user", content="pin ruff"),
            ChatMessage(role="assistant", content="done"),
        ]
    )
    await session.event_log.add(
        TurnComplete(
            session_id=session_id,
            tokens=10,
            cost=0.0,
            tier="brain",
            duration=0.1,
            seq=1,
        )
    )


def _daemon(tmp_path: Path, workspace: Path) -> Daemon:
    _write(memory_dir(workspace) / "MEMORY.md", "old\n")
    mock = MockProvider(
        scripts={
            "test-worker": _script(
                {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "new\n"}]}
            )
        }
    )
    daemon = Daemon(data_dir=tmp_path / "data", provider=mock)
    daemon.config = make_config()
    return daemon


class TestEndSession:
    async def test_end_session_emits_proposal_and_does_not_write(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        reply = await daemon._handle_message(
            json.dumps({"type": "end_session", "session_id": sid}),
            None,
        )
        assert reply is None
        session = daemon.session_registry.get(sid)
        assert session is not None
        proposals = [e for e in session.event_log.all_events if isinstance(e, MemoryProposal)]
        assert len(proposals) == 1
        assert proposals[0].files[0].after == "new\n"
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_end_session_skips_when_no_turn(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert [e for e in session.event_log.all_events if isinstance(e, MemoryProposal)] == []
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()


class TestGracefulQuit:
    async def test_shutdown_distills_live_sessions_without_writing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._distill_live_sessions()
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert any(isinstance(e, MemoryProposal) for e in session.event_log.all_events)
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_shutdown_skips_when_provider_cannot_distill(self, tmp_path: Path) -> None:
        """Quit distill is best-effort: a streaming-only mock must not crash it."""
        ws = tmp_path / "ws"
        _write(memory_dir(ws) / "MEMORY.md", "old\n")
        daemon = Daemon(data_dir=tmp_path / "data", provider=ScriptedProvider())
        daemon.config = make_config()
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._distill_live_sessions()
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert [e for e in session.event_log.all_events if isinstance(e, MemoryProposal)] == []
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
