"""Retry a scheduled run that failed for a transient reason (TD-3814).

A down provider at 7:45 tries again after the delay, then the regular
slot resumes. A rejected key, a full context, and a missing folder do
not. Run now never retries. Delivery is once per slot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.test_loop import make_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.scheduler.history import list_runs
from tstd.scheduler.models import Job, JobDraft, JobValidationError, validate_draft
from tstd.scheduler.retry import (
    RetryError,
    failure_reason,
    is_transient_failure,
    parse_retries,
    parse_retry_delay,
)
from tstd.scheduler.runner import RecordingDeliver, TurnResult, run_due_jobs, run_manual_job
from tstd.scheduler.store import get_job, list_jobs, save_job

_NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_RETRY = "2026-08-21T18:10:00+00:00"
_REGULAR = "2026-08-21T19:00:00+00:00"
_LATER = "2026-08-21T20:00:00+00:00"

# The acceptance table. A reason that is not here and not a 5xx/429 is
# not transient; these rows are the ones the story names, plus the codes
# the provider actually emits for them.
_REASONS: list[tuple[str | None, bool]] = [
    ("connection_error", True),
    ("timeout", True),
    ("timed_out", True),
    ("rate_limited", True),
    ("rate_limit", True),
    ("server_error", True),
    ("bad_gateway", True),
    ("service_unavailable", True),
    ("gateway_timeout", True),
    ("http_500", True),
    ("http_502", True),
    ("http_503", True),
    ("http_504", True),
    ("http_599", True),
    ("HTTP_503", True),
    ("500", True),
    ("429", True),
    ("http_429", True),
    ("scheduled run failed: timed out waiting for turn_complete", True),
    ("connection refused", True),
    ("Connection reset by peer", True),
    ("ConnectError: failed", True),
    ("rate limit exceeded", True),
    ("context_overflow", False),
    ("context_length_exceeded", False),
    ("auth_failed", False),
    ("api_key_rejected", False),
    ("missing_api_key", False),
    ("workspace_missing", False),
    ("preset_missing", False),
    ("engine_unavailable", False),
    ("workspace is not a directory: /gone", False),
    ("preset 'vllm' no longer exists", False),
    ("grok engine is unavailable", False),
    ("disk full", False),
    ("http_400", False),
    ("http_401", False),
    ("http_404", False),
    ("http_413", False),
    ("401", False),
    ("600", False),
    ("", False),
    (None, False),
]


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
        "retries": 1,
        "retry_delay": "10 minutes",
    }
    data.update(over)
    return Job.model_validate(data)


def _daemon(tmp_path: Path, provider: MockProvider | None = None) -> tuple[Daemon, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = _workspace(tmp_path)
    daemon = Daemon(
        data_dir=data_dir,
        provider=provider or MockProvider(default=Script(kind="stream", content="digest ready")),
    )
    daemon.config = make_config()
    return daemon, data_dir, workspace


async def _turn(workspace: Path, message: str) -> TurnResult:
    del workspace, message
    return TurnResult("digest ready")


@pytest.mark.parametrize(("reason", "transient"), _REASONS)
def test_transient_classification(reason: str | None, transient: bool) -> None:
    assert is_transient_failure(reason) is transient


def test_the_error_code_wins_over_a_sentence_that_looks_retryable() -> None:
    reason = failure_reason(
        ok=False,
        error_code="context_overflow",
        summary="timed out talking to the provider",
    )
    assert is_transient_failure(reason) is False
    assert (
        is_transient_failure(
            failure_reason(ok=False, error_code="auth_failed", summary="connection refused")
        )
        is False
    )
    assert failure_reason(ok=True, error_code="timeout", summary="digest ready") is None


@pytest.mark.parametrize(
    ("value", "count"),
    [(None, 0), ("", 0), (0, 0), (1, 1), (3, 3), ("2", 2)],
)
def test_parse_retries(value: object, count: int) -> None:
    assert parse_retries(value) == count


@pytest.mark.parametrize("value", [True, False, 4, -1, "4", "soon", 1.5])
def test_parse_retries_rejects(value: object) -> None:
    with pytest.raises(RetryError, match="whole number"):
        parse_retries(value)


def test_a_blank_delay_is_unset_and_a_phrase_is_seconds() -> None:
    assert parse_retry_delay(None) is None
    assert parse_retry_delay("") is None
    assert parse_retry_delay("10 minutes") == 600
    assert parse_retry_delay(600) == 600


def test_retry_delay_refuses_a_secret_without_echoing_it() -> None:
    key = "sk-" + "a" * 40  # tst-secret-ok
    with pytest.raises(RetryError, match="must not contain secrets") as caught:
        parse_retry_delay(key)
    assert key not in str(caught.value)


def test_retries_default_the_delay_to_ten_minutes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    job = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            retries=2,
        )
    )
    assert job.retries == 2
    assert job.retry_delay == 600
    assert job.attempt == 0
    assert job.resume_at is None
    none = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
        )
    )
    assert none.retries == 0
    assert none.retry_delay is None


def test_a_boolean_retry_count_is_not_one(tmp_path: Path) -> None:
    raw = _job(_workspace(tmp_path)).model_dump()
    raw["retries"] = True
    with pytest.raises(ValidationError):
        Job.model_validate(raw)


def test_a_job_file_without_retry_fields_still_loads(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    raw = _job(workspace, retries=0).model_dump()
    raw.pop("retries")
    raw.pop("retry_delay")
    raw.pop("attempt")
    raw.pop("resume_at")
    save_job(tmp_path, Job.model_validate(raw))
    stored = list_jobs(tmp_path)[0]
    assert stored.retries == 0
    assert stored.retry_delay is None
    assert stored.attempt == 0
    assert stored.resume_at is None


async def _fire(
    data: Path,
    now: datetime,
    result: TurnResult,
) -> RecordingDeliver:
    deliver = RecordingDeliver()

    async def turn(_ws: Path, _message: str) -> TurnResult:
        return result

    await run_due_jobs(data, now, run_turn=turn, deliver=deliver)
    return deliver


async def test_a_transient_failure_retries_then_succeeds_once(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    save_job(data, _job(_workspace(tmp_path)))
    first = await _fire(
        data,
        _NOW,
        TurnResult("provider down", ok=False, error_code="connection_error", session_id="s1"),
    )
    assert first.records == []
    waiting = get_job(data, "inbox")
    assert waiting is not None
    assert waiting.attempt == 1
    assert waiting.resume_at == _REGULAR
    assert waiting.next_run == _RETRY
    assert waiting.last_status == "failed"
    assert waiting.last_summary == "attempt 1 of 2: provider down"
    assert waiting.paused is False

    second = await _fire(
        data,
        datetime(2026, 8, 21, 18, 10, tzinfo=UTC),
        TurnResult("digest ready", ok=True, session_id="s2"),
    )
    assert second.records == [("window", "attempt 2 of 2: digest ready")]
    done = get_job(data, "inbox")
    assert done is not None
    assert done.attempt == 0
    assert done.resume_at is None
    assert done.next_run == _REGULAR
    assert done.last_status == "ok"
    assert done.last_summary == "attempt 2 of 2: digest ready"
    runs = list_runs(data, "inbox")
    assert [(run.attempt, run.attempts, run.status, run.summary) for run in runs] == [
        (2, 2, "ok", "digest ready"),
        (1, 2, "failed", "provider down"),
    ]
    assert all(run.trigger == "schedule" for run in runs)


@pytest.mark.parametrize(
    ("result", "summary"),
    [
        (TurnResult("no", ok=False, error_code="auth_failed"), "no"),
        (TurnResult("no", ok=False, error_code="api_key_rejected"), "no"),
        (TurnResult("no", ok=False, error_code="context_overflow"), "no"),
        (
            TurnResult("workspace is not a directory: /gone", ok=False),
            "workspace is not a directory: /gone",
        ),
        (
            TurnResult("preset 'vllm' no longer exists", ok=False),
            "preset 'vllm' no longer exists",
        ),
        (TurnResult("disk full", ok=False), "disk full"),
    ],
)
async def test_a_non_transient_failure_does_not_retry(
    tmp_path: Path, result: TurnResult, summary: str
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    save_job(data, _job(_workspace(tmp_path), retries=3))
    deliver = await _fire(data, _NOW, result)
    stored = get_job(data, "inbox")
    assert stored is not None
    assert stored.attempt == 0
    assert stored.resume_at is None
    assert stored.next_run == _REGULAR
    assert stored.last_status == "failed"
    assert deliver.records == [("window", f"attempt 1 of 4: {summary}")]
    runs = list_runs(data, "inbox")
    assert len(runs) == 1
    assert runs[0].attempt == 1
    assert runs[0].attempts == 4
    assert runs[0].summary == summary
    # The retry instant is not due. Nothing fires again until the regular slot.
    again = await _fire(
        data,
        datetime(2026, 8, 21, 18, 10, tzinfo=UTC),
        TurnResult("should not run"),
    )
    assert again.records == []
    still = get_job(data, "inbox")
    assert still is not None
    assert still.last_summary == f"attempt 1 of 4: {summary}"


async def test_exhausted_retries_resume_the_regular_slot_and_reset(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = _workspace(tmp_path)
    save_job(data, _job(workspace, retries=1))
    down = TurnResult("provider down", ok=False, error_code="timeout")
    assert (await _fire(data, _NOW, down)).records == []
    second = await _fire(data, datetime(2026, 8, 21, 18, 10, tzinfo=UTC), down)
    assert second.records == [("window", "attempt 2 of 2: provider down")]
    stored = get_job(data, "inbox")
    assert stored is not None
    assert stored.attempt == 0
    assert stored.resume_at is None
    assert stored.next_run == _REGULAR
    assert len(list_runs(data, "inbox")) == 2

    # The resumed slot starts at attempt 1 again, not at 3.
    third = await _fire(data, datetime(2026, 8, 21, 19, 0, tzinfo=UTC), down)
    assert third.records == []
    again = get_job(data, "inbox")
    assert again is not None
    assert again.attempt == 1
    assert again.resume_at == _LATER
    assert again.next_run == "2026-08-21T19:10:00+00:00"
    assert again.last_summary == "attempt 1 of 2: provider down"


async def test_a_success_resets_the_counter(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    save_job(data, _job(_workspace(tmp_path)))
    await _fire(data, _NOW, TurnResult("down", ok=False, error_code="server_error"))
    await _fire(
        data,
        datetime(2026, 8, 21, 18, 10, tzinfo=UTC),
        TurnResult("ok", ok=True),
    )
    stored = get_job(data, "inbox")
    assert stored is not None and stored.attempt == 0 and stored.resume_at is None
    await _fire(
        data,
        datetime(2026, 8, 21, 19, 0, tzinfo=UTC),
        TurnResult("down", ok=False, error_code="http_503"),
    )
    nxt = get_job(data, "inbox")
    assert nxt is not None
    assert nxt.attempt == 1
    assert nxt.last_summary == "attempt 1 of 2: down"


async def test_run_now_never_retries(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = _workspace(tmp_path)
    armed = (_NOW + timedelta(hours=2)).isoformat()
    save_job(data, _job(workspace, next_run=armed, retries=3))
    deliver = RecordingDeliver()

    async def turn(_ws: Path, _message: str) -> TurnResult:
        return TurnResult("provider down", ok=False, error_code="connection_error")

    armed_job = get_job(data, "inbox") or _job(workspace)
    await run_manual_job(data, armed_job, _NOW, run_turn=turn, deliver=deliver)
    stored = get_job(data, "inbox")
    assert stored is not None
    assert stored.next_run == armed
    assert stored.attempt == 0
    assert stored.resume_at is None
    assert stored.paused is False
    assert stored.last_status == "failed"
    assert deliver.records == [("window", "provider down")]
    runs = list_runs(data, "inbox")
    assert len(runs) == 1
    assert runs[0].trigger == "manual"
    assert runs[0].attempt is None
    assert runs[0].attempts is None
    assert runs[0].scheduled_for is None
    # Still not due, so the tick does not pick up a retry the manual run invented.
    assert await run_due_jobs(data, _NOW, run_turn=turn, deliver=RecordingDeliver()) == []


async def test_grace_skips_a_late_regular_slot_and_not_a_late_retry(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    workspace = _workspace(tmp_path)
    late = (_NOW - timedelta(hours=10)).isoformat()
    calls: list[str] = []

    async def turn(_ws: Path, _message: str) -> TurnResult:
        calls.append("ran")
        return TurnResult("digest ready")

    save_job(data, _job(workspace, next_run=late, grace="30 minutes", retries=3))
    deliver = RecordingDeliver()
    assert await run_due_jobs(data, _NOW, run_turn=turn, deliver=deliver) == ["inbox"]
    assert calls == []
    skipped = get_job(data, "inbox")
    assert skipped is not None
    assert skipped.last_status == "missed"
    assert skipped.attempt == 0
    assert skipped.resume_at is None
    assert skipped.next_run == _REGULAR
    assert len(deliver.records) == 1

    save_job(
        data,
        _job(
            workspace,
            next_run=late,
            grace=1800,
            retries=1,
            attempt=1,
            resume_at=_REGULAR,
        ),
    )
    calls.clear()
    retry_deliver = RecordingDeliver()
    assert await run_due_jobs(data, _NOW, run_turn=turn, deliver=retry_deliver) == ["inbox"]
    assert calls == ["ran"]
    resumed = get_job(data, "inbox")
    assert resumed is not None
    assert resumed.last_status == "ok"
    assert resumed.attempt == 0
    assert resumed.resume_at is None
    assert resumed.next_run == _REGULAR
    assert resumed.last_summary == "attempt 2 of 2: digest ready"
    assert retry_deliver.records == [("window", resumed.last_summary)]


async def test_a_one_shot_is_spent_after_the_retry_succeeds(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    save_job(
        data,
        _job(_workspace(tmp_path), cadence=None, next_run=_NOW.isoformat(), retries=1),
    )
    first = await _fire(data, _NOW, TurnResult("down", ok=False, error_code="bad_gateway"))
    assert first.records == []
    waiting = get_job(data, "inbox")
    assert waiting is not None
    assert waiting.paused is False
    assert waiting.next_run == _RETRY
    assert waiting.attempt == 1
    assert waiting.resume_at is None
    done_deliver = await _fire(
        data,
        datetime(2026, 8, 21, 18, 10, tzinfo=UTC),
        TurnResult("digest ready"),
    )
    assert done_deliver.records == [("window", "attempt 2 of 2: digest ready")]
    spent = get_job(data, "inbox")
    assert spent is not None
    assert spent.paused is True
    assert spent.next_run is None
    assert spent.attempt == 0
    assert spent.cadence is None


async def test_save_keeps_pause_and_clears_retries(tmp_path: Path) -> None:
    daemon, data, workspace = _daemon(tmp_path)
    try:
        created = json.loads(
            await daemon._handle_message(
                json.dumps(
                    {
                        "type": "save_job",
                        "id": "inbox",
                        "workspace": str(workspace),
                        "instruction": "summarize the inbox",
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                        "retries": 2,
                    }
                ),
                None,
            )
            or ""
        )
        assert created["type"] == "job_list"
        row = created["jobs"][0]
        assert row["retries"] == 2
        assert row["retry_delay"] == 600
        assert row["attempt"] == 0

        save_job(
            data,
            (get_job(data, "inbox") or _job(workspace)).model_copy(
                update={"attempt": 1, "resume_at": _REGULAR, "next_run": _RETRY}
            ),
        )
        paused = json.loads(
            await daemon._handle_message(
                json.dumps(
                    {
                        "type": "save_job",
                        "id": "inbox",
                        "paused": True,
                        "next_run": _RETRY,
                        "cadence": "every 1 hour",
                    }
                ),
                None,
            )
            or ""
        )
        held = get_job(data, "inbox")
        assert held is not None
        assert held.paused is True
        assert held.attempt == 1
        assert held.resume_at == _REGULAR
        assert held.retries == 2
        assert held.next_run == _RETRY
        assert paused["jobs"][0]["attempt"] == 1

        cleared = json.loads(
            await daemon._handle_message(
                json.dumps(
                    {
                        "type": "save_job",
                        "id": "inbox",
                        # Omitted paused is false on the wire. Save sends the row's flag.
                        "paused": True,
                        "retries": 0,
                        "retry_delay": "",
                    }
                ),
                None,
            )
            or ""
        )
        assert cleared["type"] == "job_list"
        restored = get_job(data, "inbox")
        assert restored is not None
        assert restored.retries == 0
        assert restored.retry_delay is None
        assert restored.attempt == 0
        assert restored.resume_at is None
        assert restored.next_run == _REGULAR
        assert restored.paused is True

        bad = json.loads(
            await daemon._handle_message(
                json.dumps(
                    {
                        "type": "save_job",
                        "id": "nope",
                        "retries": 4,
                        "workspace": str(workspace),
                        "instruction": "x",
                        "cadence": "every 1 hour",
                        "deliver_to": "window",
                    }
                ),
                None,
            )
            or ""
        )
        assert bad["code"] == "job_invalid"
        assert "Retries" in bad["message"]
        assert get_job(data, "nope") is None
    finally:
        await daemon._shutdown()


async def test_the_provider_code_retries_then_delivers_once(tmp_path: Path) -> None:
    """The mock fails the first turn with a connection error, then answers.

    This is the path a real scheduled fire uses: the code comes off
    ``turn_complete``, not off a test double's ``TurnResult``.
    """
    provider = MockProvider(
        default=Script(
            kind="stream",
            content="digest ready",
            fail_times=1,
            status_code=503,
            error_code="connection_error",
        )
    )
    daemon, data, workspace = _daemon(tmp_path, provider)
    save_job(data, _job(workspace, retries=1))
    try:
        assert await daemon.run_due_jobs(_NOW) == ["inbox"]
        waiting = get_job(data, "inbox")
        assert waiting is not None
        assert waiting.attempt == 1
        assert waiting.next_run == _RETRY
        assert waiting.resume_at == _REGULAR
        assert waiting.last_status == "failed"
        assert daemon._scheduler_deliver.records == []
        assert await daemon.run_due_jobs(datetime(2026, 8, 21, 18, 10, tzinfo=UTC)) == ["inbox"]
        done = get_job(data, "inbox")
        assert done is not None
        assert done.attempt == 0
        assert done.resume_at is None
        assert done.next_run == _REGULAR
        assert done.last_status == "ok"
        assert done.last_summary == "attempt 2 of 2: digest ready"
        assert daemon._scheduler_deliver.records == [("window", done.last_summary)]
        runs = list_runs(data, "inbox")
        assert [run.attempt for run in runs] == [2, 1]
        assert runs[0].summary == "digest ready"
    finally:
        await daemon._shutdown()


def test_turning_retries_off_is_a_validation_error_not_a_secret_echo(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    with pytest.raises(JobValidationError, match="Retry delay") as caught:
        validate_draft(
            JobDraft(
                workspace=str(workspace),
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                retries=1,
                retry_delay="soon",
            )
        )
    assert "soon" in str(caught.value)
    key = "sk-" + "b" * 40  # tst-secret-ok
    with pytest.raises(JobValidationError, match="must not contain secrets") as secret:
        validate_draft(
            JobDraft(
                workspace=str(workspace),
                instruction="summarize the inbox",
                cadence="every 1 hour",
                deliver_to="window",
                retries=1,
                retry_delay=key,
            )
        )
    assert key not in str(secret.value)
