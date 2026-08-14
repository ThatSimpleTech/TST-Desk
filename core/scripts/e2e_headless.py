"""CLI entry for the M1 headless harness (TD-1401).

    uv run python scripts/e2e_headless.py [--workspace PATH] [--data-dir PATH]

Exits 0 when every check passes, 1 otherwise.  The harness itself lives in
``tstd.e2e_harness``; this shim only handles the import path and argv.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.e2e_harness import main

if __name__ == "__main__":
    sys.exit(main())
