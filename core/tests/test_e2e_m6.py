"""M6 exit test: mock desktop CU + mock TD-1710 browser path (TD-3405).

The harness lives in ``tstd.e2e_m6``. This wrapper pins it into CI.
Not marked ``live`` — the mock drivers are the path, like TD-3204.
The live Playwright test is opt-in and must not claim a browser it
did not launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tstd.browser import playwright_available
from tstd.e2e_m6 import have_display, run_m6, run_m6_browser_live


async def test_m6_exit_harness(tmp_path: Path) -> None:
    result = await run_m6(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"M6 harness failed:\n{result.report()}"
    assert result.elapsed < 60.0


@pytest.mark.live
async def test_m6_browser_live(tmp_path: Path) -> None:
    if not playwright_available():
        pytest.skip("Playwright is not installed; CI green is the mock six-verb path")
    if not have_display():
        pytest.skip("no display; not a live browser")
    result = await run_m6_browser_live(tmp_path / "workspace", tmp_path / "data")
    launched = next(
        (ok for name, ok, _ in result.checks if name == "live browser launched"),
        False,
    )
    if not launched:
        detail = next(
            (detail for name, _, detail in result.checks if name == "live browser launched"),
            "Playwright did not launch",
        )
        pytest.skip(f"not a live browser: {detail}")
    assert result.ok, f"M6 live browser failed:\n{result.report()}"
