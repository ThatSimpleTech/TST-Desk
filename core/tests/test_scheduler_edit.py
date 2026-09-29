"""Edit a scheduled job in place (TD-3810). The receipt and the id stay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tstd.daemon import Daemon
from tstd.protocol import JobEntry, JobList, parse_daemon_event
from tstd.scheduler.models import Job
from tstd.scheduler.store import list_jobs, save_job

_ARMED = "2026-08-21T18:00:00+00:00"
_ONE_SHOT = "2026-09-30T15:00:00+00:00"
_RECEIPT = "2026-08-21T15:00:00+00:00"
_REQUIRED = "Cadence or next run is required"

# (id, patch, expected fields or the error sentence). The base row is a
# recurring job with an armed slot and a receipt. Omitted schedule fields
# keep; "" clears.
_PATCHES: list[tuple[str, dict[str, Any], dict[str, Any] | str]] = [
    (
        "keep",
        {"instruction": "count the mail"},
        {"instruction": "count the mail", "cadence": "every 1 hour", "next_run": _ARMED},
    ),
    (
        "same-cadence",
        {"cadence": "every 1 hour"},
        {"cadence": "every 1 hour", "next_run": _ARMED},
    ),
    (
        "rearm",
        {"cadence": "every 2 hours"},
        {"cadence": "every 2 hours", "next_run": None},
    ),
    (
        "to-once",
        {"cadence": "", "next_run": _ONE_SHOT},
        {"cadence": None, "next_run": _ONE_SHOT},
    ),
    (
        "pause",
        {"paused": True},
        {"paused": True, "cadence": "every 1 hour", "next_run": _ARMED},
    ),
    ("cleared", {"cadence": "", "next_run": ""}, _REQUIRED),
    ("blank", {"cadence": "  ", "next_run": "  "}, _REQUIRED),
]


def _job(workspace: Path, job_id: str, **over: Any) -> Job:
    fields: dict[str, Any] = {
        "id": job_id,
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": _ARMED,
        "deliver_to": "window",
        "paused": False,
        "last_run": _RECEIPT,
        "last_status": "ok",
        "last_summary": "three new messages",
        "last_session_id": "sess-1",
    }
    fields.update(over)
    return Job(**fields)


async def _handle(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _row(listed: JobList, job_id: str) -> JobEntry:
    match = [job for job in listed.jobs if job.id == job_id]
    assert len(match) == 1
    return match[0]


def _receipt(row: JobEntry, job_id: str) -> None:
    assert row.id == job_id
    assert row.last_run == _RECEIPT
    assert row.last_status == "ok"
    assert row.last_summary == "three new messages"
    assert row.last_session_id == "sess-1"


async def test_edit_keeps_the_receipt_and_clears_with_an_empty_string(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    for job_id, _patch, _expect in _PATCHES:
        save_job(data, _job(workspace, job_id))
    daemon = Daemon(data_dir=data)
    try:
        for job_id, patch, expect in _PATCHES:
            raw = await _handle(daemon, {"type": "save_job", "id": job_id, **patch})
            stored = next(job for job in list_jobs(data) if job.id == job_id)
            if isinstance(expect, str):
                assert raw["type"] == "error", job_id
                assert raw["code"] == "job_invalid"
                assert raw["message"] == expect
                assert "pydantic" not in raw["message"]
                assert stored.cadence == "every 1 hour"
                assert stored.next_run == _ARMED
                assert stored.last_summary == "three new messages"
                continue
            listed = parse_daemon_event(json.dumps(raw))
            assert isinstance(listed, JobList)
            row = _row(listed, job_id)
            _receipt(row, job_id)
            for key, value in expect.items():
                assert getattr(row, key) == value, job_id
                assert getattr(stored, key) == value, job_id
    finally:
        await daemon._shutdown()


async def test_switch_recurring_to_one_shot_and_back(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data, _job(workspace, "switch"))
    daemon = Daemon(data_dir=data)
    try:
        once = await _handle(
            daemon,
            {"type": "save_job", "id": "switch", "cadence": "", "next_run": _ONE_SHOT},
        )
        listed = parse_daemon_event(json.dumps(once))
        assert isinstance(listed, JobList)
        row = _row(listed, "switch")
        assert row.cadence is None
        assert row.next_run == _ONE_SHOT
        _receipt(row, "switch")

        back = await _handle(
            daemon,
            {"type": "save_job", "id": "switch", "cadence": "daily at 9", "next_run": ""},
        )
        listed = parse_daemon_event(json.dumps(back))
        assert isinstance(listed, JobList)
        row = _row(listed, "switch")
        assert row.cadence == "0 9 * * *"
        assert row.next_run is None
        assert row.id == "switch"
        _receipt(row, "switch")
    finally:
        await daemon._shutdown()


async def test_spent_one_shot_resume_and_blank_edit_still_save(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(
        data,
        _job(workspace, "spent", cadence=None, next_run=None, paused=True),
    )
    daemon = Daemon(data_dir=data)
    try:
        # Resume sends the row back, nulls included — not a schedule edit.
        resumed = await _handle(
            daemon,
            {
                "type": "save_job",
                "id": "spent",
                "workspace": str(workspace),
                "instruction": "summarize the inbox",
                "cadence": None,
                "next_run": None,
                "deliver_to": "window",
                "paused": False,
            },
        )
        listed = parse_daemon_event(json.dumps(resumed))
        assert isinstance(listed, JobList)
        row = listed.jobs[0]
        assert row.paused is False
        assert row.cadence is None
        assert row.next_run is None
        _receipt(row, "spent")

        # The form's blanks are "" . Already-empty fields are not a change.
        edited = await _handle(
            daemon,
            {
                "type": "save_job",
                "id": "spent",
                "instruction": "revised once",
                "cadence": "",
                "next_run": "",
                "paused": False,
            },
        )
        listed = parse_daemon_event(json.dumps(edited))
        assert isinstance(listed, JobList)
        row = listed.jobs[0]
        assert row.instruction == "revised once"
        assert row.cadence is None
        assert row.next_run is None
        _receipt(row, "spent")

        paused = await _handle(daemon, {"type": "save_job", "id": "spent", "paused": True})
        listed = parse_daemon_event(json.dumps(paused))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].paused is True
        assert listed.jobs[0].next_run is None
        assert listed.jobs[0].cadence is None
    finally:
        await daemon._shutdown()


async def test_changed_workspace_is_resolved_and_must_exist(tmp_path: Path) -> None:
    data = tmp_path / "data"
    current = tmp_path / "current"
    current.mkdir()
    known = tmp_path / "Documents" / "Client Reports"
    known.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    missing = tmp_path / "gone"
    save_job(data, _job(current, "move"))
    daemon = Daemon(data_dir=data)
    daemon.workspace_pins = [str(known)]
    try:
        renamed = await _handle(
            daemon, {"type": "save_job", "id": "move", "workspace": "client reports"}
        )
        listed = parse_daemon_event(json.dumps(renamed))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].workspace == str(known)
        _receipt(listed.jobs[0], "move")

        moved = await _handle(daemon, {"type": "save_job", "id": "move", "workspace": str(other)})
        listed = parse_daemon_event(json.dumps(moved))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].workspace == str(other)

        refused = await _handle(
            daemon,
            {"type": "save_job", "id": "move", "workspace": str(missing), "instruction": "nope"},
        )
        assert refused["code"] == "job_invalid"
        assert refused["message"] == f"Workspace: folder not found: {missing}"
        assert list_jobs(data)[0].workspace == str(other)
        assert list_jobs(data)[0].instruction == "summarize the inbox"

        bare = await _handle(daemon, {"type": "save_job", "id": "move", "workspace": "Nowhere"})
        assert bare["code"] == "job_invalid"
        assert bare["message"].startswith(
            "Workspace: must be a full folder path such as ~/Documents/project (got 'Nowhere')"
        )
        assert list_jobs(data)[0].workspace == str(other)
    finally:
        await daemon._shutdown()


async def test_unchanged_workspace_stays_lenient_when_the_folder_is_gone(tmp_path: Path) -> None:
    data = tmp_path / "data"
    missing = tmp_path / "gone"
    save_job(data, _job(missing, "moved"))
    daemon = Daemon(data_dir=data)
    try:
        edited = await _handle(
            daemon,
            {
                "type": "save_job",
                "id": "moved",
                "workspace": str(missing) + "/",
                "instruction": "still here",
            },
        )
        listed = parse_daemon_event(json.dumps(edited))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].instruction == "still here"
        assert listed.jobs[0].workspace == str(missing)
        _receipt(listed.jobs[0], "moved")

        paused = parse_daemon_event(
            json.dumps(await _handle(daemon, {"type": "save_job", "id": "moved", "paused": True}))
        )
        assert isinstance(paused, JobList)
        assert paused.jobs[0].paused is True

        deleted = await _handle(daemon, {"type": "delete_job", "job_id": "moved"})
        listed = parse_daemon_event(json.dumps(deleted))
        assert isinstance(listed, JobList)
        assert listed.jobs == []
    finally:
        await daemon._shutdown()


async def test_legacy_job_keeps_utc_unless_the_edit_sends_a_zone(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(data, _job(workspace, "legacy", cadence="45 7 * * 1-5", timezone=None))
    daemon = Daemon(data_dir=data)
    try:
        # The daemon does not invent a zone. A cadence edit that omits one
        # stays on UTC and drops the slot the old cadence armed.
        changed = await _handle(
            daemon, {"type": "save_job", "id": "legacy", "cadence": "daily at 9"}
        )
        listed = parse_daemon_event(json.dumps(changed))
        assert isinstance(listed, JobList)
        assert listed.jobs[0].timezone is None
        assert listed.jobs[0].cadence == "0 9 * * *"
        assert listed.jobs[0].next_run is None
        _receipt(listed.jobs[0], "legacy")

        save_job(data, _job(workspace, "adopt", cadence="45 7 * * 1-5", timezone=None))
        adopted = await _handle(
            daemon,
            {
                "type": "save_job",
                "id": "adopt",
                "cadence": "0 8 * * 1-5",
                "timezone": "America/Chicago",
            },
        )
        listed = parse_daemon_event(json.dumps(adopted))
        assert isinstance(listed, JobList)
        row = _row(listed, "adopt")
        assert row.timezone == "America/Chicago"
        assert row.cadence == "0 8 * * 1-5"
        assert row.next_run is None
        _receipt(row, "adopt")

        save_job(data, _job(workspace, "quiet", cadence="45 7 * * 1-5", timezone=None))
        quiet = await _handle(
            daemon, {"type": "save_job", "id": "quiet", "instruction": "leave the clock"}
        )
        listed = parse_daemon_event(json.dumps(quiet))
        assert isinstance(listed, JobList)
        row = _row(listed, "quiet")
        assert row.timezone is None
        assert row.cadence == "45 7 * * 1-5"
        assert row.next_run == _ARMED
        _receipt(row, "quiet")

        # A job that already has a zone keeps it when the cadence changes.
        # The pane omits timezone in that case; the stale slot still drops.
        save_job(data, _job(workspace, "zoned", cadence="45 7 * * 1-5", timezone="America/Chicago"))
        kept = await _handle(daemon, {"type": "save_job", "id": "zoned", "cadence": "0 8 * * 1-5"})
        listed = parse_daemon_event(json.dumps(kept))
        assert isinstance(listed, JobList)
        row = _row(listed, "zoned")
        assert row.timezone == "America/Chicago"
        assert row.cadence == "0 8 * * 1-5"
        assert row.next_run is None
        _receipt(row, "zoned")
    finally:
        await daemon._shutdown()
