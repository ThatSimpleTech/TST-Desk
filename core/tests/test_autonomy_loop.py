"""Unattended autonomy scheduler (TD-4101).

Covers: iterate without a further user message; Class A/B never prompt;
Class B still ledgers; Class C stops and notifies; cap faults notify and
complete (they do not pause); the SessionRunner progresses with no
viewer attached; start_autonomy launches that session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_cap_enforcement import (
    big_tool_call_sequence,
    make_echo_session,
    wait_for_state,
)
from tests.test_dispatch import make_classifier, make_config, start_loop, wait_for_turn
from tests.test_loop import start_loop as start_text_loop
from tstd.autonomy import Boundary
from tstd.autonomy.charter import Charter
from tstd.autonomy.classifier import DecisionClassifier
from tstd.autonomy.ledger import DecisionLedger
from tstd.autonomy.runner import (
    CLASS_C_STOP,
    CONTINUE_PREFIX,
    DOD_MET,
    advance_autonomy,
    first_prompt,
    should_notify,
    stop_reason,
)
from tstd.autonomy.supervisor import maybe_check_drift
from tstd.autonomy.worker import AmbiguousClassifier
from tstd.boundary_config import BoundaryConfig, CapsSection
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig
from tstd.protocol import ApprovalRequest, DecisionLogged, TurnComplete
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import ToolDispatcher, create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard


def make_charter(
    *,
    max_iterations: int = 2,
    spend_usd: float = 25.0,
    wall_clock_hours: float = 8.0,
    definition_of_done: list[str] | None = None,
    allowed_commands: list[str] | None = None,
    source_of_truth: list[str] | None = None,
) -> Charter:
    return Charter.model_validate(
        {
            "objective": "Ship the CSV importer",
            "definition_of_done": definition_of_done or ["The suite is green"],
            "source_of_truth": source_of_truth or [],
            "boundary": {
                "writable_paths": ["**"],
                "allowed_commands": allowed_commands or ["echo"],
                "network": "deny",
            },
            "caps": {
                "spend_usd": spend_usd,
                "wall_clock_hours": wall_clock_hours,
                "max_iterations": max_iterations,
            },
            "stop_conditions": [],
        }
    )


def _wire_autonomy(session: Session, charter: Charter) -> list[str]:
    session.autonomy = True
    session.charter = charter
    session.boundary_config.boundary = charter.boundary
    session.boundary_config.caps = charter.caps
    notified: list[str] = []

    async def _notify(message: str) -> None:
        notified.append(message)

    session.autonomy_notify = _notify
    return notified


# ── Scheduler unit ───────────────────────────────────────────────────────


class TestScheduler:
    def test_first_prompt_states_the_objective(self) -> None:
        charter = make_charter()
        text = first_prompt(charter)
        assert "Ship the CSV importer" in text
        assert "Do not wait for a user" in text

    def test_iteration_cap_is_a_stop(self) -> None:
        charter = make_charter(max_iterations=2)
        assert stop_reason(charter=charter, finished_turns=1, class_c=False) is None
        reason = stop_reason(charter=charter, finished_turns=2, class_c=False)
        assert reason is not None
        assert reason.startswith("iteration cap")
        assert should_notify(reason)

    def test_class_c_notifies_and_clean_complete_does_not(self) -> None:
        assert should_notify(CLASS_C_STOP)
        assert should_notify(DOD_MET)
        assert not should_notify("definition of done")

    async def test_advance_queues_then_stops(self) -> None:
        session = Session("/tmp/ws")
        session.autonomy = True
        session.charter = make_charter(max_iterations=2)
        assert await advance_autonomy(session) is True
        assert session.autonomy_turns == 1
        assert await advance_autonomy(session) is False
        assert session.autonomy_stop_reason is not None
        assert session.autonomy_stop_reason.startswith("iteration cap")

    async def test_advance_hooks_supervisor_every_n(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter(max_iterations=20)
        session.autonomy_check_every = 5
        prompts: list[str] = []

        async def _validator(prompt: str) -> str:
            prompts.append(prompt)
            return '{"serves_objective": true, "class_a_drifted": false, "progress_real": true}'

        session.validator_call = _validator
        for _ in range(4):
            assert await advance_autonomy(session) is True
        assert prompts == []
        assert await advance_autonomy(session) is True
        assert len(prompts) == 1
        assert session.last_drift_check is not None
        assert session.last_drift_check.serves_objective is True

    async def test_advance_hooks_supervisor_on_class_b(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = True
        session.charter = make_charter(max_iterations=20)
        session.autonomy_class_b = True
        calls = 0

        async def _validator(_prompt: str) -> str:
            nonlocal calls
            calls += 1
            return "YES\nNO\nYES"

        session.validator_call = _validator
        assert await advance_autonomy(session) is True
        assert session.autonomy_turns == 1
        assert calls == 1
        assert session.autonomy_class_b is False
        assert session.last_drift_check is not None
        assert session.last_drift_check.progress_real is True

    async def test_interactive_advance_helper_never_checks(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.autonomy = False
        session.charter = make_charter(max_iterations=20)
        session.autonomy_turns = 5
        session.autonomy_class_b = True
        called = False

        async def _validator(_prompt: str) -> str:
            nonlocal called
            called = True
            return "YES\nNO\nYES"

        session.validator_call = _validator
        assert await maybe_check_drift(session) is None
        assert called is False


# ── Loop ─────────────────────────────────────────────────────────────────


class TestUnattendedLoop:
    async def test_two_turns_without_a_further_user_message(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        charter = make_charter(max_iterations=2)
        notified = _wire_autonomy(session, charter)
        mock = MockProvider(
            scripts={
                "test-brain": Script(kind="stream", content="Working"),
                "test-worker": Script(kind="text", content="RED"),
            }
        )
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message(first_prompt(charter))
        await wait_for_turn(session, 2)
        await wait_for_state(session, "complete")
        assert not runner.is_running
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        assert len(completes) == 2
        brain = [c for c in mock.calls if c.model == "test-brain"]
        assert len(brain) == 2
        follow = [m.content for m in brain[1].messages if m.role == "user"]
        assert any(c is not None and CONTINUE_PREFIX in c for c in follow)
        assert notified  # iteration cap notifies
        assert any("iteration cap" in n for n in notified)
        assert any("Branch:" in n for n in notified)
        assert session.autonomy_stop_reason is not None
        assert session.autonomy_stop_reason.startswith("iteration cap")

    async def test_interactive_session_still_waits(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        mock = MockProvider(scripts={"test-brain": Script(kind="stream", content="ok")})
        runner = await start_text_loop(session, TierRouter(), mock, make_config())
        await session.add_user_message("hello")
        await wait_for_turn(session, 1)
        await asyncio.sleep(0.2)
        completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
        assert len(completes) == 1
        assert runner.is_running
        assert session.state == "running"
        await runner.cancel()

    async def test_class_b_runs_and_ledgers_without_a_prompt(self, tmp_path: Path) -> None:
        charter = make_charter(max_iterations=8)
        session, dispatcher = make_echo_session(
            tmp_path,
            BoundaryConfig(boundary=charter.boundary, caps=charter.caps),
        )
        _wire_autonomy(session, charter)
        dispatcher.policy = PolicyConfig()
        dispatcher.approval_handler = None
        dispatcher.classifier = make_classifier(str(tmp_path))
        dispatcher.autonomy_fn = lambda: session.autonomy
        dispatcher.ledger = DecisionLedger(tmp_path)
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="echo",
                        tool_arguments='{"message": "hi"}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message(first_prompt(charter))
        await wait_for_turn(session, 1)
        assert not any(isinstance(e, ApprovalRequest) for e in session.event_log.all_events)
        logged = [e for e in session.event_log.all_events if isinstance(e, DecisionLogged)]
        assert logged and logged[0].decision_class == "B"
        await runner.cancel()

    async def test_class_c_stops_and_notifies(self, tmp_path: Path) -> None:
        ws = tmp_path
        (ws / "AGENTS.md").write_text("# hi\n", encoding="utf-8")
        session = Session(str(ws))
        charter = make_charter(max_iterations=8)
        notified = _wire_autonomy(session, charter)
        bound = Boundary(workspace_root=ws)

        async def _unused(_prompt: str) -> str:
            return "B"

        registry = create_registry()
        dispatcher = ToolDispatcher(registry)
        dispatcher.classifier = AmbiguousClassifier(
            static=DecisionClassifier(bound),
            call_worker=_unused,
        )
        dispatcher.path_guard = PathGuard(bound)
        dispatcher.policy = PolicyConfig()
        dispatcher.autonomy_fn = lambda: session.autonomy
        dispatcher.on_class_c = session.mark_class_c
        register_builtin_handlers(dispatcher)
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="fs_write",
                        tool_arguments=json.dumps({"path": "AGENTS.md", "content": "rewritten"}),
                    ),
                ]
            }
        )
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), registry, dispatcher
        )
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.autonomy_class_c
        assert session.autonomy_stop_reason == CLASS_C_STOP
        assert any(CLASS_C_STOP in n for n in notified)
        assert any("Branch:" in n for n in notified)
        assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "# hi\n"
        assert not runner.is_running

    async def test_spend_cap_notifies_and_does_not_pause(self, tmp_path: Path) -> None:
        charter = make_charter(spend_usd=0.01, max_iterations=40)
        session, dispatcher = make_echo_session(
            tmp_path, BoundaryConfig(caps=CapsSection(spend_usd=0.01, max_iterations=40))
        )
        notified = _wire_autonomy(session, charter)
        dispatcher.skip_all_fn = lambda: True
        dispatcher.autonomy_fn = lambda: session.autonomy
        mock = MockProvider(sequences=big_tool_call_sequence())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )
        await session.add_user_message(first_prompt(charter))
        await wait_for_state(session, "complete")
        assert session.state != "paused"
        assert any("spend cap" in n for n in notified)
        assert any("Branch:" in n for n in notified)
        assert not runner.is_running


# ── Daemon launch ────────────────────────────────────────────────────────


class TestLaunch:
    async def test_launch_marks_the_session_and_seeds_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def _idle(session: Session, *_args: object, **_kwargs: object) -> None:
            content = await session.wait_for_user_message()
            if content is not None:
                seen.append(content)

        monkeypatch.setattr("tstd.daemon.agent_loop", _idle)
        daemon = Daemon(data_dir=tmp_path / "data")
        charter = make_charter(max_iterations=2)
        session_id = await daemon._launch_autonomy_run(tmp_path, charter)
        session = daemon.session_registry.get(session_id)
        assert session is not None
        assert session.autonomy is True
        assert session.charter is not None
        assert session.charter.objective == charter.objective
        assert session.boundary_config.caps.max_iterations == 2
        await wait_for_state(session, "complete")
        assert seen and "Ship the CSV importer" in seen[0]
        await daemon._shutdown()
