"""Stop ends a turn at once, even while the provider waits or retries (ERR2A-05).

Stop was checked only between chunks. A request that never answered, or a
retry's sleep, yielded none, so on default settings a stopped turn ran on
for 8 min 8 s and the cancelled request was sent three more times.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from tests.test_loop import make_config, start_loop, wait_for_turn
from tstd.mock import MockProvider, Script
from tstd.provider import (
    ChatCompletionRequest,
    ProviderError,
    RetryConfig,
    StreamChunk,
    retry_stream,
)
from tstd.router import TierRouter
from tstd.session import Session


class HangingProvider(MockProvider):
    """The first brain call never answers; later calls are the script's."""

    def __init__(self) -> None:
        super().__init__(sequences={"test-brain": [Script(kind="stream", content="second turn")]})
        self.hung = 0
        self.cancelled = asyncio.Event()

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        if request.model == "test-brain" and self.hung == 0:
            self.hung += 1
            self.calls.append(request)
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return
        async for item in super().chat_completion_stream(request):
            yield item


class RetryingProvider(MockProvider):
    """A brain call that fails retryably and waits a minute before trying again."""

    def __init__(self) -> None:
        super().__init__(sequences={"test-brain": [Script(kind="stream", content="unused")]})
        self.opened = 0

    async def chat_completion_stream(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[StreamChunk | ProviderError]:
        if request.model != "test-brain":
            async for item in super().chat_completion_stream(request):
                yield item
            return

        async def attempt() -> AsyncIterator[StreamChunk | ProviderError]:
            self.opened += 1
            yield ProviderError(
                code="rate_limited", message="slow down", status_code=429, retryable=True
            )

        config = RetryConfig(max_retries=5, initial_delay=60.0, max_delay=60.0)
        async for item in retry_stream(attempt, config):
            yield item


async def _stop_after_start(session: Session, started: asyncio.Event | None = None) -> float:
    await asyncio.sleep(0.3)
    begun = time.monotonic()
    await session.stop_turn()
    return begun


async def test_stop_ends_a_hung_request_at_once() -> None:
    session = Session("/tmp/ws")
    provider = HangingProvider()
    runner = await start_loop(session, TierRouter(lead_turns=80), provider, make_config())
    try:
        await session.add_user_message("first question")
        begun = await _stop_after_start(session)
        first = await asyncio.wait_for(wait_for_turn(session, 1), 5)
        assert time.monotonic() - begun < 2.0
        assert first.error_code == "stopped"
        await asyncio.wait_for(provider.cancelled.wait(), 2)  # the request itself was cut
        await session.add_user_message("next")
        second = await asyncio.wait_for(wait_for_turn(session, 2), 5)
        assert second.failed is False
    finally:
        await runner.cancel()


async def test_stop_during_a_retry_wait_sends_nothing_more() -> None:
    session = Session("/tmp/ws")
    provider = RetryingProvider()
    runner = await start_loop(session, TierRouter(lead_turns=80), provider, make_config())
    try:
        await session.add_user_message("first question")
        begun = await _stop_after_start(session)
        first = await asyncio.wait_for(wait_for_turn(session, 1), 5)
        assert time.monotonic() - begun < 2.0
        assert first.error_code == "stopped"
        await asyncio.sleep(0.3)
        assert provider.opened == 1  # the request was not sent again
    finally:
        await runner.cancel()


async def test_cancel_ends_a_hung_request_too() -> None:
    session = Session("/tmp/ws")
    provider = HangingProvider()
    runner = await start_loop(session, TierRouter(lead_turns=80), provider, make_config())
    try:
        await session.add_user_message("first question")
        await asyncio.sleep(0.3)
        await session.cancel()
        await asyncio.wait_for(provider.cancelled.wait(), 3)
    finally:
        await runner.cancel()
