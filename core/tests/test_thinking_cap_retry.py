"""A reply that spent the whole output cap thinking (TD-5233).

On most providers a reasoning model's thinking counts against
``max_tokens``. A 4096-token cap could go entirely on thinking: the stream
ended ``finish_reason: "length"`` with no answer and no tool call, the turn
failed as ``empty_completion`` with advice to start a new session (wrong at
28k of a 256k window), and nothing was retried. Now the reply is withdrawn
and asked once more with a larger cap, and a turn that still gets no answer
says which setting to raise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_cap_enforcement import make_echo_session
from tests.test_dispatch import start_loop as start_dispatch_loop
from tests.test_loop import start_loop, wait_for_turn
from tests.test_reply_guard import _BodyGateway, _varied
from tests.test_turn_stream_errors import _DONE, _answer, _event, _text
from tstd.boundary_config import BoundaryConfig
from tstd.config import ModelConfig, Preset, ProviderRetryConfig, ReplyGuardConfig, TierConfig
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.output_cap import cap_setting, cut_call_note, raised_output_cap
from tstd.protocol import (
    AssistantReasoning,
    AssistantRetract,
    ContextCompacted,
    DaemonEvent,
    TurnComplete,
)
from tstd.provider import ChatCompletionRequest, ProviderClient, TimeoutConfig
from tstd.reply_retry import StreamResult, shown_deltas, stream_reply, thinking_ate_cap
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner

_CAP = 4_096
_WINDOW = 262_144
_RAISED = 16_384
_ANSWER = "Use the second option."
_NO_ANSWER = "No answer: the model ended its turn without writing anything. Resend to try again."
_CONTEXT_NOTE = "The conversation is also close to its context limit, so a new chat may help."


def _tier(slug: str, *, cap: int, window: int, guard: ReplyGuardConfig | None) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url="http://mock.local/v1",
        input_price=1.0,
        output_price=2.0,
        cache_read_price=0.5,
        context_window=window,
        max_output_tokens=cap,
        reply_guard=guard,
    )


def _config(
    *,
    cap: int = _CAP,
    window: int = _WINDOW,
    retries: int = 3,
    guard: ReplyGuardConfig | None = None,
) -> ModelConfig:
    config = ModelConfig(
        presets={
            "test": Preset(
                brain=_tier("test-brain", cap=cap, window=window, guard=guard),
                worker=_tier("test-worker", cap=cap, window=window, guard=None),
                validator=_tier("test-validator", cap=cap, window=window, guard=None),
            ),
        },
        active_preset="test",
    )
    config.provider_retry = ProviderRetryConfig(
        max_retries=retries, initial_delay=0.001, max_delay=0.001
    )
    return config


def _thinks_to_cap() -> Script:
    """A reasoning model that spends the whole cap thinking."""
    return Script(kind="stream", reasoning="Weighing every option first", finish_reason="length")


async def _run(
    tmp_path: Path,
    scripts: list[Script],
    config: ModelConfig,
    *,
    message: str = "Which option?",
    stop_on_retract: bool = False,
) -> tuple[Session, TurnComplete, MockProvider]:
    session = Session(str(tmp_path))
    mock = MockProvider(
        sequences={"test-brain": scripts},
        scripts={"test-brain": Script(kind="stream", content="UNEXPECTED EXTRA CALL")},
    )
    runner = await start_loop(session, TierRouter(), mock, config)
    try:
        await session.add_user_message(message)
        if stop_on_retract:
            await _until(lambda: bool(_of(session, AssistantRetract)))
            await session.stop_turn()
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
    return session, turn, mock


async def _until(check: Any) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("condition not met")


def _of(session: Session, kind: type[Any]) -> list[Any]:
    return [e for e in session.event_log.all_events if isinstance(e, kind)]


def _brain_calls(mock: MockProvider) -> list[ChatCompletionRequest]:
    return [call for call in mock.calls if call.model == "test-brain"]


def _caps(mock: MockProvider) -> list[int | None]:
    return [call.max_tokens for call in _brain_calls(mock)]


def _shown(session: Session) -> str:
    return "".join(shown_deltas(session.event_log.all_events))


def _last_assistant(session: Session) -> str:
    return str([m for m in session.conversation if m.role == "assistant"][-1].content)


# ── The retry ───────────────────────────────────────────────────────────


async def test_a_reply_that_thought_to_the_cap_is_asked_again_with_a_larger_cap(
    tmp_path: Path,
) -> None:
    session, turn, mock = await _run(
        tmp_path, [_thinks_to_cap(), Script(kind="stream", content=_ANSWER)], _config()
    )

    # Before TD-5233: one call, and a failed empty_completion turn.
    assert (turn.failed, turn.error_code) == (False, None)
    assert _caps(mock) == [_CAP, _RAISED]
    (retract,) = _of(session, AssistantRetract)
    assert retract.reason == "output_cap_thinking"
    first_thought = _of(session, AssistantReasoning)[0]
    assert retract.response_id == first_thought.response_id
    assert _shown(session) == _ANSWER
    assert _last_assistant(session) == _ANSWER


async def test_both_attempts_at_the_cap_fail_naming_the_setting(tmp_path: Path) -> None:
    session, turn, mock = await _run(tmp_path, [_thinks_to_cap(), _thinks_to_cap()], _config())

    assert (turn.failed, turn.error_code) == (True, "output_cap_thinking")
    assert _caps(mock) == [_CAP, _RAISED]
    assert len(_of(session, AssistantRetract)) == 1
    text = _last_assistant(session)
    assert text == (
        "No answer: the model used its whole output limit thinking, including one retry at "
        "16,384 tokens. Raise max_output_tokens for the brain tier (presets.test.brain), now "
        "4,096, in Settings → Configuration. New chats use the new limit."
    )
    # The context is far from full, so starting over is not the advice.
    assert "new chat may help" not in text and "new session" not in text.lower()
    assert _shown(session) == text


async def test_an_empty_reply_not_at_the_cap_is_not_retried(tmp_path: Path) -> None:
    thinks_then_stops = Script(kind="stream", reasoning="Hmm, well")
    session, turn, mock = await _run(tmp_path, [thinks_then_stops], _config())

    assert (turn.failed, turn.error_code) == (True, "empty_completion")
    assert len(_brain_calls(mock)) == 1
    assert not _of(session, AssistantRetract)
    assert _last_assistant(session) == _NO_ANSWER


async def test_max_retries_zero_turns_it_off(tmp_path: Path) -> None:
    session, turn, mock = await _run(tmp_path, [_thinks_to_cap()], _config(retries=0))

    assert (turn.failed, turn.error_code) == (True, "output_cap_thinking")
    assert _caps(mock) == [_CAP]
    assert not _of(session, AssistantRetract)
    assert _last_assistant(session) == (
        "No answer: the model used its whole output limit (4,096 tokens) thinking. Raise "
        "max_output_tokens for the brain tier (presets.test.brain) in Settings → "
        "Configuration. New chats use the new limit."
    )


@pytest.mark.parametrize(
    ("code", "status"), [("insufficient_credits", 402), ("context_overflow", 400)]
)
async def test_a_refused_larger_cap_keeps_the_first_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, code: str, status: int
) -> None:
    refused = Script(kind="error", error_code=code, status_code=status, content="refused")
    with caplog.at_level(logging.WARNING, logger="tstd.reply_retry"):
        session, turn, mock = await _run(tmp_path, [_thinks_to_cap(), refused], _config())

    # The cause is the cap, not the refusal of the larger one.
    assert (turn.failed, turn.error_code) == (True, "output_cap_thinking")
    assert _caps(mock) == [_CAP, _RAISED]
    assert "(4,096 tokens)" in _last_assistant(session)
    # No tighter retry for an overflow, and nothing elided for one.
    assert not _of(session, ContextCompacted)
    (warning,) = [r for r in caplog.records if "larger output cap was refused" in r.getMessage()]
    assert warning.extra_fields["code"] == code  # type: ignore[attr-defined]


async def test_a_retryable_failure_of_the_larger_cap_is_the_turns(tmp_path: Path) -> None:
    down = Script(kind="error", status_code=503, content="upstream unavailable")
    _, turn, mock = await _run(tmp_path, [_thinks_to_cap(), down], _config())

    assert (turn.failed, turn.error_code) == (True, "server_error")
    assert len(_brain_calls(mock)) == 2


async def test_no_retry_when_the_window_has_no_room_for_double(tmp_path: Path) -> None:
    session, turn, mock = await _run(tmp_path, [_thinks_to_cap()], _config(window=9_000))

    assert (turn.failed, turn.error_code) == (True, "output_cap_thinking")
    assert _caps(mock) == [_CAP]
    assert not _of(session, AssistantRetract)
    assert "(4,096 tokens)" in _last_assistant(session)


async def test_the_larger_cap_is_bounded_by_the_room_in_the_window(tmp_path: Path) -> None:
    window = 14_000
    _, turn, mock = await _run(
        tmp_path,
        [_thinks_to_cap(), Script(kind="stream", content=_ANSWER)],
        _config(window=window),
    )

    assert not turn.failed
    first, raised = _caps(mock)
    assert first == _CAP
    assert raised is not None
    assert 2 * _CAP <= raised < 4 * _CAP
    assert raised <= window - 1_024


async def test_stop_during_the_retry_ends_the_turn_as_stopped(tmp_path: Path) -> None:
    slow = Script(kind="stream", content="An answer that streams slowly", chunk_delay=0.2)
    session, turn, _ = await _run(
        tmp_path, [_thinks_to_cap(), slow], _config(), stop_on_retract=True
    )

    assert (turn.failed, turn.error_code) == (False, "stopped")
    assert not any("No answer" in str(m.content) for m in session.conversation)
    assert _shown(session) == ""


class _StopsAsItEnds(MockProvider):
    """Stop lands after the last chunk, before the loop reads the reply."""

    def __init__(self, session: Session, script: Script) -> None:
        super().__init__(scripts={"test-brain": script})
        self._session = session

    async def chat_completion_stream(self, request: ChatCompletionRequest) -> AsyncIterator[Any]:
        async for item in super().chat_completion_stream(request):
            yield item
        await self._session.stop_turn()


async def test_stop_as_the_reply_ends_is_not_a_failed_turn(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    mock = _StopsAsItEnds(session, _thinks_to_cap())
    runner = await start_loop(session, TierRouter(), mock, _config())
    try:
        await session.add_user_message("Which option?")
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert (turn.failed, turn.error_code) == (False, "stopped")
    assert len(_brain_calls(mock)) == 1
    assert not _of(session, AssistantRetract)
    assert not any("No answer" in str(m.content) for m in session.conversation)


async def test_a_cut_tool_call_at_the_larger_cap_names_that_cap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session, dispatcher = make_echo_session(tmp_path, BoundaryConfig())
    cut = Script(
        kind="tool_call",
        tool_batch=(("echo", '{"message": "first"}'), ("echo", '{"message": "seco')),
        finish_reason="length",
    )
    mock = MockProvider(
        sequences={
            "test-brain": [_thinks_to_cap(), cut, Script(kind="stream", content="Sent one.")]
        }
    )
    runner = await start_dispatch_loop(
        session, TierRouter(lead_turns=80), mock, _config(), None, dispatcher
    )
    try:
        with caplog.at_level(logging.WARNING, logger="tstd.output_cap"):
            await session.add_user_message("echo twice")
            turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert not turn.failed
    replay = _brain_calls(mock)[-1].messages
    replies = {m.tool_call_id: m.content for m in replay if m.role == "tool"}
    assert replies["call_mock_2"] == cut_call_note(_RAISED)
    caps = [
        r.extra_fields["max_output_tokens"]  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "reply hit the output cap"
    ]
    assert caps == [_RAISED]


# ── Near the context limit ──────────────────────────────────────────────


async def test_a_backup_that_thought_to_its_cap_names_its_own_setting(tmp_path: Path) -> None:
    """The backup's own max_output_tokens applied, so the copy names it (TD-5233)."""
    config = _config()
    backup = _tier("test-backup", cap=8_192, window=_WINDOW, guard=None)
    config.presets["test"].brain.fallbacks = [backup]
    down = Script(kind="error", status_code=503, content="upstream unavailable")
    session = Session(str(tmp_path))
    mock = MockProvider(
        sequences={"test-brain": [down], "test-backup": [_thinks_to_cap(), _thinks_to_cap()]},
    )
    runner = await start_loop(session, TierRouter(), mock, config)
    try:
        await session.add_user_message("Which option?")
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    assert (turn.failed, turn.error_code) == (True, "output_cap_thinking")
    assert [(c.model, c.max_tokens) for c in mock.calls if c.model == "test-backup"] == [
        ("test-backup", 8_192),
        ("test-backup", 32_768),
    ]
    assert _last_assistant(session) == (
        "No answer: the model used its whole output limit thinking, including one retry at "
        "32,768 tokens. Raise max_output_tokens for the brain tier's backup "
        "(presets.test.brain.fallbacks[0]), now 8,192, in Settings → Configuration. New chats "
        "use the new limit."
    )


