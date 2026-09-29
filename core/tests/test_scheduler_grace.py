"""Skip a scheduled slot that is already hours late (TD-3813).

A grace is seconds. Omitted, a due slot still runs once. Past it, the
tick spends the slot, records ``missed``, and delivers one line. Run now
does not look at the grace.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.scheduler.grace import GraceError, parse_grace, past_grace, skip_line
from tstd.scheduler.history import list_runs
from tstd.scheduler.models import Job, JobDraft, validate_draft
from tstd.scheduler.runner import InFlight, RecordingDeliver, run_due_jobs, run_manual_job
from tstd.scheduler.store import get_job, jobs_path, list_jobs, save_job

_NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_NEXT_HOUR = "2026-08-21T19:00:00+00:00"
# Friday 7:45 AM CDT. Ten hours later is 5:45 PM, still Friday.
_SLOT = "2026-08-21T12:45:00+00:00"
_MORNING_NOW = datetime(2026, 8, 21, 22, 45, tzinfo=UTC)
_MONDAY = "2026-08-24T12:45:00+00:00"
_LINE = "Skipped the 7:45 AM run \u2014 10 h late"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _job(workspace: Path, **over: object) -> Job:
    data: dict[str, object] = {
        "id": "inbox",
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": _NOW.isoformat(),
        "deliver_to": "window",
    }
    data.update(over)
    return Job.model_validate(data)


def _daemon(tmp_path: Path) -> tuple[Daemon, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    daemon = Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content="digest ready")),
    )
    daemon.config = make_config()
    return daemon, data_dir, workspace


async def _message(daemon: Daemon, payload: dict[str, object]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    body: dict[str, Any] = json.loads(raw)
    return body


def _create(workspace: Path, **over: object) -> dict[str, object]:
    data: dict[str, object] = {
        "type": "save_job",
        "id": "inbox",
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "deliver_to": "window",
    }
    data.update(over)
    return data


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("2 hours", 7200),
        ("2 Hours", 7200),
        ("every 2 hours", 7200),
        ("120 minutes", 7200),
        ("30 minute", 1800),
        ("30 minutes", 1800),
        ("1 hour", 3600),
        ("1 hours", 3600),
        ("  2   hours  ", 7200),
        ("7200", 7200),
        (7200, 7200),
        ("1 day", 86400),
        ("366 days", 366 * 86400),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_parse_grace(value: object, seconds: int | None) -> None:
    assert parse_grace(value) == seconds


@pytest.mark.parametrize(
    ("value", "match", "hidden"),
    [
        (True, "duration", None),
        (False, "duration", None),
        (0, "longer than nothing", None),
        (-5, "longer than nothing", None),
        ("0", "longer than nothing", None),
        ("2.5 hours", "not understood", None),
        ("30 min", "not understood", None),
        ("an hour", "not understood", None),
        ("soon", "not understood", None),
        (367 * 86400, "cannot be longer than 366 days", str(367 * 86400)),
        (10**12, "cannot be longer than 366 days", str(10**12)),
        ("20000 hours", "cannot be longer than 366 days", "20000"),
    ],
)
def test_parse_grace_rejects(value: object, match: str, hidden: str | None) -> None:
    with pytest.raises(GraceError, match=match) as caught:
        parse_grace(value)
    if hidden is not None:
        assert hidden not in str(caught.value)


def test_parse_grace_refuses_a_secret_without_echoing_it() -> None:
    key = "sk-" + "a" * 40  # tst-secret-ok
    with pytest.raises(GraceError, match="must not contain secrets") as caught:
        parse_grace(key)
    assert key not in str(caught.value)


def test_an_unparsed_grace_is_cut_before_it_fills_the_error() -> None:
    text = "x" * 80
    with pytest.raises(GraceError, match="not understood") as caught:
        parse_grace(text)
    assert "x" * 41 not in str(caught.value)
    assert "\u2026" in str(caught.value)


def test_a_draft_stores_the_phrase_as_seconds(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    job = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            grace="2 hours",
        )
    )
    assert job.grace == 7200
    dumped = job.model_dump()
    assert dumped["grace"] == 7200
    assert Job.model_validate(dumped).grace == 7200
    blank = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            grace="",
        )
    )
    assert blank.grace is None


def test_a_boolean_grace_is_not_one_second(tmp_path: Path) -> None:
    """``True`` is an ``int``. Without the bool check it would store 1."""
    workspace = _workspace(tmp_path)
    raw = _job(workspace).model_dump()
    raw["grace"] = True
    with pytest.raises(ValidationError):
        Job.model_validate(raw)


def test_equal_lateness_is_not_past_grace() -> None:
    slot = _NOW - timedelta(hours=2)
    assert past_grace(slot, 7200, _NOW) is False
    assert past_grace(slot, 7199, _NOW) is True
    assert past_grace(slot, None, _NOW) is False
    assert past_grace(None, 7200, _NOW) is False


@pytest.mark.parametrize(
    ("slot", "zone", "late", "line"),
    [
        (
            datetime(2026, 8, 21, 7, 45, tzinfo=UTC),
            None,
            timedelta(hours=10),
            "Skipped the 7:45 AM run \u2014 10 h late",
        ),
        (
            datetime(2026, 8, 21, 12, 45, tzinfo=UTC),
            "America/Chicago",
            timedelta(hours=10),
            "Skipped the 7:45 AM run \u2014 10 h late",
        ),
        (
            datetime(2026, 8, 21, 7, 45, tzinfo=UTC),
            None,
            timedelta(hours=2, seconds=1),
            "Skipped the 7:45 AM run \u2014 2 h 1 min late",
        ),
        (
            datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
            None,
            timedelta(minutes=35),
            "Skipped the 12:00 AM run \u2014 35 min late",
        ),
        (
            datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
            None,
            timedelta(seconds=1),
            "Skipped the 12:00 PM run \u2014 1 min late",
        ),
    ],
)
def test_skip_line(slot: datetime, zone: str | None, late: timedelta, line: str) -> None:
    assert skip_line(slot, zone, slot + late) == line
    if zone == "America/Chicago":
        assert "12:45" not in line


@pytest.mark.parametrize(
    ("late", "grace", "fires"),
    [
        (timedelta(minutes=30), 3600, True),
        (timedelta(hours=1), 3600, True),
        (timedelta(hours=1, seconds=1), 3600, False),
        (timedelta(hours=10), None, True),
        (timedelta(0), 7200, True),
        (timedelta(hours=2, seconds=1), "2 hours", False),
        (timedelta(hours=2), 7200, True),
    ],
)
async def test_lateness_against_grace(
    tmp_path: Path,
    late: timedelta,
    grace: int | str | None,
    fires: bool,
) -> None:
    workspace = _workspace(tmp_path)
    over: dict[str, object] = {"next_run": (_NOW - late).isoformat()}
    if grace is not None:
        over["grace"] = grace
    save_job(tmp_path, _job(workspace, **over))
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "ran"

    deliver = RecordingDeliver()
    armed = list_jobs(tmp_path)[0].next_run
    assert await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=deliver) == ["inbox"]
    stored = list_jobs(tmp_path)[0]
    expect = 7200 if grace == "2 hours" else grace
    assert stored.grace == expect
    assert stored.paused is False
    assert stored.next_run == _NEXT_HOUR
    runs = list_runs(tmp_path, "inbox")
    assert len(runs) == 1
    assert runs[0].trigger == "schedule"
    assert runs[0].scheduled_for == armed
    assert runs[0].session_id is None
    if fires:
        assert calls == ["summarize the inbox"]
        assert stored.last_status == "ok"
        assert stored.last_summary == "ran"
        assert runs[0].status == "ok"
        assert deliver.records == [("window", "ran")]
        return
    assert calls == []
    assert stored.last_status == "missed"
    assert stored.last_session_id is None
    assert stored.last_summary is not None and stored.last_summary.startswith("Skipped the ")
    assert runs[0].status == "missed"
    assert runs[0].summary == stored.last_summary
    assert deliver.records == [("window", stored.last_summary)]


async def test_a_job_file_without_grace_still_runs_when_late(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    raw = _job(workspace, next_run=(_NOW - timedelta(hours=10)).isoformat()).model_dump()
    raw.pop("grace")
    path = jobs_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "jobs": [raw]}), encoding="utf-8")
    assert list_jobs(tmp_path)[0].grace is None
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "ran"

    ran = await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver())
    assert ran == ["inbox"]
    stored = list_jobs(tmp_path)[0]
    assert calls == ["summarize the inbox"]
    assert stored.grace is None
    assert stored.last_status == "ok"


async def test_a_phrase_on_disk_is_seconds(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    raw = _job(workspace).model_dump()
    raw["grace"] = "2 hours"
    path = jobs_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "jobs": [raw]}), encoding="utf-8")
    loaded = list_jobs(tmp_path)[0]
    assert loaded.grace == 7200
    assert loaded.model_dump()["grace"] == 7200


async def test_a_missed_one_shot_is_spent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    past = (_NOW - timedelta(hours=10)).isoformat()
    save_job(tmp_path, _job(workspace, cadence=None, next_run=past, grace="30 minutes"))
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "ran"

    ran = await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver())
    assert ran == ["inbox"]
    stored = list_jobs(tmp_path)[0]
    assert calls == []
    assert stored.paused is True
    assert stored.next_run is None
    assert stored.grace == 1800
    assert stored.last_status == "missed"
    assert list_runs(tmp_path, "inbox")[0].trigger == "schedule"
    save_job(tmp_path, stored.model_copy(update={"paused": False}))
    assert await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver()) == []
    resumed = list_jobs(tmp_path)[0]
    assert resumed.paused is False
    assert resumed.next_run is None
    assert resumed.last_status == "missed"
    assert resumed.grace == 1800
    assert calls == []


async def test_run_now_fires_a_slot_that_is_past_grace(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    past = (_NOW - timedelta(hours=10)).isoformat()
    job = _job(workspace, next_run=past, grace=1800)
    save_job(tmp_path, job)
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "by hand"

    deliver = RecordingDeliver()
    await run_manual_job(tmp_path, job, _NOW, run_turn=turn, deliver=deliver)
    stored = list_jobs(tmp_path)[0]
    assert calls == ["summarize the inbox"]
    assert stored.last_status == "ok"
    assert stored.last_summary == "by hand"
    assert stored.next_run == past
    assert stored.paused is False
    assert stored.grace == 1800
    assert deliver.records == [("window", "by hand")]
    assert list_runs(tmp_path, "inbox")[0].trigger == "manual"
    assert list_runs(tmp_path, "inbox")[0].status == "ok"


async def test_an_in_flight_late_job_stays_due(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    past = (_NOW - timedelta(hours=10)).isoformat()
    save_job(tmp_path, _job(workspace, next_run=past, grace=1800))
    guard = InFlight()
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "ran"

    assert guard.claim("inbox")
    deliver = RecordingDeliver()
    held = await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=deliver, in_flight=guard)
    assert held == []
    assert calls == []
    assert list_jobs(tmp_path)[0].next_run == past
    assert list_jobs(tmp_path)[0].last_run is None
    guard.release("inbox")
    later = await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=deliver, in_flight=guard)
    assert later == ["inbox"]
    assert calls == []
    assert list_jobs(tmp_path)[0].last_status == "missed"


async def test_save_keeps_clears_and_sets_grace(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    try:
        plain = await _message(daemon, _create(workspace, id="plain"))
        assert plain["type"] == "job_list"
        plain_job = get_job(data, "plain")
        assert plain_job is not None and plain_job.grace is None

        created = await _message(daemon, _create(workspace, grace="2 hours"))
        assert next(row["grace"] for row in created["jobs"] if row["id"] == "inbox") == 7200
        await _message(daemon, _create(workspace))
        inbox = get_job(data, "inbox")
        assert inbox is not None and inbox.grace == 7200

        cleared = await _message(daemon, _create(workspace, grace=""))
        inbox = next(row for row in cleared["jobs"] if row["id"] == "inbox")
        assert inbox["grace"] is None

        phrased = await _message(daemon, _create(workspace, grace="30 minutes"))
        assert next(row["grace"] for row in phrased["jobs"] if row["id"] == "inbox") == 1800
        numbered = await _message(daemon, _create(workspace, grace=21600))
        assert next(row["grace"] for row in numbered["jobs"] if row["id"] == "inbox") == 21600

        current = get_job(data, "inbox")
        assert current is not None
        save_job(
            data,
            current.model_copy(
                update={
                    "last_status": "ok",
                    "last_summary": "the receipt",
                    "last_run": _NOW.isoformat(),
                }
            ),
        )
        paused = await _message(
            daemon,
            {
                "type": "save_job",
                "id": "inbox",
                "workspace": str(workspace),
                "instruction": "summarize the inbox",
                "cadence": "every 1 hour",
                "deliver_to": "window",
                "paused": True,
            },
        )
        row = next(item for item in paused["jobs"] if item["id"] == "inbox")
        assert row["paused"] is True
        assert row["grace"] == 21600
        assert row["last_status"] == "ok"
        assert row["last_summary"] == "the receipt"

        bad = await _message(daemon, _create(workspace, grace="soon"))
        assert bad["type"] == "error"
        assert bad["code"] == "job_invalid"
        assert "If late" in bad["message"]
        still = get_job(data, "inbox")
        assert still is not None and still.grace == 21600
        assert still.paused is True
        assert still.last_summary == "the receipt"

        key = "sk-" + "a" * 40  # tst-secret-ok
        secret = await _message(daemon, _create(workspace, id="secret", grace=key))
        assert secret["code"] == "job_invalid"
        assert key not in secret["message"]
        assert "must not contain secrets" in secret["message"]
        assert get_job(data, "secret") is None

        huge = await _message(daemon, _create(workspace, id="huge", grace="20000 hours"))
        assert huge["code"] == "job_invalid"
        assert "20000" not in huge["message"]
        assert "366" in huge["message"]
        assert get_job(data, "huge") is None
    finally:
        await daemon._shutdown()


async def test_clearing_grace_lets_a_late_slot_run(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    past = (_NOW - timedelta(hours=10)).isoformat()
    try:
        await _message(daemon, _create(workspace, grace="2 hours"))
        edited = await _message(daemon, _create(workspace, grace="", next_run=past))
        row = next(item for item in edited["jobs"] if item["id"] == "inbox")
        assert row["grace"] is None
        assert row["next_run"] == past
        assert await daemon.run_due_jobs(_NOW) == ["inbox"]
        stored = get_job(data, "inbox")
        assert stored is not None
        assert stored.last_status == "ok"
        assert stored.last_summary == "digest ready"
        assert stored.grace is None
        assert daemon.session_registry.count == 1
    finally:
        await daemon._shutdown()


async def test_a_saved_grace_skips_the_late_slot(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    past = (_NOW - timedelta(hours=2)).isoformat()
    try:
        await _message(daemon, _create(workspace, grace="30 minutes"))
        await _message(daemon, _create(workspace, next_run=past))
        assert get_job(data, "inbox") is not None
        stored_before = get_job(data, "inbox")
        assert stored_before is not None and stored_before.grace == 1800
        assert await daemon.run_due_jobs(_NOW) == ["inbox"]
        stored = get_job(data, "inbox")
        assert stored is not None
        assert stored.last_status == "missed"
        assert stored.last_session_id is None
        assert stored.grace == 1800
        assert daemon.session_registry.count == 0
        assert stored.last_summary is not None
        assert daemon._scheduler_deliver.records == [("window", stored.last_summary)]
    finally:
        await daemon._shutdown()


async def test_a_slept_through_digest_is_skipped_not_run(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    save_job(
        data,
        _job(
            workspace,
            cadence="45 7 * * 1-5",
            next_run=_SLOT,
            timezone="America/Chicago",
            grace=7200,
        ),
    )
    sent: list[str] = []

    async def capture(payload: str) -> int:
        sent.append(payload)
        return 1

    daemon.ws_server.broadcast = capture  # type: ignore[method-assign]
    try:
        assert await daemon.run_due_jobs(_MORNING_NOW) == ["inbox"]
        stored = get_job(data, "inbox")
        assert stored is not None
        assert stored.last_status == "missed"
        assert stored.last_summary == _LINE
        assert stored.last_session_id is None
        assert stored.next_run == _MONDAY
        assert stored.grace == 7200
        assert stored.paused is False
        assert daemon.session_registry.count == 0
        assert daemon._scheduler_deliver.records == [("window", _LINE)]
        runs = list_runs(data, "inbox")
        assert len(runs) == 1
        assert runs[0].status == "missed"
        assert runs[0].trigger == "schedule"
        assert runs[0].scheduled_for == _SLOT
        assert runs[0].summary == _LINE
        assert runs[0].session_id is None
        assert len(sent) == 1
        pushed = json.loads(sent[0])
        assert pushed["type"] == "job_list"
        row = pushed["jobs"][0]
        assert row["grace"] == 7200
        assert row["last_status"] == "missed"
        assert row["last_summary"] == _LINE
        assert row["next_run"] == _MONDAY
        assert row["last_session_id"] is None
    finally:
        await daemon._shutdown()


async def test_run_now_ignores_grace(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    past = (_NOW - timedelta(hours=10)).isoformat()
    save_job(data, _job(workspace, next_run=past, grace=1800))
    try:
        body = await _message(daemon, {"type": "run_job", "job_id": "inbox"})
        assert body["type"] == "job_list"
        await asyncio.wait_for(daemon._tasks[-1], timeout=10)
        stored = get_job(data, "inbox")
        assert stored is not None
        assert stored.last_status == "ok"
        assert stored.last_summary == "digest ready"
        assert stored.next_run == past
        assert stored.grace == 1800
        assert stored.paused is False
        assert daemon.session_registry.count == 1
    finally:
        await daemon._shutdown()
