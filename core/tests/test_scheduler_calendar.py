"""Skip a regular slot when a local calendar blocks that day (TD-3818).

The file is on disk. A matching event advances the slot, records
``skipped``, and delivers nothing. Run now, a retry, and a chained fire
do not consult it. An unreadable file still runs.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import JobRunEntry
from tstd.scheduler import calendar as calendar_mod
from tstd.scheduler.calendar import begin_tick, load_calendar, note_unreadable
from tstd.scheduler.edit import apply_job_edit
from tstd.scheduler.history import list_runs
from tstd.scheduler.ics import parse_ics, slot_blocked
from tstd.scheduler.models import Job, JobDraft, JobValidationError, validate_draft
from tstd.scheduler.runner import RecordingDeliver, run_due_jobs, run_manual_job
from tstd.scheduler.schedule import next_run_after, normalize_next_run
from tstd.scheduler.store import get_job, save_job

_TITLE = "ZQ-Holiday-Title"
_NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
_SLOT = _NOW.isoformat()
_NOTE = "calendar unreadable"

_HOLIDAY = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:20001225
DTEND;VALUE=DATE:20001226
SUMMARY:ZQ-Holiday-Title
RRULE:FREQ=YEARLY
END:VEVENT
END:VCALENDAR
"""

_PTO = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/Chicago:20260902T090000
DTEND;TZID=America/Chicago:20260902T170000
SUMMARY:PTO Wednesday
RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261001T000000Z
END:VEVENT
END:VCALENDAR
"""

_EMPTY = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"


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
        "next_run": _SLOT,
        "deliver_to": "window",
    }
    data.update(over)
    return Job.model_validate(data)


def _write(tmp_path: Path, text: str, name: str = "holidays.ics") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _norm(path: Path) -> str:
    return os.path.normpath(str(path))


def _blocked(text: str, slot: str, tz: str | None, match: str | None) -> bool:
    parsed = parse_ics(text)
    assert parsed.unreadable is False
    when = datetime.fromisoformat(slot)
    return slot_blocked(parsed, when, tz, match)


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
    ("text", "slot", "tz", "match", "blocked"),
    [
        (_HOLIDAY, "2026-12-25T18:00:00+00:00", "America/Chicago", None, True),
        (_HOLIDAY, "2026-12-26T05:30:00+00:00", "America/Chicago", None, True),
        (_HOLIDAY, "2026-12-26T06:00:00+00:00", "America/Chicago", None, False),
        (_HOLIDAY, "2026-07-04T15:00:00+00:00", "America/Chicago", None, False),
        (_PTO, "2026-09-30T15:00:00+00:00", "America/Chicago", "holiday|PTO|OOO", True),
        (_PTO, "2026-09-30T22:00:00+00:00", "America/Chicago", "holiday|PTO|OOO", False),
        (_PTO, "2026-10-07T15:00:00+00:00", "America/Chicago", "holiday|PTO|OOO", False),
        (
            _PTO.replace("PTO Wednesday", "Team meeting"),
            "2026-09-30T15:00:00+00:00",
            "America/Chicago",
            "holiday|PTO|OOO",
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260928\nDTEND;VALUE=DATE:20260929\n"
            "SUMMARY:Company Holiday\nRRULE:FREQ=DAILY;COUNT=3\n"
            "END:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T12:00:00+00:00",
            "UTC",
            "holiday",
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260928\nDTEND;VALUE=DATE:20260929\n"
            "SUMMARY:Company Holiday\nRRULE:FREQ=DAILY;COUNT=3\n"
            "END:VEVENT\nEND:VCALENDAR\n",
            "2026-10-01T12:00:00+00:00",
            "UTC",
            None,
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART:20260930T090000\nDTEND:20260930T100000\n"
            "SUMMARY:standup\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T14:30:00+00:00",
            "America/Chicago",
            None,
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART:20260930T090000\nDTEND:20260930T100000\n"
            "SUMMARY:standup\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T15:00:00+00:00",
            "America/Chicago",
            None,
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            'DTSTART;TZID="America/New_York":20260930T090000\n'
            'DTEND;TZID="America/New_York":20260930T100000\n'
            "SUMMARY:east\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T13:30:00+00:00",
            "America/Chicago",
            None,
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;TZID=SomeApp/America/Chicago:20260930T090000\n"
            "DTEND;TZID=SomeApp/America/Chicago:20260930T100000\n"
            "SUMMARY:path\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T14:30:00+00:00",
            "UTC",
            None,
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART:20261225T220000\nDTEND:20261226T020000\n"
            "SUMMARY:late\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-12-26T07:30:00+00:00",
            "America/Chicago",
            None,
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART:20261225T220000\nDTEND:20261226T020000\n"
            "SUMMARY:late\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-12-26T08:00:00+00:00",
            "America/Chicago",
            None,
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20201224\nDTEND;VALUE=DATE:20201226\n"
            "RRULE:FREQ=YEARLY\nSUMMARY:eve\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-12-25T18:00:00+00:00",
            "America/Chicago",
            None,
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20201224\nDTEND;VALUE=DATE:20201226\n"
            "RRULE:FREQ=YEARLY\nSUMMARY:eve\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-12-26T18:00:00+00:00",
            "America/Chicago",
            None,
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20001225\nDTEND;VALUE=DATE:20001226\n"
            "RRULE:FREQ=YEARLY;COUNT=1\nSUMMARY:once\n"
            "END:VEVENT\nEND:VCALENDAR\n",
            "2026-12-25T12:00:00+00:00",
            "UTC",
            None,
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260930\nDTEND;VALUE=DATE:20261001\n"
            "SUMMARY:out of office\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T12:00:00+00:00",
            "UTC",
            "holiday|PTO|OOO",
            False,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260930\nDTEND;VALUE=DATE:20261001\n"
            "SUMMARY:Holiday\nBEGIN:VALARM\nSUMMARY:ZQ-Alarm-Title\n"
            "END:VALARM\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-09-30T12:00:00+00:00",
            "UTC",
            "ZQ-Alarm",
            False,
        ),
        (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260930\r\nDTEND;VALUE=DATE:20261001\r\n"
            "SUMMARY:Company Hol\r\n iday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            "2026-09-30T12:00:00+00:00",
            "UTC",
            "holiday",
            True,
        ),
        (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260930\nDTEND;VALUE=DATE:20261001\n"
            "RRULE:FREQ=MONTHLY\nSUMMARY:month\nEND:VEVENT\nEND:VCALENDAR\n",
            "2026-10-30T12:00:00+00:00",
            "UTC",
            None,
            False,
        ),
    ],
)
def test_fixture_events_block_only_the_matching_slot(
    text: str,
    slot: str,
    tz: str | None,
    match: str | None,
    blocked: bool,
) -> None:
    assert _blocked(text, slot, tz, match) is blocked


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20260930\n",
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:no start\nEND:VEVENT\nEND:VCALENDAR\n",
    ],
)
def test_unparseable_text_is_unreadable(text: str) -> None:
    assert parse_ics(text).unreadable is True
    assert parse_ics(text).events == ()


def test_an_empty_calendar_is_readable_and_blocks_nothing() -> None:
    parsed = parse_ics(_EMPTY)
    assert parsed.unreadable is False
    assert slot_blocked(parsed, _NOW, "UTC", None) is False


def test_a_good_event_survives_a_trailing_unclosed_one() -> None:
    text = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20260930\nDTEND;VALUE=DATE:20261001\n"
        "SUMMARY:Holiday\nEND:VEVENT\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261001\n"
    )
    assert _blocked(text, "2026-09-30T12:00:00+00:00", "UTC", None) is True


def test_parsing_does_not_open_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("calendar parse opened a connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    text = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "DTSTART;VALUE=DATE:20260930\nDTEND;VALUE=DATE:20261001\n"
        "SUMMARY:Holiday\n"
        "URL:https://example.test/cal\n"
        "ATTACH:https://example.test/file.ics\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    assert _blocked(text, "2026-09-30T12:00:00+00:00", "UTC", None) is True


def test_calendar_modules_do_not_name_a_client() -> None:
    root = Path(__file__).resolve().parents[1] / "tstd" / "scheduler"
    banned = ("httpx", "urllib", "requests", "socket", "http.client", "aiohttp")
    names = ("ics.py", "ics_lines.py", "ics_recur.py", "calendar.py")
    for name in names:
        text = (root / name).read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, name


def test_note_reserves_room_and_is_not_repeated() -> None:
    noted = note_unreadable("digest ready")
    assert noted == "digest ready \u2014 calendar unreadable"
    assert note_unreadable(noted) == noted
    assert note_unreadable("Calendar Unreadable already") == "Calendar Unreadable already"
    huge = note_unreadable("x" * 5000)
    assert len(huge) <= 2000
    assert huge.casefold().count(_NOTE) == 1
    assert huge.endswith(_NOTE)


def test_an_old_job_loads_without_calendar_fields(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    dumped = _job(workspace).model_dump()
    dumped.pop("skip_calendar")
    dumped.pop("skip_match")
    loaded = Job.model_validate(dumped)
    assert loaded.skip_calendar is None
    assert loaded.skip_match is None


def test_match_list_is_stored_normalized(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    job = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            skip_match=" holiday | PTO | ",
        )
    )
    assert job.skip_match == "holiday|PTO"
    blank = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            skip_match=" | ",
        )
    )
    assert blank.skip_match is None


def test_a_secret_in_the_path_is_not_echoed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    key = "sk-" + "a" * 20  # tst-secret-ok
    with pytest.raises(JobValidationError, match="must not contain secrets") as caught:
        validate_draft(
            JobDraft(
                workspace=str(workspace),
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                skip_calendar=f"/tmp/{key}.ics",
            )
        )
    assert key not in str(caught.value)
    assert "Skip calendar" in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("holidays.ics", "full file path"),
        ("https://example.test/h.ics", "filesystem path"),
        ("/tmp/holidays.txt", ".ics file"),
        ("/tmp/not-a-dir/", ".ics file"),
    ],
)
def test_a_bad_calendar_path_is_refused(tmp_path: Path, raw: str, match: str) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(JobValidationError, match=match):
        validate_draft(
            JobDraft(
                workspace=str(workspace),
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                skip_calendar=raw,
            )
        )


def test_a_symlink_is_stored_as_given(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    target = _write(tmp_path, _EMPTY, "real.ics")
    link = tmp_path / "link.ics"
    link.symlink_to(target)
    job = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            skip_calendar=str(link),
        )
    )
    assert job.skip_calendar == os.path.normpath(str(link))


def test_cache_reads_once_per_tick_and_again_when_mtime_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path, _HOLIDAY)
    reads = {"n": 0}
    real = calendar_mod._read_text

    def counting(file: Path) -> str:
        reads["n"] += 1
        return real(file)

    monkeypatch.setattr(calendar_mod, "_read_text", counting)
    begin_tick()
    first = load_calendar(str(path))
    path.write_text(_EMPTY, encoding="utf-8")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    second = load_calendar(str(path))
    assert reads["n"] == 1
    assert second.events == first.events
    assert slot_blocked(second, datetime(2026, 12, 25, 18, tzinfo=UTC), "UTC", None)
    begin_tick()
    third = load_calendar(str(path))
    assert reads["n"] == 2
    assert third.unreadable is False
    assert third.events == ()


def test_a_missing_file_is_reread_on_the_next_tick(tmp_path: Path) -> None:
    path = tmp_path / "later.ics"
    begin_tick()
    assert load_calendar(str(path)).unreadable is True
    path.write_text(_EMPTY, encoding="utf-8")
    assert load_calendar(str(path)).unreadable is True
    begin_tick()
    assert load_calendar(str(path)).unreadable is False


def test_an_oversized_file_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write(tmp_path, _EMPTY)
    reads = {"n": 0}

    def counting(file: Path) -> str:
        reads["n"] += 1
        return file.read_text(encoding="utf-8")

    monkeypatch.setattr(calendar_mod, "_read_text", counting)
    monkeypatch.setattr(calendar_mod, "_MAX_BYTES", 8)
    begin_tick()
    assert load_calendar(str(path)).unreadable is True
    assert reads["n"] == 0


async def _turn_ok(_workspace: Path, _message: str) -> str:
    return "digest ready"


async def test_a_blocked_day_is_skipped_and_not_delivered(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    save_job(tmp_path, _job(workspace, skip_calendar=str(path), timezone="UTC"))
    deliver = RecordingDeliver()
    with caplog.at_level(logging.INFO):
        ran = await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=deliver)
    assert ran == ["inbox"]
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "skipped"
    assert stored.last_summary == "calendar"
    assert stored.last_session_id is None
    expected = normalize_next_run(next_run_after("every 1 hour", _NOW, None).isoformat())
    assert stored.next_run == expected
    assert deliver.records == []
    runs = list_runs(tmp_path, "inbox")
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert runs[0].summary == "calendar"
    assert runs[0].trigger == "schedule"
    assert _TITLE not in caplog.text
    assert _TITLE not in (stored.last_summary or "")


async def test_two_jobs_share_one_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    save_job(tmp_path, _job(workspace, id="one", skip_calendar=str(path)))
    save_job(tmp_path, _job(workspace, id="two", skip_calendar=str(path)))
    reads = {"n": 0}
    real = calendar_mod._read_text

    def counting(file: Path) -> str:
        reads["n"] += 1
        return real(file)

    monkeypatch.setattr(calendar_mod, "_read_text", counting)
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "digest ready"

    await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver())
    assert calls == []
    assert reads["n"] == 1


async def test_a_one_shot_on_a_blocked_day_is_spent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    save_job(
        tmp_path,
        _job(workspace, cadence=None, next_run=_SLOT, skip_calendar=str(path)),
    )
    deliver = RecordingDeliver()
    await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=deliver)
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "skipped"
    assert stored.paused is True
    assert stored.next_run is None
    assert deliver.records == []


async def test_a_blocked_parent_does_not_start_its_child(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    later = (_NOW + timedelta(days=2)).isoformat()
    save_job(tmp_path, _job(workspace, id="parent", then="child", skip_calendar=str(path)))
    save_job(tmp_path, _job(workspace, id="child", next_run=later))
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "digest ready"

    ran = await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver())
    assert ran == ["parent"]
    assert calls == []
    child = get_job(tmp_path, "child")
    assert child is not None
    assert child.last_run is None
    assert child.next_run == later
    assert list_runs(tmp_path, "child") == []


async def test_run_now_ignores_the_calendar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    job = _job(workspace, skip_calendar=str(path))
    save_job(tmp_path, job)

    def boom(_path: str) -> None:
        raise AssertionError("run now read the calendar")

    monkeypatch.setattr(calendar_mod, "load_calendar", boom)
    deliver = RecordingDeliver()
    await run_manual_job(tmp_path, job, _NOW, run_turn=_turn_ok, deliver=deliver)
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_summary == "digest ready"
    assert stored.next_run == _SLOT
    assert deliver.records == [("window", "digest ready")]


async def test_a_retry_ignores_the_calendar(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    resume = (_NOW + timedelta(days=1)).isoformat()
    save_job(
        tmp_path,
        _job(
            workspace,
            skip_calendar=str(path),
            retries=1,
            attempt=1,
            resume_at=resume,
            next_run=_SLOT,
        ),
    )
    calls: list[str] = []

    async def turn(_workspace: Path, message: str) -> str:
        calls.append(message)
        return "digest ready"

    await run_due_jobs(tmp_path, _NOW, run_turn=turn, deliver=RecordingDeliver())
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert calls == ["summarize the inbox"]
    assert stored.last_status == "ok"
    assert stored.attempt == 0
    assert stored.next_run == resume


async def test_grace_wins_over_a_blocked_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    late = _NOW - timedelta(hours=10)
    path = _write(tmp_path, _all_day_for(late))
    save_job(
        tmp_path,
        _job(workspace, next_run=late.isoformat(), grace=1800, skip_calendar=str(path)),
    )
    reads = {"n": 0}

    def counting(file: Path) -> str:
        reads["n"] += 1
        return file.read_text(encoding="utf-8")

    monkeypatch.setattr(calendar_mod, "_read_text", counting)
    deliver = RecordingDeliver()
    await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=deliver)
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "missed"
    assert reads["n"] == 0
    assert deliver.records != []


async def test_an_unreadable_file_still_runs_and_notes_it_once(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, "this is not a calendar")
    save_job(tmp_path, _job(workspace, skip_calendar=str(path)))
    deliver = RecordingDeliver()
    await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=deliver)
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_summary == f"digest ready \u2014 {_NOTE}"
    assert stored.last_summary is not None
    assert stored.last_summary.casefold().count(_NOTE) == 1
    assert deliver.records == [("window", stored.last_summary)]
    assert list_runs(tmp_path, "inbox")[0].summary == stored.last_summary


async def test_an_empty_calendar_runs_without_the_note(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _EMPTY)
    save_job(tmp_path, _job(workspace, skip_calendar=str(path)))
    deliver = RecordingDeliver()
    await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=deliver)
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_summary == "digest ready"
    assert deliver.records == [("window", "digest ready")]


async def test_a_file_removed_after_save_is_unreadable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    save_job(tmp_path, _job(workspace, skip_calendar=str(path)))
    path.unlink()
    await run_due_jobs(tmp_path, _NOW, run_turn=_turn_ok, deliver=RecordingDeliver())
    stored = get_job(tmp_path, "inbox")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_summary is not None
    assert _NOTE in stored.last_summary


def test_pause_keeps_a_calendar_whose_file_is_gone(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _EMPTY)
    job = _job(workspace, skip_calendar=str(path), skip_match="PTO")
    path.unlink()
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
    assert edited.skip_calendar == os.path.normpath(str(path))
    assert edited.skip_match == "PTO"


def test_an_edit_to_a_missing_file_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _EMPTY)
    job = _job(workspace, skip_calendar=str(path))
    missing = tmp_path / "other.ics"
    with pytest.raises(JobValidationError, match="file not found") as caught:
        apply_job_edit(
            job,
            workspace=None,
            instruction=None,
            cadence=None,
            next_run=None,
            deliver_to=None,
            paused=False,
            timezone=None,
            known_workspaces=[],
            preset=None,
            engine=None,
            known_presets={},
            grace=None,
            retries=None,
            retry_delay=None,
            skip_calendar=str(missing),
        )
    assert "Skip calendar" in str(caught.value)
    assert str(caught.value).count("Skip calendar") == 1


def test_clearing_the_calendar_on_edit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    path = _write(tmp_path, _EMPTY)
    job = _job(workspace, skip_calendar=str(path), skip_match="PTO")
    edited = apply_job_edit(
        job,
        workspace=None,
        instruction=None,
        cadence=None,
        next_run=None,
        deliver_to=None,
        paused=False,
        timezone=None,
        known_workspaces=[],
        preset=None,
        engine=None,
        known_presets={},
        grace=None,
        retries=None,
        retry_delay=None,
        skip_calendar="",
        skip_match="",
    )
    assert edited.skip_calendar is None
    assert edited.skip_match is None


def test_skipped_is_a_history_status() -> None:
    entry = JobRunEntry(
        started_at="2026-12-25T15:00:00+00:00",
        scheduled_for="2026-12-25T13:00:00+00:00",
        trigger="schedule",
        status="skipped",
        summary="calendar",
    )
    assert entry.status == "skipped"
    assert entry.summary == "calendar"


async def test_save_requires_the_file_and_the_list_shows_a_skip(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    path = _write(tmp_path, _all_day_for(_NOW))
    sent: list[str] = []

    async def capture(payload: str) -> int:
        sent.append(payload)
        return 1

    daemon.ws_server.broadcast = capture  # type: ignore[method-assign]
    try:
        missing_path = str(tmp_path / "nope.ics")
        missing = await _message(daemon, _create(workspace, skip_calendar=missing_path))
        assert missing["code"] == "job_invalid"
        assert "file not found" in missing["message"]
        assert get_job(data, "inbox") is None

        created = await _message(
            daemon,
            _create(workspace, skip_calendar=str(path), skip_match=" holiday | PTO "),
        )
        row = next(item for item in created["jobs"] if item["id"] == "inbox")
        assert row["skip_calendar"] == _norm(path)
        assert row["skip_match"] == "holiday|PTO"

        paused = await _message(daemon, {"type": "save_job", "id": "inbox", "paused": True})
        held = next(item for item in paused["jobs"] if item["id"] == "inbox")
        assert held["paused"] is True
        assert held["skip_calendar"] == _norm(path)

        await _message(daemon, {"type": "save_job", "id": "inbox", "paused": False})
        # The armed slot is in the future until we stamp the holiday instant.
        armed = get_job(data, "inbox")
        assert armed is not None
        save_job(data, armed.model_copy(update={"next_run": _SLOT}))
        assert await daemon.run_due_jobs(_NOW) == ["inbox"]
        stored = get_job(data, "inbox")
        assert stored is not None
        assert stored.last_status == "skipped"
        assert stored.last_summary == "calendar"
        assert daemon.session_registry.count == 0
        assert daemon._scheduler_deliver.records == []
        pushed = json.loads(sent[-1])
        assert pushed["type"] == "job_list"
        wire = pushed["jobs"][0]
        assert wire["last_status"] == "skipped"
        assert wire["last_summary"] == "calendar"
        assert _TITLE not in sent[-1]
    finally:
        await daemon._shutdown()


def _all_day_for(when: datetime) -> str:
    day = when.astimezone(UTC).date()
    start = day.strftime("%Y%m%d")
    end = (day + timedelta(days=1)).strftime("%Y%m%d")
    return (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        f"DTSTART;VALUE=DATE:{start}\nDTEND;VALUE=DATE:{end}\n"
        f"SUMMARY:{_TITLE}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
