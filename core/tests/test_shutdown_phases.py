"""Shutdown step timing and the distill reap (TD-4844)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tstd.shutdown_phases import ShutdownPhases, reap_shutdown_distill


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
