"""Job templates (TD-3816): built-ins, the data-dir file, and the verbs."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tstd.daemon import Daemon
from tstd.protocol import JobTemplates, parse_daemon_event
from tstd.scheduler.models import JobValidationError
from tstd.scheduler.store import list_jobs
from tstd.scheduler.templates import (
    TemplateDraft,
    TemplateError,
    delete_template,
    list_templates,
    save_template,
    templates_path,
)

_SECRET = "sk-testsecretvalue1234"
_CATALOG = {"vllm": object()}


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _private() -> int:
    return 0o666 if sys.platform == "win32" else 0o600


def _draft(**over: object) -> TemplateDraft:
    base: dict[str, object] = {
        "name": "Inbox digest",
        "instruction": "summarize the inbox",
        "cadence": "weekdays at 7:45",
        "deliver_to": "window",
    }
    base.update(over)
    return TemplateDraft(**base)  # type: ignore[arg-type]


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = Daemon(data_dir=tmp_path / "data")
    try:
        yield instance
    finally:
        await instance._shutdown()


async def _handle(daemon: Daemon, payload: dict[str, object]) -> dict[str, object]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, object] = json.loads(raw)
    return parsed


def test_builtins_ship_as_data_and_listing_creates_nothing(tmp_path: Path) -> None:
    rows = list_templates(tmp_path)
    assert [row.id for row in rows] == ["weekday-morning-digest", "one-shot-reminder"]
    digest, reminder = rows
    assert digest.name == "Weekday morning digest"
    assert digest.builtin is True
    assert digest.cadence == "weekdays at 7:45"
    assert digest.next_run is None
    assert digest.grace == 7200
    assert digest.retries == 1
    assert digest.retry_delay == 600
    assert digest.deliver_to == "window"
    assert digest.instruction == ""
    assert reminder.name == "One-shot reminder"
    assert reminder.builtin is True
    assert reminder.cadence is None
    assert reminder.next_run == "tomorrow at 9:00"
    assert reminder.grace is None
    assert reminder.retries == 0
    assert reminder.retry_delay is None
    assert reminder.deliver_to == "window"
    assert reminder.instruction == ""
    blob = json.dumps([row.__dict__ for row in rows])
    assert "http://" not in blob
    assert "https://" not in blob
    assert "://" not in blob
    for row in rows:
        assert row.preset is None
        assert row.engine is None
        assert row.workspace is None
    assert not templates_path(tmp_path).exists()
    assert list_jobs(tmp_path) == []


def test_user_template_follows_the_data_dir(tmp_path: Path) -> None:
    folder = tmp_path / "Reports"
    folder.mkdir()
    missing = tmp_path / "not-created-yet"
    saved = save_template(
        tmp_path / "a",
        _draft(
            workspace="reports",
            grace="2 hours",
            retries=1,
            preset="vllm",
            engine="native",
        ),
        _CATALOG,
        known_workspaces=[str(folder)],
    )
    user = saved[-1]
    assert user.builtin is False
    assert user.cadence == "weekdays at 7:45"
    assert "45 7" not in (user.cadence or "")
    assert user.grace == 7200
    assert user.retries == 1
    assert user.retry_delay == 600
    assert user.preset == "vllm"
    assert user.engine == "native"
    assert user.workspace == os.path.normpath(str(folder))
    path = templates_path(tmp_path / "a")
    assert path.is_file()
    assert _mode(path) == _private()
    assert list_templates(tmp_path / "b") == list(saved[:2])
    no_folder = save_template(
        tmp_path / "a",
        _draft(name="Later", workspace=str(missing), cadence=None, next_run="tomorrow at 9am"),
        _CATALOG,
    )
    assert no_folder[-1].workspace == os.path.normpath(str(missing))
    assert no_folder[-1].next_run == "tomorrow at 9:00"
    assert not (tmp_path / "a" / "scheduler" / "jobs.json").exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tomorrow at 9:00", "tomorrow at 9:00"),
        ("tomorrow at 9", "tomorrow at 9:00"),
        ("tomorrow at 9am", "tomorrow at 9:00"),
        ("tomorrow at 9:00pm", "tomorrow at 21:00"),
        ("tomorrow at 12am", "tomorrow at 0:00"),
        ("tomorrow at 12pm", "tomorrow at 12:00"),
        ("  Tomorrow   at  9:05 AM ", "tomorrow at 9:05"),
    ],
)
def test_tomorrow_rule_is_stored_not_an_instant(tmp_path: Path, raw: str, expected: str) -> None:
    saved = save_template(
        tmp_path,
        _draft(cadence=None, next_run=raw, instruction=""),
        _CATALOG,
    )
    assert saved[-1].next_run == expected
    assert saved[-1].cadence is None


@pytest.mark.parametrize(
    "over",
    [
        {"instruction": f"use {_SECRET}"},
        {"name": _SECRET},
        {"cadence": f"every 1 hour {_SECRET}"},
        {"cadence": None, "next_run": f"tomorrow at 9:00 {_SECRET}"},
        {"preset": "missing"},
        {"cadence": "whenever"},
        {"cadence": "weekdays at 7:45", "next_run": "tomorrow at 9:00"},
        {"cadence": None, "next_run": None},
        {"workspace": "reports/q"},
        {"name": "  "},
        {"name": "n" * 81},
    ],
)
def test_save_refuses_the_same_rules_as_a_job(tmp_path: Path, over: dict[str, object]) -> None:
    path = templates_path(tmp_path)
    with pytest.raises(JobValidationError) as caught:
        save_template(tmp_path, _draft(**over), _CATALOG)
    assert _SECRET not in str(caught.value)
    assert not path.exists()


def test_removed_preset_still_lists_but_cannot_be_saved(tmp_path: Path) -> None:
    path = templates_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "templates": [
                    {
                        "id": "weekday-morning-digest",
                        "name": "Hijack",
                        "instruction": "nope",
                        "cadence": "weekdays at 7:45",
                        "deliver_to": "window",
                    },
                    {
                        "id": "../x",
                        "name": "Escape",
                        "instruction": "x",
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                    },
                    {
                        "id": "kept",
                        "name": "Kept",
                        "instruction": "keep",
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                        "preset": "removed",
                    },
                    {
                        "id": "bad",
                        "name": "Bad",
                        "instruction": _SECRET,
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    rows = list_templates(tmp_path)
    assert [row.id for row in rows] == ["weekday-morning-digest", "one-shot-reminder", "kept"]
    assert rows[0].instruction == ""
    assert rows[-1].preset == "removed"
    assert path.read_bytes() == before
    with pytest.raises(TemplateError) as builtin:
        delete_template(tmp_path, "weekday-morning-digest")
    assert builtin.value.code == "template_builtin"
    assert path.read_bytes() == before
    assert any(row.id == "weekday-morning-digest" for row in list_templates(tmp_path))
    gone = delete_template(tmp_path, "kept")
    assert [row.id for row in gone] == ["weekday-morning-digest", "one-shot-reminder"]
    with pytest.raises(JobValidationError, match="not in the catalog"):
        save_template(tmp_path, _draft(preset="removed"), _CATALOG)


def test_unreadable_file_does_not_log_its_body(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = templates_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json " + _SECRET, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tstd.scheduler.templates"):
        rows = list_templates(tmp_path)
    assert [row.id for row in rows] == ["weekday-morning-digest", "one-shot-reminder"]
    assert _SECRET not in caplog.text
    assert path.read_text(encoding="utf-8").endswith(_SECRET)


def test_malformed_rows_are_not_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = templates_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "templates": [
                    {
                        "id": "bad",
                        "name": "Bad",
                        "instruction": _SECRET,
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="tstd.scheduler.templates"):
        list_templates(tmp_path)
    assert _SECRET not in caplog.text


async def test_protocol_lists_saves_and_refuses_a_builtin(daemon: Daemon, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    listed = parse_daemon_event(json.dumps(await _handle(daemon, {"type": "list_job_templates"})))
    assert isinstance(listed, JobTemplates)
    assert listed.seq == 1
    assert [row.name for row in listed.templates[:2]] == [
        "Weekday morning digest",
        "One-shot reminder",
    ]
    assert not templates_path(daemon.data_dir).exists()

    saved_raw = await _handle(
        daemon,
        {
            "type": "save_job_template",
            "name": "Inbox digest",
            "instruction": "summarize the inbox",
            "cadence": "weekdays at 7:45",
            "deliver_to": "window",
            "grace": "2 hours",
            "retries": 1,
            "preset": "vllm",
            "engine": "native",
            "workspace": str(workspace),
        },
    )
    saved = parse_daemon_event(json.dumps(saved_raw))
    assert isinstance(saved, JobTemplates)
    user = saved.templates[-1]
    assert user.builtin is False
    assert user.cadence == "weekdays at 7:45"
    assert user.preset == "vllm"
    assert list_jobs(daemon.data_dir) == []
    assert daemon.session_registry.count == 0

    secret = await _handle(
        daemon,
        {
            "type": "save_job_template",
            "name": "Nope",
            "instruction": _SECRET,
            "cadence": "every 1 hour",
            "deliver_to": "window",
        },
    )
    assert secret["type"] == "error"
    assert secret["code"] == "template_invalid"
    assert _SECRET not in json.dumps(secret)

    unknown = await _handle(
        daemon,
        {
            "type": "save_job_template",
            "name": "Nope",
            "instruction": "summarize",
            "cadence": "every 1 hour",
            "deliver_to": "window",
            "preset": "not-a-preset",
        },
    )
    assert unknown["code"] == "template_invalid"
    assert "not in the catalog" in str(unknown["message"])

    refused = await _handle(
        daemon,
        {"type": "delete_job_template", "template_id": "weekday-morning-digest"},
    )
    assert refused["code"] == "template_builtin"
    still = parse_daemon_event(json.dumps(await _handle(daemon, {"type": "list_job_templates"})))
    assert isinstance(still, JobTemplates)
    assert any(row.id == "weekday-morning-digest" for row in still.templates)
    deleted = parse_daemon_event(
        json.dumps(await _handle(daemon, {"type": "delete_job_template", "template_id": user.id}))
    )
    assert isinstance(deleted, JobTemplates)
    assert all(row.id != user.id for row in deleted.templates)
