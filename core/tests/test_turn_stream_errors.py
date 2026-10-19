"""A cut-off reply is an error, not a finished answer (TD-5152).

LiteLLM reports a failure partway through a streamed completion as an
in-band ``data: {"error": {...}}`` event and then ends the body without
``data: [DONE]``. The chunk parser read that event as nothing, so the
turn stream ended cleanly and the loop kept the partial text as the
answer. TD-5146 fixed this for the off-turn calls only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.test_dispatch import attach_auto_approver, make_config, wait_for_turn
from tstd.loop import agent_loop
from tstd.protocol import AssistantDelta, TurnComplete
from tstd.provider import (
    ChatCompletionRequest,
    ChatMessage,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    TimeoutConfig,
    complete_streamed,
)
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.tools import ToolDispatcher, ToolRegistry

_BASE = "http://gateway.test/v1"
_SSE = {"content-type": "text/event-stream"}
_DONE = b"data: [DONE]\n\n"


def _event(data: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(data).encode() + b"\n\n"


def _text(text: str, *, finish: str | None = None) -> bytes:
    return _event(
        {
            "id": "c1",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
        }
    )


def _litellm_error(message: str, code: str) -> bytes:
    """The shape LiteLLM's proxy writes when the upstream call fails mid-stream."""
    return _event({"error": {"message": message, "type": None, "param": None, "code": code}})


def _answer(text: str) -> bytes:
    return _text(text) + _text("", finish="stop") + _DONE


Reply = Callable[[int], bytes | AsyncIterator[bytes]]


class _Gateway:
    """An httpx transport that streams ``reply(attempt)`` and counts requests."""

    def __init__(self, reply: Reply) -> None:
        self._reply = reply
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        del request
        self.requests += 1
        return httpx.Response(200, headers=_SSE, content=self._reply(self.requests))

    def client(self, *, retries: int = 2, timeout: TimeoutConfig | None = None) -> ProviderClient:
        return ProviderClient(
            _BASE,
            "test-key",
            timeout=timeout,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            retry_config=RetryConfig(
                max_retries=retries, initial_delay=0.001, max_delay=0.001, jitter_factor=0.0
            ),
        )


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="forge", messages=[ChatMessage(role="user", content="hi")], stream=True
    )


async def _stream(client: ProviderClient) -> list[StreamChunk | ProviderError]:
    try:
        return [item async for item in client.chat_completion_stream(_request())]
    finally:
        await client.close()


def _shown(items: list[StreamChunk | ProviderError]) -> str:
    return "".join(i.delta.content or "" for i in items if isinstance(i, StreamChunk))


def _errors(items: list[StreamChunk | ProviderError]) -> list[ProviderError]:
    return [i for i in items if isinstance(i, ProviderError)]


# ── the turn stream ──────────────────────────────────────────────────────


