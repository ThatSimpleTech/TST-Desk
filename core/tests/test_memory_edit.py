"""Edited accept writes the edited bytes (TD-2403)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_memory_end import _daemon, _open, _seed_turn
from tstd.memory_store import memory_dir
from tstd.protocol import MemoryProposal


async def _propose(tmp_path: Path) -> tuple[object, str, MemoryProposal, Path]:
    ws = tmp_path / "ws"
    daemon = _daemon(tmp_path, ws)
    sid = await _open(daemon, ws)
    await _seed_turn(daemon, sid)
    await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
    session = daemon.session_registry.get(sid)
    assert session is not None
    proposal = next(e for e in session.event_log.all_events if isinstance(e, MemoryProposal))
    return daemon, sid, proposal, ws


async def _close(daemon: object, sid: str) -> None:
    runner = daemon.session_registry.get_runner(sid)  # type: ignore[attr-defined]
    if runner is not None:
        await runner.cancel()
    await daemon._shutdown()  # type: ignore[attr-defined]


class TestMemoryEdit:
    async def test_accept_commits_edited_bytes(self, tmp_path: Path) -> None:
        daemon, sid, proposal, ws = await _propose(tmp_path)
        reply = await daemon._handle_message(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "type": "memory_edit",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                    "files": [{"path": ".tst/memory/MEMORY.md", "content": "edited by hand\n"}],
                }
            ),
            None,
        )
        assert reply is None
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == (
            "edited by hand\n"
        )
        await _close(daemon, sid)

    async def test_empty_content_deletes(self, tmp_path: Path) -> None:
        daemon, sid, proposal, ws = await _propose(tmp_path)
        target = memory_dir(ws) / "MEMORY.md"
        assert target.is_file()
        await daemon._handle_message(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "type": "memory_edit",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                    "files": [{"path": ".tst/memory/MEMORY.md", "content": ""}],
                }
            ),
            None,
        )
        assert not target.exists()
        await _close(daemon, sid)

    async def test_path_outside_the_proposal_is_ignored(self, tmp_path: Path) -> None:
        daemon, sid, proposal, ws = await _propose(tmp_path)
        sneak = memory_dir(ws) / "gotchas.md"
        sneak.write_text("stay\n", encoding="utf-8")
        await daemon._handle_message(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "type": "memory_edit",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                    "files": [
                        {"path": ".tst/memory/MEMORY.md", "content": "kept\n"},
                        {"path": ".tst/memory/gotchas.md", "content": "injected\n"},
                    ],
                }
            ),
            None,
        )
        assert sneak.read_text(encoding="utf-8") == "stay\n"
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "kept\n"
        await _close(daemon, sid)
