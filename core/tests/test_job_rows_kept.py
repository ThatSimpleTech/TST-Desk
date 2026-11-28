"""A scheduled job this build cannot read is kept, not erased (ARTSCHED-32).

A row that fails validation (a newer build's `deliver_to: teams`, a hand
edit) used to vanish from the list, and the next save wrote the file
without it. Now it is carried through every write as it was, and the
list says how many such rows there are.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.platform_helpers import scheduler_fixture_ws
from tstd.scheduler.models import JobDraft
from tstd.scheduler.store import delete_job, jobs_path, list_jobs, save_job, unreadable_jobs


def _draft(workspace: Path, instruction: str) -> JobDraft:
    return JobDraft(
        workspace=str(workspace),
        instruction=instruction,
        cadence="every 1 hour",
        deliver_to="window",
    )


def test_an_unreadable_row_survives_saves_and_deletes(tmp_path: Path) -> None:
    ws = scheduler_fixture_ws(tmp_path)
    first = save_job(tmp_path, _draft(ws, "summarize the inbox"))
    path = jobs_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    future = {**payload["jobs"][0], "id": "job-from-a-newer-build", "deliver_to": "teams"}
    payload["jobs"].append(future)
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert [job.id for job in list_jobs(tmp_path)] == [first.id]
    assert unreadable_jobs(tmp_path) == 1

    second = save_job(tmp_path, _draft(ws, "tidy the downloads"))
    delete_job(tmp_path, first.id)

    rows = json.loads(path.read_text(encoding="utf-8"))["jobs"]
    assert future in rows  # unchanged, byte for byte as JSON
    assert [row["id"] for row in rows if row is not future and row != future] == [second.id]
    assert unreadable_jobs(tmp_path) == 1


def test_a_readable_store_reports_nothing_unreadable(tmp_path: Path) -> None:
    ws = scheduler_fixture_ws(tmp_path)
    save_job(tmp_path, _draft(ws, "summarize the inbox"))
    assert len(list_jobs(tmp_path)) == 1
    assert unreadable_jobs(tmp_path) == 0
