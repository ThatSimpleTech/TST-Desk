"""A real tstd whose shutdown never finishes on its own (TD-5021).

Run from ``core/`` as ``python -m tests.shutdown_child DATA_DIR [PARENT_PID]``.
The MCP close phase never returns, so only the 5 s shutdown budget can
end the process. A test sends one trigger (parent death, the websocket
``shutdown``) and checks the process still exits inside the budget.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from tstd.daemon import Daemon
from tstd.logging import log_directory, setup_logging


async def _hang() -> None:
    await asyncio.Event().wait()


def main() -> None:
    data_dir = Path(sys.argv[1])
    parent = int(sys.argv[2]) if len(sys.argv) > 2 else None
    setup_logging(level="INFO", log_dir=log_directory(data_dir))

    async def _run() -> None:
        daemon = Daemon(data_dir=data_dir, parent_pid=parent)
        daemon._parent_poll_interval = 0.05
        daemon._mcp.aclose = _hang  # type: ignore[method-assign]
        await daemon.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
