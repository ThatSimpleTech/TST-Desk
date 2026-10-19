"""Off-turn calls stream, think less, and do not resend a timeout (TD-5146).

The after-write work check and memory distill each made one non-streamed
call with the model's default thinking. Behind LiteLLM, a non-streamed
request whose client gives up keeps generating, and a timeout was resent
up to ``max_retries`` times. On 2026-10-14 one work check held a session
busy for about 20 minutes, and a forge slot with it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tstd.autonomy.verify import maybe_verify_after_turn, note_tool_result
from tstd.config import AutonomyConfig, ModelConfig, Preset, TierConfig
from tstd.cost import CostTracker
from tstd.memory_distill import DistillTurn, distill_session
from tstd.protocol import VerifyResult
from tstd.provider import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Delta,
    DeltaToolCall,
    ProviderClient,
    ProviderError,
    RetryConfig,
    StreamChunk,
    Usage,
    collect_stream,
    complete_streamed,
)
from tstd.session import Session

# Each tier's own value, so a test can tell which tier's body was sent.
_BRAIN = {"chat_template_kwargs": {"reasoning_effort": "medium"}}
_WORKER = {"chat_template_kwargs": {"reasoning_effort": "low"}}
_VALIDATOR = {"chat_template_kwargs": {"reasoning_effort": "low"}, "top_k": 7}
_BASE = "http://forge.test/v1"


def _config() -> ModelConfig:
    def tier(slug: str, extra_body: dict[str, Any]) -> TierConfig:
        return TierConfig(
            slug=slug,
            base_url=_BASE,
            input_price=1.0,
            output_price=2.0,
            cache_read_price=0.5,
            context_window=100_000,
            max_output_tokens=1_000,
            extra_body=extra_body,
        )

    return ModelConfig(
        presets={
            "test": Preset(
                brain=tier("test-brain", _BRAIN),
                worker=tier("test-worker", _WORKER),
                validator=tier("test-validator", _VALIDATOR),
            )
        },
        active_preset="test",
        autonomy=AutonomyConfig(verify="after_write"),
    )


def _sse(*events: dict[str, Any]) -> bytes:
    lines = [b"data: " + json.dumps(e).encode() + b"\n\n" for e in events]
    return b"".join(lines) + b"data: [DONE]\n\n"


def _answer(text: str) -> httpx.Response:
    body = _sse(
        {
            "id": "c1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "reasoning_content": "Look at the diff."},
                    "finish_reason": None,
                }
            ],
        },
        {"id": "c1", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {
            "id": "c1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57},
        },
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


class _Wire:
    """An httpx transport that records each request body and replies in turn."""

    def __init__(self, *replies: Callable[[httpx.Request], httpx.Response]) -> None:
        self._replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        reply = self._replies[min(len(self.bodies), len(self._replies)) - 1]
        return reply(request)

    def client(self, *, retries: int = 8) -> ProviderClient:
        return ProviderClient(
            _BASE,
            "test-key",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            retry_config=RetryConfig(
                max_retries=retries, initial_delay=0.001, max_delay=0.001, jitter_factor=0.0
            ),
        )


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _busy(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(503, json={"error": {"message": "busy"}})


def _says(text: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: _answer(text)


class _Assembler:
    async def assemble(self, name: str, **kwargs: object) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(text=f"You review a {name} diff.")


async def _verify(tmp_path: Path, provider: object) -> VerifyResult:
    session = Session(str(tmp_path))
    note_tool_result(session, "fs_write", "success", diff="--- a\n+++ b\n+hello\n")
    config = _config()

    async def client_for(tier: TierConfig) -> object:
        del tier
        return provider

    await maybe_verify_after_turn(
        session,
        config,
        CostTracker(config),
        client_for,  # type: ignore[arg-type]
        _Assembler(),  # type: ignore[arg-type]
    )
    results = [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]
    assert len(results) == 1
    return results[0]


class TestWorkCheck:
    async def test_streams_with_the_tier_extra_body(self, tmp_path: Path) -> None:
        wire = _Wire(_says("verdict: pass\nLooks fine."))
        result = await _verify(tmp_path, wire.client())

        assert len(wire.bodies) == 1
        body = wire.bodies[0]
        assert body["stream"] is True
        assert body["model"] == "test-validator"
        assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
        assert body["top_k"] == 7
        assert body["max_tokens"] == 1_000
        assert "retry_on_timeout" not in body
        assert result.verdict == "pass"
        assert "Looks fine" in result.summary
        assert "Look at the diff" not in result.summary
        assert result.cost > 0

    async def test_a_timeout_is_reported_not_resent(self, tmp_path: Path) -> None:
        wire = _Wire(_timeout)
        result = await _verify(tmp_path, wire.client(retries=8))

        # Before TD-5146: nine attempts, each a full read timeout long.
        assert len(wire.bodies) == 1
        assert result.verdict == "error"
        assert "timed out" in result.summary.lower()

    async def test_a_busy_server_is_still_retried(self, tmp_path: Path) -> None:
        wire = _Wire(_busy, _says("verdict: fail\nBroke the build."))
        result = await _verify(tmp_path, wire.client(retries=8))

        assert len(wire.bodies) == 2
        assert result.verdict == "fail"

    async def test_a_cut_off_stream_is_an_error_not_a_verdict(self, tmp_path: Path) -> None:
        """LiteLLM reports a failure mid-stream as an in-band error event."""

        def cut_off(request: httpx.Request) -> httpx.Response:
            del request
            body = (
                b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"verdict: pa"}}]}\n\n'
                b'data: {"error":{"message":"upstream failed","code":"500"}}\n\n'
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        wire = _Wire(cut_off)
        result = await _verify(tmp_path, wire.client(retries=8))

        assert len(wire.bodies) == 1
        assert result.verdict == "error"

    async def test_cancelling_the_check_hangs_up(self, tmp_path: Path) -> None:
        """Ending the session or quitting closes the response: the server's hang-up.

        Stop reaches the check too (TD-5151, tests/test_verify_stop.py).
        """
        opened = asyncio.Event()
        closed = asyncio.Event()

        class _Endless(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"verdict"}}]}\n\n'
                opened.set()
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                closed.set()

        def endless(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_Endless()
            )

        wire = _Wire(endless)
        task = asyncio.create_task(_verify(tmp_path, wire.client()))
        await asyncio.wait_for(opened.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wire.bodies[0]["stream"] is True
        assert closed.is_set()


class TestDistill:
    async def test_streams_with_the_worker_extra_body(self, tmp_path: Path) -> None:
        wire = _Wire(_says("[]"))
        turns = [DistillTurn("user", "Remember the port is 8009."), DistillTurn("assistant", "OK.")]
        await distill_session(tmp_path, turns, wire.client(), _config())

        assert len(wire.bodies) == 1
        body = wire.bodies[0]
        assert body["stream"] is True
        assert body["model"] == "test-worker"
        assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
        assert "top_k" not in body

    async def test_a_timeout_is_not_resent(self, tmp_path: Path) -> None:
        wire = _Wire(_timeout)
        turns = [DistillTurn("user", "hi"), DistillTurn("assistant", "hello")]
        proposal = await distill_session(tmp_path, turns, wire.client(retries=8), _config())

        assert proposal is None
        assert len(wire.bodies) == 1


class TestRetryPolicy:
    def test_the_flag_never_reaches_the_wire(self) -> None:
        request = ChatCompletionRequest(model="m", messages=[], retry_on_timeout=False)
        assert "retry_on_timeout" not in request.to_dict()

    async def test_turn_streams_still_retry_a_timeout(self) -> None:
        wire = _Wire(_timeout, _says("hi"))
        request = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="x")])
        result = await complete_streamed(wire.client(retries=3), request)

        assert len(wire.bodies) == 2
        assert isinstance(result, ChatCompletionResponse)
        assert result.message.content == "hi"

    async def test_non_streamed_calls_honour_the_flag(self) -> None:
        wire = _Wire(_timeout)
        request = ChatCompletionRequest(model="m", messages=[], retry_on_timeout=False)
        result = await wire.client(retries=8).chat_completion(request)

        assert isinstance(result, ProviderError)
        assert result.code == "timeout"
        assert len(wire.bodies) == 1


async def _items(*items: StreamChunk | ProviderError) -> AsyncIterator[StreamChunk | ProviderError]:
    for item in items:
        yield item


def _chunk(delta: Delta, *, finish: str | None = None, usage: Usage | None = None) -> StreamChunk:
    return StreamChunk(id="s1", delta=delta, finish_reason=finish, usage=usage)


class TestCollect:
    async def test_folds_content_tool_calls_and_usage(self) -> None:
        stream = _items(
            _chunk(Delta(reasoning="hmm")),
            _chunk(Delta(content="verdict: ")),
            _chunk(Delta(content="pass")),
            _chunk(
                Delta(
                    tool_calls=[
                        DeltaToolCall(
                            index=0, id="t1", function_name="f", function_arguments='{"a"'
                        )
                    ]
                )
            ),
            _chunk(Delta(tool_calls=[DeltaToolCall(index=0, function_arguments=":1}")])),
            _chunk(
                Delta(),
                finish="stop",
                usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            ),
        )
        result = await collect_stream(stream, model="m")

        assert isinstance(result, ChatCompletionResponse)
        assert result.id == "s1"
        assert result.model == "m"
        assert result.message.content == "verdict: pass"
        assert result.message.tool_calls is not None
        call = result.message.tool_calls[0]
        assert (call.id, call.function and call.function.name) == ("t1", "f")
        assert call.function is not None and call.function.arguments == '{"a":1}'
        assert result.finish_reason == "stop"
        assert result.usage is not None and result.usage.total_tokens == 5

    async def test_the_first_error_wins(self) -> None:
        error = ProviderError(code="stream_interrupted", message="cut", retryable=False)
        result = await collect_stream(_items(_chunk(Delta(content="half")), error), model="m")
        assert result is error

    async def test_an_empty_stream_is_an_error(self) -> None:
        result = await collect_stream(_items(), model="m")
        assert isinstance(result, ProviderError)
        assert result.code == "empty_stream"

    async def test_no_finish_reason_is_an_error(self) -> None:
        result = await collect_stream(_items(_chunk(Delta(content="verdict: pa"))), model="m")
        assert isinstance(result, ProviderError)
        assert result.code == "stream_interrupted"

    async def test_an_early_error_closes_the_stream(self) -> None:
        closed = False

        class _Provider:
            async def chat_completion_stream(
                self, request: ChatCompletionRequest
            ) -> AsyncIterator[StreamChunk | ProviderError]:
                nonlocal closed
                assert request.stream is True
                try:
                    yield ProviderError(code="rate_limited", message="slow down", retryable=True)
                    yield _chunk(Delta(content="never read"))
                finally:
                    closed = True

        request = ChatCompletionRequest(model="m", messages=[])
        result = await complete_streamed(_Provider(), request)
        assert isinstance(result, ProviderError)
        assert closed
