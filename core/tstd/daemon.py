"""TST Desk daemon — async process with graceful lifecycle.

The daemon owns sessions, the WebSocket server, and the event loop.
It is spawned by the Tauri host and communicates via a local WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging import get_logger, setup_logging, user_data_dir
from .ws import WebSocketServer

log = get_logger("tstd.daemon")


@dataclass
class DaemonState:
    """Mutable state tracked by the daemon."""

    started_at: float = field(default_factory=time.time)
    active_sessions: int = 0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at


class Daemon:
    """Async daemon process with clean startup and shutdown.

    Usage:
        daemon = Daemon()
        await daemon.run()
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or user_data_dir()
        self.state = DaemonState()
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self.ws_server = WebSocketServer(self.data_dir)

    async def run(self) -> None:
        """Start the daemon and run until shutdown is requested."""
        log.info(
            "starting",
            extra={"extra_fields": {"version": self._version(), "data_dir": str(self.data_dir)}},
        )

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._on_signal)

        # Start subsystems
        try:
            await self._serve()
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

        log.info(
            "stopped",
            extra={"extra_fields": {"uptime": f"{self.state.uptime:.1f}s"}},
        )

    async def _serve(self) -> None:
        """Main serving loop — start subsystems and wait for shutdown."""
        await self.ws_server.start()
        await self._shutdown_event.wait()

    async def _shutdown(self) -> None:
        """Graceful shutdown: cancel tasks, close sockets, flush state."""
        log.info("shutting down")

        # Stop the WebSocket server
        await self.ws_server.stop()

        # Cancel all running tasks
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        log.info("shutdown complete")

    def _on_signal(self) -> None:
        """Handle OS signals for graceful shutdown."""
        log.info("signal received")
        self._shutdown_event.set()

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__

    def health(self) -> dict[str, Any]:
        """Return a health/diagnostics snapshot."""
        return {
            "version": self._version(),
            "uptime": self.state.uptime,
            "active_sessions": self.state.active_sessions,
            "data_dir": str(self.data_dir),
            "shutdown_requested": self._shutdown_event.is_set(),
            "ws_port": self.ws_server.port,
            "ws_clients": self.ws_server.client_count,
        }


def main() -> None:
    """CLI entry point: run the daemon until interrupted."""
    parser = argparse.ArgumentParser(description="TST Desk daemon")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument("--data-dir", default=None, help="Override the user data directory")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    setup_logging(level=args.log_level)

    async def _run() -> None:
        daemon = Daemon(data_dir=data_dir)
        await daemon.run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
