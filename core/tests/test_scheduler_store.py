"""Scheduled job store (TD-3803). Persist and parse; do not run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.platform_helpers import scheduler_fixture_ws
from tstd.scheduler.models import Job, JobDraft, JobValidationError, validate_draft
from tstd.scheduler.parse import parse_job_request
from tstd.scheduler.store import delete_job, get_job, jobs_path, list_jobs, save_job


def _draft(
    workspace: Path,
    *,
    instruction: str = "summarize the inbox",
    cadence: str | None = "every 1 hour",
    next_run: str | None = None,
    deliver_to: str = "window",
    paused: bool = False,
    job_id: str | None = None,
) -> JobDraft:
    return JobDraft(
        id=job_id,
        workspace=str(workspace),
        instruction=instruction,
        cadence=cadence,
        next_run=next_run,
        deliver_to=deliver_to,  # type: ignore[arg-type]
        paused=paused,
    )


def test_jobs_path_is_under_user_data_dir(tmp_path: Path) -> None:
    assert jobs_path(tmp_path) == tmp_path / "scheduler" / "jobs.json"


def test_absent_is_empty(tmp_path: Path) -> None:
    assert list_jobs(tmp_path) == []


def test_persist_roundtrip_cadence(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    saved = save_job(tmp_path, _draft(workspace, deliver_to="slack"))
    assert saved.workspace == str(workspace)
    assert saved.cadence == "every 1 hour"
    assert saved.next_run is None
    assert saved.deliver_to == "slack"
    assert saved.paused is False
    loaded = list_jobs(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].id == saved.id
    assert loaded[0].instruction == "summarize the inbox"
    assert get_job(tmp_path, saved.id) == saved


def test_persist_next_run_iso(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    saved = save_job(
        tmp_path,
        _draft(
            workspace,
            cadence=None,
            next_run="2026-08-22T09:00:00Z",
            deliver_to="ntfy",
        ),
    )
    assert saved.cadence is None
    assert saved.next_run == "2026-08-22T09:00:00+00:00"
    assert saved.deliver_to == "ntfy"


def test_jobs_live_in_data_dir_not_workspace(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "project"
    workspace.mkdir()
    save_job(data, _draft(workspace))
    assert (data / "scheduler" / "jobs.json").is_file()
    assert not (workspace / "scheduler").exists()
    assert list(workspace.rglob("jobs.json")) == []


def test_atomic_write_is_valid_json(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _draft(workspace, cadence="0 9 * * 1-5"))
    raw = json.loads(jobs_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["jobs"][0]["cadence"] == "0 9 * * 1-5"
    assert raw["jobs"][0]["workspace"] == str(workspace)


def test_restricted_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(tmp_path, _draft(workspace))
    mode = jobs_path(tmp_path).stat().st_mode & 0o777
    if sys.platform == "win32":
        assert mode == 0o666
    else:
        assert mode == 0o600


def test_junk_is_empty(tmp_path: Path) -> None:
    path = jobs_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("nope\n", encoding="utf-8")
    assert list_jobs(tmp_path) == []


def test_malformed_row_is_dropped(tmp_path: Path) -> None:
    path = jobs_path(tmp_path)
    path.parent.mkdir(parents=True)
    ws = scheduler_fixture_ws("one")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {"id": "bad"},
                    {
                        "id": "good",
                        "workspace": str(ws),
                        "instruction": "ping",
                        "cadence": "every 2 hours",
                        "deliver_to": "window",
                        "paused": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    jobs = list_jobs(tmp_path)
    assert [job.id for job in jobs] == ["good"]


def test_relative_workspace_refused(tmp_path: Path) -> None:
    with pytest.raises(JobValidationError, match="absolute"):
        validate_draft(
            JobDraft(
                workspace="relative/ws",
                instruction="do the thing",
                cadence="every 1 day",
                deliver_to="window",
            )
        )
    assert not jobs_path(tmp_path).exists()


def test_workspace_secret_refused(tmp_path: Path) -> None:
    with pytest.raises(JobValidationError, match="secrets"):
        validate_draft(
            JobDraft(
                workspace="/ws/sk-" + ("x" * 20),
                instruction="do the thing",
                cadence="every 1 day",
                deliver_to="window",
            )
        )


def test_instruction_secret_refused_on_save(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(JobValidationError, match="secrets"):
        save_job(
            tmp_path,
            _draft(workspace, instruction="use sk-" + ("y" * 20)),
        )
    assert list_jobs(tmp_path) == []


def test_cadence_or_next_run_not_both(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(JobValidationError, match="not both"):
        validate_draft(_draft(workspace, cadence="every 1 hour", next_run="2026-08-22T09:00:00Z"))


def test_cadence_or_next_run_required() -> None:
    with pytest.raises(JobValidationError, match="cadence or next_run"):
        validate_draft(
            JobDraft(
                workspace="/ws/one",
                instruction="do the thing",
                deliver_to="window",
            )
        )


def test_unknown_deliver_to_refused() -> None:
    with pytest.raises(ValidationError):
        Job.model_validate(
            {
                "id": "j1",
                "workspace": "/ws/one",
                "instruction": "do the thing",
                "cadence": "every 1 hour",
                "deliver_to": "discord",
            }
        )


def test_parse_json_is_draft_and_does_not_persist(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    text = json.dumps(
        {
            "workspace": str(workspace),
            "instruction": "check the build",
            "cadence": "every 2 hours",
            "deliver_to": "slack",
        }
    )
    draft = parse_job_request(text)
    assert draft.instruction == "check the build"
    assert draft.cadence == "every 2 hours"
    assert draft.deliver_to == "slack"
    assert draft.workspace == str(workspace)
    assert list(tmp_path.rglob("*")) == []
    assert list_jobs(tmp_path) == []


def test_parse_fenced_json() -> None:
    draft = parse_job_request(
        '```json\n{"instruction":"ping","deliver_to":"ntfy","cadence":"every 1 day"}\n```'
    )
    assert draft.instruction == "ping"
    assert draft.deliver_to == "ntfy"


def test_parse_key_values() -> None:
    draft = parse_job_request(
        "workspace: /ws/one\n"
        "instruction: review the changelog\n"
        "cadence: 0 9 * * *\n"
        "deliver_to: window\n"
    )
    assert draft.workspace == "/ws/one"
    assert draft.instruction == "review the changelog"
    assert draft.cadence == "0 9 * * *"
    assert draft.deliver_to == "window"


def test_parse_natural_language() -> None:
    draft = parse_job_request("every 2 hours in /ws/proj summarize the inbox and deliver to slack")
    assert draft.workspace == "/ws/proj"
    assert draft.cadence == "every 2 hours"
    assert draft.deliver_to == "slack"
    assert draft.instruction == "summarize the inbox"
    assert draft.paused is False


def test_parse_cron_and_next_run() -> None:
    cron = parse_job_request("0 9 * * 1-5 in /ws/one check the build deliver to window")
    assert cron.cadence == "0 9 * * 1-5"
    assert cron.workspace == "/ws/one"
    assert cron.instruction == "check the build"
    assert cron.deliver_to == "window"
    once = parse_job_request("next run 2026-08-22T09:00:00Z in /ws/one send the digest via ntfy")
    assert once.next_run == "2026-08-22T09:00:00Z"
    assert once.deliver_to == "ntfy"
    assert once.instruction == "send the digest"


def test_parse_incomplete_is_editable_not_saved(tmp_path: Path) -> None:
    draft = parse_job_request("remind me to check the build")
    assert draft.instruction == "remind me to check the build"
    assert draft.workspace is None
    assert draft.cadence is None
    with pytest.raises(JobValidationError, match="missing"):
        validate_draft(draft)
    assert list_jobs(tmp_path) == []


def test_save_is_a_second_call(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    draft = parse_job_request(
        json.dumps(
            {
                "workspace": str(workspace),
                "instruction": "ping",
                "cadence": "every 1 hour",
                "deliver_to": "window",
            }
        )
    )
    assert list_jobs(tmp_path) == []
    job = validate_draft(draft)
    assert list_jobs(tmp_path) == []
    saved = save_job(tmp_path, job)
    assert [item.id for item in list_jobs(tmp_path)] == [saved.id]


def test_update_preserves_id_and_order(tmp_path: Path) -> None:
    first = save_job(tmp_path, _draft(tmp_path / "a", instruction="one", job_id="aaa"))
    second = save_job(tmp_path, _draft(tmp_path / "b", instruction="two", job_id="bbb"))
    updated = save_job(
        tmp_path,
        first.model_copy(update={"instruction": "one-edited", "paused": True}),
    )
    jobs = list_jobs(tmp_path)
    assert [job.id for job in jobs] == [first.id, second.id]
    assert jobs[0].instruction == "one-edited"
    assert jobs[0].paused is True
    assert updated.id == first.id


def test_delete_job(tmp_path: Path) -> None:
    saved = save_job(tmp_path, _draft(tmp_path / "ws"))
    assert delete_job(tmp_path, saved.id) is True
    assert list_jobs(tmp_path) == []
    assert delete_job(tmp_path, saved.id) is False


def test_parse_module_does_not_import_store() -> None:
    src = Path(parse_job_request.__code__.co_filename).read_text(encoding="utf-8")
    assert "from .store" not in src
    assert "import store" not in src


def test_store_never_invokes_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    def boom(*_args: object, **_kwargs: object) -> None:
        started.append("session")
        raise AssertionError("scheduler must not start a session")

    monkeypatch.setattr("tstd.session.SessionRunner.__init__", boom)
    monkeypatch.setattr("tstd.session.SessionRunner.start", boom)

    src = Path(save_job.__code__.co_filename).read_text(encoding="utf-8")
    assert "SessionRunner" not in src
    assert "tstd.session" not in src
    assert "asyncio" not in src

    draft = parse_job_request(
        json.dumps(
            {
                "workspace": str(tmp_path / "ws"),
                "instruction": "ping",
                "cadence": "every 1 hour",
                "deliver_to": "window",
            }
        )
    )
    job = validate_draft(draft)
    saved = save_job(tmp_path, job)
    assert list_jobs(tmp_path)
    assert delete_job(tmp_path, saved.id) is True
    assert started == []
