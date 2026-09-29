"""Report accuracy and B-rate for recorded classifier judgments (TD-708).

    uv run python scripts/eval_judgment_accuracy.py
    uv run python scripts/eval_judgment_accuracy.py --fixtures PATH

The fixture's replies are the whole input.  There is no network call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.autonomy.judgment_eval import format_report, run_recorded_eval

_DEFAULT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "judgment_accuracy.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score recorded classifier judgments.")
    parser.add_argument("--fixtures", type=Path, default=_DEFAULT)
    args = parser.parse_args(argv)
    print(format_report(run_recorded_eval(args.fixtures)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
