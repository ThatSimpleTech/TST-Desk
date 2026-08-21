"""CLI entry for the M7 exit harness (TD-3806).

    uv run python scripts/e2e_m7.py [--workspace PATH] [--data-dir PATH]

Runs against the scripted mock provider. Exits 0 when every check
passes, 1 otherwise. The harness itself lives in ``tstd.e2e_m7``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.e2e_m7 import main

if __name__ == "__main__":
    sys.exit(main())
