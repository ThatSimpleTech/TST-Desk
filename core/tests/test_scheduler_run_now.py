"""Run now (TD-3809): a manual fire that does not move the schedule."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.scheduler.models import Job
from tstd.scheduler.runner import (
    InFlight,
    RecordingDeliver,
    TurnResult,
    begin_manual_run,
    run_due_jobs,
    run_manual_job,
)
from tstd.scheduler.store import get_job, list_jobs, save_job

_NEXT = "2026-08-21T12:00:00+00:00"
_LATER = "2026-08-21T16:00:00+00:00"
_SLOT = "2026-08-21T12:45:00+00:00"


def _now() -> datetime:
    return datetime(2026, 8, 21, 15, 0, tzinfo=UTC)


def _job(
    workspace: Path,
    *,
    job_id: str = "inbox",
    cadence: str | None = "every 1 hour",
    next_run: str | None = _NEXT,
    deliver_to: str = "window",
    paused: bool = False,
    timezone: str | None = None,
) -> Job:
    return Job(
        id=job_id,
        workspace=str(workspace),
        instruction="summarize the inbox",
        cadence=cadence,
        next_run=next_run,
        deliver_to=deliver_to,  # type: ignore[arg-type]
        paused=paused,
        timezone=timezone,
    )


def _daemon(tmp_path: Path) -> tuple[Daemon, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    daemon = Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content="digest ready")),
    )
    daemon.config = make_config()
    return daemon, data_dir, workspace


async def _send(daemon: Daemon, job_id: str) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps({"type": "run_job", "job_id": job_id}), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _hold(daemon: Daemon) -> tuple[asyncio.Event, asyncio.Event]:
    """Block the scheduled turn until the test opens the gate."""
    gate = asyncio.Event()
    entered = asyncio.Event()
    original = daemon._scheduled_run_turn

    async def held(workspace: Path, message: str) -> TurnResult:
        entered.set()
        await gate.wait()
        return await original(workspace, message)

    daemon._scheduled_run_turn = held  # type: ignore[method-assign]
    return gate, entered


async def _ok(_workspace: Path, _message: str) -> str:
    return "done"


def test_claim_is_exclusive() -> None:
    guard = InFlight()
    assert guard.claim("inbox") is True
    assert guard.claim("inbox") is False
    assert "inbox" in guard
    guard.release("inbox")
    assert "inbox" not in guard
    guard.release("inbox")
    assert guard.claim("inbox") is True


async def test_begin_manual_run_names_the_refusal(tmp_path: Path) -> None:
    guard = InFlight()
    assert await begin_manual_run(tmp_path, "missing", guard) == "job_not_found"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _job(workspace))
    started = await begin_manual_run(tmp_path, "inbox", guard)
    assert isinstance(started, Job)
    assert await begin_manual_run(tmp_path, "inbox", guard) == "job_running"


@pytest.mark.parametrize(
    ("paused", "cadence", "next_run", "deliver_to", "timezone"),
    [
        (False, "every 1 hour", _NEXT, "window", None),
        (True, "every 1 hour", _NEXT, "slack", None),
        (False, None, _SLOT, "ntfy", None),
        (False, "45 7 * * 1-5", _SLOT, "window", "America/Chicago"),
    ],
)
async def test_manual_run_stamps_a_receipt_without_moving_the_schedule(
    tmp_path: Path,
    paused: bool,
    cadence: str | None,
    next_run: str,
    deliver_to: str,
    timezone: str | None,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(
        workspace,
        paused=paused,
        cadence=cadence,
        next_run=next_run,
        deliver_to=deliver_to,
        timezone=timezone,
    )
    save_job(tmp_path, job)
    deliver = RecordingDeliver()
    await run_manual_job(tmp_path, job, _now(), run_turn=_ok, deliver=deliver)
    stored = list_jobs(tmp_path)[0]
    assert stored.paused is paused
    assert stored.next_run == next_run
    assert stored.cadence == job.cadence
    assert stored.timezone == timezone
    assert stored.last_status == "ok"
    assert stored.last_summary == "done"
    assert stored.last_run == "2026-08-21T15:00:00+00:00"
    assert deliver.records == [(deliver_to, "done")]


async def test_a_failed_manual_run_still_leaves_the_slot(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(workspace)
    save_job(tmp_path, job)

    async def explode(_workspace: Path, _message: str) -> str:
        raise RuntimeError("disk full")

    await run_manual_job(tmp_path, job, _now(), run_turn=explode, deliver=RecordingDeliver())
    stored = list_jobs(tmp_path)[0]
    assert stored.last_status == "failed"
    assert stored.last_summary is not None and "disk full" in stored.last_summary
    assert stored.next_run == _NEXT
    assert stored.paused is False


async def test_a_pause_during_the_turn_is_not_undone(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _job(workspace))
    moved = "2026-09-01T00:00:00+00:00"

    async def turn(_workspace: Path, _message: str) -> str:
        current = get_job(tmp_path, "inbox")
        assert current is not None
        save_job(tmp_path, current.model_copy(update={"paused": True, "next_run": moved}))
        return "kept"

    await run_manual_job(
        tmp_path, _job(workspace), _now(), run_turn=turn, deliver=RecordingDeliver()
    )
    stored = list_jobs(tmp_path)[0]
    assert stored.paused is True
    assert stored.next_run == moved
    assert stored.last_summary == "kept"


async def test_the_tick_skips_an_in_flight_job_and_fires_it_later(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _job(workspace))
    guard = InFlight()
    calls: list[str] = []

    async def fake(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "once"

    assert guard.claim("inbox")
    deliver = RecordingDeliver()
    assert (
        await run_due_jobs(tmp_path, _now(), run_turn=fake, deliver=deliver, in_flight=guard) == []
    )
    assert calls == []
    assert deliver.records == []
    stored = list_jobs(tmp_path)[0]
    assert stored.next_run == _NEXT
    assert stored.last_run is None
    guard.release("inbox")
    assert await run_due_jobs(
        tmp_path, _now(), run_turn=fake, deliver=deliver, in_flight=guard
    ) == ["inbox"]
    assert calls == ["summarize the inbox"]
    assert list_jobs(tmp_path)[0].next_run == _LATER


async def test_unknown_job_is_job_not_found(tmp_path: Path) -> None:
    daemon, data_dir, _workspace = _daemon(tmp_path)
    try:
        err = await _send(daemon, "missing")
        assert err["type"] == "error"
        assert err["code"] == "job_not_found"
        assert daemon._tasks == []
        assert list_jobs(data_dir) == []
    finally:
        await daemon._shutdown()


async def test_paused_job_can_run_without_unpausing(tmp_path: Path) -> None:
    daemon, data_dir, workspace = _daemon(tmp_path)
    save_job(data_dir, _job(workspace, paused=True, deliver_to="slack"))
    try:
        body = await _send(daemon, "inbox")
        assert body["type"] == "job_list"
        await asyncio.wait_for(daemon._tasks[-1], timeout=10)
        stored = list_jobs(data_dir)[0]
        assert stored.paused is True
        assert stored.next_run == _NEXT
        assert stored.last_status == "ok"
        assert stored.last_summary == "digest ready"
        assert stored.last_session_id
        assert daemon._scheduler_deliver.records == [("slack", "digest ready")]
        # Still paused, so the overdue slot does not fire on the tick.
        assert await daemon.run_due_jobs(_now()) == []
        assert list_jobs(data_dir)[0].next_run == _NEXT
    finally:
        await daemon._shutdown()


async def test_run_now_replies_before_the_turn_and_the_tick_leaves_the_slot(
    tmp_path: Path,
) -> None:
    daemon, data_dir, workspace = _daemon(tmp_path)
    save_job(data_dir, _job(workspace))
    gate, entered = _hold(daemon)
    sent: list[str] = []

    async def capture(payload: str) -> int:
        sent.append(payload)
        return 1

    daemon.ws_server.broadcast = capture  # type: ignore[method-assign]
    try:
        reply = asyncio.create_task(_send(daemon, "inbox"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        body = await asyncio.wait_for(reply, timeout=2)
        assert body["type"] == "job_list"
        row = body["jobs"][0]
        assert row["running"] is True
        assert row["next_run"] == _NEXT
        assert list_jobs(data_dir)[0].last_run is None
        task = daemon._tasks[-1]
        assert not task.done()

        again = await _send(daemon, "inbox")
        assert again["code"] == "job_running"

        skipped = await asyncio.wait_for(daemon.run_due_jobs(_now()), timeout=2)
        assert skipped == []
        assert list_jobs(data_dir)[0].next_run == _NEXT
        assert list_jobs(data_dir)[0].last_run is None

        sent.clear()
        gate.set()
        await asyncio.wait_for(task, timeout=10)
        stored = list_jobs(data_dir)[0]
        assert stored.last_status == "ok"
        assert stored.last_summary == "digest ready"
        assert stored.last_session_id
        assert stored.next_run == _NEXT
        assert stored.paused is False
        assert daemon._scheduler_deliver.records == [("window", "digest ready")]
        pushed = [json.loads(item) for item in sent if json.loads(item).get("type") == "job_list"]
        assert pushed
        assert pushed[-1]["jobs"][0]["running"] is False
        assert pushed[-1]["jobs"][0]["last_summary"] == "digest ready"
        assert pushed[-1]["jobs"][0]["next_run"] == _NEXT

        assert await daemon.run_due_jobs(_now()) == ["inbox"]
        assert list_jobs(data_dir)[0].next_run == _LATER
    finally:
        gate.set()
        pending = [task for task in daemon._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await daemon._shutdown()


async def test_run_now_while_the_tick_holds_the_job_is_refused(tmp_path: Path) -> None:
    daemon, data_dir, workspace = _daemon(tmp_path)
    save_job(data_dir, _job(workspace))
    gate, entered = _hold(daemon)
    tick = asyncio.create_task(daemon.run_due_jobs(_now()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        err = await _send(daemon, "inbox")
        assert err["code"] == "job_running"
        assert list_jobs(data_dir)[0].last_run is None
        assert list_jobs(data_dir)[0].next_run == _NEXT
        gate.set()
        assert await asyncio.wait_for(tick, timeout=10) == ["inbox"]
        assert list_jobs(data_dir)[0].next_run == _LATER
    finally:
        gate.set()
        await asyncio.gather(tick, return_exceptions=True)
        await daemon._shutdown()
