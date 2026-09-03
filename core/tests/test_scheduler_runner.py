"""Wake due jobs, run one turn, deliver once (TD-3804)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.scheduler.models import Job, JobDraft, validate_draft
from tstd.scheduler.runner import RecordingDeliver, run_due_jobs, run_turn_on_daemon
from tstd.scheduler.schedule import advance_job, due_jobs, next_run_after
from tstd.scheduler.store import list_jobs, save_job


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
    job = _job(Path("/ws"), job_id="p", paused=True)
    assert due_jobs([job], _now()) == []


def test_future_next_run_is_not_due() -> None:
    job = _job(Path("/ws"), job_id="f", next_run="2026-08-21T16:00:00+00:00")
    assert due_jobs([job], _now()) == []


def test_past_next_run_is_due() -> None:
    job = _job(Path("/ws"), job_id="d", next_run="2026-08-21T12:00:00+00:00")
    assert [item.id for item in due_jobs([job], _now())] == ["d"]


def test_cadence_only_is_not_due_until_armed() -> None:
    job = _job(Path("/ws"), job_id="c", cadence="every 1 hour", next_run=None)
    assert due_jobs([job], _now()) == []


def test_overdue_interval_advances_once_from_now() -> None:
    job = _job(
        Path("/ws"),
        job_id="h",
        cadence="every 1 hour",
        next_run="2026-08-21T12:00:00+00:00",
    )
    advanced = advance_job(job, _now())
    assert advanced.next_run == "2026-08-21T16:00:00+00:00"
    assert advanced.cadence == "every 1 hour"
    assert advanced.paused is False


def test_one_shot_pauses() -> None:
    job = _job(Path("/ws"), job_id="once", cadence=None, next_run="2026-08-21T12:00:00+00:00")
    advanced = advance_job(job, _now())
    assert advanced.paused is True
    assert advanced.cadence is None


def test_a_job_with_no_schedule_and_no_run_is_still_refused() -> None:
    # Widening the invariant for spent one-shots must not let a job with
    # nothing to run by it on the way in.
    with pytest.raises(ValidationError):
        Job(
            id="nope",
            workspace="/ws",
            instruction="ship it",
            cadence=None,
            next_run=None,
            deliver_to="window",
        )


def test_a_spent_one_shot_does_not_fire_again_when_resumed() -> None:
    # Pause/Resume is the only handle the rail gives a job. Keeping the old
    # slot meant toggling a finished one-shot ran the instruction again on
    # the very next tick, with nothing on screen to warn about it.
    job = _job(Path("/ws"), job_id="once", cadence=None, next_run="2026-08-21T12:00:00+00:00")
    spent = advance_job(job, _now())
    assert spent.next_run is None

    resumed = spent.model_copy(update={"paused": False})
    a_week_later = _now() + timedelta(days=7)
    assert due_jobs([resumed], a_week_later) == []


def test_cron_next_is_after_now() -> None:
    nxt = next_run_after("0 9 * * 1-5", _now())
    assert nxt > _now()
    assert nxt.hour == 9
    assert nxt.minute == 0
    assert nxt.weekday() < 5


def test_restricting_both_day_fields_ors_them() -> None:
    """Vixie cron: ``0 0 1 * 1`` is "the 1st, and every Monday".

    ANDing the two fields instead silently drops almost every fire — the
    job would only run when the 1st happened to land on a Monday.
    """
    start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    fires: list[datetime] = []
    cursor = start
    for _ in range(6):
        cursor = next_run_after("0 0 1 * 1", cursor)
        fires.append(cursor)

    # Every Monday in April, then 1 May (a Friday) because the day-of-month
    # half matches on its own.
    assert [f.strftime("%Y-%m-%d") for f in fires] == [
        "2026-04-06",
        "2026-04-13",
        "2026-04-20",
        "2026-04-27",
        "2026-05-01",
        "2026-05-04",
    ]


def test_one_restricted_day_field_still_ands() -> None:
    """With day-of-week ``*`` there is nothing to OR with — the 15th only."""
    cursor = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    fires = []
    for _ in range(3):
        cursor = next_run_after("0 0 15 * *", cursor)
        fires.append(cursor.strftime("%Y-%m-%d"))
    assert fires == ["2026-04-15", "2026-05-15", "2026-06-15"]


def test_sunday_is_both_zero_and_seven() -> None:
    start = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    assert next_run_after("0 0 * * 0", start) == next_run_after("0 0 * * 7", start)
    assert next_run_after("0 0 * * 0", start).weekday() == 6


def test_job_may_carry_cadence_and_next_run() -> None:
    job = Job(
        id="both",
        workspace="/ws/one",
        instruction="ping",
        cadence="every 1 hour",
        next_run="2026-08-21T16:00:00+00:00",
        deliver_to="window",
    )
    assert job.cadence == "every 1 hour"
    assert job.next_run == "2026-08-21T16:00:00+00:00"


def test_draft_create_still_rejects_both() -> None:
    with pytest.raises(Exception, match="not both"):
        validate_draft(
            JobDraft(
                workspace="/ws/one",
                instruction="ping",
                cadence="every 1 hour",
                next_run="2026-08-21T16:00:00+00:00",
                deliver_to="window",
            )
        )


def test_the_daemon_defaults_its_delivery_hooks(tmp_path: Path) -> None:
    """The regression that made every channel silent (TD-3807).

    ``main()`` builds ``Daemon(data_dir=..., parent_pid=...)`` and passed no
    ``notify_send``, so ``RecordingDeliver`` got ``send=None`` and no
    ``on_window``: slack and ntfy logged "skipped (no send hook)", window
    logged a length, and a scheduled job produced nothing a user could see
    on any channel. Both hooks must be wired by default.
    """
    daemon = Daemon(data_dir=tmp_path)
    assert daemon._scheduler_deliver.send is not None
    assert daemon._scheduler_deliver.on_window is not None


def test_an_injected_send_hook_still_wins(tmp_path: Path) -> None:
    async def mine(_channel: str, _summary: str) -> None:
        return None

    daemon = Daemon(data_dir=tmp_path, notify_send=mine)  # type: ignore[arg-type]
    assert daemon._scheduler_deliver.send is mine


async def test_a_fired_job_pushes_a_fresh_list_to_open_clients(tmp_path: Path) -> None:
    """An open Scheduled pane must not sit on a fire time that has passed."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="pushy"))
    mock = MockProvider(default=Script(kind="stream", content="pushed"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()

    sent: list[str] = []

    async def capture(payload: str) -> int:
        sent.append(payload)
        return 1

    daemon.ws_server.broadcast = capture  # type: ignore[method-assign]

    ran = await daemon.run_due_jobs(_now())
    assert ran == ["pushy"]
    assert len(sent) == 1
    pushed = json.loads(sent[0])
    assert pushed["type"] == "job_list"
    row = pushed["jobs"][0]
    assert row["last_status"] == "ok"
    assert row["last_summary"] == "pushed"
    assert row["next_run"] == "2026-08-21T16:00:00+00:00"


async def test_no_due_job_pushes_nothing(tmp_path: Path) -> None:
    """A quiet tick every 15s must not spam every open client."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="later", next_run="2027-01-01T00:00:00+00:00"))
    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    daemon.config = make_config()

    sent: list[str] = []

    async def capture(payload: str) -> int:
        sent.append(payload)
        return 1

    daemon.ws_server.broadcast = capture  # type: ignore[method-assign]

    assert await daemon.run_due_jobs(_now()) == []
    assert sent == []


async def test_pausing_a_job_keeps_its_receipt(tmp_path: Path) -> None:
    """Pause is ``save_job`` with a flag flipped — it must not erase history.

    The daemon rebuilds the row from the message, so every field the
    message does not carry has to be copied from the stored job.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="keep"))
    daemon = Daemon(data_dir=data_dir, provider=MockProvider(default=Script(kind="stream")))
    daemon.config = make_config()

    async def plain(_ws: Path, _msg: str) -> str:
        return "the receipt"

    await run_due_jobs(data_dir, _now(), run_turn=plain, deliver=RecordingDeliver())
    assert list_jobs(data_dir)[0].last_summary == "the receipt"

    raw = await daemon._handle_message(
        json.dumps(
            {
                "type": "save_job",
                "id": "keep",
                "workspace": str(workspace),
                "instruction": "summarize the inbox",
                "cadence": "every 1 hour",
                "deliver_to": "window",
                "paused": True,
            }
        ),
        None,
    )
    assert raw is not None
    row = json.loads(raw)["jobs"][0]
    assert row["paused"] is True
    assert row["last_summary"] == "the receipt"
    assert row["last_status"] == "ok"
    assert list_jobs(data_dir)[0].last_summary == "the receipt"


