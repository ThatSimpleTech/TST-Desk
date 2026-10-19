"""The after-write review prompt fits the validator's window (TD-5153).

The review sent every diff and every shell output of the turn. On a local
validator the whole prompt is read before the first streamed chunk, so a
big turn timed out at the tier's read timeout, and one past the slot size
failed outright. The prompt is now cut to a token budget: shell output
first (each output keeps its tail), then the oldest diffs, with markers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tstd.autonomy.review_fit import (
    REVIEW_USER,
    TRIMMED_USER,
    fit_material,
    review_budget,
)
from tstd.autonomy.verify import maybe_verify_after_turn, note_tool_result
from tstd.compaction import estimate_tokens
from tstd.config import AutonomyConfig, ModelConfig, Preset, TierConfig
from tstd.context import PromptAssembler
from tstd.context.calibration import CalibrationStore
from tstd.context.tokens import HeuristicTokenCounter
from tstd.cost import CostTracker
from tstd.mock import MockProvider, Script
from tstd.protocol import VerifyResult
from tstd.session import Session

_BASE = "http://forge.test/v1"
_VALIDATOR_KEY = (_BASE, "test-validator")


def _tier(slug: str, *, window: int, output: int) -> TierConfig:
    return TierConfig(
        slug=slug,
        base_url=_BASE,
        input_price=1.0,
        output_price=2.0,
        cache_read_price=0.5,
        context_window=window,
        max_output_tokens=output,
    )


def _config(
    *, window: int = 8_000, output: int = 1_000, cap: int | None = None, verify: str = "after_write"
) -> ModelConfig:
    big = 100_000
    return ModelConfig(
        presets={
            "test": Preset(
                brain=_tier("test-brain", window=big, output=1_000),
                worker=_tier("test-worker", window=big, output=1_000),
                validator=_tier("test-validator", window=window, output=output),
            )
        },
        active_preset="test",
        autonomy=AutonomyConfig.model_validate(
            {"verify": verify, **({} if cap is None else {"verify_max_prompt_tokens": cap})}
        ),
    )


def _diff(n: int, lines: int = 60) -> str:
    body = "\n".join(f"+line {n}-{j} = compute_value({j})" for j in range(lines))
    return f"--- a/file{n}.py\n+++ b/file{n}.py\n@@ -0,0 +1,{lines} @@\n{body}\n"


def _output(n: int, lines: int = 150) -> str:
    body = "\n".join(f"out {n}-{j} collecting tests ... ok" for j in range(lines))
    return f"{body}\n== {n} passed in 0.{n}s =="


def _validator(prompt_tokens: int = 500) -> MockProvider:
    return MockProvider(
        scripts={
            "test-validator": Script(
                kind="text", content="verdict: pass\nLooks right.", prompt_tokens=prompt_tokens
            )
        }
    )


async def _review(
    tmp_path: Path,
    config: ModelConfig,
    diffs: list[str],
    outputs: list[str],
    *,
    provider: MockProvider | None = None,
    store: CalibrationStore | None = None,
) -> tuple[VerifyResult, list[Any]]:
    """Run one after-write review over a real validator prompt."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    session = Session(str(workspace))
    for output in outputs:
        note_tool_result(session, "shell", "success", output=output)
    for diff in diffs:
        note_tool_result(session, "fs_write", "success", diff=diff)
    mock = provider or _validator()

    async def client_for(tier: TierConfig) -> MockProvider:
        del tier
        return mock

    await maybe_verify_after_turn(
        session,
        config,
        CostTracker(config),
        client_for,
        PromptAssembler(str(workspace), home_dir=tmp_path / "home"),
        store if store is not None else CalibrationStore(),
    )
    results = [e for e in session.event_log.all_events if isinstance(e, VerifyResult)]
    assert len(results) == 1
    return results[0], [c for c in mock.calls if c.model == "test-validator"]


def _sent(call: Any) -> tuple[str, str]:
    system, user = call.messages
    return system.content, user.content


def _estimate(call: Any) -> int:
    return estimate_tokens(list(call.messages), HeuristicTokenCounter())[0]


# ── The budget ──────────────────────────────────────────────────────────


