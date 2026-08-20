"""Unanswered memory proposals are rejects (TD-2404).

Shutdown must not write. A new daemon must not apply a stale card.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_memory_end import _daemon, _open, _seed_turn
from tstd.daemon import Daemon
from tstd.memory_store import memory_dir
from tstd.protocol import MemoryProposal


async def _close(daemon: Daemon, sid: str) -> None:
    runner = daemon.session_registry.get_runner(sid)
    if runner is not None:
        await runner.cancel()
    await daemon._shutdown()


class TestUnansweredProposalIsReject:
    async def test_shutdown_with_live_proposal_writes_nothing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert any(isinstance(e, MemoryProposal) for e in session.event_log.all_events)
        assert sid in daemon._pending_memory
        await _close(daemon, sid)
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"

    async def test_shutdown_does_not_silently_accept(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        await _close(daemon, sid)
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") != "new\n"

    async def test_next_daemon_does_not_resurrect_a_proposal(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        first = _daemon(tmp_path, ws)
        sid = await _open(first, ws)
        await _seed_turn(first, sid)
        await first._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        session = first.session_registry.get(sid)
        assert session is not None
        proposal = next(e for e in session.event_log.all_events if isinstance(e, MemoryProposal))
        await _close(first, sid)

        second = Daemon(data_dir=tmp_path / "data")
        assert second._pending_memory == {}
        reply = await second._handle_message(
            json.dumps(
                {
                    "type": "memory_accept",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                }
            ),
            None,
        )
        assert reply is not None
        body = json.loads(reply)
        assert body["type"] == "error"
        assert body["code"] in {"no_memory_proposal", "session_not_found"}
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"
