"""Validator drift check (TD-4201) — detect and report only."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_autonomy_loop import make_charter
from tests.test_dispatch import make_config
from tstd.autonomy.dod import DodItemResult, DodPoll
from tstd.autonomy.ledger import DecisionLedger, LedgerEntry
from tstd.autonomy.supervisor import (
    DriftCheckResult,
    assemble_check_prompt,
    invoke_validator,
    maybe_check_drift,
    parse_drift_answer,
    should_check,
)
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.router import TierRouter
from tstd.session import Session

CLEAN_JSON = '{"serves_objective": true, "class_a_drifted": false, "progress_real": true}'
DRIFT_JSON = '{"serves_objective": false, "class_a_drifted": true, "progress_real": false}'


def _session(
    workspace: Path,
    *,
    autonomy: bool = True,
    turns: int = 5,
    class_b: bool = False,
    check_every: int = 5,
    source_of_truth: list[str] | None = None,
) -> Session:
    session = Session(str(workspace))
    session.autonomy = autonomy
    session.charter = make_charter(max_iterations=40, source_of_truth=source_of_truth)
    session.autonomy_turns = turns
    session.autonomy_class_b = class_b
    session.autonomy_check_every = check_every
    return session


async def _capture(session: Session) -> list[str]:
    prompts: list[str] = []

    async def _validator(prompt: str) -> str:
        prompts.append(prompt)
        return CLEAN_JSON

    session.validator_call = _validator
    return prompts


# ── Schedule ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("autonomy", "turns", "class_b", "check_every", "expected"),
    [
        (True, 1, False, 5, False),
        (True, 4, False, 5, False),
        (True, 5, False, 5, True),
        (True, 10, False, 5, True),
        (True, 2, True, 5, True),
        (True, 1, True, 5, True),
        (False, 5, False, 5, False),
        (False, 5, True, 5, False),
        (True, 0, False, 5, False),
    ],
)
def test_should_check_table(
    autonomy: bool,
    turns: int,
    class_b: bool,
    check_every: int,
    expected: bool,
) -> None:
    assert (
        should_check(autonomy=autonomy, turns=turns, class_b=class_b, check_every=check_every)
        is expected
    )


async def test_every_n_fourth_turn_skips_fifth_checks(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=0)
    prompts = await _capture(session)
    for n in range(1, 5):
        session.autonomy_turns = n
        assert await maybe_check_drift(session) is None
    assert prompts == []
    session.autonomy_turns = 5
    result = await maybe_check_drift(session)
    assert result is not None
    assert len(prompts) == 1
    assert session.last_drift_check is result


async def test_class_b_checks_before_n(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=2, class_b=True)
    prompts = await _capture(session)
    result = await maybe_check_drift(session)
    assert result is not None
    assert len(prompts) == 1
    assert session.autonomy_class_b is False


async def test_interactive_session_never_checks(tmp_path: Path) -> None:
    session = _session(tmp_path, autonomy=False, turns=5, class_b=True)
    prompts = await _capture(session)
    assert await maybe_check_drift(session) is None
    assert prompts == []
    assert session.last_drift_check is None


# ── Prompt contents ──────────────────────────────────────────────────────


async def test_source_of_truth_is_reread_each_check(tmp_path: Path) -> None:
    spec = tmp_path / "SPEC.md"
    spec.write_text("v1-unique-bytes\n", encoding="utf-8")
    session = _session(tmp_path, turns=5, source_of_truth=["SPEC.md"])
    prompts = await _capture(session)
    first = await maybe_check_drift(session)
    assert first is not None
    assert "v1-unique-bytes" in prompts[0]
    spec.write_text("v2-changed-bytes\n", encoding="utf-8")
    session.autonomy_turns = 10
    second = await maybe_check_drift(session)
    assert second is not None
    assert "v2-changed-bytes" in prompts[1]
    assert "v1-unique-bytes" not in prompts[1]


async def test_escaped_source_of_truth_is_skipped(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5, source_of_truth=["../outside.md"])
    outside = tmp_path.parent / "outside.md"
    outside.write_text("should-not-appear\n", encoding="utf-8")
    prompts = await _capture(session)
    await maybe_check_drift(session)
    assert prompts
    assert "should-not-appear" not in prompts[0]


async def test_prompt_includes_charter_diff_ledger_tests(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)
    session.last_dod_poll = DodPoll(
        results=(
            DodItemResult(
                item="The suite is green",
                green=False,
                detail="1 failed",
                via="worker",
            ),
        )
    )
    await DecisionLedger(tmp_path).append(
        LedgerEntry(decision_class="B", what="chose a name", why="taste")
    )
    prompts = await _capture(session)
    await maybe_check_drift(session)
    text = prompts[0]
    assert "Ship the CSV importer" in text
    assert "chose a name" in text
    assert "1 failed" in text
    assert "## Diff" in text
    assert "## Ledger" in text
    assert "## Tests" in text


async def test_missing_dod_poll_uses_no_test_output(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)
    prompt = await assemble_check_prompt(session)
    assert "no test output" in prompt


# ── Parse ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "serves", "drifted", "progress", "detected", "unparseable"),
    [
        (CLEAN_JSON, True, False, True, False, False),
        (DRIFT_JSON, False, True, False, True, False),
        ("YES\nNO\nYES", True, False, True, False, False),
        ("NO\nDRIFT\nTHRASHING", False, True, False, True, False),
        ("1. YES\n2. NO\n3. YES", True, False, True, False, False),
        ("not a structured answer", None, None, None, True, True),
        ("", None, None, None, True, True),
    ],
)
def test_parse_drift_answer_table(
    raw: str,
    serves: bool | None,
    drifted: bool | None,
    progress: bool | None,
    detected: bool,
    unparseable: bool,
) -> None:
    result = parse_drift_answer(raw)
    assert result.serves_objective is serves
    assert result.class_a_drifted is drifted
    assert result.progress_real is progress
    assert result.drift_detected is detected
    assert result.unparseable is unparseable


async def test_result_exposes_three_questions(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)

    async def _validator(_prompt: str) -> str:
        return CLEAN_JSON

    session.validator_call = _validator
    result = await maybe_check_drift(session)
    assert isinstance(result, DriftCheckResult)
    assert result.serves_objective is True
    assert result.class_a_drifted is False
    assert result.progress_real is True
    assert result.drift_detected is False
    assert session.last_drift_check is result


async def test_unparseable_is_fail_closed_report_only(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)

    async def _validator(_prompt: str) -> str:
        return "I am not sure, maybe?"

    session.validator_call = _validator
    result = await maybe_check_drift(session)
    assert result is not None
    assert result.unparseable is True
    assert result.drift_detected is True
    assert session.autonomy_stop_reason is None


# ── Cost is not a user turn ──────────────────────────────────────────────


async def test_cost_is_validator_tier_not_a_user_turn(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)
    config = make_config()
    tracker = CostTracker(config)
    session.cost_tracker = tracker
    router = TierRouter()
    session.router = router
    mock = MockProvider(scripts={"test-validator": Script(kind="text", content=CLEAN_JSON)})

    async def client_for(_cfg: object) -> MockProvider:
        return mock

    turns_before = router.turn_count
    tier_before = router.active_tier
    raw = await invoke_validator(
        "check",
        config=config,
        client_for=client_for,
        tracker=tracker,
        session=session,
    )
    assert "serves_objective" in raw
    assert any(call.tier == "validator" for call in tracker.calls)
    assert tracker.turn_cost() == 0.0
    assert router.turn_count == turns_before
    assert router.active_tier == tier_before
    assert mock.calls and mock.calls[0].model == "test-validator"


async def test_maybe_check_records_validator_without_router(tmp_path: Path) -> None:
    session = _session(tmp_path, turns=5)
    config = make_config()
    tracker = CostTracker(config)
    router = TierRouter()
    session.router = router
    mock = MockProvider(scripts={"test-validator": Script(kind="text", content=CLEAN_JSON)})

    async def client_for(_cfg: object) -> MockProvider:
        return mock

    async def _call(prompt: str) -> str:
        return await invoke_validator(
            prompt, config=config, client_for=client_for, tracker=tracker, session=session
        )

    session.validator_call = _call
    result = await maybe_check_drift(session)
    assert result is not None
    assert result.serves_objective is True
    assert any(call.tier == "validator" for call in tracker.calls)
    assert router.turn_count == 0
    assert router.active_tier == "brain"
