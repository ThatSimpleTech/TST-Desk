"""A heartbeat while the daemon starts, so the host waits on progress, not a clock.

The host gave up on a daemon that had not written port.json within 30
seconds, and a long-used install (183 chats) took longer than that in 0.5.4
and 0.5.5. From the moment the process starts until port.json exists, this
rewrites ``starting.json`` in the data folder every couple of seconds with
the pid, the phase and the time. The host keeps waiting while it is fresh,
up to a generous ceiling, and still fails fast on a daemon that never beats.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path

FILE_NAME = "starting.json"
BEAT_SECONDS = 2.0


class StartHeartbeat:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / FILE_NAME
        self.phase = "starting"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="start-heartbeat", daemon=True)

    def start(self) -> StartHeartbeat:
        self._beat()
        self._thread.start()
        return self

    def stop(self) -> None:
        """The port is open: the host no longer needs a heartbeat."""
        self._stop.set()
        with contextlib.suppress(OSError):
            self.path.unlink()

    def _run(self) -> None:
        while not self._stop.wait(BEAT_SECONDS):
            self._beat()

    def _beat(self) -> None:
        body = json.dumps({"pid": os.getpid(), "phase": self.phase, "at": time.time()})
        tmp = self.path.with_name(f".{FILE_NAME}.{os.getpid()}.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
