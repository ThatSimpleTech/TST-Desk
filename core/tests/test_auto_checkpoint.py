"""Autonomy checkpoint branch (TD-4102) — spec §12.8.

Writes on an unattended run land on ``tst/auto/<charter-slug>``, never
``main``. The ledger's undo is ``git revert <that sha>``. A workspace
without git refuses the run. Interactive sessions still use
``tst/session/<id>`` and still degrade when git is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_autonomy_loop import make_charter
from tests.test_charter import VALID_FRONTMATTER, _write_charter
from tests.test_checkpoint import _git, _git_allow_fail, _tree_files, make_repo
from tests.test_checkpoint_wiring import (
    _write_handler,
    make_dispatcher,
    make_write_registry,
)
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tests.test_start_autonomy import _ok
from tstd.autonomy import Checkpointer, auto_branch, charter_slug, session_branch
from tstd.autonomy.checkpoint import (
    AUTO_BRANCH_PREFIX,
    NO_GIT_FOR_AUTONOMY,
    checkpoint_start_error,
)
from tstd.autonomy.classifier import DecisionClass
from tstd.autonomy.ledger import DecisionLedger, LedgerEntry
from tstd.autonomy.start import run_autonomy_start
from tstd.config import AutonomyConfig
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.protocol import DecisionLogged
from tstd.router import TierRouter
from tstd.session import Session


async def _checkpoint_file(cp: Checkpointer, path: Path) -> object:
    return await cp.checkpoint(
        [path],
        tool_name="fs_write",
        tool_call_id="tc-1",
        decision_class=DecisionClass.A,
        decision_rule="workspace-write",
    )


# ── Slug and branch guards ──────────────────────────────────────────────


class TestCharterSlug:
    def test_slug_from_objective(self) -> None:
        assert charter_slug("Ship the CSV importer") == "ship-the-csv-importer"
        assert make_charter().slug == "ship-the-csv-importer"

    def test_slug_never_names_main(self) -> None:
        assert charter_slug("main") == "run-main"
        assert charter_slug("MASTER") == "run-master"
        assert charter_slug("123") == "run-123"


class TestAutoBranch:
    def test_prefix_and_guards(self) -> None:
        assert auto_branch("ship-csv") == "tst/auto/ship-csv"
        for bad in ("", "a/b", "-flag", "main", "master"):
            with pytest.raises(ValueError):
                auto_branch(bad)

    def test_session_branch_unchanged(self) -> None:
        assert session_branch("abc-123") == "tst/session/abc-123"


# ── Commits land on tst/auto/<slug>, never main ─────────────────────────


class TestAutoBranchCommits:
    async def test_checkpoint_commits_to_auto_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        main_before = _git(repo, "rev-parse", "refs/heads/main")
        head_before = _git(repo, "rev-parse", "HEAD")
        slug = make_charter().slug
        cp = Checkpointer(repo, "sess-1", branch=auto_branch(slug))
        (repo / "b.txt").write_text("hello\n")

        outcome = await _checkpoint_file(cp, repo / "b.txt")

        assert outcome.status == "committed"
        assert outcome.branch == f"{AUTO_BRANCH_PREFIX}{slug}"
        tip = _git(repo, "rev-parse", f"refs/heads/{outcome.branch}")
        assert tip == outcome.commit
        assert _tree_files(repo, tip)["b.txt"] == "hello\n"
        assert _git(repo, "rev-parse", "refs/heads/main") == main_before
        assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
        assert _git(repo, "rev-parse", "HEAD") == head_before
        assert _git_allow_fail(repo, "rev-parse", "refs/heads/tst/session/sess-1").returncode != 0

    def test_constructor_refuses_a_primary_branch(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="tst/auto"):
            Checkpointer(tmp_path, "sess-1", branch="main")


# ── Ledger undo is git revert of that commit ────────────────────────────


class TestLedgerUndo:
    async def test_revert_of_the_checkpoint_is_the_printed_undo(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        slug = make_charter().slug
        cp = Checkpointer(repo, "sess-1", branch=auto_branch(slug))
        (repo / "b.txt").write_text("hello\n")
        outcome = await _checkpoint_file(cp, repo / "b.txt")
        assert outcome.commit is not None

        entry = LedgerEntry(
            decision_class="A",
            what="fs_write b.txt",
            why="in-workspace edit",
            commit=outcome.commit,
        )
        assert entry.undo_command == f"git revert {outcome.commit}"

        undo = tmp_path / "undo"
        _git(repo, "worktree", "add", str(undo), outcome.branch)
        _git(undo, "revert", "--no-edit", outcome.commit)
        assert not (undo / "b.txt").exists()
        assert (undo / "a.txt").read_text() == "base\n"


# ── Non-git refuses the run ─────────────────────────────────────────────


class TestNonGitRefuses:
    async def test_probe_names_the_auto_branch(self, tmp_path: Path) -> None:
        assert await checkpoint_start_error(tmp_path) == NO_GIT_FOR_AUTONOMY

    async def test_start_refuses_without_a_repo(self, tmp_path: Path) -> None:
        _write_charter(tmp_path, VALID_FRONTMATTER)
        result = await run_autonomy_start(
            tmp_path, config=AutonomyConfig(), which=lambda _n: None, inspect=_ok
        )
        assert result.ready is False
        assert result.signed is False
        assert result.error == NO_GIT_FOR_AUTONOMY

    async def test_daemon_does_not_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tstd.config import default_config_yaml, load_config

        _write_charter(tmp_path, VALID_FRONTMATTER)
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(default_config_yaml(), encoding="utf-8")
        monkeypatch.setattr("tstd.daemon.cached_config", lambda: load_config(cfg_path))

        async def _no_launch(_self: object, _workspace: Path, _charter: object) -> str:
            raise AssertionError("a non-git workspace must not launch a run")

        monkeypatch.setattr("tstd.daemon.Daemon._launch_autonomy_run", _no_launch)
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(
            json.dumps({"type": "start_autonomy", "workspace_path": str(tmp_path)}),
            None,
        )
        assert raw is not None
        payload = json.loads(raw)
        assert payload["type"] == "autonomy_start"
        assert payload["ready"] is False
        assert payload["session_id"] is None
        assert "tst/auto" in (payload.get("error") or "")
        await daemon._shutdown()


# ── Loop wires the auto branch on an unattended session ─────────────────


class TestLoopWiresAutoBranch:
    async def test_write_lands_on_auto_branch_and_ledgers_revert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        main_before = _git(repo, "rev-parse", "refs/heads/main")
        charter = make_charter(max_iterations=8)
        session = Session(str(repo))
        session.autonomy = True
        session.charter = charter
        registry = make_write_registry()
        dispatcher = make_dispatcher(repo, registry)
        dispatcher.register_handler("fs_write", _write_handler)
        assert dispatcher.checkpointer is None

        monkeypatch.chdir(repo)
        args = json.dumps({"path": "b.txt", "content": "loop write\n"})
        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(kind="tool_call", tool_name="fs_write", tool_arguments=args),
                    Script(kind="stream", content="Done"),
                ]
            }
        )
        runner = await start_loop(
            session, TierRouter(lead_turns=3), mock, make_config(), registry, dispatcher
        )
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)

        branch = f"refs/heads/tst/auto/{charter.slug}"
        tip = _git(repo, "rev-parse", branch)
        assert _tree_files(repo, tip)["b.txt"] == "loop write\n"
        assert _git(repo, "rev-parse", "refs/heads/main") == main_before
        session_ref = f"refs/heads/tst/session/{session.id}"
        assert _git_allow_fail(repo, "rev-parse", session_ref).returncode != 0

        logged = [e for e in session.event_log.all_events if isinstance(e, DecisionLogged)]
        assert logged and logged[0].commit == tip
        entries = DecisionLedger(repo).read()
        assert entries
        assert entries[0].undo_command == f"git revert {tip}"
        await runner.cancel()
