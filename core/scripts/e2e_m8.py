"""CLI entry for the M8 exit harness (TD-3904).

    uv run python scripts/e2e_m8.py [--workspace PATH] [--data-dir PATH]

Probes the shipped ``vllm`` loopback. Exits 0 on a live pass or a
heading-match skip, 1 on a failed live turn or an off-box refuse.
The harness itself lives in ``tstd.e2e_m8``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.e2e_m8 import main

if __name__ == "__main__":
    sys.exit(main())
