"""M10 exit test: mock MCP, slash, and skill invoke (TD-4604).

The harness lives in ``tstd.e2e_m10``. This wrapper pins it into CI.
Not marked ``live`` — the mock provider and a fake stdio MCP speaker
are the path, like TD-4304. This harness is the M10 exit criterion.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_m10 import run_m10


async def test_m10_exit_harness(tmp_path: Path) -> None:
    result = await run_m10(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"M10 harness failed:\n{result.report()}"
    assert result.elapsed < 60.0