@pytest.mark.parametrize(
    ("scripts", "code", "sent", "finish"),
    [
        ([_thinks_to_cap(), _thinks_to_cap()], "output_cap_thinking", _RAISED, "length"),
        ([Script(kind="stream", reasoning="Hmm")], "empty_completion", _CAP, "stop"),
    ],
)
async def test_the_empty_reply_warning_names_model_cap_reason_prompt_and_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    scripts: list[Script],
    code: str,
    sent: int,
    finish: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="tstd.loop"):
        await _run(tmp_path, scripts, _config())

    (record,) = [r for r in caplog.records if r.getMessage() == "turn failed: empty completion"]
    fields = record.extra_fields  # type: ignore[attr-defined]
    assert fields["model"] == "test-brain"
    assert fields["max_tokens"] == sent
    assert fields["finish_reason"] == finish
    assert fields["error_code"] == code
    assert isinstance(fields["prompt_tokens"], int) and fields["prompt_tokens"] > 0


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (Script(kind="stream", reasoning="Hmm", finish_reason="length"), "output_cap_thinking"),
        (Script(kind="stream", reasoning="Hmm"), "empty_completion"),
    ],
)
async def test_a_near_full_context_adds_the_new_chat_note(
    tmp_path: Path, script: Script, code: str
) -> None:
    # A single turn cannot be compacted, so the prompt stays past the
    # budget (0.8 of 7000) and leaves no room for a larger cap.
    session, turn, mock = await _run(
        tmp_path, [script], _config(cap=1_000, window=8_000), message="hi " + "word " * 4_600
    )

    assert (turn.failed, turn.error_code) == (True, code)
    assert len(_brain_calls(mock)) == 1
    assert _last_assistant(session).endswith(" " + _CONTEXT_NOTE)


