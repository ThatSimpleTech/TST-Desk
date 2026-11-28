"""A project's own instructions load only once its folder is trusted (SECUX2-01).

The manager's rules: trust per folder by real path, covering edits to the
kinds of file there when it was given; a new kind asks again with only the
new files; nothing grandfathered; unattended runs go ahead without the
files and say so; Settings lists trusted folders with Revoke.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tstd import folder_trust
from tstd.context.discover import SteeringFileResolver
from tstd.context.skill_discovery import SkillDiscoverer
from tstd.context.workers import load_workers
from tstd.folder_trust import FolderTrustStore
from tstd.mock import MockProvider, Script

WORKER = "---\nname: helper\ndescription: helps\n---\nHelp.\n"
SKILL = "---\nname: deploy\ndescription: deploys\n---\nDeploy.\n"


def _project(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("Always answer in pirate speak.\n", encoding="utf-8")
    (root / ".tst" / "rules").mkdir(parents=True)
    (root / ".tst" / "rules" / "style.md").write_text("Use tabs.\n", encoding="utf-8")
    return root


@pytest.fixture
def store(tmp_path: Path) -> FolderTrustStore:
    made = FolderTrustStore(tmp_path / "data")
    folder_trust.bind(made)
    return made


def _loaded(project: Path, home: Path) -> list[str]:
    return [s.path.name for s in SteeringFileResolver(home_dir=home).resolve(project)]


def test_nothing_loads_before_a_decision(tmp_path: Path, store: FolderTrustStore) -> None:
    project = _project(tmp_path / "repo")
    assert _loaded(project, tmp_path / "home") == []
    view = store.view(project)
    assert view.state == "pending"
    assert {f.path for f in view.files} == {"AGENTS.md", ".tst/rules/style.md"}


def test_trusting_loads_them_and_covers_edits(tmp_path: Path, store: FolderTrustStore) -> None:
    project = _project(tmp_path / "repo")
    store.decide(project, True)
    assert _loaded(project, tmp_path / "home") == ["AGENTS.md", "style.md"]
    (project / "AGENTS.md").write_text("Edited.\n", encoding="utf-8")
    (project / ".tst" / "rules" / "more.md").write_text("More.\n", encoding="utf-8")
    assert store.view(project).state == "trusted"


def test_a_new_kind_asks_again_for_only_the_new_files(
    tmp_path: Path, store: FolderTrustStore
) -> None:
    project = _project(tmp_path / "repo")
    store.decide(project, True)
    (project / ".tst" / "agents").mkdir()
    (project / ".tst" / "agents" / "helper.md").write_text(WORKER, encoding="utf-8")
    (project / ".tst" / "skills" / "deploy").mkdir(parents=True)
    (project / ".tst" / "skills" / "deploy" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    view = store.view(project)
    assert view.state == "pending"
    assert {f.kind for f in view.files} == {"workers", "skills"}
    # The kinds already trusted keep loading; the new ones wait.
    assert _loaded(project, tmp_path / "home") == ["AGENTS.md", "style.md"]
    assert load_workers(project, tmp_path / "home").workers == ()
    assert SkillDiscoverer(home_dir=tmp_path / "home").discover(project) == []
    store.decide(project, True)
    assert [w.name for w in load_workers(project, tmp_path / "home").workers] == ["helper"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlink_or_a_rename_inherits_nothing(tmp_path: Path, store: FolderTrustStore) -> None:
    project = _project(tmp_path / "repo")
    store.decide(project, True)
    link = tmp_path / "link"
    link.symlink_to(project)
    assert store.view(link).state == "trusted"  # the same folder, by its real path
    other = _project(tmp_path / "copy")
    assert store.view(other).state == "pending"
    moved = tmp_path / "moved"
    os.rename(project, moved)
    assert store.view(moved).state == "pending"


def test_declining_and_revoking(tmp_path: Path, store: FolderTrustStore) -> None:
    project = _project(tmp_path / "repo")
    assert store.decide(project, False).state == "declined"
    assert _loaded(project, tmp_path / "home") == []
    store.decide(project, True)
    assert [f for f, _ in store.trusted_folders()] == [os.path.realpath(project)]
    store.revoke(project)
    assert store.view(project).state == "pending"


def test_the_users_own_files_always_load(tmp_path: Path, store: FolderTrustStore) -> None:
    home = tmp_path / "home"
    (home / ".tstdesk").mkdir(parents=True)
    (home / ".tstdesk" / "AGENTS.md").write_text("Mine.\n", encoding="utf-8")
    project = _project(tmp_path / "repo")
    assert _loaded(project, home) == ["AGENTS.md"]  # the user's, not the project's
    assert str(SteeringFileResolver(home_dir=home).resolve(project)[0].path).startswith(str(home))


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


@pytest.mark.folder_trust
async def test_the_window_is_asked_and_the_answer_loads_them(tmp_path: Path, fleet: Fleet) -> None:
    project = _project(tmp_path / "repo")
    expected = folder_trust.real(project)
    daemon = await fleet.start(
        tmp_path / "d", provider=MockProvider(default=Script(kind="stream", content="ok"))
    )
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(project)}, "session_state")
        sid = str(opened["session_id"])
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        wall = await recv_until(ws, lambda f: f.get("type") == "boundary_update")
        assert wall["folder_trust"]["state"] == "pending"
        assert {f["path"] for f in wall["folder_trust"]["files"]} == {
            "AGENTS.md",
            ".tst/rules/style.md",
        }
        await ws.send(json.dumps({"type": "decide_folder_trust", "session_id": sid, "trust": True}))
        again = await recv_until(
            ws, lambda f: f.get("type") == "boundary_update" and f.get("folder_trust") is None
        )
        assert again["folder_trust"] is None
        state = await ask(ws, {"type": "get_setup_state"}, "setup_state")
    assert [t["folder"] for t in state["trusted_folders"]] == [expected]


@pytest.mark.folder_trust
async def test_a_scheduled_run_goes_ahead_without_them_and_says_so(tmp_path: Path) -> None:
    from tests.test_scheduler_runner import _job, _now, make_config
    from tstd.daemon import Daemon
    from tstd.scheduler.runner import (
        NOT_TRUSTED_NOTE,
        RecordingDeliver,
        run_due_jobs,
        run_turn_on_daemon,
    )
    from tstd.scheduler.store import list_jobs, save_job

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    project = _project(tmp_path / "repo")
    save_job(data_dir, _job(project, job_id="inbox"))
    daemon = Daemon(
        data_dir=data_dir,
        provider=MockProvider(default=Script(kind="stream", content="digest ready")),
    )
    daemon.config = make_config()
    await run_due_jobs(
        data_dir,
        _now(),
        run_turn=lambda ws, msg: run_turn_on_daemon(daemon, ws, msg),
        deliver=RecordingDeliver(),
    )
    stored = list_jobs(data_dir)[0]
    assert stored.last_status == "ok"
    assert stored.last_summary is not None
    assert stored.last_summary.splitlines() == [NOT_TRUSTED_NOTE, "digest ready"]


def test_a_chats_worktree_reads_its_projects_trust(tmp_path: Path, store: FolderTrustStore) -> None:
    project = _project(tmp_path / "repo")
    worktree = _project(tmp_path / "data" / "worktrees" / "chat-1")
    store.set_project_lookup(
        lambda key: str(project) if key == folder_trust.real(worktree) else None
    )
    store.decide(project, True)
    assert store.view(worktree).state == "trusted"
    assert _loaded(worktree, tmp_path / "home") == ["AGENTS.md", "style.md"]
