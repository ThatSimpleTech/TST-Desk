"""Run history (TD-3811): one JSONL record per fire, listed newest first."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tstd.daemon import Daemon
from tstd.protocol import JobRuns, parse_daemon_event
from tstd.scheduler.history import append_run, history_path, list_runs
from tstd.scheduler.models import Job, JobValidationError
from tstd.scheduler.runner import RecordingDeliver, TurnResult, run_due_jobs, run_manual_job
from tstd.scheduler.store import delete_job, get_job, list_jobs, save_job

_NEXT = "2026-08-21T12:00:00+00:00"
_KEY = "sk-" + "a" * 40  # tst-secret-ok


def _now() -> datetime:
    return datetime(2026, 8, 21, 15, 0, tzinfo=UTC)


def _later() -> datetime:
    return datetime(2026, 8, 21, 16, 30, tzinfo=UTC)


def _job(workspace: Path, *, job_id: str = "inbox") -> Job:
    return Job(
        id=job_id,
        workspace=str(workspace),
        instruction="summarize the inbox",
        cadence="every 1 hour",
        next_run=_NEXT,
        deliver_to="window",
    )


def _stamp(second: int) -> str:
    return f"2026-08-21T15:00:{second:02d}+00:00"


def _append(
    data_dir: Path,
    job_id: str = "inbox",
    *,
    started_at: str = "2026-08-21T15:00:00+00:00",
    scheduled_for: str | None = _NEXT,
    trigger: str = "schedule",
    status: str = "ok",
    summary: str | None = "digest",
    session_id: str | None = "sess-1",
) -> None:
    append_run(
        data_dir,
        job_id,
        started_at=started_at,
        scheduled_for=scheduled_for,
        trigger=trigger,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=summary,
        session_id=session_id,
    )


async def _handle(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


async def test_schedule_and_manual_each_append_a_record(tmp_path: Path) -> None:
    """Both triggers add a line. The row receipt stays the newest one."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data_dir, _job(workspace))

    async def scheduled(_ws: Path, _message: str) -> TurnResult:
        return TurnResult("from the schedule", ok=True, session_id="sess-sched")

    async def manual(_ws: Path, _message: str) -> TurnResult:
        return TurnResult("from run now", ok=True, session_id="sess-manual")

    deliver = RecordingDeliver()
    await run_due_jobs(data_dir, _now(), run_turn=scheduled, deliver=deliver)
    fresh = get_job(data_dir, "inbox")
    assert fresh is not None
    await run_manual_job(data_dir, fresh, _later(), run_turn=manual, deliver=deliver)

    path = history_path(data_dir, "inbox")
    assert path == data_dir / "scheduler" / "history" / "inbox.jsonl"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert not (workspace / "scheduler").exists()

    runs = list_runs(data_dir, "inbox")
    assert [(run.trigger, run.scheduled_for, run.started_at, run.session_id) for run in runs] == [
        ("manual", None, "2026-08-21T16:30:00+00:00", "sess-manual"),
        ("schedule", _NEXT, "2026-08-21T15:00:00+00:00", "sess-sched"),
    ]
    assert [run.summary for run in runs] == ["from run now", "from the schedule"]
    assert [run.status for run in runs] == ["ok", "ok"]

    stored = list_jobs(data_dir)[0]
    assert stored.last_run == "2026-08-21T16:30:00+00:00"
    assert stored.last_status == "ok"
    assert stored.last_summary == "from run now"
    assert stored.last_session_id == "sess-manual"
    # The scheduled fire moved the slot; Run now did not move it again.
    assert stored.next_run == "2026-08-21T16:00:00+00:00"
    assert deliver.records == [("window", "from the schedule"), ("window", "from run now")]


async def test_a_failed_fire_is_a_failed_record(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data_dir, _job(workspace))

    async def boom(_ws: Path, _message: str) -> TurnResult:
        return TurnResult("disk full", ok=False, session_id="sess-fail")

    await run_due_jobs(data_dir, _now(), run_turn=boom, deliver=RecordingDeliver())
    runs = list_runs(data_dir, "inbox")
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].summary == "disk full"
    assert runs[0].session_id == "sess-fail"
    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "failed"
    assert stored.last_summary == "disk full"


async def test_a_run_summary_is_redacted_in_the_log(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data_dir, _job(workspace))

    async def leaks(_ws: Path, _message: str) -> str:
        return "the key is " + _KEY

    await run_due_jobs(data_dir, _now(), run_turn=leaks, deliver=RecordingDeliver())
    text = history_path(data_dir, "inbox").read_text(encoding="utf-8")
    assert _KEY not in text
    assert "[REDACTED]" in text
    summary = list_runs(data_dir, "inbox")[0].summary or ""
    assert _KEY not in summary
    assert "[REDACTED]" in summary


def test_the_log_keeps_the_newest_fifty(tmp_path: Path) -> None:
    for second in range(51):
        _append(tmp_path, started_at=_stamp(second), summary=f"n{second}")
    runs = list_runs(tmp_path, "inbox")
    assert len(runs) == 50
    assert runs[0].started_at == _stamp(50)
    assert runs[-1].started_at == _stamp(1)
    assert _stamp(0) not in history_path(tmp_path, "inbox").read_text(encoding="utf-8")


def test_a_missing_log_is_empty_and_creates_nothing(tmp_path: Path) -> None:
    assert list_runs(tmp_path, "inbox") == []
    assert not history_path(tmp_path, "inbox").exists()


