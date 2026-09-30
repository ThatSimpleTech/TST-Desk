"""Shutdown step timing and the distill reap (TD-4844)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tstd.provider import ChatCompletionRequest
from tstd.shutdown_phases import (
    ProviderFlight,
    ShutdownPhases,
    SignalingProvider,
    reap_shutdown_distill,
)


async def test_a_finished_phase_over_a_second_is_named(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = {"t": 0.0}

    async def work() -> None:
        now["t"] = 1.25

    phases = ShutdownPhases(clock=lambda: now["t"])
    with caplog.at_level(logging.INFO, logger="tstd.shutdown"):
        await phases.run("audit", work())
        phases.log_if_slow()

    named = [
        record for record in caplog.records if record.getMessage() == "shutdown phase took longest"
    ]
    assert len(named) == 1
    assert named[0].extra_fields["phase"] == "audit"
    assert named[0].extra_fields["seconds"] == 1.25


async def test_a_short_phase_is_not_named(caplog: pytest.LogCaptureFixture) -> None:
    now = {"t": 0.0}

    async def work() -> None:
        now["t"] = 0.2

    phases = ShutdownPhases(clock=lambda: now["t"])
    with caplog.at_level(logging.INFO, logger="tstd.shutdown"):
        await phases.run("ws_stop", work())
        phases.log_if_slow()

    assert caplog.records == []


async def test_a_phase_still_running_is_named(caplog: pytest.LogCaptureFixture) -> None:
    release = asyncio.Event()
    named = asyncio.Event()
    now = {"t": 10.0}

    async def work() -> None:
        await release.wait()

    async def crossed(_seconds: float) -> None:
        now["t"] = 11.4

    phases = ShutdownPhases(
        clock=lambda: now["t"],
        sleep=crossed,
        on_slow=lambda _name, _seconds: named.set(),
    )
    with caplog.at_level(logging.INFO, logger="tstd.shutdown"):
        task = asyncio.create_task(phases.run("distill", work()))
        await named.wait()
        release.set()
        await task

    in_flight = [
        record
        for record in caplog.records
        if record.getMessage() == "shutdown phase took longest"
        and record.extra_fields["phase"] == "distill"
        and record.extra_fields["seconds"] == 1.4
    ]
    assert in_flight


async def test_reap_keeps_a_distill_that_finished() -> None:
    ran = asyncio.Event()

    async def finish() -> None:
        ran.set()

    task = asyncio.create_task(finish())
    await asyncio.wait({task})
    await reap_shutdown_distill(task)
    assert ran.is_set()
    assert not task.cancelled()


async def test_reap_cancels_a_distill_still_on_the_provider() -> None:
    started = asyncio.Event()

    async def hang() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(hang())
    await started.wait()
    await reap_shutdown_distill(task)
    assert task.cancelled()


async def test_reap_does_not_swallow_a_distill_failure() -> None:
    async def boom() -> None:
        raise RuntimeError("distill broke")

    task = asyncio.create_task(boom())
    await asyncio.wait({task})
    with pytest.raises(RuntimeError, match="distill broke"):
        await reap_shutdown_distill(task)


async def test_reap_waits_out_local_prep_for_a_ready_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subsystems can finish before the memory read. A ready provider still proposes."""
    release = asyncio.Event()
    finished = asyncio.Event()
    flight = ProviderFlight()

    async def distill() -> None:
        await release.wait()
        fut: asyncio.Future[None] = asyncio.ensure_future(_ready())
        flight.begin(fut)
        try:
            await fut
        finally:
            flight.end()
        finished.set()

    async def _ready() -> None:
        return None

    task = asyncio.create_task(distill())
    await asyncio.sleep(0)
    assert not task.done()
    reap_task = asyncio.create_task(reap_shutdown_distill(task, flight))
    await asyncio.sleep(0)
    assert not reap_task.done()
    release.set()
    with caplog.at_level(logging.INFO, logger="tstd.shutdown"):
        await reap_task
    assert finished.is_set()
    assert not task.cancelled()
    assert not any(
        record.getMessage() == "distill skipped; provider call still in flight"
        for record in caplog.records
    )


async def test_reap_cancels_a_provider_call_that_does_not_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    flight = ProviderFlight()
    request = ChatCompletionRequest(model="test-worker", messages=[])

    class _Hang:
        async def chat_completion(self, request: ChatCompletionRequest) -> None:
            del request
            await asyncio.Event().wait()

    async def distill() -> None:
        await SignalingProvider(_Hang(), flight).chat_completion(request)  # type: ignore[arg-type]

    task = asyncio.create_task(distill())
    await asyncio.sleep(0)
    assert flight.pending is not None
    with caplog.at_level(logging.INFO, logger="tstd.shutdown"):
        await asyncio.wait_for(reap_shutdown_distill(task, flight), 1)
    assert task.cancelled()
    assert any(
        record.getMessage() == "distill skipped; provider call still in flight"
        for record in caplog.records
    )
