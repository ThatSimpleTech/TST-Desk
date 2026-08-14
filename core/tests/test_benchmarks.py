"""Performance regression gates (TD-1404).

Each measured metric is compared against the committed baseline in
``perf_baselines.json``; a run fails when its median exceeds the stated
threshold — 3x the baseline or baseline + 250 ms, whichever is larger.
The slack term keeps sub-100 ms metrics from flapping on loaded CI
runners while the factor catches real algorithmic regressions.

Baselines are re-recorded deliberately, never by this suite:
``uv run python scripts/benchmarks.py --record``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tstd import benchmarks

BASELINE_PATH = Path(__file__).parent / "perf_baselines.json"

REGRESSION_FACTOR = 3.0
REGRESSION_SLACK_S = 0.25


def _payload() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("metric", list(benchmarks.MEASURERS))
async def test_no_regression(metric: str, tmp_path: Path) -> None:
    baseline = _payload()["metrics"][metric]
    assert baseline is not None, f"{metric} has no baseline — record one first"
    value = await benchmarks.MEASURERS[metric](tmp_path / metric)
    ceiling = max(baseline * REGRESSION_FACTOR, baseline + REGRESSION_SLACK_S)
    assert value <= ceiling, (
        f"{metric} regressed: {value:.3f}s vs baseline {baseline:.3f}s "
        f"(ceiling {ceiling:.3f}s = 3x baseline or +{REGRESSION_SLACK_S}s)"
    )


def test_baselines_cover_every_measurer() -> None:
    """A measurer without a baseline row is a silent gap in the gate."""
    payload = _payload()
    for metric in benchmarks.MEASURERS:
        assert metric in payload["metrics"], f"{metric} missing from perf_baselines.json"


def test_pending_metrics_name_their_blocker() -> None:
    """Pending criteria stay visible with the story that unblocks them."""
    payload = _payload()
    assert payload["pending"] == benchmarks.PENDING
    for metric, story in payload["pending"].items():
        assert payload["metrics"].get(metric) is None
        assert story.startswith("TD-")
