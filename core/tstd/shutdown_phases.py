"""Time the steps of a graceful shutdown (TD-4844).

The shutdown budget ends a step that never returns. It does not say
which step. A step still running after a second is logged while it is
in flight, so the next hang is a line in the log instead of a missing
one. Logging is not a deadline: the step is not abandoned here.

Quit distill is the step that used to sit in a provider read. The reap
below keeps a call that can finish in-process and cancels one that is
actually waiting on the network (TD-4851).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .logging import get_logger
from .memory_distill import DistillProvider
from .provider import ChatCompletionRequest, ChatCompletionResponse, ProviderError

log = get_logger("tstd.shutdown")

# Long enough that a quiet close does not chatter. Short of the shutdown
# budget, so a stuck step is named before that budget expires.
SLOW_PHASE_SECONDS = 1.0


class ShutdownPhases:
    """Record how long each shutdown step took."""

    def __init__(
        self,
        *,
        slow_after: float = SLOW_PHASE_SECONDS,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_slow: Callable[[str, float], None] | None = None,
    ) -> None:
        self._slow_after = slow_after
        self._clock = clock
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._on_slow = on_slow
        self._elapsed: list[tuple[str, float]] = []

    async def run(self, name: str, awaitable: Awaitable[object]) -> None:
        """Await *awaitable* and remember its duration under *name*."""
        started = self._clock()

        async def _watch() -> None:
            await self._sleep(self._slow_after)
            self._announce(name, self._clock() - started)

        watcher = asyncio.create_task(_watch(), name=f"shutdown-phase-{name}")
        try:
            await awaitable
        finally:
            if not watcher.done():
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            self._elapsed.append((name, self._clock() - started))

    def log_if_slow(self) -> None:
        """Name the longest finished step when it exceeded a second."""
        if not self._elapsed:
            return
        name, seconds = max(self._elapsed, key=lambda item: item[1])
        if seconds <= self._slow_after:
            return
        self._announce(name, seconds)

    def _announce(self, name: str, seconds: float) -> None:
        log.info(
            "shutdown phase took longest",
            extra={"extra_fields": {"phase": name, "seconds": round(seconds, 3)}},
        )
        if self._on_slow is not None:
            self._on_slow(name, seconds)


class ProviderFlight:
    """The quit-distill provider call, once ``chat_completion`` has started.

    Empty while distill is reading memory files or writing the proposal.
    Those are local. The network read is the only part shutdown refuses
    to wait out (TD-4844, TD-4851). ``generation`` moves on every begin
    and end so a waiter blocks on an event instead of polling.
    """

    def __init__(self) -> None:
        self.pending: asyncio.Future[Any] | None = None
        self._generation = 0
        self._event = asyncio.Event()

    @property
    def generation(self) -> int:
        return self._generation

    def begin(self, fut: asyncio.Future[Any]) -> None:
        self.pending = fut
        self._generation += 1
        self._event.set()

    def end(self) -> None:
        self.pending = None
        self._generation += 1
        self._event.set()

    async def wait_for_change(self, generation: int) -> None:
        while self._generation == generation:
            # Clear, then re-check, before waiting. ``begin`` and ``end``
            # run on this loop, so nothing can move ``generation`` between
            # the check and the ``await``.
            self._event.clear()
            if self._generation != generation:
                return
            await self._event.wait()


class SignalingProvider:
    """Publish the provider future before awaiting it.

    The reaper has to see the future while it is still running. Awaiting
    ``chat_completion`` directly hides that future inside this task, and
    on 3.11 cancelling this task does not cancel a nested one. The
    future is created first so one loop turn can finish an in-process
    provider, and so a network read can be cancelled on its own.
    """

    def __init__(self, inner: DistillProvider, flight: ProviderFlight) -> None:
        self._inner = inner
        self._flight = flight

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        fut = asyncio.ensure_future(self._inner.chat_completion(request))
        self._flight.begin(fut)
        try:
            return await fut
        finally:
            self._flight.end()


async def reap_shutdown_distill(
    task: asyncio.Task[None],
    flight: ProviderFlight | None = None,
) -> None:
    """Keep a distill that can finish. Drop one that is still on the network.

    Quit distill is best-effort (TD-2302). A provider read may sit for
    minutes, and awaiting that read burned the shutdown budget after a
    finished turn (TD-4844). With no *flight*, a task that has not
    finished is cancelled immediately — the unit-test shape, and any
    caller that cannot tell a memory read from a socket read.

    With a *flight*, local prep and an in-process provider are awaited.
    A future still pending after one loop turn is the socket read, and
    it is cancelled. A stuck disk read can still hold this wait; the
    process shutdown budget remains the backstop for that.
    """
    if flight is None:
        await _reap_unsignaled(task)
        return
    await _reap_signaled(task, flight)


async def _reap_unsignaled(task: asyncio.Task[None]) -> None:
    if task.done():
        await task
        return
    await _cancel_inflight(task, None)


async def _reap_signaled(task: asyncio.Task[None], flight: ProviderFlight) -> None:
    while not task.done():
        pending = flight.pending
        if pending is None:
            await _wait_until_progress(task, flight)
            continue
        if not pending.done():
            # One turn. ``MockProvider.chat_completion`` has no ``await``,
            # so the task scheduled by ``ensure_future`` finishes here.
            # An httpx read, or a hang parked on an ``Event``, does not.
            await asyncio.sleep(0)
        if task.done():
            break
        current = flight.pending
        if current is None or current.done():
            # Let distill record the result and either finish or open
            # the next session's call. Awaiting the task itself would
            # sit on that next call if it is the network.
            await asyncio.sleep(0)
            continue
        await _cancel_inflight(task, current)
        return
    await task


async def _wait_until_progress(task: asyncio.Task[None], flight: ProviderFlight) -> None:
    changed = asyncio.create_task(
        flight.wait_for_change(flight.generation),
        name="shutdown-distill-flight",
    )
    try:
        await asyncio.wait({task, changed}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not changed.done():
            changed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await changed


async def _cancel_inflight(
    task: asyncio.Task[None],
    pending: asyncio.Future[Any] | None,
) -> None:
    log.info("distill skipped; provider call still in flight")
    # 3.11 does not cancel a nested task when its parent is cancelled.
    if pending is not None and not pending.done():
        pending.cancel()
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if pending is None:
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        # Retrieve it. A cancelled parent does not always consume the
        # child, and an unread exception fails the suite (TD-4846).
        await pending
