"""Distill accept is not a tool write (TD-2303).

The proposal never enters fs_write or the dispatcher. Accept writes
through the memory store (TD-2104), not the tool handlers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_memory_end import _daemon, _open, _seed_turn
from tstd.memory_store import memory_dir
from tstd.protocol import MemoryProposal


class TestDistillIsNotAToolWrite:
    async def test_proposal_call_has_no_tools(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        mock = daemon._provider
        assert mock is not None
        assert mock.calls  # type: ignore[union-attr]
        assert mock.calls[0].tools is None  # type: ignore[union-attr]
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_accept_writes_via_the_memory_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        session = daemon.session_registry.get(sid)
        assert session is not None
        proposal = next(e for e in session.event_log.all_events if isinstance(e, MemoryProposal))

        async def boom_write(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("fs_write must not run on distill-accept")

        monkeypatch.setattr("tstd.tools.write.fs_write", boom_write)
        monkeypatch.setattr("tstd.tools.handlers.fs_write", boom_write)

        def boom_classify(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("classifier must not run on distill-accept")

        monkeypatch.setattr("tstd.autonomy.classifier.DecisionClassifier.classify", boom_classify)

        store_writes: list[Path] = []
        real = __import__("tstd.memory_store", fromlist=["replace_memory_file"]).replace_memory_file

        def wrap(path: Path, content: str, max_lines: int) -> None:
            store_writes.append(Path(path))
            real(path, content, max_lines)

        monkeypatch.setattr("tstd.memory_trigger.replace_memory_file", wrap)

        reply = await daemon._handle_message(
            json.dumps(
                {
                    "type": "memory_accept",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                }
            ),
            None,
        )
        assert reply is None
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "new\n"
        assert store_writes
        assert all("memory" in p.parts for p in store_writes)
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_reject_writes_nothing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        daemon = _daemon(tmp_path, ws)
        sid = await _open(daemon, ws)
        await _seed_turn(daemon, sid)
        await daemon._handle_message(json.dumps({"type": "end_session", "session_id": sid}), None)
        session = daemon.session_registry.get(sid)
        assert session is not None
        proposal = next(e for e in session.event_log.all_events if isinstance(e, MemoryProposal))
        await daemon._handle_message(
            json.dumps(
                {
                    "type": "memory_reject",
                    "session_id": sid,
                    "proposal_id": proposal.proposal_id,
                }
            ),
            None,
        )
        assert memory_dir(ws).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"
        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()
