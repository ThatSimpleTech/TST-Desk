"""A long local run survives a server restart and a slow prompt read (TD-5183).

Two ways a multi-hour turn on a local llama.cpp server used to die:

- The server restarted mid-reply (it lent its second GPU to another job).
  The stream ended inside a tool call, and the turn failed as
  ``stream_interrupted``. Now a reply the endpoint cut off is withdrawn and
  asked again, up to twice, the way a stall is (TD-5159).
- Back on one GPU, the server read a cold 40k-token prompt for ten minutes
  without a byte, past ``stream_idle_timeout``, and every retry started the
  read again. Now llama.cpp's ``prompt_progress`` events, sent when a tier
  asks for ``return_progress``, count as signs of life and never reach the
  window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.test_dispatch import make_registry_and_dispatcher
from tests.test_stall_retry import _IDLE, _of, _run, _shown, _tool_call
from tests.test_turn_stream_errors import (
    _answer,
    _drops_after,
    _event,
    _Gateway,
    _litellm_error,
    _stream,
    _text,
)
from tstd.protocol import AssistantRetract, ToolCall
from tstd.provider import ProviderError, StreamChunk
from tstd.session import Session


def _progress(processed: int, total: int = 40000) -> bytes:
    """One llama.cpp progress event, as a ``return_progress`` request gets."""
    return _event(
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "finish_reason": None, "delta": {"role": "assistant", "content": None}}
            ],
            "prompt_progress": {
                "total": total,
                "cache": 0,
                "processed": processed,
                "time_ms": processed // 40,
            },
        }
    )


async def _reads_slowly(steps: int, gap: float, then: bytes) -> AsyncIterator[bytes]:
    """A server that reads the prompt for ``steps * gap`` seconds, then answers."""
    for n in range(1, steps + 1):
        await asyncio.sleep(gap)
        yield _progress(n * 2048)
    yield then


async def _silent_for(seconds: float, then: bytes) -> AsyncIterator[bytes]:
    await asyncio.sleep(seconds)
    yield then


def _half_tool_call() -> bytes:
    """A tool call that starts streaming and never finishes."""
    call = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "fs_write", "arguments": '{"path": "src/App.tsx", "content": "imp'},
    }
    return _event({"id": "c1", "choices": [{"index": 0, "delta": {"tool_calls": [call]}}]})


class TestASlowPromptRead:
    async def test_progress_keeps_a_read_longer_than_the_idle_timeout_alive(self) -> None:
        # 0.6 s of reading against a 0.2 s idle timeout, a beat every 0.1 s.
        gateway = _Gateway(lambda _: _reads_slowly(6, 0.1, _answer("Read it.")))
        items = await _stream(gateway.client(retries=2, timeout=_IDLE))

        assert not [i for i in items if isinstance(i, ProviderError)]
        assert gateway.requests == 1
        assert "".join(i.delta.content or "" for i in items if isinstance(i, StreamChunk)) == (
            "Read it."
        )

    async def test_progress_never_reaches_the_reply(self) -> None:
        plain = await _stream(_Gateway(lambda _: _answer("Read it.")).client(timeout=_IDLE))
        with_progress = await _stream(
            _Gateway(lambda _: _reads_slowly(3, 0.01, _answer("Read it."))).client(timeout=_IDLE)
        )

        assert len(with_progress) == len(plain)

    async def test_silence_that_long_is_still_a_stall(self) -> None:
        gateway = _Gateway(lambda _: _silent_for(0.6, _answer("Read it.")))
        items = await _stream(gateway.client(retries=0, timeout=_IDLE))

        (error,) = [i for i in items if isinstance(i, ProviderError)]
        assert error.code == "stream_stalled"


class TestACutOffReply:
    async def test_a_tool_call_cut_off_by_a_restart_is_asked_again(self, tmp_path: Path) -> None:
        """The run's failure: the stream ended inside a tool call."""
        registry, dispatcher = make_registry_and_dispatcher()
        runs: list[str] = []

        async def echo(
            session: Session, message: str, count: int = 1, tool_call_id: str = ""
        ) -> str:
            del session, count, tool_call_id
            runs.append(message)
            return f"Echo: {message}"

        dispatcher.register_handler("echo", echo)

        def reply(attempt: int) -> bytes:
            if attempt == 1:
                return _text("Writing it. ") + _half_tool_call()
            if attempt == 2:
                return _text("Writing it. ") + _tool_call("hi")
            return _answer("Done.")

        gateway = _Gateway(reply)
        session, turn = await _run(tmp_path, gateway, dispatcher=dispatcher, registry=registry)

        assert (turn.failed, turn.error_code) == (False, None)
        assert gateway.requests == 3
        # The half call never ran; the whole one ran once.
        assert runs == ["hi"]
        assert len(_of(session, ToolCall)) == 1
        (retract,) = _of(session, AssistantRetract)
        assert retract.reason == "stream_interrupted"
        assert _shown(session) == "Writing it. Done."

    async def test_a_dropped_connection_is_asked_again(self, tmp_path: Path) -> None:
        gateway = _Gateway(
            lambda attempt: (
                _drops_after(_text("The fix is ")) if attempt == 1 else _answer("Fixed.")
            )
        )
        session, turn = await _run(tmp_path, gateway)

        assert not turn.failed
        assert gateway.requests == 2
        assert _shown(session) == "Fixed."

    @pytest.mark.parametrize("status", ["500", "503", "429"])
    async def test_an_endpoint_error_event_is_asked_again(
        self, tmp_path: Path, status: str
    ) -> None:
        gateway = _Gateway(
            lambda attempt: (
                _text("Half ") + _litellm_error("upstream failed", status)
                if attempt == 1
                else _answer("Whole.")
            )
        )
        session, turn = await _run(tmp_path, gateway)

        assert not turn.failed
        assert gateway.requests == 2
        assert _shown(session) == "Whole."

    async def test_a_request_error_event_is_not(self, tmp_path: Path) -> None:
        """A 4xx partway is the request's fault: asking again repeats it."""
        gateway = _Gateway(lambda _: _text("Half ") + _litellm_error("bad request", "400"))
        session, turn = await _run(tmp_path, gateway)

        assert (turn.failed, turn.error_code) == (True, "stream_interrupted")
        assert gateway.requests == 1
        assert not _of(session, AssistantRetract)

    async def test_max_retries_zero_turns_it_off(self, tmp_path: Path) -> None:
        gateway = _Gateway(lambda _: _text("half"))
        session, turn = await _run(tmp_path, gateway, retries=0)

        assert (turn.failed, turn.error_code) == (True, "stream_interrupted")
        assert gateway.requests == 1
        assert not _of(session, AssistantRetract)


class TestTheFlag:
    @pytest.mark.parametrize(
        ("script", "resendable"),
        [
            (lambda: _drops_after(_text("half ")), True),
            (lambda: _text("half ") + _half_tool_call(), True),
            (lambda: _text("half "), True),
            (lambda: _text("half ") + _litellm_error("upstream failed", "502"), True),
            (lambda: _text("half ") + _litellm_error("bad request", "400"), False),
        ],
        ids=["dropped", "mid-tool-call", "no-finish", "inband-502", "inband-400"],
    )
    async def test_which_cut_offs_are_resendable(self, script, resendable: bool) -> None:  # type: ignore[no-untyped-def]
        items = await _stream(_Gateway(lambda _: script()).client(retries=0))

        (error,) = [i for i in items if isinstance(i, ProviderError)]
        assert error.code == "stream_interrupted"
        assert error.resendable is resendable
