"""An unreadable line in a chat's log leaves no hole in the replay (ERR2B-05).

The loader dropped the line and kept the others' seq numbers. A window read
the missing number as a lost frame and re-attached, the replay had the same
hole, and it looped about 90 times a second. Each unreadable line now loads
as an ``event_unreadable`` at its seq.
"""

from __future__ import annotations

import json
from pathlib import Path

from tstd.protocol import EventUnreadable
from tstd.session_persist import SessionPersist


def _line(seq: int, text: str = "hi") -> str:
    return json.dumps({"type": "assistant_delta", "session_id": "s1", "seq": seq, "delta": text})


def _load(tmp_path: Path, lines: list[str]) -> list[object]:
    path = tmp_path / "s1" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = SessionPersist._read_events(path)
    assert events is not None
    return events


def _seqs(events: list[object]) -> list[int]:
    return [e.seq for e in events]  # type: ignore[attr-defined]


def test_a_cut_line_in_the_middle_is_filled(tmp_path: Path) -> None:
    lines = [_line(1), _line(2), _line(3)[:30], _line(4)]
    events = _load(tmp_path, lines)
    assert _seqs(events) == [1, 2, 3, 4]
    hole = events[2]
    assert isinstance(hole, EventUnreadable)
    assert (hole.session_id, hole.kind) == ("s1", "")


def test_a_newer_builds_event_keeps_its_seq_and_type(tmp_path: Path) -> None:
    future = json.dumps({"type": "a_newer_event", "session_id": "s1", "seq": 2, "x": 1})
    events = _load(tmp_path, [_line(1), future, _line(3)])
    assert _seqs(events) == [1, 2, 3]
    assert isinstance(events[1], EventUnreadable) and events[1].kind == "a_newer_event"


def test_a_cut_last_line_with_no_seq_adds_nothing(tmp_path: Path) -> None:
    events = _load(tmp_path, [_line(1), _line(2), '{"type": "assistant_del'])
    assert _seqs(events) == [1, 2]


def test_a_good_log_loads_unchanged(tmp_path: Path) -> None:
    events = _load(tmp_path, [_line(1), _line(2)])
    assert not any(isinstance(e, EventUnreadable) for e in events)
    assert _seqs(events) == [1, 2]
