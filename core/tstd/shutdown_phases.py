"""Time the steps of a graceful shutdown (TD-4844).

The shutdown budget ends a step that never returns. It does not say
which step. A step still running after a second is logged while it is
in flight, so the next hang is a line in the log instead of a missing
one. Logging is not a deadline: the step is not abandoned here.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from .logging import get_logger

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


async def reap_shutdown_distill(task: asyncio.Task[None]) -> None:
    """Keep a distill that already finished. Do not wait out one that has not.

    Quit distill is best-effort (TD-2302): a provider that cannot
    complete is logged and skipped, and quit still reaps. The provider
    read is allowed to sit for minutes. Awaiting that read here is what
    burned the shutdown budget after a finished turn. Cancelling the
    task fails the read; ``_run_distill`` already logs a provider
    failure, and a cancel is this line.
    """
    if task.done():
        await task
        return
    log.info("distill skipped; provider call still in flight")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
