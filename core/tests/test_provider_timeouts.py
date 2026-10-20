"""Provider timeouts per tier and the stream-idle watchdog (TD-5106).

A mock endpoint streams, stalls, or only sends SSE keep-alive comments.
The timeouts are scaled to fractions of a second; the behaviour is the
one a hung local model server shows over minutes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from tstd.config import TierConfig
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    TimeoutConfig,
    complete_streamed,
)

_FAST_RETRY = RetryConfig(max_retries=2, initial_delay=0.01, max_delay=0.01)


def _chunk(text: str, *, finish: str | None = None) -> bytes:
    data = {
        "id": "c1",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(data)}\n\n".encode()


Script = Callable[[int], AsyncIterator[bytes]]


def _endpoint(script: Script, attempts: list[int]) -> httpx.MockTransport:
    """Each request streams ``script(attempt_number)``."""

    async def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(len(attempts) + 1)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=script(len(attempts)),
        )

    return httpx.MockTransport(handle)


def _client(transport: httpx.MockTransport, timeout: TimeoutConfig) -> ProviderClient:
    return ProviderClient(
        base_url="http://127.0.0.1:9/v1",
        api_key=None,
        timeout=timeout,
        client=httpx.AsyncClient(transport=transport),
        retry_config=_FAST_RETRY,
    )


def _request() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="local", messages=[ChatMessage(role="user", content="hi")])


async def _collect(client: ProviderClient) -> list[StreamChunk | ProviderError]:
    return [item async for item in client.chat_completion_stream(_request())]


def _text(items: list[StreamChunk | ProviderError]) -> str:
    return "".join(i.delta.content or "" for i in items if isinstance(i, StreamChunk))


# ── watchdog ─────────────────────────────────────────────────────────────


class TestStreamIdleWatchdog:
    async def test_stall_before_output_is_retried(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            if attempt == 1:
                await asyncio.sleep(5)  # accepted, then silent
            yield _chunk("hello")
            yield _chunk("", finish="stop")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=0.2))
        items = await _collect(client)
        await client.close()
        assert attempts == [1, 2]
        assert not any(isinstance(i, ProviderError) for i in items)
        assert _text(items) == "hello"

    async def test_keepalive_pings_do_not_count_as_output(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            for _ in range(100):  # five seconds of ": ping" and nothing else
                yield b": ping\n\n"
                await asyncio.sleep(0.05)

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=0.3))
        loop = asyncio.get_running_loop()
        started = loop.time()
        items = await _collect(client)
        elapsed = loop.time() - started
        await client.close()
        (error,) = items
        assert isinstance(error, ProviderError)
        assert error.code == "stream_stalled"
        assert "stream_idle_timeout" in error.message
        assert "after accepting the request" in error.message
        assert len(attempts) == 3  # first try plus provider_retry's two
        assert elapsed < 3.0

    async def test_stall_after_output_is_not_retried(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            yield _chunk("partial ")
            await asyncio.sleep(5)
            yield _chunk("never")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=0.2))
        items = await _collect(client)
        await client.close()
        assert attempts == [1]
        assert _text(items) == "partial "
        error = items[-1]
        assert isinstance(error, ProviderError)
        assert error.code == "stream_stalled" and not error.retryable
        assert "partway through the reply" in error.message

    async def test_an_off_turn_stall_is_returned_not_resent(self) -> None:
        """A stall is a timeout: the work check and memory distill, which
        never resend one (TD-5146), get it after one wait, not three."""

        async def script(attempt: int) -> AsyncIterator[bytes]:
            await asyncio.sleep(5)  # accepted, then silent
            yield _chunk("never")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=0.2))
        result = await complete_streamed(client, replace(_request(), retry_on_timeout=False))
        await client.close()
        assert attempts == [1]
        assert isinstance(result, ProviderError)
        assert result.code == "stream_stalled"

    async def test_a_steady_stream_outlives_the_idle_timeout(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            for i in range(12):
                await asyncio.sleep(0.05)
                yield _chunk(str(i % 10))
            yield _chunk("", finish="stop")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=0.2))
        items = await _collect(client)
        await client.close()
        assert not any(isinstance(i, ProviderError) for i in items)
        assert _text(items) == "012345678901"

    async def test_watchdog_off_waits_for_the_socket_read_timeout(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            yield b": ping\n\n"
            await asyncio.sleep(0.4)
            yield _chunk("late")
            yield _chunk("", finish="stop")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=None, read=5.0))
        items = await _collect(client)
        await client.close()
        assert _text(items) == "late"


# ── a failed attempt hangs up at once ───────────────────────────────────


class _Body(httpx.AsyncByteStream):
    """A response body that logs when the client hangs up on it."""

    def __init__(self, chunks: AsyncIterator[bytes], log: list[str], attempt: int) -> None:
        self._chunks = chunks
        self._log = log
        self._attempt = attempt

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self._log.append(f"hang up {self._attempt}")


def _logged_endpoint(script: Script, log: list[str]) -> httpx.MockTransport:
    async def handle(request: httpx.Request) -> httpx.Response:
        attempt = sum(entry.startswith("request") for entry in log) + 1
        log.append(f"request {attempt}")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Body(script(attempt), log, attempt),
        )

    return httpx.MockTransport(handle)


class TestFailedAttemptsHangUp:
    """TD-5154: an attempt that ends in an error is closed when its error is
    handed on, not later when the garbage collector reaches it.

    The stall error is yielded from inside the response's ``async with``,
    so an attempt nobody closed kept its connection open, and the server
    kept generating, until the asyncgen finalizer ran. That cleanup closes
    the nested generators in no fixed order, which is what made
    ``test_an_off_turn_stall_is_returned_not_resent`` fail once.
    """

    async def test_a_returned_stall_is_hung_up_before_the_caller_resumes(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            await asyncio.sleep(5)
            yield _chunk("never")

        log: list[str] = []
        client = _client(_logged_endpoint(script, log), TimeoutConfig(stream_idle=0.2))
        result = await complete_streamed(client, replace(_request(), retry_on_timeout=False))
        assert log == ["request 1", "hang up 1"]
        await client.close()
        assert isinstance(result, ProviderError) and result.code == "stream_stalled"

    async def test_a_stalled_attempt_is_hung_up_before_the_retry(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            if attempt == 1:
                await asyncio.sleep(5)
            yield _chunk("hello")
            yield _chunk("", finish="stop")

        log: list[str] = []
        client = _client(_logged_endpoint(script, log), TimeoutConfig(stream_idle=0.2))
        items = await _collect(client)
        assert log == ["request 1", "hang up 1", "request 2", "hang up 2"]
        await client.close()
        assert _text(items) == "hello"

    async def test_a_consumer_that_stops_early_hangs_up(self) -> None:
        """The loop ends a turn mid-stream (Stop, a tool call it refuses)."""

        async def script(attempt: int) -> AsyncIterator[bytes]:
            for i in range(100):
                yield _chunk(str(i % 10))
                await asyncio.sleep(0.01)

        log: list[str] = []
        client = _client(_logged_endpoint(script, log), TimeoutConfig(stream_idle=1.0))
        stream = client.chat_completion_stream(_request())
        first = await anext(stream)
        assert isinstance(first, StreamChunk)
        await stream.aclose()  # type: ignore[attr-defined]
        assert log == ["request 1", "hang up 1"]
        await client.close()

    async def test_the_turn_loop_hangs_up_when_it_stops_reading(self, tmp_path: Path) -> None:
        """The loop's own reader closes the stream it stops reading (TD-5158).

        It breaks out of ``async for`` on Stop or a provider error and never
        called ``aclose``, so the hang-up waited for the async-generator
        finalizer, a task scheduled after the reader had returned.
        """
        from tests.test_dispatch import make_config
        from tstd.cost import CostTracker
        from tstd.loop import _stream_and_parse
        from tstd.session import Session

        async def script(attempt: int) -> AsyncIterator[bytes]:
            for i in range(100):
                yield _chunk(str(i % 10))
                await asyncio.sleep(0.01)

        log: list[str] = []
        client = _client(_logged_endpoint(script, log), TimeoutConfig(stream_idle=1.0))
        config = make_config()
        session = Session(str(tmp_path))
        session._stop_turn_event.set()  # Stop: the reader breaks at the first chunk
        result = await _stream_and_parse(
            client,
            "local",
            [ChatMessage(role="user", content="hi")],
            session,
            None,
            CostTracker(config),
            "brain",
            config.tier("brain"),
        )
        # No await between the reader returning and this check: the
        # finalizer's task has not had a turn yet.
        assert log == ["request 1", "hang up 1"]
        assert result[2] is True and result[3] == "stopped"
        await client.close()


# ── total_timeout ────────────────────────────────────────────────────────


class TestTotalTimeout:
    async def test_total_caps_a_stream_that_never_ends(self) -> None:
        async def script(attempt: int) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.05)
                yield _chunk("x")

        attempts: list[int] = []
        client = _client(_endpoint(script, attempts), TimeoutConfig(stream_idle=1.0, total=0.4))
        items = await _collect(client)
        await client.close()
        error = items[-1]
        assert isinstance(error, ProviderError)
        assert error.code == "timeout" and not error.retryable
        assert "total_timeout (0.4 s)" in error.message
        assert attempts == [1]

    async def test_total_caps_a_non_streaming_request(self) -> None:
        calls: list[int] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            await asyncio.sleep(5)
            return httpx.Response(200, json={})

        client = ProviderClient(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            timeout=TimeoutConfig(total=0.2),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            retry_config=RetryConfig(max_retries=1, initial_delay=0.01, max_delay=0.01),
        )
        result = await client.chat_completion(_request())
        await client.close()
        assert isinstance(result, ProviderError)
        assert result.code == "timeout"
        assert "total_timeout" in result.message
        assert len(calls) == 2  # retried once under provider_retry

    async def test_no_total_by_default(self) -> None:
        async def handle(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.3)
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "local",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        client = ProviderClient(
            base_url="http://127.0.0.1:9/v1",
            api_key=None,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        result = await client.chat_completion(_request())
        await client.close()
        assert isinstance(result, ChatCompletionResponse)


# ── config ───────────────────────────────────────────────────────────────


def _tier(**extra: Any) -> TierConfig:
    return TierConfig(
        slug="m",
        base_url="http://127.0.0.1:8080/v1",
        input_price=0,
        output_price=0,
        cache_read_price=0,
        context_window=8192,
        max_output_tokens=256,
        **extra,
    )


class TestTierKeys:
    def test_defaults_are_the_old_fixed_values(self) -> None:
        timeouts = _tier().timeouts()
        old = TimeoutConfig()
        assert (timeouts.connect, timeouts.read) == (old.connect, old.read) == (30.0, 120.0)
        assert timeouts.total is None
        assert timeouts.stream_idle == 300.0
        httpx_timeout = timeouts.to_httpx_timeout()
        assert httpx_timeout.pool == 180.0  # the old "total" was this

    def test_keys_reach_the_client(self) -> None:
        tier = _tier(
            connect_timeout=5,
            read_timeout=900,
            total_timeout=1800,
            stream_idle_timeout=600,
        )
        client = ProviderClient(base_url=tier.base_url, api_key=None, timeout=tier.timeouts())
        assert client._client.timeout.connect == 5
        assert client._client.timeout.read == 900
        assert client.timeout.total == 1800
        assert client.timeout.stream_idle == 600

    def test_null_turns_the_watchdog_off(self) -> None:
        assert _tier(stream_idle_timeout=None).timeouts().stream_idle is None

    @pytest.mark.parametrize("key", ["connect_timeout", "read_timeout", "total_timeout"])
    def test_keys_must_be_positive(self, key: str) -> None:
        with pytest.raises(ValueError):
            _tier(**{key: 0})
