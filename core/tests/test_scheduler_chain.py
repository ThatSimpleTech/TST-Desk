"""Run one job after another succeeds (TD-3817).

An ok end starts ``then`` once. The child's own slot stays put. A
failure, a skip, and a park do not. A cycle is refused at save. Five
follow-ons run; the sixth does not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tstd.daemon import Daemon
from tstd.scheduler.chain import require_chain
from tstd.scheduler.history import list_runs
from tstd.scheduler.models import Job, JobDraft, JobValidationError, validate_draft
from tstd.scheduler.runner import (
    InFlight,
    RecordingDeliver,
    TurnResult,
    run_due_jobs,
    run_manual_job,
)
from tstd.scheduler.store import get_job, jobs_path, save_job

_NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
_FUTURE = "2026-09-01T18:00:00+00:00"
_PARENT_NEXT = "2026-08-21T19:00:00+00:00"
_RETRY_AT = datetime(2026, 8, 21, 18, 10, tzinfo=UTC)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _job(workspace: Path, **over: object) -> Job:
    data: dict[str, object] = {
        "id": "digest",
        "workspace": str(workspace),
        "instruction": "summarize the inbox",
        "cadence": "every 1 hour",
        "next_run": _NOW.isoformat(),
        "deliver_to": "window",
    }
    data.update(over)
    return Job.model_validate(data)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    return data, _workspace(tmp_path)


class _Turns:
    """Scripted turns, keyed by instruction. An unscripted fire succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._queued: dict[str, list[TurnResult]] = {}

    def when(self, instruction: str, *results: TurnResult) -> _Turns:
        self._queued.setdefault(instruction, []).extend(results)
        return self

    async def __call__(self, _workspace: Path, message: str) -> TurnResult:
        self.calls.append(message)
        queued = self._queued.get(message)
        if queued:
            return queued.pop(0)
        return TurnResult(message)


async def _due(
    data: Path,
    turns: _Turns,
    now: datetime = _NOW,
    guard: InFlight | None = None,
) -> RecordingDeliver:
    deliver = RecordingDeliver()
    await run_due_jobs(data, now, run_turn=turns, deliver=deliver, in_flight=guard)
    return deliver


def _idle(guard: InFlight, *ids: str) -> None:
    for job_id in ids:
        assert job_id not in guard


def _store(data: Path, workspace: Path, **parent_over: object) -> None:
    child = _job(
        workspace,
        id="follow",
        instruction="draft the follow-up",
        cadence=None,
        next_run=_FUTURE,
    )
    save_job(data, child)
    save_job(data, _job(workspace, then="follow", **parent_over))


async def _handle(daemon: Daemon, payload: dict[str, Any]) -> dict[str, Any]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _save(workspace: Path, job_id: str, **over: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "save_job",
        "id": job_id,
        "workspace": str(workspace),
        "instruction": job_id,
        "cadence": "every 1 hour",
        "deliver_to": "window",
    }
    payload.update(over)
    return payload


