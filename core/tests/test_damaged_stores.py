"""One unreadable store file never wipes the chats or the schedules (ERR2B-04).

sessions.json and scheduler/jobs.json were read whole and believed: a
truncated or garbled file started the app with no chats or no schedules, no
word was said, and the next save overwrote the file for good.
"""

from __future__ import annotations

import json
from pathlib import Path

from tstd.scheduler.models import JobDraft
from tstd.scheduler.store import jobs_path, list_jobs, recovery, save_job
from tstd.session_store import SessionStore

ROWS = [
    {"session_id": "s1", "workspace_path": "/ws/one", "state": "idle", "title": "One"},
    {"session_id": "s2", "workspace_path": "/ws/two", "state": "idle", "title": "Two"},
]


def _damaged(path: Path, text: str = '[{"session_id": "s1", "work') -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.read_bytes()


def _kept(folder: Path, name: str) -> list[Path]:
    return sorted(folder.glob(f"{name}.damaged-*"))


def _folder(data: Path, session_id: str, workspace: str | None, first: str) -> None:
    folder = data / "sessions" / session_id
    folder.mkdir(parents=True)
    system = "You are TST Desk.\n\n" + (f"Workspace root: {workspace}\n" if workspace else "")
    conversation = [
        {"role": "system", "content": system},
        {"role": "user", "content": first},
    ]
    (folder / "conversation.json").write_text(json.dumps(conversation), encoding="utf-8")


async def test_a_damaged_list_is_kept_and_rebuilt_from_backup_and_folders(tmp_path: Path) -> None:
    store_file = tmp_path / "sessions.json"
    (tmp_path / "sessions.json.bak").write_text(json.dumps(ROWS), encoding="utf-8")
    original = _damaged(store_file)
    _folder(tmp_path, "s1", "/ws/one", "already listed")
    _folder(tmp_path, "s3", "/ws/three", "Fix the login page")
    _folder(tmp_path, "s4", None, "no workspace line, so not rebuilt")

    store = SessionStore(tmp_path)

    by_id = {r.session_id: r for r in store.records()}
    assert sorted(by_id) == ["s1", "s2", "s3"]
    assert (by_id["s3"].workspace_path, by_id["s3"].title) == ("/ws/three", "Fix the login page")
    assert store.recovery is not None
    assert (store.recovery.store, store.recovery.from_backup, store.recovery.rebuilt) == (
        "sessions",
        2,
        1,
    )
    [kept] = _kept(tmp_path, "sessions.json")
    assert kept.name == store.recovery.kept_as
    assert kept.read_bytes() == original

    await store.upsert("s9", "/ws/new", "idle")  # the save that used to make the loss permanent
    assert kept.read_bytes() == original
    saved = {row["session_id"] for row in json.loads(store_file.read_text())}
    assert saved == {"s1", "s2", "s3", "s9"}


async def test_every_save_keeps_the_file_it_replaces(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    await store.upsert("s1", "/ws", "idle")
    await store.upsert("s2", "/ws", "idle")
    backup = json.loads((tmp_path / "sessions.json.bak").read_text())
    assert [row["session_id"] for row in backup] == ["s1"]
    assert (tmp_path / "sessions.json.bak").stat().st_mode & 0o777 == 0o600


def test_with_nothing_to_recover_from_it_says_so(tmp_path: Path) -> None:
    _damaged(tmp_path / "sessions.json", "not json at all")
    store = SessionStore(tmp_path)
    assert store.records() == []
    assert store.recovery is not None
    assert (store.recovery.from_backup, store.recovery.rebuilt) == (0, 0)


def test_a_readable_list_is_no_recovery(tmp_path: Path) -> None:
    (tmp_path / "sessions.json").write_text(json.dumps(ROWS), encoding="utf-8")
    store = SessionStore(tmp_path)
    assert len(store.records()) == 2 and store.recovery is None
    assert not list(tmp_path.glob("sessions.json.damaged-*"))


def _draft(tmp_path: Path, instruction: str) -> JobDraft:
    return JobDraft(
        workspace=str(tmp_path),
        instruction=instruction,
        cadence="every 1 hour",
        deliver_to="window",
    )


def test_a_damaged_jobs_file_is_kept_and_read_back_from_its_backup(tmp_path: Path) -> None:
    save_job(tmp_path, _draft(tmp_path, "first"))
    save_job(tmp_path, _draft(tmp_path, "second"))  # the backup now holds "first"
    original = _damaged(jobs_path(tmp_path), '{"version": 1, "jobs": [{"id": "trunc')

    jobs = list_jobs(tmp_path)

    assert [j.instruction for j in jobs] == ["first"]
    found = recovery(tmp_path)
    assert found is not None and (found.store, found.from_backup) == ("jobs", 1)
    [kept] = (tmp_path / "scheduler").glob("jobs.json.damaged-*")
    assert kept.read_bytes() == original
    save_job(tmp_path, _draft(tmp_path, "third"))
    assert kept.read_bytes() == original
    assert [j.instruction for j in list_jobs(tmp_path)] == ["first", "third"]


def test_jobs_from_a_newer_format_are_kept_not_dropped(tmp_path: Path) -> None:
    newer = json.dumps({"version": 99, "jobs": [{"id": "j1", "future": True}]})
    original = _damaged(jobs_path(tmp_path), newer)
    assert list_jobs(tmp_path) == []
    [kept] = (tmp_path / "scheduler").glob("jobs.json.damaged-*")
    assert kept.read_bytes() == original
