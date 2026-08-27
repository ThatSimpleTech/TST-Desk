"""Wake due jobs, run one turn, deliver once (TD-3804)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.scheduler.models import Job, JobDraft, validate_draft
from tstd.scheduler.runner import RecordingDeliver, run_due_jobs, run_turn_on_daemon
from tstd.scheduler.schedule import advance_job, due_jobs, next_run_after
from tstd.scheduler.store import list_jobs, save_job

_SCHED_FIXTURE_ROOT = Path(__file__).resolve().parent / "_sched_fixture_ws"


def _fixture_ws(name: str = "ws") -> Path:
    path = (_SCHED_FIXTURE_ROOT / name).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> datetime:
    return datetime(2026, 8, 21, 15, 0, tzinfo=UTC)


def _workspace(root: Path, name: str = "ws") -> Path:
    path = root / name
    path.mkdir()
    return path


def _job(
    workspace: Path,
    *,
    job_id: str,
    instruction: str = "summarize the inbox",
    cadence: str | None = "every 1 hour",
    next_run: str | None = "2026-08-21T12:00:00+00:00",
    deliver_to: str = "window",
    paused: bool = False,
) -> Job:
    return Job(
        id=job_id,
        workspace=str(workspace),
        instruction=instruction,
        cadence=cadence,
        next_run=next_run,
        deliver_to=deliver_to,  # type: ignore[arg-type]
        paused=paused,
    )


async def _start_daemon(
    data_dir: Path,
    mock: MockProvider,
    *,
    notify_send: object | None = None,
    scheduler_tick: float = 60.0,
) -> tuple[Daemon, asyncio.Task[None]]:
    daemon = Daemon(
        data_dir=data_dir,
        provider=mock,
        notify_send=notify_send,  # type: ignore[arg-type]
        scheduler_tick=scheduler_tick,
    )
    daemon.config = make_config()
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


async def _stop_daemon(daemon: Daemon, task: asyncio.Task[None]) -> None:
    daemon._shutdown_event.set()
    await asyncio.wait_for(task, timeout=10.0)


def test_paused_is_never_due() -> None:
    job = _job(_fixture_ws(), job_id="p", paused=True)
    assert due_jobs([job], _now()) == []


def test_future_next_run_is_not_due() -> None:
    job = _job(_fixture_ws(), job_id="f", next_run="2026-08-21T16:00:00+00:00")
    assert due_jobs([job], _now()) == []


def test_past_next_run_is_due() -> None:
    job = _job(_fixture_ws(), job_id="d", next_run="2026-08-21T12:00:00+00:00")
    assert [item.id for item in due_jobs([job], _now())] == ["d"]


def test_cadence_only_is_not_due_until_armed() -> None:
    job = _job(_fixture_ws(), job_id="c", cadence="every 1 hour", next_run=None)
    assert due_jobs([job], _now()) == []


def test_overdue_interval_advances_once_from_now() -> None:
    job = _job(
        _fixture_ws(),
        job_id="h",
        cadence="every 1 hour",
        next_run="2026-08-21T12:00:00+00:00",
    )
    advanced = advance_job(job, _now())
    assert advanced.next_run == "2026-08-21T16:00:00+00:00"
    assert advanced.cadence == "every 1 hour"
    assert advanced.paused is False


def test_one_shot_pauses() -> None:
    job = _job(_fixture_ws(), job_id="once", cadence=None, next_run="2026-08-21T12:00:00+00:00")
    advanced = advance_job(job, _now())
    assert advanced.paused is True
    assert advanced.cadence is None


def test_cron_next_is_after_now() -> None:
    nxt = next_run_after("0 9 * * 1-5", _now())
    assert nxt > _now()
    assert nxt.hour == 9
    assert nxt.minute == 0
    assert nxt.weekday() < 5


def test_job_may_carry_cadence_and_next_run() -> None:
    ws_one = _fixture_ws("one")
    job = Job(
        id="both",
        workspace=str(ws_one),
        instruction="ping",
        cadence="every 1 hour",
        next_run="2026-08-21T16:00:00+00:00",
        deliver_to="window",
    )
    assert job.cadence == "every 1 hour"
    assert job.next_run == "2026-08-21T16:00:00+00:00"


def test_draft_create_still_rejects_both() -> None:
    ws_one = _fixture_ws("one")
    with pytest.raises(Exception, match="not both"):
        validate_draft(
            JobDraft(
                workspace=str(ws_one),
                instruction="ping",
                cadence="every 1 hour",
                next_run="2026-08-21T16:00:00+00:00",
                deliver_to="window",
            )
        )


async def test_due_job_one_turn_one_deliver(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="inbox"))
    mock = MockProvider(default=Script(kind="stream", content="digest ready"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()
    deliver = RecordingDeliver()
    ran = await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert ran == ["inbox"]
    assert len(mock.calls) == 1
    assert deliver.records == [("window", "digest ready")]
    stored = list_jobs(data_dir)[0]
    assert stored.next_run == "2026-08-21T16:00:00+00:00"
    sessions = await daemon.session_registry.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].workspace_path == str(workspace)


async def test_three_overdue_jobs_fire_once_each(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    mock = MockProvider(default=Script(kind="stream", content="once"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()
    overdue = "2026-08-21T12:00:00+00:00"
    for index in range(3):
        workspace = _workspace(tmp_path, f"ws{index}")
        save_job(
            data_dir,
            _job(workspace, job_id=f"job-{index}", next_run=overdue, cadence="every 1 hour"),
        )
    deliver = RecordingDeliver()
    ran = await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert ran == ["job-0", "job-1", "job-2"]
    assert len(mock.calls) == 3
    assert deliver.records == [("window", "once")] * 3
    for job in list_jobs(data_dir):
        assert job.next_run == "2026-08-21T16:00:00+00:00"
    again = await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert again == []
    assert len(mock.calls) == 3


async def test_one_job_three_hours_overdue_is_one_run(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(
        data_dir,
        _job(workspace, job_id="stale", next_run="2026-08-21T12:00:00+00:00"),
    )
    turns: list[str] = []

    async def fake_turn(_workspace: Path, message: str) -> str:
        turns.append(message)
        return "ok"

    deliver = RecordingDeliver()
    await run_due_jobs(data_dir, _now(), run_turn=fake_turn, deliver=deliver)
    await run_due_jobs(data_dir, _now(), run_turn=fake_turn, deliver=deliver)
    assert turns == ["summarize the inbox"]
    assert deliver.records == [("window", "ok")]


async def test_paused_job_is_skipped(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="quiet", paused=True))
    turns: list[str] = []

    async def fake_turn(_workspace: Path, _message: str) -> str:
        turns.append("ran")
        return "nope"

    deliver = RecordingDeliver()
    ran = await run_due_jobs(data_dir, _now(), run_turn=fake_turn, deliver=deliver)
    assert ran == []
    assert turns == []
    assert deliver.records == []


async def test_one_shot_delivers_then_pauses(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(
        data_dir,
        _job(workspace, job_id="once", cadence=None, next_run="2026-08-21T14:00:00+00:00"),
    )
    mock = MockProvider(default=Script(kind="stream", content="done"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()
    deliver = RecordingDeliver()
    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert deliver.records == [("window", "done")]
    stored = get_only(data_dir)
    assert stored.paused is True
    again = await run_due_jobs(
        data_dir,
        _now() + timedelta(hours=2),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert again == []
    assert len(mock.calls) == 1


def get_only(data_dir: Path) -> Job:
    jobs = list_jobs(data_dir)
    assert len(jobs) == 1
    return jobs[0]


async def test_slack_calls_injected_send(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="hook", deliver_to="slack"))
    sent: list[tuple[str, str]] = []

    async def send(channel: str, summary: str) -> None:
        sent.append((channel, summary))

    deliver = RecordingDeliver(send=send)
    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=_echo_turn,
        deliver=deliver,
    )
    assert sent == [("slack", "echo: summarize the inbox")]
    assert deliver.records == [("slack", "echo: summarize the inbox")]


async def _echo_turn(workspace: Path, message: str) -> str:
    return f"echo: {message}"


async def test_ntfy_without_send_is_recorded_not_http(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="quiet-ntfy", deliver_to="ntfy"))
    deliver = RecordingDeliver()
    await run_due_jobs(data_dir, _now(), run_turn=_echo_turn, deliver=deliver)
    assert deliver.records == [("ntfy", "echo: summarize the inbox")]


async def test_caps_and_classifier_still_apply(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    agents = workspace / "AGENTS.md"
    agents.write_text("stay\n", encoding="utf-8")
    cfg = workspace / ".tst"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "caps:\n  spend_usd: 9.5\n  max_iterations: 12\n",
        encoding="utf-8",
    )
    save_job(
        data_dir,
        _job(workspace, job_id="wall", instruction="write the steering file"),
    )
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments=json.dumps({"path": "AGENTS.md", "content": "hacked"}),
                ),
                Script(kind="stream", content="could not write"),
            ]
        },
        default=Script(kind="stream", content="ok"),
    )
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()
    deliver = RecordingDeliver()
    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=deliver,
    )
    assert agents.read_text(encoding="utf-8") == "stay\n"
    sessions = await daemon.session_registry.list_sessions()
    assert len(sessions) == 1
    session = sessions[0]
    assert session.boundary_config.caps.spend_usd == 9.5
    assert session.boundary_config.caps.max_iterations == 12
    results = [
        event for event in session.event_log.all_events if isinstance(event, ToolResultEvent)
    ]
    assert results
    assert results[0].status == "error"
    assert results[0].error_code == "boundary_refusal"
    assert deliver.records == [("window", "could not write")]


async def test_daemon_tick_on_start_revives_once(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(
        data_dir,
        _job(
            workspace,
            job_id="revive",
            next_run=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
        ),
    )
    mock = MockProvider(default=Script(kind="stream", content="revived"))
    daemon, task = await _start_daemon(data_dir, mock)
    try:
        for _ in range(80):
            if daemon._scheduler_deliver.records:
                break
            await asyncio.sleep(0.05)
        assert daemon._scheduler_deliver.records == [("window", "revived")]
        assert len(mock.calls) == 1
        stored = get_only(data_dir)
        assert stored.next_run is not None
        when = datetime.fromisoformat(stored.next_run)
        assert when > datetime.now(UTC)
    finally:
        await _stop_daemon(daemon, task)


async def test_cadence_only_is_armed_not_fired(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(
        data_dir,
        _job(workspace, job_id="later", cadence="every 2 hours", next_run=None),
    )
    turns: list[str] = []

    async def fake_turn(_workspace: Path, _message: str) -> str:
        turns.append("ran")
        return "nope"

    deliver = RecordingDeliver()
    ran = await run_due_jobs(data_dir, _now(), run_turn=fake_turn, deliver=deliver)
    assert ran == []
    assert turns == []
    stored = get_only(data_dir)
    assert stored.next_run == "2026-08-21T17:00:00+00:00"
