"""Park a scheduled run that is waiting on an approval card (TD-3815).

The runner returns when the session asks, records ``waiting``, and does
not open a retry. The same history line becomes ok or failed when the
user answers. A restart, a cancel, and the next slot while it is still
parked each have one outcome and do not leak the park.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import ApprovalRequest
from tstd.scheduler.edit import apply_job_edit
from tstd.scheduler.history import append_run, list_runs
from tstd.scheduler.models import Job
from tstd.scheduler.park import PARKED_MISS, UNANSWERED, park_run, revive_parked
from tstd.scheduler.runner import RecordingDeliver, TurnResult, run_due_jobs
from tstd.scheduler.store import get_job, list_jobs, save_job, transform_job
from tstd.session import Session

_NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_SLOT = "2026-08-21T07:45:00+00:00"
_AFTER = "2026-08-21T19:00:00+00:00"
_LATER = "2026-08-21T20:00:00+00:00"
_RETRY = "2026-08-21T18:10:00+00:00"
_TOOL = "Run `echo hi`"
_WAITING = f"7:45 AM job is waiting for your approval: {_TOOL}"


def _provider(*steps: str) -> MockProvider:
    scripts: list[Script] = []
    for step in steps:
        if step == "tool":
            scripts.append(
                Script(
                    kind="tool_call",
                    tool_name="shell",
                    tool_arguments='{"command": "echo hi"}',
                )
            )
        else:
            scripts.append(Script(kind="stream", content="all done"))
    return MockProvider(sequences={"test-brain": scripts})


def _job(workspace: Path, **over: object) -> Job:
    data: dict[str, object] = {
        "id": "inbox",
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": _SLOT,
        "deliver_to": "window",
        "retries": 2,
        "retry_delay": 600,
    }
    data.update(over)
    return Job.model_validate(data)


def _watchers(daemon: Daemon, job_id: str = "inbox") -> list[asyncio.Task[None]]:
    name = f"park:{job_id}"
    return [task for task in daemon._tasks if task.get_name() == name]


def _card(session: Session) -> ApprovalRequest:
    for event in reversed(session.event_log.all_events):
        if isinstance(event, ApprovalRequest):
            return event
    raise AssertionError("no approval card")


async def _fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    steps: tuple[str, ...] = ("tool", "stream"),
    budget: float = 15,
    **over: object,
) -> tuple[Daemon, Path, MockProvider, float]:
    monkeypatch.setattr("tstd.scheduler.runner._TURN_TIMEOUT_SECS", budget)
    provider = _provider(*steps)
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data, _job(workspace, **over))
    daemon = Daemon(data_dir=data, provider=provider)
    daemon.config = make_config()
    started = time.monotonic()
    try:
        await asyncio.wait_for(daemon.run_due_jobs(_NOW), timeout=20)
    except BaseException:
        await daemon._shutdown()
        raise
    return daemon, data, provider, time.monotonic() - started


def _stored(data: Path) -> Job:
    job = get_job(data, "inbox")
    assert job is not None
    return job


def _session(daemon: Daemon, data: Path) -> Session:
    job = _stored(data)
    assert job.last_session_id
    session = daemon.session_registry.get(job.last_session_id)
    assert isinstance(session, Session)
    return session


async def _answer(daemon: Daemon, session: Session, approved: bool) -> None:
    card = _card(session)
    assert session.resolve_approval(card.tool_call_id, approved)
    watchers = [task for task in _watchers(daemon) if not task.done()]
    assert len(watchers) == 1
    await asyncio.wait_for(watchers[0], timeout=10)


async def test_an_approval_parks_without_the_turn_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card is not a timeout, and a retry must not open another session."""
    daemon, data, provider, elapsed = await _fire(tmp_path, monkeypatch)
    try:
        assert elapsed < 8
        job = _stored(data)
        assert job.last_status == "waiting"
        assert job.last_summary == _TOOL
        assert job.last_session_id
        assert job.parked_session_id == job.last_session_id
        assert job.parked_started_at == job.last_run
        assert job.attempt == 0
        assert job.resume_at is None
        assert job.next_run == _AFTER
        assert job.next_run != _RETRY
        session = _session(daemon, data)
        assert session.state == "awaiting_approval"
        assert _card(session).summary == _TOOL
        assert len(_watchers(daemon)) == 1
        assert daemon._scheduler_deliver.records == [("window", _WAITING)]
        runs = list_runs(data, "inbox")
        assert len(runs) == 1
        assert runs[0].status == "waiting"
        assert runs[0].summary == _TOOL
        assert runs[0].session_id == job.last_session_id
        assert runs[0].scheduled_for == _SLOT
        assert runs[0].trigger == "schedule"
        assert runs[0].attempt is None
        assert runs[0].attempts is None
        calls = len(provider.calls)
        await _answer(daemon, session, True)
        done = _stored(data)
        assert done.last_status == "ok"
        assert done.last_summary == "all done"
        assert done.parked_session_id is None
        assert done.parked_started_at is None
        assert done.next_run == _AFTER
        assert done.attempt == 0
        assert done.last_run == job.last_run
        settled = list_runs(data, "inbox")
        assert len(settled) == 1
        assert settled[0].status == "ok"
        assert settled[0].summary == "all done"
        assert settled[0].started_at == runs[0].started_at
        assert daemon._scheduler_deliver.records == [("window", _WAITING), ("window", "all done")]
        assert len(provider.calls) > calls
    finally:
        await daemon._shutdown()


