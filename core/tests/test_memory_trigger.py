"""Distill trigger (TD-2302).

End session and graceful quit share ``distill_if_due``. No completed
turn, or unchanged memory, emits nothing. The disk is not written.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_loop import make_config
from tstd.memory_store import memory_dir
from tstd.memory_trigger import completed_turn_count, distill_if_due
from tstd.mock import MockProvider, Script
from tstd.protocol import MemoryProposal, TurnComplete
from tstd.provider import ChatMessage
from tstd.session import Session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _script(payload: object) -> Script:
    return Script(kind="text", content=json.dumps(payload))


async def _with_turn(session: Session, *, failed: bool = False) -> None:
    session.conversation.extend(
        [
            ChatMessage(role="user", content="pin ruff"),
            ChatMessage(role="assistant", content="done"),
        ]
    )
    await session.event_log.add(
        TurnComplete(
            session_id=session.id,
            tokens=10,
            cost=0.0,
            tier="brain",
            duration=0.1,
            failed=failed,
            seq=1,
        )
    )


class TestDistillIfDue:
    async def test_no_completed_turn_emits_nothing(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(scripts={"test-worker": _script({"changes": []})})
        assert await distill_if_due(session, mock, make_config()) is None
        assert mock.calls == []

    async def test_failed_turn_does_not_count(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        await _with_turn(session, failed=True)
        assert completed_turn_count(session) == 0
        mock = MockProvider(scripts={"test-worker": _script({"changes": []})})
        assert await distill_if_due(session, mock, make_config()) is None

    async def test_changed_memory_emits_a_proposal(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        await _with_turn(session)
        _write(memory_dir(tmp_path) / "MEMORY.md", "old\n")
        mock = MockProvider(
            scripts={
                "test-worker": _script(
                    {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "new\n"}]}
                )
            }
        )
        emitted = await distill_if_due(session, mock, make_config())
        assert emitted is not None
        assert isinstance(emitted.event, MemoryProposal)
        assert emitted.event.session_id == session.id
        assert emitted.event.proposal_id == emitted.proposal_id
        assert emitted.event.files[0].path == ".tst/memory/MEMORY.md"
        assert emitted.event.files[0].diff
        assert memory_dir(tmp_path).joinpath("MEMORY.md").read_text(encoding="utf-8") == "old\n"

    async def test_unchanged_memory_emits_nothing(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        await _with_turn(session)
        _write(memory_dir(tmp_path) / "MEMORY.md", "same\n")
        mock = MockProvider(
            scripts={
                "test-worker": _script(
                    {"changes": [{"action": "replace", "path": "MEMORY.md", "content": "same\n"}]}
                )
            }
        )
        assert await distill_if_due(session, mock, make_config()) is None
