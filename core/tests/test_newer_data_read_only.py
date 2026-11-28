"""A data folder a newer version wrote is opened read-only (UPGRADE-02).

The test the manager set: drive a newer stamp, then a start, a send
attempt and a quit, and assert that no store file's content or mtime
changed. Log files may still be written, and port.json is how a window
finds this daemon, so both are left out of the comparison.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tstd.data_version import DATA_FORMAT, STAMP_NAME, set_read_only
from tstd.mock import MockProvider, Script

#: Written while running and allowed in read-only mode.
EXEMPT_PARTS = {"logs"}
EXEMPT_NAMES = {"port.json", "remote-port.json", "tstd.lock", "data_dir.lock"}


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()
        set_read_only(None)


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    found: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or EXEMPT_PARTS & set(rel.parts) or path.name in EXEMPT_NAMES:
            continue
        found[str(rel)] = (path.stat().st_mtime_ns, path.read_bytes())
    return found


async def test_nothing_in_a_newer_folder_changes(tmp_path: Path, fleet: Fleet) -> None:
    data = tmp_path / "d"
    work = tmp_path / "ws"
    work.mkdir()
    mock = MockProvider(default=Script(kind="stream", content="done"))

    # A real folder first: a chat with a turn in it, then a clean quit.
    first = await fleet.start(data, provider=mock)
    async with window(first) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "hi"}))
        await recv_until(ws, lambda f: f.get("type") == "turn_complete")
    await fleet.stop(first)

    # A newer version ran on it since.
    (data / STAMP_NAME).write_text(
        json.dumps({"format": DATA_FORMAT + 1, "written_by": "9.9.9"}), encoding="utf-8"
    )
    old = os.stat(data / STAMP_NAME).st_mtime_ns - 10_000_000_000
    for path in data.rglob("*"):
        if path.is_file():
            os.utime(path, ns=(old, old))  # any write moves an mtime past this
    before = _snapshot(data)

    second = await fleet.start(data, provider=mock)
    async with window(second) as ws:
        state = await ask(ws, {"type": "get_setup_state"}, "setup_state")
        assert state["data_written_by"] == "9.9.9"
        frame = {"type": "user_message", "session_id": sid, "content": "again"}
        refused = await ask(ws, frame, "error")
        assert refused["code"] == "data_read_only"
    await fleet.stop(second)

    after = _snapshot(data)
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert changed == [], "\n".join(changed)
