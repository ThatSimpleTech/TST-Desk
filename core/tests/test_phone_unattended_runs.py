"""A paired phone starts unattended runs only if the computer said so (TD-5298).

An autonomy run works to its charter with nobody there, Run now starts a
job's turn at once, and a saved job runs later in a session nobody opened.
From a phone each was a run started with nobody at the computer. It is now
the phone permission ``unattended_runs``. Pausing and deleting a job stay
open: they only stop runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_add_model import _stop
from tests.test_mcp_peer import _drain, _fresh, _pair
from tests.test_models_on_phone import _seeded
from tests.test_peer_policy import (
    LOOPBACK_PLAIN,
    MACHINE,
    PHONE_PLAIN,
    PHONE_VIEWER,
    PLAIN_REMOTE,
    VIEWER,
    _code,
    _msg,
)
from tstd.peer_policy import check
from tstd.phone_verbs import PHONE_OPEN, required_capability
from tstd.scheduler.store import list_jobs

GRANTED = frozenset({"unattended_runs"})
JOB = {"instruction": "Check the build", "cadence": "every 2 hours", "deliver_to": "window"}
STARTS: list[tuple[str, dict[str, Any]]] = [
    ("start_autonomy", {"workspace_path": "/work/proj"}),
    ("run_job", {"job_id": "job-1"}),
    ("save_job", {**JOB, "workspace": "/work/proj"}),
    ("save_job", {"id": "job-1", "paused": False}),
    ("save_job", {"id": "job-1", "instruction": "Something else"}),
]


@pytest.mark.parametrize(("kind", "fields"), STARTS)
def test_each_start_needs_the_permission(kind: str, fields: dict[str, Any]) -> None:
    msg = _msg(kind, **fields)
    assert required_capability(msg) == "unattended_runs"
    assert kind not in PHONE_OPEN
    for phone in (PHONE_VIEWER, PHONE_PLAIN):
        assert _code(check(msg, phone, bridged=False)) == "phone_permission_off"
        assert _code(check(msg, phone, bridged=False, grants={"computer_use"})) == (
            "phone_permission_off"
        )
        assert check(msg, phone, bridged=False, grants=GRANTED) is None
    assert _code(check(msg, PLAIN_REMOTE, bridged=False, grants=GRANTED)) == "phone_permission_off"
    for own in (LOOPBACK_PLAIN, VIEWER):
        assert check(msg, own, bridged=False) is None


def test_another_computer_keeps_its_own_rule() -> None:
    run = _msg("run_job", job_id="job-1")
    assert _code(check(run, MACHINE, bridged=False, grants=GRANTED)) == "not_allowed"


@pytest.mark.parametrize(
    ("kind", "fields"),
    [("save_job", {"id": "job-1", "paused": True}), ("delete_job", {"job_id": "job-1"})],
)
def test_pausing_and_deleting_need_nothing(kind: str, fields: dict[str, Any]) -> None:
    msg = _msg(kind, **fields)
    assert required_capability(msg) is None
    for peer in (PHONE_VIEWER, PHONE_PLAIN):
        assert check(msg, peer, bridged=False) is None


async def test_a_phone_saves_a_job_only_after_the_grant_and_the_computer_is_told(
    tmp_path: Path,
) -> None:
    daemon, task, ws = await _seeded(tmp_path)
    phone, _ = await _pair(daemon)
    folder = tmp_path / "proj"
    folder.mkdir()
    save = {"type": "save_job", **JOB, "workspace": str(folder)}
    try:
        refused = await _fresh(phone, save, "job_list")
        assert refused["type"] == "error" and refused["code"] == "phone_permission_off"
        assert "Starting unattended runs" in refused["message"]
        assert list_jobs(daemon.data_dir) == []
        grant = {"type": "set_phone_permission", "capability": "unattended_runs", "enabled": True}
        await _fresh(ws, grant)
        await _drain(ws)
        assert (await _fresh(phone, save, "job_list"))["type"] == "job_list"
        [job] = list_jobs(daemon.data_dir)
        told = [e for e in await _drain(ws) if e["type"] == "phone_change"]
        assert [(e["capability"], e["summary"]) for e in told] == [
            ("unattended_runs", f"saved the scheduled job {job.id}.")
        ]
        # A pause is no use of the permission: nobody is told.
        await _fresh(phone, {"type": "save_job", "id": job.id, "paused": True}, "job_list")
        assert [e for e in await _drain(ws) if e["type"] == "phone_change"] == []
    finally:
        await phone.close()
        await _stop(task, ws)
