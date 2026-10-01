"""How long one scheduled turn may run (TD-3819).

The config is the default. A job's own limit wins. Hitting it cancels
the turn and is not a retry. Edit keeps an omitted value and clears a
blank. A template may carry the same phrase.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tests.test_loop import make_config
from tstd.config import ConfigError, default_config_yaml, load_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.scheduler.edit import apply_job_edit
from tstd.scheduler.limit import parse_max_run, run_limit_seconds, stop_summary
from tstd.scheduler.models import Job, JobDraft, JobValidationError, validate_draft
from tstd.scheduler.store import get_job, jobs_path, save_job
from tstd.scheduler.templates import TemplateDraft, list_templates, save_template, templates_path
from tstd.session import Session

_NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_SECRET = "sk-" + ("a" * 40)

_PARSE: list[tuple[object, int | type[Exception] | None]] = [
    (None, None),
    ("", None),
    ("   ", None),
    ("20 minutes", 1200),
    ("every 20 minutes", 1200),
    ("1 minute", 60),
    ("60 minutes", 3600),
    ("1 hour", 3600),
    (1200, 1200),
    ("1200", 1200),
    (30, ValueError),
    ("30", ValueError),
    ("30 seconds", ValueError),
    ("61 minutes", ValueError),
    ("2 hours", ValueError),
    ("1 day", ValueError),
    (True, ValueError),
    (1.5, ValueError),
    (_SECRET, ValueError),
]


def _base_job(**over: object) -> Job:
    data: dict[str, object] = {
        "id": "inbox",
        "workspace": "/ws",
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": "2026-08-21T07:45:00+00:00",
        "deliver_to": "window",
        "max_run": 1200,
    }
    data.update(over)
    return Job.model_validate(data)


def _edit(job: Job, **over: object) -> Job:
    fields: dict[str, object] = {
        "workspace": None,
        "instruction": None,
        "cadence": None,
        "next_run": None,
        "deliver_to": None,
        "paused": False,
        "timezone": None,
        "known_workspaces": [],
        "preset": None,
        "engine": None,
        "known_presets": {},
        "grace": None,
        "retries": None,
        "retry_delay": None,
        "max_run": None,
    }
    fields.update(over)
    return apply_job_edit(job, **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(("raw", "expected"), _PARSE)
def test_parse_max_run(raw: object, expected: int | type[Exception] | None) -> None:
    if isinstance(expected, type):
        with pytest.raises(
            ValueError,
            match=r"1 to 60 minutes|must not contain secrets|not understood",
        ):
            parse_max_run(raw)
        if isinstance(raw, str) and raw == _SECRET:
            try:
                parse_max_run(raw)
            except ValueError as exc:
                assert _SECRET not in str(exc)
        return
    assert parse_max_run(raw) == expected


def test_the_job_limit_wins_over_the_config() -> None:
    assert run_limit_seconds(None, 900) == 900
    assert run_limit_seconds(1200, 0.2) == 1200
    assert stop_summary(900) == "stopped after 15 minutes (max run time)"
    assert stop_summary(60) == "stopped after 1 minute (max run time)"
    assert stop_summary(0.2) == "stopped after 1 minute (max run time)"


def test_shipped_config_is_fifteen_minutes(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(default_config_yaml(), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.scheduler.max_run_seconds == 900


@pytest.mark.parametrize("bad", [0, 3601])
def test_config_outside_an_hour_is_refused(tmp_path: Path, bad: int) -> None:
    text = default_config_yaml().replace("max_run_seconds: 900", f"max_run_seconds: {bad}", 1)
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"scheduler\.max_run_seconds"):
        load_config(path)


def test_a_user_file_without_the_section_inherits_the_shipped_default(tmp_path: Path) -> None:
    raw = yaml.safe_load(default_config_yaml())
    assert isinstance(raw, dict)
    raw.pop("scheduler", None)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_config(path).scheduler.max_run_seconds == 900


def test_validate_draft_labels_a_bad_limit() -> None:
    with pytest.raises(JobValidationError, match="Max run:") as caught:
        validate_draft(
            JobDraft(
                workspace="/ws",
                instruction="summarize",
                cadence="every 1 hour",
                deliver_to="window",
                max_run="2 hours",
            )
        )
    assert "2 hours" not in str(caught.value)


def test_edit_keeps_an_omitted_limit_and_clears_a_blank() -> None:
    job = _base_job()
    assert _edit(job).max_run == 1200
    assert _edit(job, max_run="").max_run is None
    assert _edit(job, max_run="5 minutes").max_run == 300


def test_a_job_file_without_the_key_still_loads(tmp_path: Path) -> None:
    save_job(tmp_path, _base_job(max_run=None))
    path = jobs_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    row = raw["jobs"][0]
    row.pop("max_run", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = get_job(tmp_path, "inbox")
    assert loaded is not None
    assert loaded.max_run is None


def test_a_template_may_carry_the_limit(tmp_path: Path) -> None:
    saved = save_template(
        tmp_path,
        TemplateDraft(
            name="Research",
            instruction="search the web",
            cadence="weekdays at 9:00",
            deliver_to="window",
            max_run="20 minutes",
        ),
        {},
    )
    user = saved[-1]
    assert user.max_run == 1200
    assert list_templates(tmp_path)[-1].max_run == 1200
    blob = templates_path(tmp_path).read_text(encoding="utf-8")
    assert "1200" in blob
    with pytest.raises(JobValidationError, match="Max run:"):
        save_template(
            tmp_path,
            TemplateDraft(
                name="Too long",
                instruction="search",
                cadence="weekdays at 9:00",
                deliver_to="window",
                max_run="2 hours",
            ),
            {},
        )


def test_builtins_leave_the_limit_unset(tmp_path: Path) -> None:
    rows = list_templates(tmp_path)
    assert [row.max_run for row in rows] == [None, None]


async def _run(tmp_path: Path, *, config_seconds: float, max_run: int | None, delay: float) -> Job:
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _base_job(workspace=str(workspace), max_run=max_run, retries=2, retry_delay=600)
    save_job(data, job)
    daemon = Daemon(
        data_dir=data,
        provider=MockProvider(default=Script(kind="stream", content="digest", chunk_delay=delay)),
    )
    daemon.config = make_config()
    daemon.config.scheduler.max_run_seconds = config_seconds
    try:
        await daemon.run_due_jobs(_NOW)
        stored = get_job(data, "inbox")
        assert stored is not None
        return stored
    finally:
        await daemon._shutdown()


async def test_the_config_default_cancels_and_is_not_retried(tmp_path: Path) -> None:
    job = await _run(tmp_path, config_seconds=0.2, max_run=None, delay=30)
    assert job.last_status == "failed"
    assert job.attempt == 0
    assert job.resume_at is None
    assert job.next_run == "2026-08-21T19:00:00+00:00"
    assert job.last_summary is not None
    assert "max run time" in job.last_summary
    assert "timed out" not in job.last_summary
    assert job.last_session_id
    # The daemon is shut down inside _run. The receipt is the proof the
    # slot finished instead of arming the 10-minute retry.


async def test_the_per_job_limit_wins(tmp_path: Path) -> None:
    """A short config would have stopped this turn. The job's minute does not."""
    data = tmp_path / "data"
    data.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_job(
        data,
        _base_job(workspace=str(workspace), max_run=60, retries=0),
    )
    daemon = Daemon(
        data_dir=data,
        provider=MockProvider(default=Script(kind="stream", content="digest", chunk_delay=0.25)),
    )
    daemon.config = make_config()
    daemon.config.scheduler.max_run_seconds = 0.15
    try:
        await daemon.run_due_jobs(_NOW)
        job = get_job(data, "inbox")
        assert job is not None
        assert job.last_status == "ok"
        assert job.last_summary == "digest"
        assert job.last_session_id
        session = daemon.session_registry.get(job.last_session_id)
        assert isinstance(session, Session)
        # The waiter returns on turn_complete, which can land before the
        # loop task marks the session complete. Cancelled would mean the
        # short config won.
        assert session.state != "cancelled"
    finally:
        await daemon._shutdown()
