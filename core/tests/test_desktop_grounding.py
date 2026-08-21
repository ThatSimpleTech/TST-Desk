"""Grounding honesty: click target vs landing on the mock (TD-3304).

Default mock lands exactly. A scripted offset whose hypot is over the
stated tolerance fails the eval — that miss does not ship as "it works".
Rows are mock only; live pointer accuracy is not claimed. Linux is E20.
"""

from __future__ import annotations

from math import hypot
from pathlib import Path

import pytest

from tstd.desktop.grounding import (
    CLICK_MISS_TOLERANCE_POINTS,
    EVAL_PLATFORMS,
    FIXTURE_PAGES,
    FIXTURE_TARGET,
    RecordingMockDesktop,
    assert_samples_within_tolerance,
    format_eval_markdown,
    miss_points,
    run_recorded_eval,
)
from tstd.desktop.mcp_driver import LIVE_PLATFORMS

_DECISIONS = Path(__file__).resolve().parents[2] / "DECISIONS.md"
_SECTION_HEAD = "## 2026-08-21 — TD-3304"


def _td3304_section() -> str:
    text = _DECISIONS.read_text(encoding="utf-8")
    start = text.index(_SECTION_HEAD)
    rest = text[start:]
    nxt = rest.find("\n## ", 1)
    return rest if nxt == -1 else rest[:nxt]


class TestRecordedMockEval:
    async def test_default_lands_exactly_on_each_supported_os(self) -> None:
        samples = await run_recorded_eval()
        assert [s.platform for s in samples] == list(EVAL_PLATFORMS)
        assert EVAL_PLATFORMS == ("darwin", "win32")
        for sample, platform in zip(samples, EVAL_PLATFORMS, strict=True):
            assert sample.fixture == FIXTURE_PAGES[platform]
            assert sample.intended == FIXTURE_TARGET
            assert sample.landed == FIXTURE_TARGET
            assert sample.miss == 0.0
            assert sample.source == "mock"
        assert_samples_within_tolerance(samples)

    async def test_scripted_offset_over_tolerance_fails(self) -> None:
        # hypot(3, 3) ≈ 4.243 > 4.0
        samples = await run_recorded_eval(landing_offset=(3.0, 3.0))
        assert all(sample.miss > CLICK_MISS_TOLERANCE_POINTS for sample in samples)
        with pytest.raises(AssertionError, match="not shipping as it works"):
            assert_samples_within_tolerance(samples)

    async def test_scripted_offset_at_tolerance_passes(self) -> None:
        samples = await run_recorded_eval(landing_offset=(CLICK_MISS_TOLERANCE_POINTS, 0.0))
        assert all(sample.miss == CLICK_MISS_TOLERANCE_POINTS for sample in samples)
        assert_samples_within_tolerance(samples)

    async def test_recording_wrapper_keeps_intended_and_landed(self) -> None:
        driver = RecordingMockDesktop(landing_offset=(1.5, -2.0), platform="win32")
        await driver.click(100.0, 50.0)
        sample = driver.samples[0]
        assert sample.intended == (100.0, 50.0)
        assert sample.landed == (101.5, 48.0)
        assert sample.miss == pytest.approx(hypot(1.5, -2.0))
        assert sample.miss == pytest.approx(miss_points(sample.intended, sample.landed))


class TestHonestyBounds:
    def test_eval_platforms_match_live_cu_and_exclude_linux(self) -> None:
        assert set(EVAL_PLATFORMS) == set(LIVE_PLATFORMS)
        assert "linux" not in EVAL_PLATFORMS
        with pytest.raises(ValueError, match="E20"):
            RecordingMockDesktop(platform="linux")

    def test_tolerance_is_four_points(self) -> None:
        assert CLICK_MISS_TOLERANCE_POINTS == 4.0

    async def test_decisions_records_the_mock_eval(self) -> None:
        samples = await run_recorded_eval()
        section = _td3304_section()
        table = format_eval_markdown(samples)
        assert table in section
        assert "4.0" in section
        assert "live pointer accuracy is not claimed" in section.lower()
        assert "E20" in section
        linux_rows = [
            line
            for line in section.splitlines()
            if line.startswith("|") and "linux" in line.split("|")[1].strip().casefold()
        ]
        assert linux_rows == []
        assert "UI-TARS as a grounding model is TD-3902" in section
