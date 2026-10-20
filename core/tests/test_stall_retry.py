"""A stall after output began is withdrawn and retried (TD-5159).

TD-5106's watchdog ends a stream that goes quiet. Before any output the
provider retries it. After output began the turn used to end as
``stream_stalled`` with the partial text on screen. Now the loop sends an
``assistant_retract`` for that response and asks once more, so the user
ends up with one reply. A mock gateway streams through the real
``ProviderClient``, so the real watchdog trips, scaled to 0.2 s.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    make_registry_and_dispatcher,
    wait_for_turn,
)
from tests.test_turn_stream_errors import _DONE, _answer, _event, _Gateway, _text
from tstd.cli_turn import _TurnReader
from tstd.config import ProviderRetryConfig
from tstd.loop import agent_loop
from tstd.protocol import (
    AssistantDelta,
    AssistantReasoning,
    AssistantRetract,
    DaemonEvent,
    ToolCall,
    TurnComplete,
    UserTurn,
    parse_daemon_event,
)
from tstd.provider import ProviderClient, ProviderError, TimeoutConfig, complete_streamed
from tstd.reply_retry import shown_deltas
from tstd.router import TierRouter
from tstd.scheduler.email_delivery import final_assistant_text
from tstd.scheduler.runner import turn_outcome
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, ToolRegistry

_IDLE = TimeoutConfig(stream_idle=0.2)


def _reasoning(text: str) -> bytes:
    return _event({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning": text}}]})


def _tool_call(message: str) -> bytes:
    call = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "echo", "arguments": json.dumps({"message": message})},
    }
    return (
        _event({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [call]}}]})
        + _text("", finish="tool_calls")
        + _DONE
    )


async def _stalls_after(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    await asyncio.sleep(5)


async def _run(
    tmp_path: Path,
    gateway: _Gateway,
    *,
    retries: int = 2,
    delay: float = 0.001,
    dispatcher: ToolDispatcher | None = None,
    registry: ToolRegistry | None = None,
    stop_on_retract: bool = False,
) -> tuple[Session, TurnComplete]:
    session = Session(str(tmp_path))
    if dispatcher is None:
        dispatcher = attach_auto_approver(ToolDispatcher(ToolRegistry()))
    client = gateway.client(retries=retries, timeout=_IDLE)
    config = make_config()
    # The daemon builds every client's retry policy from this same key.
    config.provider_retry = ProviderRetryConfig(
        max_retries=retries, initial_delay=delay, max_delay=delay
    )

    async def factory(*_args: object) -> ProviderClient:
        return client

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s,
            TierRouter(),
            factory,
            config,
            tool_registry=registry,
            tool_dispatcher=dispatcher,
        ),
    )
    await runner.start()
    try:
        await session.add_user_message("fix it")
        if stop_on_retract:
            await _until(lambda: bool(_of(session, AssistantRetract)))
            await session.stop_turn()
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
        await client.close()
    return session, turn


async def _until(check: Any) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not met")


def _of(session: Session, kind: type[Any]) -> list[Any]:
    return [e for e in session.event_log.all_events if isinstance(e, kind)]


def _assistant_messages(session: Session) -> list[str]:
    return [str(m.content) for m in session.conversation if m.role == "assistant"]


def _shown(session: Session) -> str:
    """What the window ends up showing: every delta not withdrawn."""
    return "".join(shown_deltas(session.event_log.all_events))


class TestTurn:
    async def test_a_stall_after_output_is_withdrawn_and_retried(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda attempt: (
                _stalls_after(_text("The fix is to dele")) if attempt == 1 else _answer("Done.")
            )
        )
        session, turn = await _run(tmp_path, gateway)

        # Before TD-5159: one request, and the turn failed as stream_stalled
        # with the partial text on screen.
        assert (turn.failed, turn.error_code) == (False, None)
        assert gateway.requests == 2
        (retract,) = _of(session, AssistantRetract)
        assert retract.reason == "stream_stalled"
        first, *rest = _of(session, AssistantDelta)
        assert first.delta == "The fix is to dele"
        assert first.response_id == retract.response_id
        assert rest and all(d.response_id not in (None, retract.response_id) for d in rest)
        # The retract lands before the retry's first word.
        events = session.event_log.all_events
        assert events.index(retract) < events.index(rest[0])
        # One reply: on screen and in the conversation.
        assert _shown(session) == "Done."
        assert _assistant_messages(session) == ["Done."]

    async def test_thinking_from_the_stalled_attempt_is_withdrawn_too(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda attempt: (
                _stalls_after(_reasoning("Let me look"))
                if attempt == 1
                else _reasoning("Looked.") + _answer("Done.")
            )
        )
        session, turn = await _run(tmp_path, gateway)

        assert not turn.failed
        (retract,) = _of(session, AssistantRetract)
        stalled, retried = _of(session, AssistantReasoning)
        assert stalled.response_id == retract.response_id
        assert retried.response_id not in (None, retract.response_id)

    async def test_tool_calls_already_run_are_not_run_again(self, tmp_path: Path) -> None:
        registry, dispatcher = make_registry_and_dispatcher()
        runs: list[str] = []

        async def echo(
            session: Session, message: str, count: int = 1, tool_call_id: str = ""
        ) -> str:
            del session, count, tool_call_id
            runs.append(message)
            return f"Echo: {message}"

        dispatcher.register_handler("echo", echo)

        def reply(attempt: int) -> bytes | AsyncIterator[bytes]:
            if attempt == 1:
                return _text("Checking. ") + _tool_call("hi")
            if attempt == 2:
                return _stalls_after(_text("It says hi, so the fi"))
            return _answer("It says hi.")

        gateway = _Gateway(reply)
        session, turn = await _run(tmp_path, gateway, dispatcher=dispatcher, registry=registry)

        assert not turn.failed
        assert gateway.requests == 3
        assert runs == ["hi"]
        assert len(_of(session, ToolCall)) == 1
        # Only the stalled response's text goes; the text before the tool stays.
        (retract,) = _of(session, AssistantRetract)
        withdrawn = [
            d.delta for d in _of(session, AssistantDelta) if d.response_id == retract.response_id
        ]
        assert withdrawn == ["It says hi, so the fi"]
        assert _shown(session) == "Checking. It says hi."

    async def test_it_is_retried_once_not_per_provider_retry(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _stalls_after(_text(f"attempt {attempt} ")))
        session, turn = await _run(tmp_path, gateway, retries=3)

        assert (turn.failed, turn.error_code) == (True, "stream_stalled")
        assert gateway.requests == 2
        # The first attempt is withdrawn; the second stays, as before this
        # story, above the failure line that says it was discarded.
        (retract,) = _of(session, AssistantRetract)
        assert _shown(session).startswith("attempt 2 I encountered an error:")
        assert "attempt 1" not in _shown(session)
        assert retract.response_id == _of(session, AssistantDelta)[0].response_id
        assert all("attempt" not in m for m in _assistant_messages(session))

    async def test_max_retries_zero_turns_it_off(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _stalls_after(_text("partial ")))
        session, turn = await _run(tmp_path, gateway, retries=0)

        assert (turn.failed, turn.error_code) == (True, "stream_stalled")
        assert gateway.requests == 1
        assert not _of(session, AssistantRetract)

    async def test_a_stall_before_output_is_still_the_providers_retry(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _stalls_after() if attempt == 1 else _answer("Done."))
        session, turn = await _run(tmp_path, gateway)

        assert not turn.failed
        assert gateway.requests == 2
        # Nothing was shown, so there is nothing to withdraw.
        assert not _of(session, AssistantRetract)

    async def test_a_cut_off_reply_is_not_retried(self, tmp_path: Path) -> None:
        """Only a stall: a gateway that gave up mid-reply stays TD-5152's."""
        gateway = _Gateway(lambda attempt: _text("half"))
        session, turn = await _run(tmp_path, gateway)

        assert (turn.failed, turn.error_code) == (True, "stream_interrupted")
        assert gateway.requests == 1
        assert not _of(session, AssistantRetract)

    async def test_stop_during_the_wait_keeps_nothing(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda attempt: _stalls_after(_text("The fix is ")))
        loop = asyncio.get_running_loop()
        started = loop.time()
        session, turn = await _run(tmp_path, gateway, delay=30.0, stop_on_retract=True)

        assert turn.error_code == "stopped"
        assert gateway.requests == 1
        assert loop.time() - started < 3.0
        # The withdrawn text is not kept as a stopped turn's partial answer.
        assert all("The fix is" not in m for m in _assistant_messages(session))
        assert _shown(session) == ""


