"""Click-target vs landing eval on the mock driver (TD-3304).

The default mock lands on the intended point. A scripted offset is a miss
when ``hypot`` exceeds ``CLICK_MISS_TOLERANCE_POINTS``. This helper never
labels a sample as live — it cannot see a real display.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import hypot

from .mock import MockDesktopDriver

# Logical points, same space as TD-3301 ``coordinate_space=points``.
# A miss strictly above this bound fails the eval; it does not ship as
# "it works".
CLICK_MISS_TOLERANCE_POINTS = 4.0

# Recorded mock-eval rows (TD-3304). Linux is a live CU platform
# (TD-2001) but has no click-target fixture page here — live accuracy
# is the sidecar desktop suite, not this table.
EVAL_PLATFORMS: tuple[str, ...] = ("darwin", "win32")

FIXTURE_PAGES: dict[str, str] = {
    "darwin": "fixture/macos-click-target.html",
    "win32": "fixture/windows-click-target.html",
}

# Same control box on both mock pages. Not a measured live coordinate.
FIXTURE_TARGET: tuple[float, float] = (240.0, 160.0)


def miss_points(intended: tuple[float, float], landed: tuple[float, float]) -> float:
    """Euclidean distance in points between intended and landed."""
    return hypot(landed[0] - intended[0], landed[1] - intended[1])


@dataclass(frozen=True)
class ClickSample:
    """One recorded click: where we aimed vs where the mock landed."""

    platform: str
    fixture: str
    intended_x: float
    intended_y: float
    landed_x: float
    landed_y: float
    source: str = "mock"

    @property
    def intended(self) -> tuple[float, float]:
        return (self.intended_x, self.intended_y)

    @property
    def landed(self) -> tuple[float, float]:
        return (self.landed_x, self.landed_y)

    @property
    def miss(self) -> float:
        return miss_points(self.intended, self.landed)

    def within_tolerance(self, tolerance: float = CLICK_MISS_TOLERANCE_POINTS) -> bool:
        return self.miss <= tolerance


class RecordingMockDesktop(MockDesktopDriver):
    """Eval-only mock: records intended (x, y) vs landed (x, y).

    ``landing_offset`` is injected by the eval. Default ``(0, 0)`` lands
    exactly. Not wired into the dispatcher.
    """

    def __init__(
        self,
        *,
        landing_offset: tuple[float, float] = (0.0, 0.0),
        platform: str = "darwin",
        fixture: str | None = None,
        foreground_title: str = "Mock Window",
        foreground_app: str = "mock",
    ) -> None:
        super().__init__(
            foreground_title=foreground_title,
            foreground_app=foreground_app,
        )
        if fixture is None:
            if platform not in FIXTURE_PAGES:
                raise ValueError(f"no eval fixture for {platform!r}")
            fixture = FIXTURE_PAGES[platform]
        self.landing_offset = landing_offset
        self.platform = platform
        self.fixture = fixture
        self.samples: list[ClickSample] = []

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        expect_window: str | None = None,
    ) -> str:
        dx, dy = self.landing_offset
        landed_x = x + dx
        landed_y = y + dy
        self.samples.append(
            ClickSample(
                platform=self.platform,
                fixture=self.fixture,
                intended_x=x,
                intended_y=y,
                landed_x=landed_x,
                landed_y=landed_y,
            )
        )
        return await super().click(
            landed_x,
            landed_y,
            button=button,
            count=count,
            expect_window=expect_window,
        )


def assert_samples_within_tolerance(
    samples: Sequence[ClickSample],
    *,
    tolerance: float = CLICK_MISS_TOLERANCE_POINTS,
) -> None:
    """Fail the eval when any recorded miss is over *tolerance* points."""
    misses = [sample for sample in samples if sample.miss > tolerance]
    if not misses:
        return
    detail = "; ".join(
        f"{sample.platform}/{sample.fixture}: intended={sample.intended} "
        f"landed={sample.landed} miss={sample.miss:.3f}pt"
        for sample in misses
    )
    raise AssertionError(
        f"click miss over {tolerance} pt tolerance — not shipping as it works: {detail}"
    )


async def run_recorded_eval(
    *,
    landing_offset: tuple[float, float] = (0.0, 0.0),
) -> list[ClickSample]:
    """One click on each supported-OS fixture page. Source is always mock."""
    samples: list[ClickSample] = []
    target_x, target_y = FIXTURE_TARGET
    for platform in EVAL_PLATFORMS:
        driver = RecordingMockDesktop(
            landing_offset=landing_offset,
            platform=platform,
        )
        await driver.click(target_x, target_y)
        samples.extend(driver.samples)
    return samples


def format_eval_markdown(samples: Sequence[ClickSample]) -> str:
    """Markdown table for DECISIONS.md — the recorded evaluation."""
    lines = [
        "| OS | fixture page | intended (pt) | landed (pt) | miss (pt) | source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for sample in samples:
        lines.append(
            f"| {sample.platform} | {sample.fixture} | "
            f"({sample.intended_x:.1f}, {sample.intended_y:.1f}) | "
            f"({sample.landed_x:.1f}, {sample.landed_y:.1f}) | "
            f"{sample.miss:.3f} | {sample.source} |"
        )
    return "\n".join(lines)