async def test_a_run_stamps_a_receipt_on_the_job(tmp_path: Path) -> None:
    """TD-3807: a fire records what happened, not just when the next one is.

    Without this the pane can say "next run 16:00" for a job that has been
    failing every hour since it was created.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="inbox"))
    mock = MockProvider(default=Script(kind="stream", content="digest ready"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()

    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=RecordingDeliver(),
    )

    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "ok"
    assert stored.last_summary == "digest ready"
    assert stored.last_run == "2026-08-21T15:00:00+00:00"
    # The session that ran it, so the pane can point the user at the work.
    assert stored.last_session_id
    assert daemon.session_registry.get(stored.last_session_id) is not None


async def test_a_missing_workspace_is_recorded_as_a_failure(tmp_path: Path) -> None:
    """The common real failure: the folder was moved or deleted."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    gone = tmp_path / "not-there"
    save_job(
        data_dir,
        Job(
            id="orphan",
            workspace=str(gone),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            next_run="2026-08-21T12:00:00+00:00",
            deliver_to="window",
        ),
    )
    mock = MockProvider(default=Script(kind="stream", content="unused"))
    daemon = Daemon(data_dir=data_dir, provider=mock)
    daemon.config = make_config()

    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=RecordingDeliver(),
    )

    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "failed"
    assert "not a directory" in (stored.last_summary or "")
    # It still advanced, so a broken job does not spin every tick.
    assert stored.next_run == "2026-08-21T16:00:00+00:00"


