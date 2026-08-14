"""Tests for the decisions ledger (TD-704).

Covers: the spec §12.3 markdown format (round-trippable), atomic and
concurrent-safe appends, the Class-A-requires-a-commit rule, and the
dispatch hook that appends and emits `decision_logged`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    DecisionClassifier,
    DecisionLedger,
    LedgerEntry,
    format_entry,
    parse_ledger,
)
from tstd.protocol import DecisionLogged as DecisionLoggedEvent
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry
from tstd.tools.boundary import PathGuard


async def _stub_worker(prompt: str) -> str:
    return "B"


def make_classifier(workspace: Path) -> AmbiguousClassifier:
    return AmbiguousClassifier(
        static=DecisionClassifier(Boundary(workspace_root=workspace)),
        call_worker=_stub_worker,
    )


# ── Markdown format (spec §12.3) ───────────────────────────────────────


class TestFormat:
    def test_format_matches_spec_123_shape(self) -> None:
        entry = LedgerEntry(
            timestamp=datetime(2026, 8, 12, 14, 22, 9, tzinfo=UTC),
            decision_class="A",
            what="Sidebar clock top-right, 240ms ease-out slide.",
            why="Charter says unobtrusive status furniture.",
            commit="3f9a1c2",
        )
        text = format_entry(entry)
        assert text == (
            "## 2026-08-12T14:22:09Z · Class A · commit 3f9a1c2\n"
            "**Chose:** Sidebar clock top-right, 240ms ease-out slide.\n"
            "**Why:** Charter says unobtrusive status furniture.\n"
            "**Undo:** `git revert 3f9a1c2`"
        )

    def test_round_trip(self) -> None:
        entry = LedgerEntry(
            decision_class="A",
            what="Rename variable x to elapsed.",
            why="Matches existing naming.",
            commit="abc123",
        )
        parsed = parse_ledger(format_entry(entry))
        assert len(parsed) == 1
        assert parsed[0].decision_class == "A"
        assert parsed[0].what == entry.what
        assert parsed[0].why == entry.why
        assert parsed[0].commit == "abc123"
        assert parsed[0].undo_command == "git revert abc123"

    def test_class_b_without_commit_has_no_undo(self) -> None:
        entry = LedgerEntry(
            decision_class="B", what="Add a dependency.", why="Needed.", commit=None
        )
        text = format_entry(entry)
        assert "Undo" not in text
        parsed = parse_ledger(text)[0]
        assert parsed.commit is None
        assert parsed.undo_command is None

    def test_parses_spec_example_verbatim(self) -> None:
        spec_example = (
            "## 2026-08-12T14:22:09Z · Class A · commit 3f9a1c2\n"
            "**Chose:** Sidebar clock top-right, 240ms ease-out slide.\n"
            '**Why:** Charter says "unobtrusive status furniture." Top-right '
            "keeps it out of the primary reading path. 240ms matches the "
            "existing panel transitions.\n"
            "**Undo:** `git revert 3f9a1c2`"
        )
        entries = parse_ledger(spec_example)
        assert len(entries) == 1
        assert entries[0].decision_class == "A"
        assert entries[0].commit == "3f9a1c2"
        assert entries[0].undo_command == "git revert 3f9a1c2"


# ── Append: atomic, accumulating, concurrent ───────────────────────────


class TestAppend:
    async def test_appends_and_reads_back(self, tmp_path: Path) -> None:
        ledger = DecisionLedger(tmp_path)
        await ledger.append(LedgerEntry(decision_class="A", what="W1", why="Y1", commit="c1"))
        await ledger.append(LedgerEntry(decision_class="B", what="W2", why="Y2"))
        entries = ledger.read()
        assert [e.what for e in entries] == ["W1", "W2"]
        assert entries[0].commit == "c1"
        assert (tmp_path / ".tst" / "autonomy" / "DECISIONS.md").exists()

    async def test_concurrent_appends_all_survive(self, tmp_path: Path) -> None:
        ledger = DecisionLedger(tmp_path)
        await asyncio.gather(
            *[
                ledger.append(LedgerEntry(decision_class="B", what=f"entry {i}", why="concurrent"))
                for i in range(20)
            ]
        )
        entries = ledger.read()
        assert len(entries) == 20
        assert {e.what for e in entries} == {f"entry {i}" for i in range(20)}

    async def test_class_a_without_commit_refused(self, tmp_path: Path) -> None:
        ledger = DecisionLedger(tmp_path)
        with pytest.raises(ValueError, match="Class A decisions require"):
            await ledger.append(LedgerEntry(decision_class="A", what="W", why="Y", commit=None))
        assert ledger.read() == []


# ── Dispatch hook (TD-704 integration) ─────────────────────────────────


class TestDispatchHook:
    async def test_class_b_decision_appends_and_emits(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        ledger = DecisionLedger(tmp_path)
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="echo",
                description="echo",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                side_effect_class="auto",
                parallel_safe=True,
            )
        )
        dispatcher = ToolDispatcher(
            registry,
            classifier=make_classifier(tmp_path),
            path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
            ledger=ledger,
        )

        async def handler(session: object, message: str, tool_call_id: str = "") -> str:
            return f"Echo: {message}"

        dispatcher.register_handler("echo", handler)

        result = await dispatcher.dispatch("c1", "echo", {"message": "hi"}, session=session)
        assert result.status == "success"
        assert result.decision_class is not None
        assert result.decision_class.value == "B"

        entries = ledger.read()
        assert len(entries) == 1
        assert entries[0].decision_class == "B"
        assert "echo" in entries[0].what

        logged = [e for e in session.event_log.all_events if isinstance(e, DecisionLoggedEvent)]
        assert len(logged) == 1
        assert logged[0].decision_class == "B"
        assert logged[0].commit is None

    async def test_class_a_without_commit_not_recorded(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        ledger = DecisionLedger(tmp_path)
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="fs_edit",
                description="edit",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                side_effect_class="auto",
                parallel_safe=False,
                path_fields=("path",),
                mutates=True,
            )
        )
        # No checkpointer: the in-workspace edit classifies as A but has
        # no commit, so it must NOT be recorded as Class A (AC 3).
        dispatcher = ToolDispatcher(
            registry,
            classifier=make_classifier(tmp_path),
            path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
            ledger=ledger,
        )

        async def handler(session: object, path: str, tool_call_id: str = "") -> str:
            return "wrote"

        dispatcher.register_handler("fs_edit", handler)

        result = await dispatcher.dispatch(
            "c1", "fs_edit", {"path": str(tmp_path / "a.txt")}, session=session
        )
        assert result.status == "success"  # the write still happened
        assert ledger.read() == []  # but nothing was logged as A
        logged = [e for e in session.event_log.all_events if isinstance(e, DecisionLoggedEvent)]
        assert logged == []
