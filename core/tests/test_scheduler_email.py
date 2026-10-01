"""Email delivery on a finished job (TD-3820).

The receipt stays the turn summary. The mail is the report. A failed
send stamps delivery without changing last_status.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_email_notify import FakeSmtp, _cert, _client_ctx, _server_ctx
from tests.test_loop import make_config
from tstd.config import EmailNotifyConfig
from tstd.notify.email import EmailNotifyError, email_subject, send
from tstd.scheduler.edit import apply_job_edit
from tstd.scheduler.email_delivery import bind_email_sender, reset_email_sender
from tstd.scheduler.history import list_runs
from tstd.scheduler.models import Job
from tstd.scheduler.runner import RecordingDeliver, TurnResult, run_manual_job
from tstd.scheduler.schedule import record_run
from tstd.scheduler.store import get_job, save_job

_PASSWORD = "s3cret-smtp-pw"
_WHEN = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)


def _job(workspace: Path) -> Job:
    return Job(
        id="research",
        workspace=str(workspace),
        instruction="Morning brief",
        cadence="every 1 day",
        next_run="2026-10-02T04:00:00+00:00",
        deliver_to="email",
        email_to="owner@example.com",
        timezone="America/Chicago",
    )


async def _server(tmp_path: Path) -> FakeSmtp:
    cert, key = _cert(tmp_path)
    server = FakeSmtp(advertise_starttls=True, implicit_tls=False, ctx=_server_ctx(cert, key))
    await server.start()
    return server


def _config(port: int):
    config = make_config()
    config.notify.email = EmailNotifyConfig(
        enabled=True,
        host="127.0.0.1",
        port=port,
        security="starttls",
        username="desk",
        from_address="desk@example.com",
        timeout_seconds=5,
    )
    return config


def _patch_password(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get() -> str:
        return _PASSWORD

    monkeypatch.setattr("tstd.keychain.get_smtp_password", _get)


async def _run(tmp_path: Path, job: Job, turn: TurnResult, sender) -> None:
    token = bind_email_sender(sender)
    try:
        await run_manual_job(
            tmp_path,
            job,
            _WHEN,
            run_turn=_turn(turn),
            deliver=RecordingDeliver(),
        )
    finally:
        reset_email_sender(token)


def _turn(result: TurnResult):
    async def run(_workspace: Path, _message: str) -> TurnResult:
        return result

    return run


async def test_the_mail_is_the_final_report_not_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_password(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(workspace)
    save_job(tmp_path, job)
    server = await _server(tmp_path)
    config = _config(server.port)

    async def sender(current: Job, body: str, when: datetime) -> None:
        await send(
            config,
            body,
            to=current.email_to or "",
            subject=email_subject(current.instruction, when, current.timezone),
            ssl_context=_client_ctx(),
        )

    try:
        await _run(
            tmp_path,
            job,
            TurnResult(
                summary="let me search the web\n\nFINAL-REPORT-TOKEN",
                report="FINAL-REPORT-TOKEN",
                ok=True,
            ),
            sender,
        )
    finally:
        await server.close()
    stored = get_job(tmp_path, "research")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_delivery == "ok"
    assert stored.last_summary is not None and "let me search" in stored.last_summary
    from email import message_from_bytes
    from email.policy import default as email_default

    message = message_from_bytes(server.data, policy=email_default)
    assert message["subject"] == "Morning brief — 2026-10-01"
    plain = message.get_body(preferencelist=("plain",))
    assert plain is not None
    assert plain.get_content().strip() == "FINAL-REPORT-TOKEN"
    assert "let me search" not in plain.get_content()
    runs = list_runs(tmp_path, "research")
    assert runs[0].status == "ok"
    assert runs[0].delivery == "ok"


async def test_a_failed_send_keeps_the_run_status(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(workspace)
    save_job(tmp_path, job)

    async def sender(_job: Job, _body: str, _when: datetime) -> None:
        raise EmailNotifyError("SMTPException", "auth failed")

    await _run(
        tmp_path,
        job,
        TurnResult(summary="the report", report="the report", ok=True),
        sender,
    )
    stored = get_job(tmp_path, "research")
    assert stored is not None
    assert stored.last_status == "ok"
    assert stored.last_delivery == "failed"
    assert stored.last_delivery_error is not None
    assert "SMTPException" in stored.last_delivery_error
    assert "auth failed" in stored.last_delivery_error
    run = list_runs(tmp_path, "research")[0]
    assert run.status == "ok"
    assert run.delivery == "failed"

    failed = _job(workspace)
    failed = failed.model_copy(update={"id": "failed-run"})
    save_job(tmp_path, failed)
    await _run(
        tmp_path,
        failed,
        TurnResult(summary="disk full", report="disk full", ok=False),
        sender,
    )
    stored_fail = get_job(tmp_path, "failed-run")
    assert stored_fail is not None
    assert stored_fail.last_status == "failed"
    assert stored_fail.last_delivery == "failed"


def test_record_run_clears_a_previous_delivery(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job = _job(workspace).model_copy(
        update={"last_delivery": "failed", "last_delivery_error": "SMTPException: auth failed"}
    )
    stamped = record_run(job, _WHEN, status="ok", summary="again", session_id=None)
    assert stamped.last_status == "ok"
    assert stamped.last_delivery is None
    assert stamped.last_delivery_error is None


def test_pause_keeps_the_address_and_the_delivery(tmp_path: Path) -> None:
    job = _job(tmp_path).model_copy(
        update={
            "last_run": "2026-10-01T12:00:00+00:00",
            "last_status": "ok",
            "last_delivery": "failed",
            "last_delivery_error": "SMTPException: auth failed",
        }
    )
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
        email_to=None,
    )
    assert edited.email_to == "owner@example.com"
    assert edited.last_delivery == "failed"
    assert edited.paused is True
