"""Stop ends a running after-write review (TD-5151).

The review runs after ``turn_complete``, so no turn was open and
``stop_turn`` ignored the click: the review kept the loop (and a server
slot) for its whole run, minutes on a local model. Only ending the
session or quitting reached it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from pytest import MonkeyPatch

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_read_tools import make_dispatcher
from tstd.autonomy.verify import (
    confirm_pending_verify,
    maybe_verify_after_turn,
    note_tool_result,
)
from tstd.config import AutonomyConfig, ModelConfig, TierConfig
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.protocol import TurnComplete, VerifyResult
from tstd.provider import ProviderClient, RetryConfig
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

# Long enough that a review Stop does not reach outlives every wait here.
_SLOW_REVIEW = Script(
    kind="stream",
    content="verdict: pass " + "still reading the diff " * 100,
    chunk_delay=0.1,
)
_QUICK_REVIEW = Script(kind="text", content="verdict: pass\nDiff is fine.")


def _config(verify: str = "after_write") -> ModelConfig:
    return make_config().model_copy(update={"autonomy": AutonomyConfig(verify=verify)})


def _mock(validator: Script, *brain: Script) -> MockProvider:
    args = json.dumps({"path": "a.txt", "content": "hello\n"})
    return MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                Script(kind="stream", content="Done"),
                *brain,
            ]
        },
        scripts={"test-validator": validator},
    )


def _validator_calls(mock: MockProvider) -> int:
    return sum(1 for c in mock.calls if c.model == "test-validator")


def _verifies(session: Session) -> list[VerifyResult]:
    return [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]


async def _until(check: Callable[[], bool], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(what)
        await asyncio.sleep(0.01)


async def _loop(tmp_path: Path, mock: MockProvider, verify: str) -> tuple[Session, SessionRunner]:
    session = Session(str(tmp_path))
    dispatcher = make_dispatcher(tmp_path)
    runner = await start_loop(
        session,
        TierRouter(lead_turns=3),
        mock,
        _config(verify),
        dispatcher.registry,
        dispatcher,
    )
    return session, runner


def _assert_stopped(session: Session) -> None:
    """One review result, stopped, with nothing read from the partial text."""
    results = [e for e in _verifies(session) if not e.pending]
    assert len(results) == 1
    assert results[0].verdict == "error"
    assert results[0].summary == "Verify stopped."
    assert results[0].cost == 0.0


def _assert_idle(session: Session) -> None:
    assert session.review_in_flight is False
    assert session.turn_in_flight is False
    assert session.stop_turn_requested is False
    assert session.state == "running"
    assert session.cancel_requested is False


# ── The wire: Stop hangs up the review's stream ─────────────────────────


class _Assembler:
    async def assemble(self, name: str, **kwargs: object) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(text=f"You review a {name} diff.")


async def test_stop_hangs_up_a_hanging_review_stream(tmp_path: Path) -> None:
    """The server sent part of a verdict and then nothing. Stop ends it."""
    opened = asyncio.Event()
    closed = asyncio.Event()

    class _Hanging(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"verdict: pass"}}]}'
            yield b"\n\n"
            opened.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    def hanging(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Hanging())

    provider = ProviderClient(
        "http://forge.test/v1",
        "test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(hanging)),
        retry_config=RetryConfig(max_retries=0),
    )
    session = Session(str(tmp_path))
    await session.set_state("running")
    note_tool_result(session, "fs_write", "success", diff="--- a\n+++ b\n+hello\n")
    config = _config()

    async def client_for(tier: TierConfig) -> Any:
        del tier
        return provider

    review = asyncio.create_task(
        maybe_verify_after_turn(
            session,
            config,
            CostTracker(config),
            client_for,
            _Assembler(),  # type: ignore[arg-type]
        )
    )
    try:
        await asyncio.wait_for(opened.wait(), 2)
        await session.stop_turn()
        done, _ = await asyncio.wait({review}, timeout=2)
        assert review in done, "Stop left the review running"
    finally:
        review.cancel()
    review.result()
    assert closed.is_set()
    _assert_stopped(session)
    _assert_idle(session)


async def test_stop_reaches_an_open_review_and_still_ignores_idle() -> None:
    session = Session("/tmp/ws")
    await session.set_state("running")
    await session.stop_turn()
    assert session.stop_turn_requested is False
    with session.review_open():
        assert session.review_in_flight is True
        await session.stop_turn()
    assert session.stop_turn_requested is True
    assert session.review_in_flight is False


async def test_a_stop_that_landed_as_the_turn_ended_skips_the_call(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    await session.set_state("running")
    await session.add_user_message("write it")
    await session.stop_turn()
    await session.event_log.add(
        TurnComplete(session_id=session.id, tokens=0, cost=0.0, tier="brain", duration=0.1, seq=1)
    )
    note_tool_result(session, "fs_write", "success", diff="+hello\n")
    mock = MockProvider(scripts={"test-validator": _QUICK_REVIEW})
    config = _config()

    async def client_for(tier: TierConfig) -> Any:
        del tier
        return mock

    await maybe_verify_after_turn(
        session,
        config,
        CostTracker(config),
        client_for,
        _Assembler(),  # type: ignore[arg-type]
    )
    assert _validator_calls(mock) == 0
    _assert_stopped(session)
    _assert_idle(session)


# ── The loop: after_write ───────────────────────────────────────────────


async def _stop_the_review(session: Session, mock: MockProvider) -> None:
    await _until(lambda: _validator_calls(mock) == 1, "the review never started")
    await session.stop_turn()
    await _until(
        lambda: any(not e.pending for e in _verifies(session)),
        "Stop left the review running",
    )


async def test_stop_ends_the_review_and_the_next_turn_runs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mock = _mock(_SLOW_REVIEW, Script(kind="stream", content="Second answer"))
    session, runner = await _loop(tmp_path, mock, "after_write")
    try:
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        await _stop_the_review(session, mock)
        _assert_stopped(session)
        await _until(lambda: not session.review_in_flight, "the review stayed open")
        _assert_idle(session)

        await session.add_user_message("And now?")
        second = await wait_for_turn(session, 2)
    finally:
        await runner.cancel()
    assert second.failed is False
    assert second.error_code is None
    assert _validator_calls(mock) == 1


async def test_a_message_queued_behind_the_review_still_runs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Stop ends what is running. A queued turn is not discarded (TD-5003)."""
    monkeypatch.chdir(tmp_path)
    mock = _mock(_SLOW_REVIEW, Script(kind="stream", content="Second answer"))
    session, runner = await _loop(tmp_path, mock, "after_write")
    try:
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)
        await _until(lambda: _validator_calls(mock) == 1, "the review never started")
        await session.add_user_message("Are you there?")
        await _stop_the_review(session, mock)
        second = await wait_for_turn(session, 2)
    finally:
        await runner.cancel()
    _assert_stopped(session)
    assert second.error_code is None


