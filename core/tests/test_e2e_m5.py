"""M5 exit test: the coworker harness must pass (TD-3204).

The harness lives in ``tstd.e2e_m5``. This wrapper pins it into CI.
Not marked ``live`` — the mock provider is the path, like TD-1401.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_m5 import run_m5


async def test_m5_exit_harness(tmp_path: Path) -> None:
    result = await run_m5(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"M5 harness failed:\n{result.report()}"
    assert result.elapsed < 60.0