class TestOffTurnCalls:
    async def test_a_stall_after_output_is_returned_not_resent(self) -> None:
        """The work check and memory distill keep TD-5146's rule."""
        gateway = _Gateway(lambda attempt: _stalls_after(_text("PASS so fa")))
        client = gateway.client(retries=3, timeout=_IDLE)
        try:
            from tests.test_turn_stream_errors import _request

            result = await complete_streamed(client, _request())
        finally:
            await client.close()

        assert isinstance(result, ProviderError)
        assert result.code == "stream_stalled"
        assert gateway.requests == 1


# ── what reads the reply back ────────────────────────────────────────────


def _withdrawn_turn() -> list[DaemonEvent]:
    return [
        UserTurn(session_id="s", content="fix it", turn_id="t1", seq=1),
        AssistantDelta(session_id="s", delta="Checking. ", response_id="r1", seq=2),
        ToolCall(session_id="s", tool_call_id="c1", name="echo", arguments={}, seq=3),
        AssistantDelta(session_id="s", delta="The fix is to dele", response_id="r2", seq=4),
        AssistantRetract(session_id="s", response_id="r2", reason="stream_stalled", seq=5),
        AssistantDelta(session_id="s", delta="Done.", response_id="r3", seq=6),
        TurnComplete(session_id="s", tokens=0, cost=0.0, tier="brain", duration=1.0, seq=7),
    ]