async def test_a_raising_turn_is_recorded_as_a_failure(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="boom"))

    async def explode(_ws: Path, _msg: str) -> str:
        raise RuntimeError("provider is down")

    await run_due_jobs(data_dir, _now(), run_turn=explode, deliver=RecordingDeliver())

    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "failed"
    assert "provider is down" in (stored.last_summary or "")


async def test_a_plain_string_turn_still_counts_as_a_run(tmp_path: Path) -> None:
    """Back-compat: run_turn may return a bare str, as the fakes do."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="plain"))

    async def plain(_ws: Path, _msg: str) -> str:
        return "done"

    await run_due_jobs(data_dir, _now(), run_turn=plain, deliver=RecordingDeliver())

    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "ok"
    assert stored.last_summary == "done"
    assert stored.last_session_id is None


async def test_a_run_summary_is_redacted_and_capped(tmp_path: Path) -> None:
    """Model output lands here, so it is redacted rather than trusted."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="leaky"))

    async def leaks(_ws: Path, _msg: str) -> str:
        return "the key is sk-" + "a" * 40 + " " + "x" * 4000

    await run_due_jobs(data_dir, _now(), run_turn=leaks, deliver=RecordingDeliver())

    stored = list_jobs(data_dir)[0]
    summary = stored.last_summary or ""
    assert "sk-" + "a" * 40 not in summary
    assert len(summary) <= 2001
    # And the receipt never reaches disk unredacted either.
    assert "sk-" + "a" * 40 not in (data_dir / "scheduler" / "jobs.json").read_text()


async def test_window_delivery_calls_the_window_hook(tmp_path: Path) -> None:
    """The default channel must reach a hook, not just a log line (TD-3807)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data_dir, _job(workspace, job_id="win", deliver_to="window"))
    seen: list[str] = []

    async def on_window(summary: str) -> None:
        seen.append(summary)

    async def plain(_ws: Path, _msg: str) -> str:
        return "windowed"

    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=plain,
        deliver=RecordingDeliver(on_window=on_window),
    )
    assert seen == ["windowed"]


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
