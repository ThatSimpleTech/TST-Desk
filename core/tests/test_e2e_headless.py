"""M1 exit test: the headless harness must pass (TD-1401).

The harness itself lives in ``tstd.e2e_harness``; this wrapper pins it
into CI with the story's sixty-second budget.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_harness import run


async def test_headless_harness_m1(tmp_path: Path) -> None:
    result = await run(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"harness failed:\n{result.report()}"
    assert result.elapsed < 60.0