class TestBudget:
    def test_default_is_sixty_percent_of_the_window(self) -> None:
        tier = _tier("v", window=65_536, output=4_096)
        assert review_budget(tier, None) == 39_321

    def test_a_configured_cap_is_used(self) -> None:
        tier = _tier("v", window=65_536, output=4_096)
        assert review_budget(tier, 30_000) == 30_000

    def test_never_more_than_the_window_less_the_reply(self) -> None:
        tier = _tier("v", window=65_536, output=4_096)
        assert review_budget(tier, 200_000) == 61_440
        assert review_budget(_tier("v", window=10_000, output=8_000), None) == 2_000

    def test_the_cap_has_a_floor(self) -> None:
        with pytest.raises(ValueError):
            AutonomyConfig.model_validate({"verify_max_prompt_tokens": 100})
        assert AutonomyConfig().verify_max_prompt_tokens is None


# ── Cutting the material ────────────────────────────────────────────────


class TestFitMaterial:
    def test_material_that_fits_is_untouched(self) -> None:
        material = fit_material([_diff(0)], [_output(0)], 100_000)
        assert material.diff == _diff(0)
        assert material.test_output == _output(0)
        assert not material.trimmed
        assert material.note() == ""

    def test_shell_output_goes_first_and_keeps_its_tail(self) -> None:
        diffs = [_diff(n, lines=10) for n in range(3)]
        outputs = [_output(n) for n in range(2)]
        room = len("\n\n".join(diffs)) + 3_000
        material = fit_material(diffs, outputs, room)

        assert material.size <= room
        assert material.omitted_diffs == 0
        assert material.diff == "\n\n".join(diffs)
        rendered = material.test_output.split("\n\n")
        assert len(rendered) == 2
        total_omitted = 0
        for n, text in enumerate(rendered):
            marker, *tail = text.split("\n")
            omitted = int(marker.removeprefix("(").split(" ")[0])
            assert omitted > 0
            assert marker == f"({omitted} earlier lines omitted)"
            # The tail is whole lines, ending with the run's result.
            original = _output(n).splitlines()
            assert tail == original[omitted:]
            assert tail[-1] == f"== {n} passed in 0.{n}s =="
            total_omitted += omitted
        assert material.omitted_lines == total_omitted
        note = material.note()
        assert (
            note
            == f"Left out to fit the validator's window: {total_omitted} lines of shell output."
        )

    def test_the_oldest_diffs_go_next_and_the_newest_stays(self) -> None:
        diffs = [_diff(n) for n in range(6)]
        outputs = [_output(n) for n in range(3)]
        room = len(diffs[-1]) * 3
        material = fit_material(diffs, outputs, room)

        assert material.size <= room
        assert material.omitted_diffs >= 1
        assert material.diff.startswith(f"({material.omitted_diffs} earlier diffs omitted)")
        assert material.diff.endswith(diffs[-1])
        assert "+++ b/file0.py" not in material.diff
        # Shell output kept a tail of every run, with its result.
        for n in range(3):
            assert f"== {n} passed in 0.{n}s ==" in material.test_output
        assert material.test_output.count("earlier lines omitted") == 3
        assert f"{material.omitted_diffs} earlier diff" in material.note()

    def test_the_newest_diff_loses_its_end_last(self) -> None:
        diffs = [_diff(0), _diff(1, lines=400)]
        outputs = [_output(0)]
        room = 4_000
        material = fit_material(diffs, outputs, room)

        assert material.size <= room
        assert material.omitted_diffs == 1
        assert material.test_output == "(1 shell output omitted)"
        assert material.cut_diff_lines > 0
        assert f"({material.cut_diff_lines} later lines of this diff omitted)" in material.diff
        assert "+++ b/file1.py" in material.diff
        assert not material.diff_lost
        assert "the last" in material.note()

    def test_no_room_for_any_diff_is_marked(self) -> None:
        material = fit_material([_diff(0)], [_output(0)], 50)
        assert material.diff_lost

    @pytest.mark.parametrize("room", [300, 1_000, 2_500, 6_000, 12_000, 20_000])
    def test_the_cut_material_never_passes_its_room(self, room: int) -> None:
        diffs = [_diff(n, lines=20 + 15 * n) for n in range(5)]
        outputs = [_output(n, lines=40 + 60 * n) for n in range(4)]
        material = fit_material(diffs, outputs, room)
        assert material.size <= room or material.diff_lost
        assert material.trimmed

    def test_a_single_long_line_keeps_its_end(self) -> None:
        line = "x" * 9_000 + "END"
        material = fit_material([_diff(0, lines=2)], [line], 3_000)
        assert material.size <= 3_000
        assert material.test_output.endswith("END")
        assert "…" in material.test_output


