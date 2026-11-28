"""The daemon beats while it starts, and stops once the port is open."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tstd.start_heartbeat import FILE_NAME, StartHeartbeat


def test_it_beats_with_the_pid_and_stops_with_the_port(tmp_path: Path) -> None:
    beat = StartHeartbeat(tmp_path).start()
    try:
        body = json.loads((tmp_path / FILE_NAME).read_text(encoding="utf-8"))
        assert body["pid"] == os.getpid()
        assert abs(body["at"] - time.time()) < 5
    finally:
        beat.stop()
    assert not (tmp_path / FILE_NAME).exists()