class TestInBandErrorEvent:
    async def test_after_output_it_is_stream_interrupted(self) -> None:
        gateway = _Gateway(
            lambda _: _text("The answer is ") + _litellm_error("upstream failed", "500")
        )
        items = await _stream(gateway.client(retries=3))

        # Before TD-5152: one chunk and no error, so the turn kept
        # "The answer is " as a finished reply.
        assert _shown(items) == "The answer is "
        (error,) = _errors(items)
        assert items[-1] is error
        assert error.code == "stream_interrupted"
        assert error.retryable is False
        assert error.status_code == 500
        assert "upstream failed" in error.message
        assert "partial reply was discarded" in error.message
        assert gateway.requests == 1

    async def test_after_a_tool_call_starts_it_is_stream_interrupted(self) -> None:
        call = _event(
            {
                "id": "c1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "fs_write", "arguments": '{"pa'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        gateway = _Gateway(lambda _: call + _litellm_error("upstream failed", "502"))
        items = await _stream(gateway.client())

        (error,) = _errors(items)
        assert error.code == "stream_interrupted"
        assert gateway.requests == 1

    @pytest.mark.parametrize(
        ("code", "mapped"),
        [("429", "rate_limited"), ("500", "server_error"), ("503", "service_unavailable")],
    )
    async def test_before_output_a_busy_error_keeps_its_retry_rule(
        self, code: str, mapped: str
    ) -> None:
        gateway = _Gateway(lambda _: _litellm_error("busy", code))
        items = await _stream(gateway.client(retries=2))

        # Before TD-5152 this was an empty stream, never the status it named.
        (error,) = items
        assert isinstance(error, ProviderError)
        assert error.code == mapped
        assert error.status_code == int(code)
        assert error.retryable is True
        assert gateway.requests == 3  # first try plus provider_retry's two

    async def test_before_output_a_busy_error_is_retried_into_an_answer(self) -> None:
        gateway = _Gateway(
            lambda attempt: _litellm_error("busy", "503") if attempt == 1 else _answer("hello")
        )
        items = await _stream(gateway.client(retries=2))

        assert not _errors(items)
        assert _shown(items) == "hello"
        assert gateway.requests == 2

    @pytest.mark.parametrize(
        ("code", "mapped"),
        [(400, "bad_request"), (401, "auth_failed"), (404, "not_found")],
    )
    async def test_before_output_a_client_error_is_not_retried(
        self, code: int, mapped: str
    ) -> None:
        body = _event({"error": {"message": "no", "code": code}})
        gateway = _Gateway(lambda _: body)
        items = await _stream(gateway.client(retries=3))

        (error,) = items
        assert isinstance(error, ProviderError)
        assert error.code == mapped
        assert error.retryable is False
        # Before TD-5152: an empty stream, retried three more times.
        assert gateway.requests == 1

    async def test_a_window_refusal_is_context_overflow(self) -> None:
        body = _event(
            {
                "error": {
                    "message": "This model's maximum context length is 8192 tokens.",
                    "code": 400,
                }
            }
        )
        gateway = _Gateway(lambda _: body)
        (error,) = await _stream(gateway.client())

        assert isinstance(error, ProviderError)
        assert error.code == "context_overflow"

    async def test_an_error_without_a_status_is_a_bad_gateway(self) -> None:
        gateway = _Gateway(lambda _: _event({"error": "boom"}))
        items = await _stream(gateway.client(retries=0))

        (error,) = items
        assert isinstance(error, ProviderError)
        assert error.code == "bad_gateway"
        assert error.retryable is True

    async def test_an_error_with_no_message_does_not_show_raw_json(self) -> None:
        gateway = _Gateway(lambda _: _text("half") + _event({"error": {"code": "500"}}))
        (error,) = _errors(await _stream(gateway.client()))

        assert error.code == "stream_interrupted"
        assert "{" not in error.message
        assert '"code"' in error.detail


class TestStreamEnd:
    async def test_no_finish_reason_and_no_done_is_interrupted(self) -> None:
        gateway = _Gateway(lambda _: _text("The answer is ") + _text("forty"))
        items = await _stream(gateway.client(retries=3))

        # Before TD-5152 the stream ended cleanly and the turn kept the text.
        assert _shown(items) == "The answer is forty"
        (error,) = _errors(items)
        assert items[-1] is error
        assert error.code == "stream_interrupted"
        assert error.retryable is False
        assert gateway.requests == 1

    async def test_done_without_a_finish_reason_is_a_finished_reply(self) -> None:
        """A server that leaves ``finish_reason`` off still says ``[DONE]``."""
        gateway = _Gateway(lambda _: _text("hello") + _DONE)
        items = await _stream(gateway.client())

        assert not _errors(items)
        assert _shown(items) == "hello"

    async def test_a_finish_reason_without_done_is_a_finished_reply(self) -> None:
        gateway = _Gateway(lambda _: _text("hello") + _text("", finish="stop"))
        items = await _stream(gateway.client())

        assert not _errors(items)
        assert _shown(items) == "hello"

    async def test_usage_after_the_finish_reason_still_arrives(self) -> None:
        usage = _event(
            {
                "id": "c1",
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )
        gateway = _Gateway(lambda _: _text("hi") + _text("", finish="stop") + usage + _DONE)
        items = await _stream(gateway.client())

        assert not _errors(items)
        assert any(isinstance(i, StreamChunk) and i.usage is not None for i in items)


class TestIdleWatchdog:
    """TD-5106's watchdog and the end-of-reply signals."""

    async def test_a_body_held_open_after_done_is_not_a_stall(self) -> None:
        async def held_open(attempt: int) -> AsyncIterator[bytes]:
            del attempt
            yield _answer("hello")
            await asyncio.sleep(5)

        gateway = _Gateway(held_open)
        loop = asyncio.get_running_loop()
        started = loop.time()
        items = await _stream(gateway.client(timeout=TimeoutConfig(stream_idle=0.2)))

        # Before TD-5152 the stream read on past [DONE] and the watchdog
        # failed a finished reply as stream_stalled.
        assert not _errors(items)
        assert _shown(items) == "hello"
        assert loop.time() - started < 1.0

    async def test_a_stall_partway_is_still_a_stall(self) -> None:
        async def stalls(attempt: int) -> AsyncIterator[bytes]:
            del attempt
            yield _text("partial ")
            await asyncio.sleep(5)

        gateway = _Gateway(stalls)
        items = await _stream(gateway.client(timeout=TimeoutConfig(stream_idle=0.2)))

        (error,) = _errors(items)
        assert error.code == "stream_stalled"
        assert gateway.requests == 1

    async def test_an_error_event_does_not_count_as_output(self) -> None:
        """An error before any chunk is retried as an error, not waited on."""

        async def errors_then_answers(attempt: int) -> AsyncIterator[bytes]:
            if attempt == 1:
                yield _litellm_error("busy", "503")
                await asyncio.sleep(5)
            yield _answer("hello")

        gateway = _Gateway(errors_then_answers)
        loop = asyncio.get_running_loop()
        started = loop.time()
        items = await _stream(gateway.client(timeout=TimeoutConfig(stream_idle=2.0)))

        assert not _errors(items)
        assert _shown(items) == "hello"
        assert gateway.requests == 2
        assert loop.time() - started < 1.0


class TestOffTurnCalls:
    async def test_an_in_band_busy_error_is_retried(self) -> None:
        gateway = _Gateway(
            lambda attempt: _litellm_error("busy", "503") if attempt == 1 else _answer("ok")
        )
        client = gateway.client(retries=2)
        try:
            result = await complete_streamed(client, _request())
        finally:
            await client.close()

        assert not isinstance(result, ProviderError)
        assert result.message.content == "ok"
        assert gateway.requests == 2


# ── the agent loop ───────────────────────────────────────────────────────


async def _run_turns(
    tmp_path: Path, gateway: _Gateway, *messages: str
) -> tuple[Session, list[TurnComplete]]:
    session = Session(str(tmp_path))
    dispatcher = attach_auto_approver(ToolDispatcher(ToolRegistry()))
    client = gateway.client()

    async def factory(*_args: object) -> ProviderClient:
        return client

    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s, TierRouter(), factory, make_config(), tool_dispatcher=dispatcher
        ),
    )
    await runner.start()
    turns = []
    try:
        for n, text in enumerate(messages, start=1):
            await session.add_user_message(text)
            turns.append(await wait_for_turn(session, n))
    finally:
        await runner.cancel()
        await client.close()
    return session, turns


def _assistant_messages(session: Session) -> list[str]:
    return [str(m.content) for m in session.conversation if m.role == "assistant"]


class TestTurn:
    async def test_an_error_event_fails_the_turn(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda _: _text("The fix is to delete ") + _litellm_error("upstream failed", "500")
        )
        session, (turn,) = await _run_turns(tmp_path, gateway, "fix it")

        assert turn.failed is True
        assert turn.error_code == "stream_interrupted"
        # The cut-off text is not kept as the model's answer.
        assert _assistant_messages(session) == [
            "I encountered an error: The provider reported an error partway through "
            "the reply (upstream failed). The partial reply was discarded. Resend to retry."
        ]
        shown = "".join(
            e.delta for e in session.event_log.all_events if isinstance(e, AssistantDelta)
        )
        assert "I encountered an error" in shown
        assert gateway.requests == 1

    async def test_a_body_that_just_ends_fails_the_turn(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda _: _text("The fix is to delete "))
        session, (turn,) = await _run_turns(tmp_path, gateway, "fix it")

        assert turn.failed is True
        assert turn.error_code == "stream_interrupted"
        assert all("The fix is" not in m for m in _assistant_messages(session))

    async def test_resending_after_a_cut_off_reply_works(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda attempt: _text("half") if attempt == 1 else _answer("The whole answer.")
        )
        session, (first, second) = await _run_turns(tmp_path, gateway, "fix it", "fix it")

        assert (first.failed, first.error_code) == (True, "stream_interrupted")
        assert (second.failed, second.error_code) == (False, None)
        assert _assistant_messages(session)[-1] == "The whole answer."
