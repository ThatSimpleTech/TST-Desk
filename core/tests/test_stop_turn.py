"""Stop ends the in-flight turn and leaves the session open (TD-5003)."""

from __future__ import annotations

import asyncio

from tests.test_loop import make_config, start_loop, wait_for_turn
from tstd.autonomy.classifier import DecisionClass
from tstd.mock import MockProvider, Script
from tstd.protocol import AssistantDelta, TurnComplete, parse_client_message
from tstd.protocol import StopTurn as StopTurnMessage
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool


def test_stop_turn_message_parses() -> None:
    msg = parse_client_message('{"type": "stop_turn", "session_id": "sess-1"}')
    assert isinstance(msg, StopTurnMessage)
    assert msg.session_id == "sess-1"


async def test_stop_while_idle_is_a_no_op() -> None:
    session = Session("/tmp/ws")
    await session.set_state("running")
    await session.stop_turn()
    assert session.stop_turn_requested is False
    assert session.state == "running"
    assert session.cancel_requested is False


async def test_stop_releases_a_paused_session() -> None:
    session = Session("/tmp/ws")
    await session.set_state("running")
    await session.add_user_message("go")
    await session.pause_at_cap("progress guard: still reading")
    assert session.state == "paused"
    await session.stop_turn()
    assert session.state == "running"
    assert session.stop_turn_requested is True
    assert session.cancel_requested is False


async def test_stop_denies_a_parked_approval() -> None:
    session = Session("/tmp/ws")
    await session.set_state("running")
    await session.add_user_message("go")
    tool = Tool(name="echo", side_effect_class="ask")
    parked = asyncio.create_task(
        session.request_approval("tc-1", tool, {}, DecisionClass.B, "run echo", "ask")
    )
    for _ in range(50):
        if session.state == "awaiting_approval":
            break
        await asyncio.sleep(0.01)
    assert session.state == "awaiting_approval"

    await session.stop_turn()
    outcome = await asyncio.wait_for(parked, timeout=2)
    assert outcome.approved is False
    assert "stopped" in outcome.message
    assert session.state == "running"
    assert session.cancel_requested is False


async def test_stop_mid_stream_keeps_the_session_and_runs_the_next_turn() -> None:
    session = Session("/tmp/ws")
    router = TierRouter(lead_turns=80)
    config = make_config()
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="stream",
                    content="one two three four five",
                    chunk_delay=0.05,
                ),
                Script(kind="stream", content="second turn"),
            ]
        }
    )
    runner = await start_loop(session, router, mock, config)
    await session.add_user_message("Hi")
    await session.add_user_message("Next please")

    for _ in range(50):
        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        if deltas:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("stream never started")

    await session.stop_turn()
    first = await wait_for_turn(session, 1)
    assert first.failed is False
    assert first.error_code == "stopped"
    assert session.state == "running"
    assert session.cancel_requested is False
    assert runner.is_running is True

    partial = "".join(
        event.delta for event in session.event_log.all_events if isinstance(event, AssistantDelta)
    )
    assert partial.startswith("one")
    user_turns = [message for message in session.conversation if message.role == "user"]
    assert any(message.content == "Hi" for message in user_turns)

    second = await wait_for_turn(session, 2)
    assert second.failed is False
    assert second.error_code is None
    assert session.state == "running"
    assert runner.is_running is True
    assert any(
        message.role == "assistant" and message.content == "second turn"
        for message in session.conversation
    )
    await runner.cancel()
    assert session.state == "cancelled"


async def test_cancel_during_a_stream_still_ends_the_session() -> None:
    session = Session("/tmp/ws")
    router = TierRouter(lead_turns=80)
    config = make_config()
    mock = MockProvider(
        scripts={
            "test-brain": Script(
                kind="stream",
                content="one two three four five",
                chunk_delay=0.05,
            )
        }
    )
    runner = await start_loop(session, router, mock, config)
    await session.add_user_message("Hi")
    for _ in range(50):
        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        if deltas:
            break
        await asyncio.sleep(0.02)

    await runner.cancel()
    assert session.state == "cancelled"
    assert runner.is_running is False
    completes = [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]
    assert completes == []
