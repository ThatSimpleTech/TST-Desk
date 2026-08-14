"""CLI for the TD-1404 performance baselines.

    uv run python scripts/benchmarks.py            # measure, print
    uv run python scripts/benchmarks.py --record   # re-baseline

``--record`` rewrites ``tests/perf_baselines.json`` with fresh medians —
a deliberate act, never something the regression test does itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.benchmarks import PENDING, run_all

BASELINES = Path(__file__).resolve().parent.parent / "tests" / "perf_baselines.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TD-1404 performance baselines")
    parser.add_argument("--record", action="store_true", help="rewrite tests/perf_baselines.json")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="tstd-bench-") as scratch:
        report = asyncio.run(run_all(Path(scratch)))

    print(report.render())

    if args.record:
        payload = {
            "recorded_at": time.strftime("%Y-%m-%d"),
            "unit": "seconds (median of repetitions)",
            "threshold": "fail when a fresh median exceeds 3x baseline or baseline + 0.25s",
            "note": "first_token_latency uses an instant mock provider: it measures the "
            "internal pipeline, not network or model time.",
            "metrics": {name: value for name, value in report.metrics.items()},
            "pending": PENDING,
        }
        BASELINES.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nrecorded → {BASELINES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
