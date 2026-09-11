"""CLI entry for the M9 exit harness (TD-4304).

    uv run python scripts/e2e_m9.py [--workspace PATH] [--data-dir PATH]

Runs against the scripted mock provider and a fake rootless runtime.
Exits 0 when every check passes, 1 otherwise. The harness itself lives
in ``tstd.e2e_m9``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.e2e_m9 import main

if __name__ == "__main__":
    sys.exit(main())
