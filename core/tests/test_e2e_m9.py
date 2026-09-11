"""M9 exit test: mock autonomy run with a tripped breaker (TD-4304).

The harness lives in ``tstd.e2e_m9``. This wrapper pins it into CI.
Not marked ``live`` — the mock provider and a fake rootless runtime
are the path, like TD-3806. This harness is the M9 exit criterion.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_m9 import run_m9


async def test_m9_exit_harness(tmp_path: Path) -> None:
    result = await run_m9(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"M9 harness failed:\n{result.report()}"
    assert result.elapsed < 60.0
