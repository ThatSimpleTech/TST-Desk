"""Scheduled rail protocol (TD-3805): list / save / delete. Does not run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tstd.daemon import Daemon
from tstd.protocol import JobList, parse_daemon_event
from tstd.scheduler.models import Job
from tstd.scheduler.store import list_jobs, save_job


async def _handle(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _draft(workspace: Path) -> dict[str, Any]:
    return {
        "type": "save_job",
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "deliver_to": "window",
    }


async def test_list_empty(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    listed = parse_daemon_event(json.dumps(await _handle(daemon, {"type": "list_jobs"})))
    assert isinstance(listed, JobList)
    assert listed.jobs == []
    assert listed.seq == 1
    await daemon._shutdown()


async def test_create_pause_delete(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    created = parse_daemon_event(json.dumps(await _handle(daemon, _draft(workspace))))
    assert isinstance(created, JobList)
    assert len(created.jobs) == 1
    job = created.jobs[0]
    assert job.instruction == "summarize the inbox"
    assert job.paused is False
    assert job.workspace == str(workspace)

    paused = parse_daemon_event(
        json.dumps(
            await _handle(
                daemon,
                {
                    "type": "save_job",
                    "id": job.id,
                    "workspace": job.workspace,
                    "instruction": job.instruction,
                    "cadence": job.cadence,
                    "deliver_to": job.deliver_to,
                    "paused": True,
                },
            )
        )
    )
    assert isinstance(paused, JobList)
    assert paused.jobs[0].paused is True

    deleted = parse_daemon_event(
        json.dumps(await _handle(daemon, {"type": "delete_job", "job_id": job.id}))
    )
    assert isinstance(deleted, JobList)
    assert deleted.jobs == []
    await daemon._shutdown()


async def test_save_does_not_start_a_session(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    await _handle(
        daemon,
        {
            "type": "save_job",
            "workspace": str(workspace),
            "instruction": "due now",
            "next_run": "2000-01-01T00:00:00+00:00",
            "deliver_to": "window",
        },
    )
    assert daemon.session_registry.count == 0
    assert list_jobs(tmp_path / "data")
    await daemon._shutdown()


async def test_invalid_save_is_typed(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    err = await _handle(daemon, {"type": "save_job", "instruction": "x"})
    assert err["type"] == "error"
    assert err["code"] == "job_invalid"
    await daemon._shutdown()


async def test_delete_missing_is_typed(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    err = await _handle(daemon, {"type": "delete_job", "job_id": "missing"})
    assert err["type"] == "error"
    assert err["code"] == "job_not_found"
    await daemon._shutdown()


async def test_pause_keeps_cadence_and_next_run(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    stored = save_job(
        data,
        Job(
            id="both",
            workspace=str(workspace),
            instruction="keep both",
            cadence="every 1 hour",
            next_run="2026-08-21T18:00:00+00:00",
            deliver_to="window",
            paused=False,
        ),
    )
    daemon = Daemon(data_dir=data)
    paused = parse_daemon_event(
        json.dumps(
            await _handle(
                daemon,
                {
                    "type": "save_job",
                    "id": stored.id,
                    "paused": True,
                },
            )
        )
    )
    assert isinstance(paused, JobList)
    row = paused.jobs[0]
    assert row.paused is True
    assert row.cadence == "every 1 hour"
    assert row.next_run == "2026-08-21T18:00:00+00:00"
    await daemon._shutdown()