# ── The loop: ask mode ──────────────────────────────────────────────────


async def _pending(tmp_path: Path, mock: MockProvider) -> tuple[Session, SessionRunner]:
    session, runner = await _loop(tmp_path, mock, "ask")
    await session.add_user_message("Write a file")
    await wait_for_turn(session, 1)
    await _until(lambda: any(e.pending for e in _verifies(session)), "no pending verify")
    return session, runner


async def test_stop_leaves_a_pending_ask_review_to_confirm(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mock = _mock(_QUICK_REVIEW)
    session, runner = await _pending(tmp_path, mock)
    try:
        await session.stop_turn()
        assert session.stop_turn_requested is False
        await confirm_pending_verify(session)
    finally:
        await runner.cancel()
    done = [e for e in _verifies(session) if not e.pending]
    assert [e.verdict for e in done] == ["pass"]
    assert _validator_calls(mock) == 1


async def test_stop_ends_a_confirmed_ask_review(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    mock = _mock(_SLOW_REVIEW, Script(kind="stream", content="Second answer"))
    session, runner = await _pending(tmp_path, mock)
    # The daemon runs a confirmed review in the handler of `run_verify`.
    confirmed = asyncio.create_task(confirm_pending_verify(session))
    try:
        await _stop_the_review(session, mock)
        done, _ = await asyncio.wait({confirmed}, timeout=2)
        assert confirmed in done, "Stop left the confirmed review running"
        _assert_stopped(session)
        _assert_idle(session)
        await session.add_user_message("And now?")
        second = await wait_for_turn(session, 2)
    finally:
        confirmed.cancel()
        await runner.cancel()
    assert second.error_code is None