async def test_a_denial_fails_the_parked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, data, _provider, _elapsed = await _fire(tmp_path, monkeypatch)
    try:
        await _answer(daemon, _session(daemon, data), False)
        job = _stored(data)
        assert job.last_status == "failed"
        assert job.last_summary == "Denied by user"
        assert job.parked_session_id is None
        assert job.attempt == 0
        assert job.resume_at is None
        runs = list_runs(data, "inbox")
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].summary == "Denied by user"
        assert "all done" not in (job.last_summary or "")
        assert daemon._scheduler_deliver.records[-1] == ("window", "Denied by user")
    finally:
        await daemon._shutdown()


async def test_cancelling_the_session_is_an_unanswered_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, data, _provider, _elapsed = await _fire(tmp_path, monkeypatch)
    try:
        await _session(daemon, data).cancel()
        await asyncio.wait_for(_watchers(daemon)[0], timeout=10)
        job = _stored(data)
        assert job.last_status == "failed"
        assert job.last_summary == UNANSWERED
        assert job.parked_session_id is None
        assert job.parked_started_at is None
        assert list_runs(data, "inbox")[0].summary == UNANSWERED
        assert daemon._scheduler_deliver.records[-1] == ("window", UNANSWERED)
    finally:
        await daemon._shutdown()