def test_then_is_a_job_id_and_a_blank_clears_it(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert _job(workspace, then="").then is None
    assert _job(workspace, then="  ").then is None
    assert _job(workspace, then=None).then is None
    for bad in ("a/b", "..", "."):
        with pytest.raises(ValidationError):
            _job(workspace, then=bad)


def test_validate_draft_does_not_require_the_follow_on_to_exist(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    job = validate_draft(
        JobDraft(
            workspace=str(workspace),
            instruction="summarize the inbox",
            cadence="every 1 hour",
            deliver_to="window",
            then="missing",
        )
    )
    assert job.then == "missing"


def test_a_receipt_save_accepts_a_dangling_follow_on(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    save_job(data, _job(workspace, then="missing"))
    stored = get_job(data, "digest")
    assert stored is not None and stored.then == "missing"


def test_a_job_file_without_then_still_loads(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    save_job(data, _job(workspace))
    path = jobs_path(data)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["jobs"][0]["then"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    stored = get_job(data, "digest")
    assert stored is not None and stored.then is None


def test_cycle_messages_name_the_loop(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    alone = _job(workspace, id="A", instruction="A", cadence=None, next_run=_FUTURE)
    points_at_a = _job(
        workspace,
        id="B",
        instruction="B",
        cadence=None,
        next_run=_FUTURE,
        then="A",
    )
    with pytest.raises(JobValidationError, match=r"^A → B → A$"):
        require_chain([alone, points_at_a], alone.model_copy(update={"then": "B"}))
    with pytest.raises(JobValidationError, match=r"^A → A$"):
        require_chain([alone], alone.model_copy(update={"then": "A"}))
    downstream = _job(
        workspace,
        id="C",
        instruction="C",
        cadence=None,
        next_run=_FUTURE,
        then="B",
    )
    b_to_c = points_at_a.model_copy(update={"then": "C"})
    with pytest.raises(JobValidationError, match=r"^B → C → B$"):
        require_chain([alone, b_to_c, downstream], alone.model_copy(update={"then": "B"}))
    with pytest.raises(JobValidationError, match=r"^Then: no job 'missing'$"):
        require_chain([alone], alone.model_copy(update={"then": "missing"}))
    require_chain([alone, b_to_c, downstream.model_copy(update={"then": None})], b_to_c)
    require_chain([alone], alone)


async def test_an_ok_scheduled_run_starts_its_follow_on_once(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    _store(data, workspace)
    guard = InFlight()
    turns = _Turns()
    turns.when("summarize the inbox", TurnResult("digest ready"))
    turns.when("draft the follow-up", TurnResult("draft ready"))
    deliver = await _due(data, turns, guard=guard)

    assert turns.calls == ["summarize the inbox", "draft the follow-up"]
    parent = get_job(data, "digest")
    child = get_job(data, "follow")
    assert parent is not None and child is not None
    assert parent.last_status == "ok"
    assert parent.next_run == _PARENT_NEXT
    assert parent.then == "follow"
    assert child.next_run == _FUTURE
    assert child.cadence is None
    assert child.paused is False
    assert child.last_status == "ok"
    parent_runs = list_runs(data, "digest")
    assert len(parent_runs) == 1 and parent_runs[0].trigger == "schedule"
    assert parent_runs[0].note is None
    runs = list_runs(data, "follow")
    assert len(runs) == 1
    assert runs[0].trigger == "chained"
    assert runs[0].status == "ok"
    assert runs[0].scheduled_for is None
    assert runs[0].note == "after digest"
    assert runs[0].attempt is None
    assert runs[0].summary == "draft ready"
    assert deliver.records == [("window", "digest ready"), ("window", "draft ready")]
    _idle(guard, "digest", "follow")

    again = await _due(data, turns, guard=InFlight())
    assert again.records == []
    assert turns.calls == ["summarize the inbox", "draft the follow-up"]
    assert len(list_runs(data, "follow")) == 1


async def test_run_now_starts_the_follow_on_without_moving_either_schedule(
    tmp_path: Path,
) -> None:
    data, workspace = _dirs(tmp_path)
    parent_slot = "2026-09-01T18:00:00+00:00"
    child_slot = "2026-09-02T18:00:00+00:00"
    save_job(
        data,
        _job(
            workspace,
            id="follow",
            instruction="draft the follow-up",
            cadence=None,
            next_run=child_slot,
        ),
    )
    parent = _job(
        workspace,
        id="digest",
        cadence="every 1 hour",
        next_run=parent_slot,
        then="follow",
    )
    save_job(data, parent)
    guard = InFlight()
    assert guard.claim("digest")
    turns = _Turns()
    turns.when("summarize the inbox", TurnResult("digest ready"))
    turns.when("draft the follow-up", TurnResult("draft ready"))
    deliver = RecordingDeliver()
    await run_manual_job(data, parent, _NOW, run_turn=turns, deliver=deliver, in_flight=guard)

    assert turns.calls == ["summarize the inbox", "draft the follow-up"]
    stored_parent = get_job(data, "digest")
    stored_child = get_job(data, "follow")
    assert stored_parent is not None and stored_child is not None
    assert stored_parent.next_run == parent_slot
    assert stored_parent.paused is False
    assert stored_child.next_run == child_slot
    assert list_runs(data, "digest")[0].trigger == "manual"
    runs = list_runs(data, "follow")
    assert len(runs) == 1
    assert runs[0].trigger == "chained"
    assert runs[0].note == "after digest"
    assert runs[0].scheduled_for is None
    assert deliver.records == [("window", "digest ready"), ("window", "draft ready")]
    # The daemon claimed the parent and releases it after this returns.
    assert "digest" in guard
    assert "follow" not in guard


async def test_a_retry_that_then_succeeds_starts_the_follow_on_once(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    _store(data, workspace, retries=1, retry_delay="10 minutes")
    turns = _Turns()
    turns.when(
        "summarize the inbox",
        TurnResult("provider down", ok=False, error_code="connection_error"),
        TurnResult("digest ready"),
    )
    turns.when("draft the follow-up", TurnResult("draft ready"))

    first = await _due(data, turns)
    assert first.records == []
    assert turns.calls == ["summarize the inbox"]
    assert list_runs(data, "follow") == []

    second = await _due(data, turns, now=_RETRY_AT)
    assert turns.calls == ["summarize the inbox", "summarize the inbox", "draft the follow-up"]
    assert second.records == [
        ("window", "attempt 2 of 2: digest ready"),
        ("window", "draft ready"),
    ]
    child = get_job(data, "follow")
    parent = get_job(data, "digest")
    assert child is not None and child.next_run == _FUTURE
    assert parent is not None and parent.attempt == 0 and parent.then == "follow"
    assert len(list_runs(data, "follow")) == 1
    assert list_runs(data, "follow")[0].trigger == "chained"


@pytest.mark.parametrize("mode", ["failed", "missed", "parked"])
async def test_a_parent_that_did_not_end_ok_does_not_chain(tmp_path: Path, mode: str) -> None:
    data, workspace = _dirs(tmp_path)
    over: dict[str, object] = {}
    turns = _Turns()
    if mode == "failed":
        turns.when("summarize the inbox", TurnResult("disk full", ok=False))
    elif mode == "parked":
        turns.when(
            "summarize the inbox",
            TurnResult("Run `echo hi`", waiting=True, session_id="sess-park"),
        )
    else:
        over["grace"] = 60
        over["next_run"] = (_NOW - timedelta(hours=2)).isoformat()
        turns.when("summarize the inbox", TurnResult("should not run"))
    _store(data, workspace, **over)
    deliver = await _due(data, turns)

    assert "draft the follow-up" not in turns.calls
    child = get_job(data, "follow")
    assert child is not None and child.last_run is None and child.next_run == _FUTURE
    assert list_runs(data, "follow") == []
    parent = get_job(data, "digest")
    assert parent is not None and parent.then == "follow"
    if mode == "failed":
        assert turns.calls == ["summarize the inbox"]
        assert parent.last_status == "failed"
        assert deliver.records == [("window", "disk full")]
    elif mode == "parked":
        assert turns.calls == ["summarize the inbox"]
        assert parent.last_status == "waiting"
    else:
        assert turns.calls == []
        assert parent.last_status == "missed"
        assert len(deliver.records) == 1


async def test_a_failed_follow_on_does_not_retry_or_continue(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    save_job(
        data,
        _job(workspace, id="later", instruction="close the loop", cadence=None, next_run=_FUTURE),
    )
    save_job(
        data,
        _job(
            workspace,
            id="follow",
            instruction="draft the follow-up",
            cadence=None,
            next_run=_FUTURE,
            then="later",
            retries=2,
            retry_delay="10 minutes",
        ),
    )
    save_job(data, _job(workspace, then="follow"))
    turns = _Turns()
    turns.when("summarize the inbox", TurnResult("digest ready"))
    turns.when(
        "draft the follow-up",
        TurnResult("provider down", ok=False, error_code="connection_error"),
    )
    deliver = await _due(data, turns)

    assert turns.calls == ["summarize the inbox", "draft the follow-up"]
    child = get_job(data, "follow")
    assert child is not None
    assert child.attempt == 0
    assert child.next_run == _FUTURE
    assert child.last_status == "failed"
    runs = list_runs(data, "follow")
    assert len(runs) == 1
    assert runs[0].trigger == "chained"
    assert runs[0].status == "failed"
    assert runs[0].attempt is None
    assert runs[0].attempts is None
    assert runs[0].note == "after digest"
    assert list_runs(data, "later") == []
    assert deliver.records == [("window", "digest ready"), ("window", "provider down")]


async def test_a_parked_follow_on_keeps_the_note_and_does_not_continue(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    save_job(
        data,
        _job(workspace, id="later", instruction="close the loop", cadence=None, next_run=_FUTURE),
    )
    save_job(
        data,
        _job(
            workspace,
            id="follow",
            instruction="draft the follow-up",
            cadence=None,
            next_run=_FUTURE,
            then="later",
        ),
    )
    save_job(data, _job(workspace, then="follow"))
    turns = _Turns()
    turns.when("summarize the inbox", TurnResult("digest ready"))
    turns.when(
        "draft the follow-up",
        TurnResult("Run `echo hi`", waiting=True, session_id="sess-park"),
    )
    await _due(data, turns)

    assert "close the loop" not in turns.calls
    child = get_job(data, "follow")
    assert child is not None
    assert child.next_run == _FUTURE
    assert child.last_status == "waiting"
    runs = list_runs(data, "follow")
    assert len(runs) == 1
    assert runs[0].trigger == "chained"
    assert runs[0].status == "waiting"
    assert runs[0].note == "after digest"
    assert runs[0].scheduled_for is None
    assert list_runs(data, "later") == []


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("paused", "paused"), ("running", "already running")],
)
async def test_a_child_that_cannot_start_is_recorded_missed(
    tmp_path: Path,
    mode: str,
    reason: str,
) -> None:
    data, workspace = _dirs(tmp_path)
    save_job(
        data,
        _job(workspace, id="later", instruction="close the loop", cadence=None, next_run=_FUTURE),
    )
    save_job(
        data,
        _job(
            workspace,
            id="follow",
            instruction="draft the follow-up",
            cadence=None,
            next_run=_FUTURE,
            then="later",
            paused=mode == "paused",
        ),
    )
    save_job(data, _job(workspace, then="follow"))
    guard = InFlight()
    if mode == "running":
        assert guard.claim("follow")
    turns = _Turns()
    turns.when("summarize the inbox", TurnResult("digest ready"))
    deliver = await _due(data, turns, guard=guard)

    assert turns.calls == ["summarize the inbox"]
    child = get_job(data, "follow")
    assert child is not None
    assert child.next_run == _FUTURE
    assert child.last_status == "missed"
    assert child.last_summary == reason
    runs = list_runs(data, "follow")
    assert len(runs) == 1
    assert runs[0].trigger == "chained"
    assert runs[0].status == "missed"
    assert runs[0].summary == reason
    assert runs[0].note == "after digest"
    assert runs[0].scheduled_for is None
    assert list_runs(data, "later") == []
    assert deliver.records == [("window", "digest ready"), ("window", reason)]
    if mode == "running":
        assert "follow" in guard
        assert "digest" not in guard
    else:
        assert child.paused is True
        _idle(guard, "digest", "follow", "later")


async def test_a_chain_runs_five_follow_ups_and_stops(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    for index in range(6, -1, -1):
        save_job(
            data,
            _job(
                workspace,
                id=f"j{index}",
                instruction=f"step {index}",
                cadence=None if index else "every 1 hour",
                next_run=_NOW.isoformat() if index == 0 else _FUTURE,
                then=None if index == 6 else f"j{index + 1}",
            ),
        )
    guard = InFlight()
    turns = _Turns()
    deliver = await _due(data, turns, guard=guard)

    assert turns.calls == [f"step {index}" for index in range(6)]
    assert "step 6" not in turns.calls
    assert deliver.records == [("window", f"step {index}") for index in range(6)]
    tail = get_job(data, "j6")
    assert tail is not None and tail.last_run is None
    assert list_runs(data, "j6") == []
    first = get_job(data, "j1")
    assert first is not None and first.next_run == _FUTURE
    opened = list_runs(data, "j1")
    assert len(opened) == 1
    assert opened[0].trigger == "chained"
    assert opened[0].note == "after j0"
    origin = get_job(data, "j0")
    assert origin is not None and origin.last_status == "ok" and origin.then == "j1"
    _idle(guard, *(f"j{index}" for index in range(7)))


async def test_a_cycle_is_refused_at_save_and_not_written(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    daemon = Daemon(data_dir=data)
    try:
        assert (await _handle(daemon, _save(workspace, "A")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "B")))["type"] == "job_list"
        linked = await _handle(daemon, {"type": "save_job", "id": "B", "then": "A"})
        assert linked["type"] == "job_list"
        err = await _handle(daemon, {"type": "save_job", "id": "A", "then": "B"})
        assert err["type"] == "error"
        assert err["code"] == "job_invalid"
        assert err["message"] == "A → B → A"
        parent = get_job(data, "A")
        child = get_job(data, "B")
        assert parent is not None and parent.then is None
        assert child is not None and child.then == "A"

        self_loop = await _handle(daemon, _save(workspace, "C", then="C"))
        assert self_loop["type"] == "error"
        assert self_loop["message"] == "C → C"
        assert get_job(data, "C") is None

        missing = await _handle(daemon, _save(workspace, "D", then="missing"))
        assert missing["code"] == "job_invalid"
        assert missing["message"] == "Then: no job 'missing'"
        assert get_job(data, "D") is None

        assert (await _handle(daemon, _save(workspace, "E")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "F", then="E")))["type"] == "job_list"
        follow = get_job(data, "F")
        assert follow is not None and follow.then == "E"
    finally:
        await daemon._shutdown()


async def test_deleting_a_job_clears_follow_ons_that_pointed_at_it(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    daemon = Daemon(data_dir=data)
    try:
        assert (await _handle(daemon, _save(workspace, "E")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "D", then="E")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "C")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "B", then="C")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "A", then="B")))["type"] == "job_list"
        deleted = await _handle(daemon, {"type": "delete_job", "job_id": "B"})
        assert deleted["type"] == "job_list"
        rows = {job["id"]: job["then"] for job in deleted["jobs"]}
        assert "B" not in rows
        assert rows["A"] is None
        assert rows["C"] is None
        assert rows["D"] == "E"
        stored = get_job(data, "A")
        assert stored is not None and stored.then is None
        kept = get_job(data, "D")
        assert kept is not None and kept.then == "E"
    finally:
        await daemon._shutdown()


async def test_edit_keeps_an_omitted_follow_on_and_a_blank_clears_it(tmp_path: Path) -> None:
    data, workspace = _dirs(tmp_path)
    daemon = Daemon(data_dir=data)
    try:
        assert (await _handle(daemon, _save(workspace, "C")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "B")))["type"] == "job_list"
        assert (await _handle(daemon, _save(workspace, "A", then="B")))["type"] == "job_list"

        renamed = await _handle(daemon, {"type": "save_job", "id": "A", "instruction": "renamed"})
        assert renamed["type"] == "job_list"
        stored = get_job(data, "A")
        assert stored is not None and stored.then == "B" and stored.instruction == "renamed"

        paused = await _handle(daemon, {"type": "save_job", "id": "A", "paused": True})
        assert paused["type"] == "job_list"
        stored = get_job(data, "A")
        assert stored is not None and stored.paused is True and stored.then == "B"

        cleared = await _handle(daemon, {"type": "save_job", "id": "A", "then": ""})
        assert cleared["type"] == "job_list"
        stored = get_job(data, "A")
        assert stored is not None and stored.then is None

        moved = await _handle(daemon, {"type": "save_job", "id": "A", "then": "C"})
        assert moved["type"] == "job_list"
        stored = get_job(data, "A")
        assert stored is not None and stored.then == "C"

        # A hand-edited loop. Pause does not send then, so it still saves.
        save_job(data, stored.model_copy(update={"then": "B"}))
        other = get_job(data, "B")
        assert other is not None
        save_job(data, other.model_copy(update={"then": "A"}))
        still = await _handle(daemon, {"type": "save_job", "id": "A", "paused": True})
        assert still["type"] == "job_list"
        stored = get_job(data, "A")
        assert stored is not None and stored.paused is True and stored.then == "B"
    finally:
        await daemon._shutdown()
