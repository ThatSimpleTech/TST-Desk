"""Scheduled rail protocol (TD-3805): list / save / delete. Does not run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tstd.daemon import Daemon
from tstd.protocol import JobDraftReply, JobList, parse_daemon_event
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


async def test_parse_job_fills_a_draft_without_saving(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    parsed = parse_daemon_event(
        json.dumps(
            await _handle(
                daemon,
                {
                    "type": "parse_job",
                    "text": f"every 2 hours in {workspace} summarize the inbox deliver to slack",
                },
            )
        )
    )
    assert isinstance(parsed, JobDraftReply)
    assert parsed.ok is True
    assert parsed.workspace == str(workspace)
    assert parsed.instruction == "summarize the inbox"
    assert parsed.cadence == "every 2 hours"
    assert parsed.deliver_to == "slack"
    assert list_jobs(tmp_path / "data") == []
    await daemon._shutdown()


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


async def _save(daemon: Daemon, **fields: Any) -> dict[str, Any]:
    payload = {
        "type": "save_job",
        "instruction": "summarize the inbox",
        "cadence": "7:45 on weekdays",
        "deliver_to": "window",
        **fields,
    }
    return await _handle(daemon, payload)


async def test_plain_english_cadence_and_zone_are_stored(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    listed = parse_daemon_event(
        json.dumps(await _save(daemon, workspace=str(workspace), timezone="America/Chicago"))
    )
    assert isinstance(listed, JobList)
    assert listed.jobs[0].cadence == "45 7 * * 1-5"
    assert listed.jobs[0].timezone == "America/Chicago"
    await daemon._shutdown()


async def test_edit_without_timezone_keeps_it_and_zone_change_rearms(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    created = parse_daemon_event(
        json.dumps(await _save(daemon, workspace=str(workspace), timezone="America/Chicago"))
    )
    assert isinstance(created, JobList)
    job_id = created.jobs[0].id
    save_job(
        tmp_path / "data",
        Job.model_validate(
            {**created.jobs[0].model_dump(), "next_run": "2026-09-28T12:45:00+00:00"}
        ),
    )
    paused = parse_daemon_event(
        json.dumps(await _handle(daemon, {"type": "save_job", "id": job_id, "paused": True}))
    )
    assert isinstance(paused, JobList)
    assert paused.jobs[0].timezone == "America/Chicago"
    assert paused.jobs[0].next_run == "2026-09-28T12:45:00+00:00"
    moved = parse_daemon_event(
        json.dumps(
            await _handle(
                daemon, {"type": "save_job", "id": job_id, "timezone": "America/New_York"}
            )
        )
    )
    assert isinstance(moved, JobList)
    assert moved.jobs[0].timezone == "America/New_York"
    assert moved.jobs[0].next_run is None
    await daemon._shutdown()


async def test_bare_workspace_name_resolves_to_the_one_known_folder(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    known = tmp_path / "Documents" / "Client Reports"
    known.mkdir(parents=True)
    daemon.workspace_pins = [str(known)]
    listed = parse_daemon_event(json.dumps(await _save(daemon, workspace="client reports")))
    assert isinstance(listed, JobList)
    assert listed.jobs[0].workspace == str(known)
    await daemon._shutdown()


async def test_ambiguous_or_unknown_workspace_name_is_a_readable_error(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    first = tmp_path / "a" / "Client Reports"
    second = tmp_path / "b" / "client reports"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    daemon.workspace_pins = [str(first), str(second)]
    for name in ("Client Reports", "Nowhere"):
        err = await _save(daemon, workspace=name)
        assert err["code"] == "job_invalid"
        assert err["message"].startswith(
            f"Workspace: must be a full folder path such as ~/Documents/project (got '{name}')"
        )
        assert "pydantic.dev" not in err["message"]
    await daemon._shutdown()


async def test_create_refuses_a_missing_folder(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    missing = tmp_path / "gone"
    err = await _save(daemon, workspace=str(missing))
    assert err["code"] == "job_invalid"
    assert err["message"] == f"Workspace: folder not found: {missing}"
    assert list_jobs(tmp_path / "data") == []
    await daemon._shutdown()


async def test_existing_job_with_a_moved_folder_can_still_be_paused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    stored = save_job(
        data,
        Job(
            id="moved",
            workspace=str(tmp_path / "gone"),
            instruction="x",
            cadence="every 1 hour",
            deliver_to="window",
        ),
    )
    daemon = Daemon(data_dir=data)
    paused = parse_daemon_event(
        json.dumps(await _handle(daemon, {"type": "save_job", "id": stored.id, "paused": True}))
    )
    assert isinstance(paused, JobList)
    assert paused.jobs[0].paused is True
    await daemon._shutdown()