# ── The review end to end ───────────────────────────────────────────────


class TestReviewFitsTheWindow:
    async def test_a_big_turn_is_cut_to_the_budget(self, tmp_path: Path) -> None:
        config = _config(window=8_000, output=1_000)
        diffs = [_diff(n, lines=120) for n in range(6)]
        outputs = [_output(n) for n in range(4)]
        result, calls = await _review(tmp_path, config, diffs, outputs)

        assert len(calls) == 1
        # Before TD-5153 the whole turn went out, about 12k tokens.
        assert _estimate(calls[0]) <= 4_800
        system, user = _sent(calls[0])
        assert diffs[-1] in system
        assert "+++ b/file0.py" not in system
        assert "earlier diffs omitted)" in system
        for n in range(4):
            assert f"== {n} passed in 0.{n}s ==" in system
            assert f"out {n}-0 collecting" not in system
        assert "earlier lines omitted)" in system
        assert user == REVIEW_USER + TRIMMED_USER
        assert result.verdict == "pass"
        assert result.summary.startswith("verdict: pass\nLooks right.")
        assert "Left out to fit the validator's window:" in result.summary
        assert "earlier diff" in result.summary

    async def test_a_turn_that_fits_is_sent_whole(self, tmp_path: Path) -> None:
        config = _config(window=100_000, output=1_000)
        result, calls = await _review(tmp_path, config, [_diff(0)], [_output(0)])

        system, user = _sent(calls[0])
        assert _diff(0) in system
        assert _output(0) in system
        assert "omitted)" not in system
        assert user == REVIEW_USER
        assert result.summary == "verdict: pass\nLooks right."

    async def test_the_configured_cap_bounds_the_prompt(self, tmp_path: Path) -> None:
        config = _config(window=100_000, output=1_000, cap=2_000)
        diffs = [_diff(n) for n in range(4)]
        outputs = [_output(n) for n in range(3)]
        _, calls = await _review(tmp_path, config, diffs, outputs)

        assert len(calls) == 1
        assert _estimate(calls[0]) <= 2_000
        assert diffs[-1] in _sent(calls[0])[0]

    async def test_the_estimate_uses_the_validator_endpoint_ratio(self, tmp_path: Path) -> None:
        store = CalibrationStore()
        store.observe(_VALIDATOR_KEY, 1_000, 2_000)  # This endpoint tokenizes 2x.
        config = _config(window=8_000, output=1_000)
        diffs = [_diff(n) for n in range(6)]
        outputs = [_output(n) for n in range(4)]
        _, calls = await _review(
            tmp_path, config, diffs, outputs, provider=_validator(1_000_000), store=store
        )

        # The raw 4-chars/token estimate fits half the budget, so the
        # calibrated one fits the budget.
        assert _estimate(calls[0]) * 2 <= 4_800
        # The review's own usage moves the ratio (clamped sample 3.0).
        assert store.ratio(_VALIDATOR_KEY) == pytest.approx(2.0 + 0.3 * (3.0 - 2.0))

    async def test_a_new_validator_endpoint_learns_from_its_review(self, tmp_path: Path) -> None:
        store = CalibrationStore()
        config = _config(window=100_000, output=1_000)
        _, calls = await _review(
            tmp_path,
            config,
            [_diff(n) for n in range(3)],
            [_output(0)],
            provider=_validator(1_000_000),
            store=store,
        )
        assert len(calls) == 1
        assert store.ratio(_VALIDATOR_KEY) == pytest.approx(3.0)

    async def test_steering_over_the_budget_is_not_sent(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text("Keep every rule.\n" * 3_000)
        config = _config(window=8_000, output=1_000)
        result, calls = await _review(tmp_path, config, [_diff(0)], [_output(0)])

        assert calls == []
        assert result.verdict == "error"
        assert result.summary.startswith("Not reviewed:")
        assert "4,800" in result.summary
