"""Interactive verify after writes (TD-4204).

Not the autonomy supervisor. Write turns may enqueue one validator
review; write-less turns never do; the result is a timeline event.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.autonomy.verify import (
    WRITE_TOOLS,
    confirm_pending_verify,
    deny_pending_verify,
    note_tool_result,
    parse_verdict,
    wrote_this_turn,
)
from tstd.config import AutonomyConfig, ModelConfig
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, CostUpdate, TurnComplete, VerifyResult
from tstd.router import TierRouter
from tstd.session import Session


def _config(*, verify: str = "after_write") -> ModelConfig:
    base = make_config()
    return base.model_copy(update={"autonomy": AutonomyConfig(verify=verify)})


def _validator_script(content: str = "verdict: pass\nDiff is fine.") -> Script:
    return Script(kind="text", content=content)


def _write_mock(*, validator: Script | None = None) -> MockProvider:
    args = json.dumps({"path": "a.txt", "content": "hello\n"})
    return MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                Script(kind="stream", content="Done"),
            ]
        },
        scripts={"test-validator": validator or _validator_script()},
    )


def validator_calls(mock: MockProvider) -> list[object]:
    return [c for c in mock.calls if c.model == "test-validator"]


async def wait_for_verify(
    session: Session,
    n: int = 1,
    *,
    pending: bool | None = None,
    _timeout: float = 3.0,
) -> VerifyResult:
    deadline = asyncio.get_running_loop().time() + _timeout
    while asyncio.get_running_loop().time() < deadline:
        events = [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]
        if pending is not None:
            events = [e for e in events if e.pending is pending]
        if len(events) >= n:
            return events[n - 1]
        await asyncio.sleep(0.02)
    raise TimeoutError(f"verify_result {n} did not arrive within {_timeout}s")


# ── Unit: write detection and verdict parsing ──────────────────────────


class TestWriteDetection:
    def test_fs_write_and_edit_are_writes(self) -> None:
        assert frozenset({"fs_write", "fs_edit"}) == WRITE_TOOLS
        session = SimpleNamespace()
        note_tool_result(session, "fs_write", "success", diff="-a\n+b\n")
        assert wrote_this_turn(session) is True

    def test_failed_write_is_not_a_write(self) -> None:
        session = SimpleNamespace()
        note_tool_result(session, "fs_write", "error", diff=None)
        assert wrote_this_turn(session) is False

    def test_shell_echo_is_not_a_write(self) -> None:
        session = SimpleNamespace()
        note_tool_result(session, "shell", "success", output="hello\n")
        assert wrote_this_turn(session) is False


class TestParseVerdict:
    def test_pass_and_fail_and_empty(self) -> None:
        assert parse_verdict("verdict: pass\nLooks good.")[0] == "pass"
        assert parse_verdict("verdict: fail\nDrift.")[0] == "fail"
        assert parse_verdict("")[0] == "error"


# ── Loop: after_write / off / write-less / autonomy / ask ──────────────


class TestInteractiveVerify:
    async def test_write_turn_after_write_calls_validator_once(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(verify="after_write"),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        result = await wait_for_verify(session)
        await runner.cancel()

        assert len(validator_calls(mock)) == 1
        assert result.verdict == "pass"
        assert result.pending is False
        assert result.session_id == session.id
        assert "Diff is fine" in result.summary
        assert result.cost > 0

        results = [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]
        assert len(results) == 1

    async def test_write_less_turn_never_verifies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="Just talking")})
        runner = await start_loop(session, TierRouter(lead_turns=3), mock, _config(), None, None)
        await session.add_user_message("Hello")
        await wait_for_turn(session, 1)
        await asyncio.sleep(0.1)
        await runner.cancel()

        assert validator_calls(mock) == []
        assert not any(isinstance(e, VerifyResult) for e in session.event_log.all_events)

    async def test_verify_off_write_never_verifies(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(verify="off"),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        await asyncio.sleep(0.1)
        await runner.cancel()

        assert validator_calls(mock) == []
        assert not any(isinstance(e, VerifyResult) for e in session.event_log.all_events)

    async def test_autonomy_session_never_emits_verify_result(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        session.autonomy = True
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(verify="after_write"),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        await asyncio.sleep(0.1)
        await runner.cancel()

        assert validator_calls(mock) == []
        assert not any(isinstance(e, VerifyResult) for e in session.event_log.all_events)

    async def test_no_extra_assistant_bubble(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        await wait_for_verify(session)
        await runner.cancel()

        turn = next(e for e in session.event_log.all_events if isinstance(e, TurnComplete))
        after = [
            e
            for e in session.event_log.all_events
            if isinstance(e, AssistantDelta) and e.seq > turn.seq
        ]
        assert after == []

    async def test_cost_attributed_to_validator(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        result = await wait_for_verify(session)
        await runner.cancel()

        assert result.cost > 0
        updates = [e for e in session.event_log.all_events if isinstance(e, CostUpdate)]
        assert any("validator" in e.cost_by_tier for e in updates)
        last = updates[-1]
        assert last.cost_by_tier["validator"] == pytest.approx(result.cost)

    async def test_ask_mode_waits_for_confirm(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(verify="ask"),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        pending = await wait_for_verify(session, pending=True)
        assert pending.pending is True
        assert validator_calls(mock) == []

        await confirm_pending_verify(session)
        done = await wait_for_verify(session, pending=False)
        await runner.cancel()

        assert len(validator_calls(mock)) == 1
        assert done.pending is False
        assert done.verdict == "pass"

    async def test_ask_mode_deny_skips_validator(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        session = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        mock = _write_mock()
        runner = await start_loop(
            session,
            TierRouter(lead_turns=3),
            mock,
            _config(verify="ask"),
            dispatcher.registry,
            dispatcher,
        )
        await session.add_user_message("Write a file")
        await wait_for_verify(session, pending=True)
        await deny_pending_verify(session)
        skipped = await wait_for_verify(session, pending=False)
        await runner.cancel()

        assert validator_calls(mock) == []
        assert skipped.summary == "Verify skipped."


def test_autonomy_config_default_is_after_write() -> None:
    assert AutonomyConfig().verify == "after_write"
    assert AutonomyConfig(verify="off").verify == "off"
    assert AutonomyConfig(verify="ask").verify == "ask"