async def test_a_restart_closes_a_park_the_watcher_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the watch is process shutdown. The next start still closes it."""
    daemon, data, _provider, _elapsed = await _fire(tmp_path, monkeypatch)
    daemon2: Daemon | None = None
    try:
        watcher = _watchers(daemon)[0]
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        parked = _stored(data)
        assert parked.last_status == "waiting"
        assert parked.parked_session_id
        assert daemon._scheduler_deliver.records == [("window", _WAITING)]
        quiet = MockProvider()
        daemon2 = Daemon(data_dir=data, provider=quiet)
        daemon2.config = make_config()
        daemon2._shutdown_event.set()
        await daemon2._scheduler_loop()
        closed = _stored(data)
        assert closed.last_status == "failed"
        assert closed.last_summary == UNANSWERED
        assert closed.parked_session_id is None
        assert closed.parked_started_at is None
        assert closed.next_run == _AFTER
        assert closed.last_run == parked.last_run
        runs = list_runs(data, "inbox")
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].summary == UNANSWERED
        assert runs[0].started_at == parked.last_run
        assert quiet.calls == []
        assert await daemon2.session_registry.list_sessions() == []
        assert daemon2._scheduler_deliver.records == [("window", UNANSWERED)]
        assert daemon._scheduler_deliver.records == [("window", _WAITING)]
    finally:
        if daemon2 is not None:
            await daemon2._shutdown()
        await daemon._shutdown()


async def test_the_next_slot_is_missed_while_the_previous_run_is_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, data, provider, _elapsed = await _fire(tmp_path, monkeypatch)
    try:
        calls = len(provider.calls)
        when = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)
        assert await daemon.run_due_jobs(when) == ["inbox"]
        assert len(provider.calls) == calls
        missed = _stored(data)
        assert missed.last_status == "waiting"
        assert missed.last_summary == _TOOL
        assert missed.parked_session_id
        assert missed.next_run == _LATER
        runs = list_runs(data, "inbox")
        assert [run.status for run in runs] == ["missed", "waiting"]
        assert runs[0].summary == PARKED_MISS
        assert runs[0].session_id is None
        assert runs[0].scheduled_for == _AFTER
        assert runs[0].trigger == "schedule"
        assert "Skipped" not in (runs[0].summary or "")
        assert daemon._scheduler_deliver.records[-1] == ("window", PARKED_MISS)
        await _answer(daemon, _session(daemon, data), True)
        done = _stored(data)
        assert done.last_status == "ok"
        assert done.last_summary == "all done"
        assert done.parked_session_id is None
        assert done.next_run == _LATER
        settled = list_runs(data, "inbox")
        assert [run.status for run in settled] == ["missed", "ok"]
        assert settled[0].summary == PARKED_MISS
        assert settled[1].summary == "all done"
        assert [text for _channel, text in daemon._scheduler_deliver.records] == [
            _WAITING,
            PARKED_MISS,
            "all done",
        ]
    finally:
        await daemon._shutdown()


async def test_run_now_parks_without_moving_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tstd.scheduler.runner._TURN_TIMEOUT_SECS", 15)
    provider = _provider("tool", "stream")
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    future = "2026-08-22T07:45:00+00:00"
    save_job(
        data,
        _job(
            workspace,
            next_run=future,
            paused=False,
            attempt=1,
            resume_at=_AFTER,
        ),
    )
    daemon = Daemon(data_dir=data, provider=provider)
    daemon.config = make_config()
    try:
        before = {id(task) for task in daemon._tasks}
        raw = await daemon._handle_message(json.dumps({"type": "run_job", "job_id": "inbox"}), None)
        assert raw is not None
        assert json.loads(raw)["type"] == "job_list"
        fresh = [task for task in daemon._tasks if id(task) not in before]
        assert len(fresh) == 1
        await asyncio.wait_for(fresh[0], timeout=20)
        job = _stored(data)
        assert job.last_status == "waiting"
        assert job.last_summary == _TOOL
        assert job.next_run == future
        assert job.paused is False
        assert job.attempt == 1
        assert job.resume_at == _AFTER
        assert job.parked_session_id == job.last_session_id
        runs = list_runs(data, "inbox")
        assert len(runs) == 1
        assert runs[0].trigger == "manual"
        assert runs[0].scheduled_for is None
        assert runs[0].status == "waiting"
        assert runs[0].attempt is None
        line = "This job is waiting for your approval: " + _TOOL
        assert daemon._scheduler_deliver.records == [("window", line)]
    finally:
        await daemon._shutdown()


async def test_an_older_watcher_does_not_clear_a_newer_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, data, _provider, _elapsed = await _fire(tmp_path, monkeypatch, steps=("tool", "tool"))
    try:
        first = _session(daemon, data)
        first_id = first.id
        first_watch = _watchers(daemon)[0]
        before = {id(task) for task in daemon._tasks}
        raw = await daemon._handle_message(json.dumps({"type": "run_job", "job_id": "inbox"}), None)
        assert raw is not None
        manual = [
            task for task in daemon._tasks if id(task) not in before and task is not first_watch
        ]
        assert len(manual) == 1
        await asyncio.wait_for(manual[0], timeout=20)
        newer = _stored(data)
        assert newer.parked_session_id
        assert newer.parked_session_id != first_id
        assert newer.last_status == "waiting"
        kept = newer.next_run
        await first.cancel()
        await asyncio.wait_for(first_watch, timeout=10)
        still = _stored(data)
        assert still.parked_session_id == newer.parked_session_id
        assert still.last_session_id == newer.last_session_id
        assert still.last_status == "waiting"
        assert still.last_summary == _TOOL
        assert still.next_run == kept
        runs = list_runs(data, "inbox")
        assert [run.status for run in runs] == ["waiting", "failed"]
        assert runs[0].session_id == newer.parked_session_id
        assert runs[1].session_id == first_id
        assert runs[1].summary == UNANSWERED
        texts = [text for _channel, text in daemon._scheduler_deliver.records]
        assert UNANSWERED not in texts
        assert texts[-1].startswith("This job is waiting for your approval:")
    finally:
        await daemon._shutdown()


async def test_a_parked_job_is_not_also_skipped_for_lateness(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stamp = "2026-08-21T18:00:00+00:00"
    save_job(
        data,
        _job(
            workspace,
            grace=60,
            last_run=stamp,
            last_status="waiting",
            last_summary=_TOOL,
            last_session_id="sess-park",
            parked_session_id="sess-park",
            parked_started_at=stamp,
        ),
    )

    async def turn(_workspace: Path, _message: str) -> str:
        raise AssertionError("parked job started a session")

    deliver = RecordingDeliver()
    assert await run_due_jobs(data, _NOW, run_turn=turn, deliver=deliver) == ["inbox"]
    job = _stored(data)
    assert job.last_status == "waiting"
    assert job.parked_session_id == "sess-park"
    assert job.next_run == _AFTER
    runs = list_runs(data, "inbox")
    assert len(runs) == 1
    assert runs[0].status == "missed"
    assert runs[0].summary == PARKED_MISS
    assert deliver.records == [("window", PARKED_MISS)]


async def test_a_park_does_not_arm_another_try(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(
        data,
        _job(workspace, next_run=_RETRY, attempt=1, resume_at=_AFTER),
    )

    async def turn(_workspace: Path, _message: str) -> TurnResult:
        return TurnResult(_TOOL, ok=True, session_id="sess-park", waiting=True)

    deliver = RecordingDeliver()
    when = datetime(2026, 8, 21, 18, 10, tzinfo=UTC)
    assert await run_due_jobs(data, when, run_turn=turn, deliver=deliver) == ["inbox"]
    job = _stored(data)
    assert job.last_status == "waiting"
    assert job.attempt == 0
    assert job.resume_at is None
    assert job.next_run == _AFTER
    assert job.parked_session_id == "sess-park"
    assert deliver.records[0][1].startswith("6:10 PM job is waiting")


async def test_waiting_without_a_session_is_a_normal_failure(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data, _job(workspace))

    async def turn(_workspace: Path, _message: str) -> TurnResult:
        return TurnResult("a tool call", ok=True, waiting=True)

    deliver = RecordingDeliver()
    await run_due_jobs(data, _NOW, run_turn=turn, deliver=deliver)
    job = _stored(data)
    assert job.last_status == "failed"
    assert job.last_summary == "attempt 1 of 3: a tool call"
    assert job.parked_session_id is None
    # Not transient, so the retries on the job do not arm another try.
    assert job.attempt == 0
    assert job.next_run == _AFTER
    assert job.resume_at is None
    assert deliver.records == [("window", "attempt 1 of 3: a tool call")]


def test_pause_keeps_the_park_link(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stamp = "2026-08-21T18:00:00+00:00"
    job = _job(
        workspace,
        last_run=stamp,
        last_status="waiting",
        last_summary=_TOOL,
        last_session_id="sess-park",
        parked_session_id="sess-park",
        parked_started_at=stamp,
    )
    edited = apply_job_edit(
        job,
        workspace=None,
        instruction=None,
        cadence=None,
        next_run=None,
        deliver_to=None,
        paused=True,
        timezone=None,
        known_workspaces=[],
        preset=None,
        engine=None,
        known_presets={},
        grace=None,
        retries=None,
        retry_delay=None,
    )
    assert edited.paused is True
    assert edited.parked_session_id == "sess-park"
    assert edited.parked_started_at == stamp
    assert edited.last_status == "waiting"
    assert edited.last_summary == _TOOL
    assert edited.last_session_id == "sess-park"


def test_an_old_jobs_file_loads_without_park_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = tmp_path / "scheduler" / "jobs.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "inbox",
                        "workspace": str(workspace),
                        "instruction": "summarize the inbox",
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                        "paused": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = list_jobs(tmp_path)
    assert len(jobs) == 1
    assert jobs[0].parked_session_id is None
    assert jobs[0].parked_started_at is None
    assert jobs[0].last_status is None


async def test_a_parked_summary_is_redacted(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    key = "sk-" + "c" * 40  # tst-secret-ok
    job = _job(workspace)
    deliver = RecordingDeliver()
    await park_run(
        data,
        job,
        _NOW,
        session_id="sess-park",
        summary=f"Run `{key}`",
        advance=True,
        trigger="schedule",
        scheduled_for=_SLOT,
        deliver=deliver,
    )
    stored = _stored(data)
    blob = (data / "scheduler" / "jobs.json").read_text(encoding="utf-8")
    history = (data / "scheduler" / "history" / "inbox.jsonl").read_text(encoding="utf-8")
    assert key not in blob
    assert key not in history
    assert stored.last_summary is not None
    assert key not in stored.last_summary
    assert "[REDACTED]" in stored.last_summary
    assert list_runs(data, "inbox")[0].summary == stored.last_summary
    assert key not in deliver.records[0][1]
    assert "[REDACTED]" in deliver.records[0][1]


async def test_revive_keeps_a_line_that_already_settled(tmp_path: Path) -> None:
    """A crash after the history rewrite must not send 'approval never answered'."""
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stamp = "2026-08-21T18:00:00+00:00"
    save_job(
        data,
        _job(
            workspace,
            next_run=_AFTER,
            last_run=stamp,
            last_status="waiting",
            last_summary=_TOOL,
            last_session_id="sess-park",
            parked_session_id="sess-park",
            parked_started_at=stamp,
        ),
    )
    append_run(
        data,
        "inbox",
        started_at=stamp,
        scheduled_for=_SLOT,
        trigger="schedule",
        status="ok",
        summary="all done",
        session_id="sess-park",
    )
    deliver = RecordingDeliver()
    assert await revive_parked(data, deliver) == 1
    job = _stored(data)
    assert job.last_status == "ok"
    assert job.last_summary == "all done"
    assert job.parked_session_id is None
    assert job.parked_started_at is None
    assert job.next_run == _AFTER
    assert deliver.records == []
    runs = list_runs(data, "inbox")
    assert len(runs) == 1
    assert runs[0].status == "ok"
    assert await revive_parked(data, deliver) == 0
    assert deliver.records == []


def test_a_miss_cannot_restore_a_park_the_watch_cleared(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stamp = "2026-08-21T18:00:00+00:00"
    save_job(
        data,
        _job(
            workspace,
            last_run=stamp,
            last_status="waiting",
            last_summary=_TOOL,
            last_session_id="sess-park",
            parked_session_id="sess-park",
            parked_started_at=stamp,
        ),
    )
    entered = threading.Event()
    release = threading.Event()
    saw: list[str | None] = []

    def clear(current: Job) -> Job:
        entered.set()
        assert release.wait(timeout=2)
        return current.model_copy(update={"parked_session_id": None, "parked_started_at": None})

    def miss(current: Job) -> Job | None:
        saw.append(current.parked_session_id)
        if not current.parked_session_id:
            return None
        return current.model_copy(update={"next_run": _LATER})

    clearer = threading.Thread(target=transform_job, args=(data, "inbox", clear))
    clearer.start()
    assert entered.wait(timeout=2)
    missed = threading.Thread(target=transform_job, args=(data, "inbox", miss))
    missed.start()
    release.set()
    clearer.join(timeout=2)
    missed.join(timeout=2)
    assert not clearer.is_alive()
    assert not missed.is_alive()
    job = _stored(data)
    assert saw == [None]
    assert job.parked_session_id is None
    assert job.next_run == _SLOT


async def test_a_turn_that_never_asks_still_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tstd.scheduler.runner._TURN_TIMEOUT_SECS", 0.4)
    provider = MockProvider(default=Script(kind="stream", content="digest ready", chunk_delay=30))
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data, _job(workspace, retries=1))
    daemon = Daemon(data_dir=data, provider=provider)
    daemon.config = make_config()
    started = time.monotonic()
    try:
        await asyncio.wait_for(daemon.run_due_jobs(_NOW), timeout=5)
        assert time.monotonic() - started < 3
        job = _stored(data)
        assert job.last_status == "failed"
        assert job.attempt == 1
        assert job.next_run == _RETRY
        assert job.resume_at == _AFTER
        assert job.parked_session_id is None
        assert "timed out" in (job.last_summary or "")
        assert daemon._scheduler_deliver.records == []
    finally:
        await daemon._shutdown()
