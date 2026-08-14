"""Tests for external import approval (TD-505).

Covers the assembler gate (external imports are pending / approved /
denied), the per-workspace allowlist persistence, the session's
``request_import_approval``, and the loop-level approval gate that parks
a turn until the import is resolved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.context import ContextAssembler, SteeringFileResolver
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, load_approved_imports, save_approved_imports
from tstd.protocol import ApprovalRequest as ApprovalRequestEvent
from tstd.router import TierRouter
from tstd.session import Session


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_assembler(home: Path) -> ContextAssembler:
    return ContextAssembler(resolver=SteeringFileResolver(home_dir=home))


async def _wait_for_approval(session: Session, _timeout: float = 3.0) -> ApprovalRequestEvent:
    """Return the first ``approval_request`` event, or raise on timeout."""
    deadline = asyncio.get_running_loop().time() + _timeout
    while asyncio.get_running_loop().time() < deadline:
        for event in session.event_log.all_events:
            if isinstance(event, ApprovalRequestEvent):
                return event
        await asyncio.sleep(0.02)
    raise TimeoutError("no approval request emitted")


# ── Assembler gate ───────────────────────────────────────────────────────


class TestAssemblerGate:
    """External imports are gated by the workspace boundary."""

    def test_external_import_is_pending_without_approval(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        external = (tmp_path / "external.md").resolve()
        _write(ws / "AGENTS.md", f"Root.\n@{external}")
        _write(external, "External content.")

        result = _make_assembler(home).assemble_sync(ws)

        assert "External content." not in result.block
        assert external in result.pending_imports
        assert any("awaiting approval" in issue for issue in result.import_issues)

    def test_approved_external_import_is_inlined(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        external = (tmp_path / "external.md").resolve()
        _write(ws / "AGENTS.md", f"Root.\n@{external}")
        _write(external, "External content.")

        result = _make_assembler(home).assemble_sync(ws, approved_imports=frozenset({external}))

        assert "External content." in result.block
        assert external not in result.pending_imports

    def test_denied_external_import_is_omitted_with_warning(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        external = (tmp_path / "external.md").resolve()
        _write(ws / "AGENTS.md", f"Root.\n@{external}")
        _write(external, "External content.")

        result = _make_assembler(home).assemble_sync(ws, denied_imports=frozenset({external}))

        assert "External content." not in result.block
        assert external not in result.pending_imports
        assert any("denied" in issue for issue in result.import_issues)

    def test_internal_import_is_not_gated(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "Root.\n@docs/arch.md")
        _write(ws / "docs" / "arch.md", "Architecture.")

        result = _make_assembler(home).assemble_sync(ws)

        assert "Architecture." in result.block
        assert not result.pending_imports

    def test_missing_external_file_is_not_pending(self, tmp_path: Path) -> None:
        home, ws = tmp_path / "home", tmp_path / "ws"
        _write(ws / "AGENTS.md", "Root.\n@/does/not/exist.md")

        result = _make_assembler(home).assemble_sync(ws)

        # Nothing to read, so nothing to approve.
        assert not result.pending_imports
        assert any("not found" in issue for issue in result.import_issues)


# ── Allowlist persistence ────────────────────────────────────────────────


class TestApprovedImportsStore:
    """``approved_external_imports`` round-trips through ``.tst/config.yaml``."""

    def test_round_trip(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(ws / ".tst" / "config.yaml", "policy:\n  rules: []\n")
        one = (tmp_path / "one.md").resolve()
        two = (tmp_path / "two.md").resolve()

        save_approved_imports(ws, [one, two])

        assert load_approved_imports(ws) == frozenset({one, two})

    def test_save_preserves_other_sections(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        _write(
            ws / ".tst" / "config.yaml",
            "boundary:\n  network: deny\npolicy:\n  rules: []\n",
        )
        one = (tmp_path / "one.md").resolve()

        save_approved_imports(ws, [one])

        text = (ws / ".tst" / "config.yaml").read_text()
        assert "boundary:" in text
        assert "network: deny" in text
        assert "policy:" in text
        assert "approved_external_imports:" in text


# ── Session approval ─────────────────────────────────────────────────────


class TestRequestImportApproval:
    """``Session.request_import_approval`` parks and resolves like TD-802."""

    async def test_emits_event_and_resolves_approve(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path / "ws"))
        await session.set_state("running")
        path = (tmp_path / "external.md").resolve()

        task = asyncio.create_task(session.request_import_approval(path))
        req = await _wait_for_approval(session)

        assert req.tool_call_id == f"external-import:{path}"
        assert req.tool_name == "external_import"
        assert req.decision_class == "C"
        assert req.summary == f"Read {path}"
        assert req.arguments == {"path": str(path)}
        assert req.proposed_always_allow is None  # class C → never always-allow
        assert session.state == "awaiting_approval"

        assert session.resolve_approval(req.tool_call_id, True)
        outcome = await task
        assert outcome.approved is True
        assert session.state == "running"

    async def test_deny(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path / "ws"))
        await session.set_state("running")
        path = (tmp_path / "external.md").resolve()

        task = asyncio.create_task(session.request_import_approval(path))
        req = await _wait_for_approval(session)

        assert session.resolve_approval(req.tool_call_id, False, "not now")
        outcome = await task
        assert outcome.approved is False
        assert "not now" in outcome.message

    async def test_timeout_denies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path / "ws"))
        session.policy = PolicyConfig(approval_timeout_seconds=0.1)
        await session.set_state("running")
        path = (tmp_path / "external.md").resolve()

        outcome = await session.request_import_approval(path)

        assert outcome.approved is False
        assert "timed out" in outcome.message


# ── Loop-level gate ──────────────────────────────────────────────────────


class TestLoopApprovalGate:
    """The loop parks a turn for an external import before proceeding."""

    async def test_approve_inlines_and_persists(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        external = (tmp_path / "external.md").resolve()
        _write(ws / "AGENTS.md", f"Root.\n@{external}")
        _write(external, "External content.")

        session = Session(str(ws))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Reply")})
        await start_loop(session, TierRouter(), mock, make_config())

        await session.add_user_message("hello")
        req = await _wait_for_approval(session)

        assert req.tool_name == "external_import"
        assert req.decision_class == "C"
        assert req.summary == f"Read {external}"
        assert session.state == "awaiting_approval"

        assert session.resolve_approval(req.tool_call_id, True)
        await wait_for_turn(session, 1)

        # Approval is remembered per workspace per file path.
        assert external in load_approved_imports(ws)

    async def test_deny_omits_and_continues(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        external = (tmp_path / "external.md").resolve()
        _write(ws / "AGENTS.md", f"Root.\n@{external}")
        _write(external, "External content.")

        session = Session(str(ws))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Reply")})
        await start_loop(session, TierRouter(), mock, make_config())

        await session.add_user_message("hello")
        req = await _wait_for_approval(session)

        assert session.resolve_approval(req.tool_call_id, False, "no")
        await wait_for_turn(session, 1)

        # Denial is not persisted; the allowlist stays empty.
        assert load_approved_imports(ws) == frozenset()
