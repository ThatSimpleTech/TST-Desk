"""Tests for tier state visibility and control (TD-1006).

The title bar needs to know what the daemon knows: which tier handles the
next turn, which slugs back the chips, and how much each tier has spent.
These tests pin the wire: ``tier_state`` on open / set_tier / tier change,
and ``cost_update`` carrying a per-tier breakdown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import CostUpdate, TierState
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry


def make_echo_session(workspace: Path) -> tuple[Session, ToolDispatcher]:
    """A session with an echo tool + dispatcher (no caps, default boundary)."""
    session = Session(str(workspace))
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Echo arguments back",
            parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )
    dispatcher = attach_auto_approver(ToolDispatcher(registry))  # TD-802: mechanics auto-approve

    async def echo_handler(session: object, message: str, tool_call_id: str = "") -> str:
        return f"Echo: {message}"

    dispatcher.register_handler("echo", echo_handler)
    return session, dispatcher


def stream_only_sequences() -> dict[str, list[Script]]:
    """A plain single-call turn on every tier slug."""
    return {
        slug: [Script(kind="stream", content="ok")]
        for slug in ("test-brain", "test-worker", "test-validator")
    }


# ── Daemon: tier_state on open + set_tier ───────────────────────────────


class TestDaemonTierState:
    async def test_open_workspace_emits_tier_state(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        response = await daemon._handle_message(
            json.dumps({"type": "open_workspace", "path": str(tmp_path)}), None
        )
        assert response is not None
        sid = json.loads(response)["session_id"]
        session = daemon.session_registry.get(sid)
        assert session is not None

        events = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert len(events) == 1
        assert events[0].tier == "brain"  # lead turns start on brain (TD-303)
        assert events[0].override is None
        # Slugs ride along so the chips show real model names; they come
        # from the loaded config, never literals here (slugs stay out of
        # source). Just pin the shape.
        assert set(events[0].model_slugs) == {"brain", "worker", "validator"}
        assert all(events[0].model_slugs.values())

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_set_tier_overrides_router_and_emits(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        response = await daemon._handle_message(
            json.dumps({"type": "open_workspace", "path": str(tmp_path)}), None
        )
        assert response is not None
        sid = json.loads(response)["session_id"]
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert session.router is not None

        # A set_tier flips the override and acknowledges with tier_state.
        result = await daemon._handle_message(
            json.dumps({"type": "set_tier", "session_id": sid, "tier": "worker"}), None
        )
        assert result is None
        assert session.router.override == "worker"

        events = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert events[-1].tier == "worker"
        assert events[-1].override == "worker"

        # Switching back pins again — the override persists (TD-303).
        await daemon._handle_message(
            json.dumps({"type": "set_tier", "session_id": sid, "tier": "validator"}), None
        )
        events = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert events[-1].tier == "validator"
        assert events[-1].override == "validator"

        runner = daemon.session_registry.get_runner(sid)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_set_tier_unknown_session(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        result = await daemon._handle_message(
            json.dumps({"type": "set_tier", "session_id": "nope", "tier": "worker"}), None
        )
        assert result is not None
        assert json.loads(result)["code"] == "session_not_found"
        await daemon._shutdown()

    async def test_set_tier_on_restored_tombstone_refused(self, tmp_path: Path) -> None:
        # A session restored after a daemon restart has no live loop — its
        # router is None and set_tier must be a fault, not a silent no-op.
        daemon = Daemon(data_dir=tmp_path / "data")
        session = await daemon.session_registry.create(str(tmp_path))
        assert session.router is None

        result = await daemon._handle_message(
            json.dumps({"type": "set_tier", "session_id": session.id, "tier": "worker"}), None
        )
        assert result is not None
        assert json.loads(result)["code"] == "session_not_live"
        await daemon._shutdown()


# ── Loop: tier_state follows routing, cost grows per call ───────────────


class TestLoopTierState:
    async def test_tier_state_follows_lead_turns_handoff(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(tmp_path)
        mock = MockProvider(sequences=stream_only_sequences())
        # Lead turns = 1: turn 1 on brain, turn 2 hands off to worker.
        runner = await start_loop(
            session, TierRouter(lead_turns=1), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("one")
        await wait_for_turn(session, 1)
        await session.add_user_message("two")
        await wait_for_turn(session, 2)

        events = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert [e.tier for e in events] == ["brain", "worker"]
        assert all(e.override is None for e in events)
        assert events[0].model_slugs == {
            "brain": "test-brain",
            "worker": "test-worker",
            "validator": "test-validator",
        }

        await runner.cancel()

    async def test_set_tier_mid_session_shows_on_next_turn(self, tmp_path: Path) -> None:
        session, dispatcher = make_echo_session(tmp_path)
        router = TierRouter(lead_turns=3)
        session.router = router
        mock = MockProvider(sequences=stream_only_sequences())
        runner = await start_loop(session, router, mock, make_config(), None, dispatcher)

        router.set_tier("validator")
        await session.add_user_message("pin me to validator")
        await wait_for_turn(session, 1)

        events = [e for e in session.event_log.all_events if isinstance(e, TierState)]
        assert events[-1].tier == "validator"
        assert events[-1].override == "validator"

        # And the validator call actually happened on the validator slug.
        assert any(call.model == "test-validator" for call in mock.calls)

        await runner.cancel()


class TestCostUpdateWire:
    async def test_cost_update_streams_per_call_with_per_tier_breakdown(
        self, tmp_path: Path
    ) -> None:
        session, dispatcher = make_echo_session(tmp_path)
        mock = MockProvider(sequences=stream_only_sequences())
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), None, dispatcher
        )

        await session.add_user_message("hello")
        await wait_for_turn(session, 1)

        updates = [e for e in session.event_log.all_events if isinstance(e, CostUpdate)]
        # One model call in this turn → at least one cost_update.
        assert updates, "cost_update must stream as costs accrue"
        last = updates[-1]
        assert last.session_cost > 0
        assert last.total_cost > 0
        # The whole turn ran on brain: the breakdown carries brain only.
        assert set(last.cost_by_tier) == {"brain"}
        assert last.cost_by_tier["brain"] == pytest.approx(last.session_cost)
        assert last.classifier_cost == 0.0

        await runner.cancel()
