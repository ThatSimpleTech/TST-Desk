"""M7 exit test: refuse 0.0.0.0, run a loopback Slack job (TD-3806).

The harness lives in ``tstd.e2e_m7``. This wrapper pins it into CI.
Not marked ``live`` — the mock provider and injected ``notify_send``
are the path, like TD-3405. This harness is the M7 exit criterion.
"""

from __future__ import annotations

from pathlib import Path

from tstd.e2e_m7 import run_m7


async def test_m7_exit_harness(tmp_path: Path) -> None:
    result = await run_m7(tmp_path / "workspace", tmp_path / "data")
    assert result.ok, f"M7 harness failed:\n{result.report()}"
    assert result.elapsed < 60.0