class TestReaders:
    def test_shown_deltas_leave_out_a_withdrawn_response(self) -> None:
        assert "".join(shown_deltas(_withdrawn_turn())) == "Checking. Done."

    def test_text_without_a_response_id_is_never_withdrawn(self) -> None:
        events: list[DaemonEvent] = [
            AssistantDelta(session_id="s", delta="Next: tests. ", seq=1),
            AssistantRetract(session_id="s", response_id="r1", reason="stream_stalled", seq=2),
        ]
        assert shown_deltas(events) == ["Next: tests. "]

    async def test_the_scheduled_run_summary_leaves_it_out(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        for event in _withdrawn_turn():
            await session.event_log.add(event)
        assert turn_outcome(session) == ("Checking. Done.", False, None)

    def test_the_mailed_reply_leaves_it_out(self) -> None:
        assert final_assistant_text(_withdrawn_turn()) == "Done."

    async def test_tst_run_leaves_it_out_of_the_reply(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reader = _TurnReader(session_id="s", output="text", ours=True, content=None)
        outcome = None
        for event in _withdrawn_turn()[1:]:
            outcome = await reader.feed(None, json.loads(event.model_dump_json()))
        assert outcome is not None and outcome.reply == "Checking. Done."
        err = capsys.readouterr().err
        assert "withdrawn" in err

    def test_the_event_round_trips_and_old_deltas_still_parse(self) -> None:
        retract = AssistantRetract(session_id="s", response_id="r1", reason="stream_stalled", seq=3)
        assert parse_daemon_event(retract.model_dump_json()) == retract
        old = parse_daemon_event(
            '{"type": "assistant_delta", "seq": 1, "session_id": "s", "delta": "x"}'
        )
        assert isinstance(old, AssistantDelta) and old.response_id is None
