"""Plain-English cadences, time zones and readable errors (TD-3808)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tstd.scheduler.models import (
    Job,
    JobDraft,
    JobValidationError,
    normalize_cadence,
    validate_draft,
)
from tstd.scheduler.parse import parse_job_request
from tstd.scheduler.phrases import find_phrase
from tstd.scheduler.schedule import advance_job, arm_cadence_job, next_run_after


@pytest.mark.parametrize(
    ("text", "cron"),
    [
        ("7:45 on weekdays", "45 7 * * 1-5"),
        ("weekdays at 7:45am", "45 7 * * 1-5"),
        ("Every Weekday at 7:45 AM", "45 7 * * 1-5"),
        ("daily at 9", "0 9 * * *"),
        ("every day at 9:30pm", "30 21 * * *"),
        ("at 9am every day", "0 9 * * *"),
        ("mondays at 8", "0 8 * * 1"),
        ("mon, wed and fri at 17:00", "0 17 * * 1,3,5"),
        ("tues and thurs at 6pm", "0 18 * * 2,4"),
        ("weekends at 10am", "0 10 * * 0,6"),
        ("hourly", "0 * * * *"),
        ("daily", "0 0 * * *"),
        ("noon", "0 12 * * *"),
        ("midnight", "0 0 * * *"),
        ("at 7:45", "45 7 * * *"),
        ("12am daily", "0 0 * * *"),
        ("12pm daily", "0 12 * * *"),
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("0 9 * * 1-5", "0 9 * * 1-5"),
        ("Every 3 Hours", "every 3 hours"),
    ],
)
def test_cadence_normalizes(text: str, cron: str) -> None:
    assert normalize_cadence(text) == cron


@pytest.mark.parametrize(
    "text",
    [
        "25:00",
        "at 7:61",
        "13pm",
        "0pm",
        "weekdays at 9 and 10",
        "sometimes",
        "every 2",
        "hourly at 9",
    ],
)
def test_cadence_rejects_what_it_cannot_read(text: str) -> None:
    with pytest.raises(JobValidationError, match="cadence not understood"):
        normalize_cadence(text)


def test_unrecognised_cadence_message_is_the_documented_one() -> None:
    with pytest.raises(JobValidationError) as info:
        normalize_cadence("whenever")
    assert str(info.value) == (
        "cadence not understood: 'whenever'. "
        "Try 'weekdays at 7:45', 'every 2 hours', or cron like '45 7 * * 1-5'"
    )


@pytest.mark.parametrize(
    "expr",
    [
        "99 99 * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
    ],
)
def test_cron_out_of_range_is_refused_at_save(expr: str) -> None:
    with pytest.raises(JobValidationError, match="impossible"):
        normalize_cadence(expr)


def test_cron_range_edges_are_allowed() -> None:
    assert normalize_cadence("59 23 31 12 7") == "59 23 31 12 7"


def test_phrase_is_found_inside_a_sentence_and_cut_out() -> None:
    draft = parse_job_request(
        "weekdays at 7:45am in ~/Documents/AST summarize my inbox deliver to window"
    )
    assert draft.cadence == "45 7 * * 1-5"
    assert draft.workspace == "~/Documents/AST"
    assert draft.instruction == "summarize my inbox"
    assert draft.deliver_to == "window"


def test_phrase_after_the_instruction() -> None:
    draft = parse_job_request("summarize the inbox every day at 9 in /ws/one")
    assert draft.cadence == "0 9 * * *"
    assert draft.instruction == "summarize the inbox"


@pytest.mark.parametrize(
    "text",
    ["look at 3 files", "on the day of the release", "summarize the inbox", "meet at 25:00"],
)
def test_ordinary_sentences_have_no_phrase(text: str) -> None:
    assert find_phrase(text) is None


def test_users_exact_input_is_friendly_and_cron_is_stored() -> None:
    job = validate_draft(
        JobDraft(workspace="/tmp", instruction="x", cadence="7:45 on weekdays", deliver_to="window")
    )
    assert job.cadence == "45 7 * * 1-5"


def test_users_exact_failure_is_one_readable_line() -> None:
    with pytest.raises(JobValidationError) as info:
        validate_draft(
            JobDraft(
                workspace="Client Reports",
                instruction="x",
                cadence="7:45 on wkdays",
                deliver_to="window",
            )
        )
    message = str(info.value)
    assert "pydantic.dev" not in message
    assert "[type=" not in message
    assert "Value error" not in message
    assert "\n" not in message
    assert message.startswith(
        "Workspace: must be a full folder path such as ~/Documents/project (got 'Client Reports'); "
        "Cadence: not understood: '7:45 on wkdays'."
    )


def test_unknown_timezone_is_refused() -> None:
    with pytest.raises(JobValidationError, match="Time zone: 'Mars/Base' is not a known"):
        validate_draft(
            JobDraft(
                workspace="/tmp",
                instruction="x",
                cadence="daily",
                deliver_to="window",
                timezone="Mars/Base",
            )
        )


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_cron_is_read_in_the_zone_across_dst() -> None:
    cron = "45 7 * * 1-5"
    friday_evening = _utc(2026, 9, 25, 23, 0)
    assert friday_evening.weekday() == 4
    assert next_run_after(cron, friday_evening, "America/Chicago") == _utc(2026, 9, 28, 12, 45)
    after_fall_back = _utc(2026, 11, 6, 23, 0)
    assert next_run_after(cron, after_fall_back, "America/Chicago") == _utc(2026, 11, 9, 13, 45)


def test_no_timezone_is_utc_as_before() -> None:
    friday_evening = _utc(2026, 9, 25, 23, 0)
    assert next_run_after("45 7 * * 1-5", friday_evening) == _utc(2026, 9, 28, 7, 45)
    assert next_run_after("45 7 * * 1-5", friday_evening, None) == next_run_after(
        "45 7 * * 1-5", friday_evening, "UTC"
    )


def test_interval_ignores_the_zone() -> None:
    now = _utc(2026, 9, 25, 23, 0)
    assert next_run_after("every 2 hours", now, "Asia/Tokyo") == now + timedelta(hours=2)


def test_spring_forward_gap_is_skipped_not_shifted() -> None:
    # 02:30 does not exist in Chicago on 2026-03-08.
    nxt = next_run_after("30 2 * * *", _utc(2026, 3, 7, 12, 0), "America/Chicago")
    assert nxt == _utc(2026, 3, 9, 7, 30)


def test_fall_back_hour_fires_once() -> None:
    first = next_run_after("30 1 * * *", _utc(2026, 11, 1, 0, 0), "America/Chicago")
    assert first == _utc(2026, 11, 1, 6, 30)  # 01:30 CDT, the first occurrence
    assert next_run_after("30 1 * * *", first, "America/Chicago") == _utc(2026, 11, 2, 7, 30)


def test_a_year_out_cron_is_found() -> None:
    nxt = next_run_after("0 0 31 12 *", _utc(2026, 1, 1, 0, 0), "Pacific/Auckland")
    assert nxt == _utc(2026, 12, 30, 11, 0)


def _job(**extra: str) -> Job:
    return Job(
        id="j",
        workspace="/tmp",
        instruction="x",
        cadence="45 7 * * 1-5",
        deliver_to="window",
        **extra,
    )


def test_arm_and_advance_use_the_job_zone() -> None:
    now = _utc(2026, 9, 25, 23, 0)
    armed = arm_cadence_job(_job(timezone="America/Chicago"), now)
    assert armed.next_run == "2026-09-28T12:45:00+00:00"
    advanced = advance_job(armed, _utc(2026, 9, 28, 12, 45))
    assert advanced.next_run == "2026-09-29T12:45:00+00:00"
    assert arm_cadence_job(_job(), now).next_run == "2026-09-28T07:45:00+00:00"


def test_old_rows_without_timezone_still_load() -> None:
    assert Job.model_validate({**_job().model_dump(), "timezone": None}).timezone is None
    raw = _job().model_dump()
    del raw["timezone"]
    assert Job.model_validate(raw).timezone is None