async def test_the_note_is_left_out_when_the_context_is_not_near_full(tmp_path: Path) -> None:
    session, _, _ = await _run(
        tmp_path, [Script(kind="stream", reasoning="Hmm")], _config(cap=1_000, window=8_000)
    )
    assert _last_assistant(session) == _NO_ANSWER


# ── The larger cap sticks for the rest of the call ──────────────────────

_IDLE = TimeoutConfig(stream_idle=0.2)


def _thinking(text: str) -> bytes:
    return _event({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": text}}]})


def _thought_to_cap(text: str = "Weighing it.") -> bytes:
    return _thinking(text) + _text("", finish="length") + _DONE


async def _stalls_after(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk
    await asyncio.sleep(5)


async def _run_gateway(
    tmp_path: Path, gateway: _BodyGateway, config: ModelConfig
) -> tuple[Session, TurnComplete]:
    session = Session(str(tmp_path))
    client: ProviderClient = gateway.client(retries=2, timeout=_IDLE)

    async def factory(*_args: object) -> ProviderClient:
        return client

    runner = SessionRunner(
        session, loop_factory=lambda s: agent_loop(s, TierRouter(), factory, config)
    )
    await runner.start()
    try:
        await session.add_user_message("Which option?")
        turn = await wait_for_turn(session, 1)
    finally:
        await runner.cancel()
        await client.close()
    return session, turn


async def test_a_stall_after_the_cap_retry_keeps_the_larger_cap(tmp_path: Path) -> None:
    def reply(attempt: int) -> bytes | AsyncIterator[bytes]:
        if attempt == 1:
            return _thought_to_cap()
        if attempt == 2:
            return _stalls_after(_thinking("Now the second"))
        return _answer(_ANSWER)

    gateway = _BodyGateway(reply)
    session, turn = await _run_gateway(tmp_path, gateway, _config())

    assert not turn.failed
    assert [body["max_tokens"] for body in gateway.bodies] == [_CAP, _RAISED, _RAISED]
    assert [r.reason for r in _of(session, AssistantRetract)] == [
        "output_cap_thinking",
        "stream_stalled",
    ]
    assert _shown(session) == _ANSWER


async def test_the_larger_cap_composes_with_the_guards_thinking_off_retry(
    tmp_path: Path,
) -> None:
    def reply(attempt: int) -> bytes | AsyncIterator[bytes]:
        if attempt == 1:
            return _thought_to_cap()
        if attempt == 2:
            return _thinking(_varied("Thinking about it.", 60)) + _DONE
        return _answer(_ANSWER)

    guard = ReplyGuardConfig(
        max_reasoning_chars=400,
        retry_extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    gateway = _BodyGateway(reply)
    _, turn = await _run_gateway(tmp_path, gateway, _config(guard=guard))

    assert not turn.failed
    assert [body["max_tokens"] for body in gateway.bodies] == [_CAP, _RAISED, _RAISED]
    assert "chat_template_kwargs" not in gateway.bodies[1]
    assert gateway.bodies[2]["chat_template_kwargs"] == {"enable_thinking": False}


# ── stream_reply on its own ─────────────────────────────────────────────


class _Log:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.events: list[DaemonEvent] = []

    async def add(self, event: DaemonEvent) -> None:
        self.events.append(event)
        if isinstance(event, AssistantRetract) and self._session.stop_on_retract:
            self._session.stop_turn_requested = True


class _FakeSession:
    """What stream_reply reads of a session, with Stop pressed on demand."""

    id = "s"

    def __init__(self, *, stop_on_retract: bool = False) -> None:
        self.stop_on_retract = stop_on_retract
        self.cancel_requested = False
        self.stop_turn_requested = False
        self.event_log = _Log(self)

    async def wait_for_turn_abort(self) -> None:
        await asyncio.sleep(10)


def _result(**fields: Any) -> StreamResult:
    base: dict[str, Any] = {
        "content": "",
        "tool_calls": {},
        "failed": False,
        "error_msg": "",
        "error_code": None,
        "raw_detail": "",
        "finish_reason": "length",
        "reasoning_details": None,
        "began": True,
        "max_tokens": _CAP,
    }
    base.update(fields)
    return StreamResult(**base)


async def _stream(
    session: _FakeSession, results: list[StreamResult], *, cap_retries: int = 1
) -> tuple[StreamResult, list[str]]:
    ids: list[str] = []

    async def attempt(response_id: str) -> StreamResult:
        ids.append(response_id)
        return results[len(ids) - 1]._replace(response_id=response_id)

    result = await stream_reply(
        attempt,
        session,  # type: ignore[arg-type]
        ProviderRetryConfig(max_retries=3, initial_delay=0.001, max_delay=0.001),
        cap_retries=cap_retries,
        raised_cap=_RAISED,
    )
    return result, ids


async def test_stop_as_the_reply_is_withdrawn_keeps_nothing() -> None:
    session = _FakeSession(stop_on_retract=True)
    result, ids = await _stream(session, [_result(), _result(content=_ANSWER)])

    assert len(ids) == 1
    assert (result.failed, result.error_msg, result.error_code) == (True, "stopped", None)
    assert result.content == ""


async def test_the_first_reply_comes_back_when_the_larger_cap_is_refused() -> None:
    session = _FakeSession()
    first = _result(reasoning="Weighing it.")
    refused = _result(failed=True, began=False, error_code="insufficient_credits")
    result, ids = await _stream(session, [first, refused])

    assert len(ids) == 2
    assert result == first._replace(response_id=ids[0])


async def test_a_refusal_after_a_later_stall_retry_is_returned_as_itself() -> None:
    """Only the larger request's refusal falls back to the first reply (TD-5233)."""
    session = _FakeSession()
    stalled = _result(failed=True, error_code="stream_stalled", max_tokens=_RAISED)
    refused = _result(failed=True, began=False, error_code="auth_failed", max_tokens=_RAISED)
    result, ids = await _stream(session, [_result(), stalled, refused])

    assert len(ids) == 3
    assert (result.failed, result.error_code) == (True, "auth_failed")


async def test_without_an_allowance_the_reply_is_returned() -> None:
    session = _FakeSession()
    result, ids = await _stream(session, [_result()], cap_retries=0)

    assert len(ids) == 1
    assert thinking_ate_cap(result)
    assert not session.event_log.events


# ── Unit ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, True),
        ({"content": " \n\t"}, True),
        ({"content": "An answer"}, False),
        ({"tool_calls": {0: {"id": "c", "name": "fs_read", "arguments": "{}"}}}, False),
        ({"failed": True, "error_code": "stream_stalled"}, False),
        ({"finish_reason": "stop"}, False),
        ({"finish_reason": None}, False),
    ],
)
def test_thinking_ate_cap(fields: dict[str, Any], expected: bool) -> None:
    assert thinking_ate_cap(_result(**fields)) is expected


@pytest.mark.parametrize(
    ("cap", "window", "prompt", "expected"),
    [
        # The incident: 4096 at 28k of 256k.
        (4_096, 262_144, 28_000, 16_384),
        # The ceiling, below four times the cap.
        (12_288, 262_144, 1_000, 32_768),
        (16_384, 262_144, 1_000, 32_768),
        # The room, after a 1024 margin on a small window...
        (4_096, 16_000, 2_000, 12_976),
        # ...and a twentieth of a large one.
        (4_096, 100_000, 80_000, 15_000),
        # Exactly double is still worth asking.
        (4_096, 10_216, 1_000, 8_192),
        # Less than double is not.
        (4_096, 11_000, 2_000, None),
        (4_096, 8_000, 9_000, None),
        # A cap at or past the ceiling has nowhere to go.
        (32_768, 1_000_000, 0, None),
        (65_536, 1_000_000, 0, None),
        (20_000, 1_000_000, 0, None),
    ],
)
def test_raised_output_cap(cap: int, window: int, prompt: int, expected: int | None) -> None:
    assert raised_output_cap(cap, window, prompt) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("worker", "vllm", 0), "the worker tier (presets.vllm.worker)"),
        (("brain", "mine", 1), "the brain tier's backup (presets.mine.brain.fallbacks[0])"),
        (("brain", "mine", 2), "the brain tier's backup (presets.mine.brain.fallbacks[1])"),
    ],
)
def test_cap_setting(args: tuple[str, str, int], expected: str) -> None:
    assert cap_setting(*args) == expected
