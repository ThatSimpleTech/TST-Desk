"""CLI entry for the headless harness (TD-1401 mock, TD-1803 live).

    uv run python scripts/e2e_headless.py [--workspace PATH] [--data-dir PATH]
    uv run python scripts/e2e_headless.py --live-endpoint URL [--live-model SLUG]

Without ``--live-endpoint`` the pass runs against the scripted mock provider
and exits 0 when every check passes, 1 otherwise.  A live pass adds two
outcomes: 2 when it could not run (no model server, or a slug the endpoint
does not serve) and 3 when the endpoint broke the OpenAI-compatible
contract, which is a different fault from the agent loop failing a check.

The harness itself lives in ``tstd.e2e_harness``; this shim only handles the
import path and argv.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tstd.e2e_harness import main

if __name__ == "__main__":
    sys.exit(main())
