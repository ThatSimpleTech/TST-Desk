"""An older build never empties the history (UPGRADE-01).

A row with one key the running build did not know was dropped, and the next
save wrote back only the rows that loaded: one start of an older build on
newer data lost every chat for good. Unknown keys are now carried through,
and a row that truly cannot be read is dropped only after a copy is kept.
"""

from __future__ import annotations

import json
from pathlib import Path

from tstd.session_store import _STORE_FILE, SessionRecord, SessionStore


def _copies(folder: Path) -> list[Path]:
    return sorted(folder.glob(f"{_STORE_FILE}.unread-*"))


def _rows(n: int, **extra: object) -> list[dict[str, object]]:
    return [
        {
            "session_id": f"s{i}",
            "workspace_path": "/ws",
            "state": "idle",
            "title": f"Chat {i}",
            **extra,
        }
        for i in range(n)
    ]


async def test_rows_from_a_newer_build_survive_a_save(tmp_path: Path) -> None:
    path = tmp_path / _STORE_FILE
    path.write_text(json.dumps(_rows(3, reviewer=None, future={"nested": [1, 2]})))
    store = SessionStore(tmp_path)
    assert sorted(r.session_id for r in store.records()) == ["s0", "s1", "s2"]
    await store.upsert("s9", "/ws", "idle")  # a new chat: the save that used to lose them
    saved = {row["session_id"]: row for row in json.loads(path.read_text())}
    assert sorted(saved) == ["s0", "s1", "s2", "s9"]
    assert saved["s0"]["reviewer"] is None
    assert saved["s0"]["future"] == {"nested": [1, 2]}
    assert saved["s0"]["title"] == "Chat 0"
    assert "unknown" not in saved["s0"] and "unknown" not in saved["s9"]
    assert not _copies(tmp_path)


async def test_this_builds_field_wins_over_a_carried_one(tmp_path: Path) -> None:
    record = SessionRecord.from_row({**_rows(1)[0], "zzz": 1})
    record.title = "Renamed"
    row = record.to_row()
    assert row["title"] == "Renamed" and row["zzz"] == 1


async def test_a_row_that_cannot_be_read_is_dropped_only_after_a_copy(tmp_path: Path) -> None:
    path = tmp_path / _STORE_FILE
    rows: list[object] = [*_rows(2), {"workspace_path": "/ws"}, "not a row"]
    path.write_text(json.dumps(rows))
    store = SessionStore(tmp_path)
    assert sorted(r.session_id for r in store.records()) == ["s0", "s1"]
    [copy] = _copies(tmp_path)
    assert json.loads(copy.read_text()) == rows
    assert copy.stat().st_mode & 0o777 == 0o600