def test_corrupt_lines_are_skipped_and_dropped_on_the_next_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = history_path(tmp_path, "inbox")
    path.parent.mkdir(parents=True)
    valid = {
        "started_at": "2026-08-21T15:00:00+00:00",
        "scheduled_for": _NEXT,
        "trigger": "schedule",
        "status": "ok",
        "summary": "kept " + _KEY,
        "session_id": "sess-1",
    }
    path.write_text(
        json.dumps(valid) + "\n\nnot-json " + _KEY + '\n{"started_at": "nope"}\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="tstd.scheduler.history"):
        runs = list_runs(tmp_path, "inbox")
    assert len(runs) == 1
    assert runs[0].summary is not None
    assert _KEY not in runs[0].summary
    assert "[REDACTED]" in runs[0].summary
    assert _KEY not in caplog.text
    assert len(caplog.records) == 2

    _append(
        tmp_path,
        started_at="2026-08-21T16:00:00+00:00",
        summary="second",
        trigger="manual",
        scheduled_for=None,
    )
    text = path.read_text(encoding="utf-8")
    assert _KEY not in text
    assert "not-json" not in text
    assert "nope" not in text
    listed = list_runs(tmp_path, "inbox")
    assert listed[0].trigger == "manual"
    assert listed[0].summary == "second"
    assert listed[1].summary is not None and listed[1].summary.startswith("kept")


def test_delete_job_removes_the_history_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _job(workspace))
    _append(tmp_path)
    path = history_path(tmp_path, "inbox")
    assert path.is_file()
    assert delete_job(tmp_path, "inbox") is True
    assert not path.exists()
    assert list_jobs(tmp_path) == []
    assert delete_job(tmp_path, "inbox") is False


def test_deleting_an_unknown_id_leaves_a_stray_log_alone(tmp_path: Path) -> None:
    _append(tmp_path, job_id="orphan")
    assert delete_job(tmp_path, "orphan") is False
    assert history_path(tmp_path, "orphan").is_file()


@pytest.mark.parametrize("job_id", ["../outside", "..", ".", "a/b", " ", ""])
def test_history_refuses_an_id_that_leaves_the_directory(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(JobValidationError):
        _append(tmp_path, job_id=job_id)
    assert list(tmp_path.rglob("*.jsonl")) == []


async def test_run_now_does_not_rewrite_history_after_a_delete(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(workspace)
    save_job(data_dir, job)
    _append(data_dir, summary="earlier")

    async def turn(_ws: Path, _message: str) -> TurnResult:
        assert delete_job(data_dir, "inbox") is True
        return TurnResult("too late", session_id="sess-gone")

    deliver = RecordingDeliver()
    await run_manual_job(data_dir, job, _now(), run_turn=turn, deliver=deliver)
    assert list_jobs(data_dir) == []
    assert not history_path(data_dir, "inbox").exists()
    assert deliver.records == [("window", "too late")]


async def test_list_job_runs_is_newest_first(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    daemon = Daemon(data_dir=data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        save_job(data_dir, _job(workspace))
        _append(data_dir, started_at="2026-08-21T15:00:00+00:00", summary="older")
        _append(
            data_dir,
            started_at="2026-08-21T16:00:00+00:00",
            summary="newer",
            trigger="manual",
            scheduled_for=None,
            session_id=None,
        )
        event = parse_daemon_event(
            json.dumps(await _handle(daemon, {"type": "list_job_runs", "job_id": "inbox"}))
        )
        assert isinstance(event, JobRuns)
        assert event.seq == 1
        assert event.job_id == "inbox"
        assert [run.started_at for run in event.runs] == [
            "2026-08-21T16:00:00+00:00",
            "2026-08-21T15:00:00+00:00",
        ]
        assert event.runs[0].trigger == "manual"
        assert event.runs[0].scheduled_for is None
        assert event.runs[0].session_id is None
        assert event.runs[1].scheduled_for == _NEXT
    finally:
        await daemon._shutdown()


async def test_list_job_runs_is_empty_until_the_job_fires(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    daemon = Daemon(data_dir=data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        save_job(data_dir, _job(workspace))
        event = parse_daemon_event(
            json.dumps(await _handle(daemon, {"type": "list_job_runs", "job_id": "inbox"}))
        )
        assert isinstance(event, JobRuns)
        assert event.runs == []
    finally:
        await daemon._shutdown()


async def test_list_job_runs_skips_a_corrupt_line(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    daemon = Daemon(data_dir=data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        save_job(data_dir, _job(workspace))
        path = history_path(data_dir, "inbox")
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"started_at":"2026-08-21T15:00:00+00:00","trigger":"schedule",'
            '"status":"ok","summary":"kept"}\n{{{{\n',
            encoding="utf-8",
        )
        event = parse_daemon_event(
            json.dumps(await _handle(daemon, {"type": "list_job_runs", "job_id": "inbox"}))
        )
        assert isinstance(event, JobRuns)
        assert len(event.runs) == 1
        assert event.runs[0].summary == "kept"
    finally:
        await daemon._shutdown()


@pytest.mark.parametrize("job_id", ["missing", "../outside"])
async def test_list_job_runs_unknown_id_is_job_not_found(tmp_path: Path, job_id: str) -> None:
    data_dir = tmp_path / "data"
    daemon = Daemon(data_dir=data_dir)
    try:
        err = await _handle(daemon, {"type": "list_job_runs", "job_id": job_id})
        assert err["type"] == "error"
        assert err["code"] == "job_not_found"
        assert job_id in err["message"]
        assert not (tmp_path / "outside.jsonl").exists()
        assert list(data_dir.rglob("*.jsonl")) == []
    finally:
        await daemon._shutdown()
