"""TD-5193: logging an event costs a write, not a re-read of the whole log.

Every streamed delta is an event. Re-reading and parsing the events file
on each append made a long streamed reply cost (deltas per second) x (file
size) of CPU, held the GIL, and starved the loop reading the model's
stream until the stream watchdog withdrew a reply that had finished.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tstd.protocol import AssistantDelta, DaemonEvent, UserTurn
from tstd.session import SessionEventLog
from tstd.session_persist import AppendResult, SessionPersist

SID = "s1"


def _delta(seq: int, text: str | None = None) -> AssistantDelta:
    return AssistantDelta(session_id=SID, delta=text or f"e{seq}", seq=seq)


def _disk_seqs(persist: SessionPersist) -> list[int]:
    path = persist.dir_for(SID) / "events.jsonl"
    return [
        int(json.loads(line)["seq"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every time a ``SessionPersist`` reads a whole events file."""
    seen: list[Path] = []
    real: Callable[[Path], list[DaemonEvent] | None] = SessionPersist._read_events

    def spy(path: Path) -> list[DaemonEvent] | None:
        seen.append(path)
        return real(path)

    monkeypatch.setattr(SessionPersist, "_read_events", staticmethod(spy))
    return seen


class TestAppendIsAWrite:
    def test_in_order_appends_under_the_cap_never_read_the_file(
        self, tmp_path: Path, reads: list[Path]
    ) -> None:
        persist = SessionPersist(tmp_path, log_max_events=1000)
        persist.prepare(SID)
        results = [persist.append_event(SID, _delta(seq)) for seq in range(1, 301)]
        # The first append has never seen the file: one read builds the counts.
        assert len(reads) == 1
        assert all(r == AppendResult(earliest_seq=1, trimmed=False) for r in results)
        assert _disk_seqs(persist) == list(range(1, 301))

    def test_trim_goes_below_the_cap_so_the_next_appends_are_writes(
        self, tmp_path: Path, reads: list[Path]
    ) -> None:
        persist = SessionPersist(tmp_path, log_max_events=20)
        for seq in range(1, 21):
            assert not persist.append_event(SID, _delta(seq)).trimmed
        result = persist.append_event(SID, _delta(21))
        # 21 over a cap of 20: the newest 18 stay (a tenth of slack).
        assert result == AppendResult(earliest_seq=4, trimmed=True)
        assert _disk_seqs(persist) == list(range(4, 22))
        reads.clear()
        for seq in (22, 23):
            assert persist.append_event(SID, _delta(seq)) == AppendResult(
                earliest_seq=4, trimmed=False
            )
        assert reads == []
        assert len(_disk_seqs(persist)) == 20
        result = persist.append_event(SID, _delta(24))
        assert result == AppendResult(earliest_seq=7, trimmed=True)
        assert _disk_seqs(persist) == list(range(7, 25))

    def test_the_window_never_passes_the_cap(self, tmp_path: Path) -> None:
        persist = SessionPersist(tmp_path, log_max_events=50)
        for seq in range(1, 400):
            result = persist.append_event(SID, _delta(seq))
            on_disk = _disk_seqs(persist)
            assert len(on_disk) <= 50
            assert on_disk[0] == result.earliest_seq
            assert on_disk == list(range(on_disk[0], seq + 1))

    def test_a_restart_rebuilds_the_counts_from_the_file_once(
        self, tmp_path: Path, reads: list[Path]
    ) -> None:
        first = SessionPersist(tmp_path, log_max_events=20)
        for seq in range(1, 16):
            first.append_event(SID, _delta(seq))
        reads.clear()
        second = SessionPersist(tmp_path, log_max_events=20)
        for seq in range(16, 21):
            assert second.append_event(SID, _delta(seq)) == AppendResult(
                earliest_seq=1, trimmed=False
            )
        assert len(reads) == 1
        # The rebuilt count knows the file holds 20: the 21st trims.
        assert second.append_event(SID, _delta(21)) == AppendResult(earliest_seq=4, trimmed=True)

    def test_load_then_append_reads_the_file_again(self, tmp_path: Path, reads: list[Path]) -> None:
        writer = SessionPersist(tmp_path, log_max_events=100)
        for seq in range(1, 11):
            writer.append_event(SID, _delta(seq))
        # A smaller cap at load rewrites the file under the writer.
        capped = SessionPersist(tmp_path, log_max_events=3)
        loaded = capped.load(SID)
        assert loaded is not None and [e.seq for e in loaded.events] == [8, 9, 10]
        reads.clear()
        assert capped.append_event(SID, _delta(11)) == AppendResult(earliest_seq=9, trimmed=True)
        assert _disk_seqs(capped) == [9, 10, 11]

    def test_another_writer_is_noticed(self, tmp_path: Path) -> None:
        one = SessionPersist(tmp_path, log_max_events=5)
        two = SessionPersist(tmp_path, log_max_events=5)
        for seq in range(1, 13):
            result = (one if seq % 2 else two).append_event(SID, _delta(seq))
            on_disk = _disk_seqs(one)
            assert on_disk == list(range(max(1, seq - 4), seq + 1))
            assert result.earliest_seq == on_disk[0]

    def test_an_out_of_order_append_is_put_in_order(
        self, tmp_path: Path, reads: list[Path]
    ) -> None:
        persist = SessionPersist(tmp_path, log_max_events=100)
        for seq in (1, 2, 3, 5):
            persist.append_event(SID, _delta(seq))
        reads.clear()
        assert persist.append_event(SID, _delta(4)) == AppendResult(earliest_seq=1, trimmed=False)
        assert _disk_seqs(persist) == [1, 2, 3, 4, 5]
        assert len(reads) == 1
        persist.append_event(SID, _delta(6))
        assert len(reads) == 1  # in order again: a write
        assert _disk_seqs(persist) == [1, 2, 3, 4, 5, 6]

    def test_remove_then_append_starts_a_new_window(self, tmp_path: Path) -> None:
        persist = SessionPersist(tmp_path, log_max_events=100)
        for seq in range(1, 6):
            persist.append_event(SID, _delta(seq))
        persist.remove(SID)
        assert persist.append_event(SID, _delta(9)) == AppendResult(earliest_seq=9, trimmed=False)
        assert _disk_seqs(persist) == [9]

    def test_a_line_cut_short_does_not_swallow_the_next_event(self, tmp_path: Path) -> None:
        """A crash mid-write leaves half a line; the next append starts a new one."""
        persist = SessionPersist(tmp_path, log_max_events=100)
        persist.append_event(SID, UserTurn(session_id=SID, turn_id="t1", content="hi", seq=1))
        path = persist.dir_for(SID) / "events.jsonl"
        half = _delta(2, "año").model_dump_json().encode("utf-8")
        with path.open("ab") as f:
            f.write(half[: half.index("ñ".encode()) + 1])  # mid-character too
        restarted = SessionPersist(tmp_path, log_max_events=100)
        restarted.append_event(SID, _delta(3))
        loaded = restarted.load(SID)
        assert loaded is not None
        assert [e.seq for e in loaded.events] == [1, 3]

    def test_a_failed_rotation_is_tried_again_on_the_next_append(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        persist = SessionPersist(tmp_path, log_max_events=3)
        for seq in (1, 2, 3):
            persist.append_event(SID, _delta(seq))
        real_replace = os.replace
        failures = iter([True])

        def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if next(failures, False):
                raise OSError("disk full")
            real_replace(src, dst)

        monkeypatch.setattr("tstd.session_persist.os.replace", flaky)
        with caplog.at_level(logging.WARNING, logger="tstd.session_persist"):
            persist.append_event(SID, _delta(4))
        assert [r.getMessage() for r in caplog.records] == ["failed to rotate session events"]
        assert _disk_seqs(persist) == [1, 2, 3, 4]
        assert persist.append_event(SID, _delta(5)) == AppendResult(earliest_seq=3, trimmed=True)
        assert _disk_seqs(persist) == [3, 4, 5]


class TestAppendCost:
    def test_append_cost_does_not_grow_with_the_file(self, tmp_path: Path) -> None:
        """20k appends of a long streamed reply, in budget and flat in cost.

        Re-reading the file on each append made this quadratic: tens of
        milliseconds per append by ten thousand lines.
        """
        total, block = 20_000, 2_000
        persist = SessionPersist(tmp_path, log_max_events=total + 1)
        text = "thinking about the next step of the plan, " * 2
        per_append: list[float] = []
        started = time.perf_counter()
        for seq in range(1, total + 1):
            t = time.perf_counter()
            persist.append_event(SID, _delta(seq, text))
            per_append.append(time.perf_counter() - t)
        elapsed = time.perf_counter() - started
        assert elapsed < 60, f"{total} appends took {elapsed:.1f}s"
        early = statistics.median(per_append[block : 2 * block])
        late = statistics.median(per_append[-block:])
        assert late < 3 * early + 0.0005, f"early {early * 1e3:.3f} ms, late {late * 1e3:.3f} ms"


class TestEventsFrom:
    async def test_the_tail_comes_back_in_seq_order(self) -> None:
        log = SessionEventLog()
        for n in range(50):
            await log.add(_delta(1, f"d{n}"))
        assert [e.seq for e in log.events_from(45)] == [45, 46, 47, 48, 49, 50]
        assert [e.seq for e in log.events_from(0)] == list(range(1, 51))
        assert log.events_from(51) == []
        await log.drop_before(30)
        assert [e.seq for e in log.events_from(1)] == list(range(30, 51))
        assert [e.seq for e in log.events_from(49)] == [49, 50]

    async def test_a_revived_log_is_in_order_whatever_disk_had(self) -> None:
        log = SessionEventLog()
        log.replace([_delta(seq) for seq in (5, 3, 9, 4)])
        assert [e.seq for e in log.events_from(4)] == [4, 5, 9]
        await log.add(_delta(1))
        assert [e.seq for e in log.events_from(9)] == [9, 10]

    async def test_a_log_out_of_order_falls_back_to_a_sort(self) -> None:
        """Adding an event already in the log moves its seq; reads stay right."""
        log = SessionEventLog()
        events = [_delta(1, f"d{n}") for n in range(5)]
        for event in events:
            await log.add(event)
        await log.add(events[1])  # the same object again: seq 2 becomes 6
        # The old sort's answer, the object twice, not a bisect's.
        assert [e.seq for e in log.events_from(5)] == [5, 6, 6]
        assert [e.seq for e in log.events_from(1)] == [1, 3, 4, 5, 6, 6]
        await log.add(events[-1])  # seq 5 becomes 7
        assert [e.seq for e in log.events_from(3)] == [3, 4, 6, 6, 7, 7]
        # A revive installs a sorted log: bisects again.
        log.replace(log.all_events)
        assert log._ordered
        assert [e.seq for e in log.events_from(6)] == [6, 6, 7, 7]

    async def test_events_from_does_not_scan_the_whole_log(self) -> None:
        log = SessionEventLog()
        log.replace([_delta(seq) for seq in range(1, 20_001)])
        started = time.perf_counter()
        for seq in range(19_000, 20_001):
            assert log.events_from(seq)[0].seq == seq
        # A scan and a sort of 20k events, a thousand times, is seconds.
        assert time.perf_counter() - started < 1.0
