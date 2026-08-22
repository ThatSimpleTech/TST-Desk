"""Checkpoint commits (TD-705) — git-state acceptance matrix.

Each meaningful unit of work commits to the session branch
``tst/session/<id>``; nothing is ever committed to ``main``; commit
messages reference the decision and session; non-git workspaces degrade
gracefully; pre-existing uncommitted changes are never clobbered; and
the matrix covers clean repo, dirty repo, no repo, detached HEAD, and
mid-rebase.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tstd.autonomy import Checkpointer
from tstd.autonomy.checkpoint import (
    DIRTY_BASELINE,
    NO_GIT,
    REBASE_IN_PROGRESS,
    DecisionClass,
    session_branch,
)

# ── Git helpers ─────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in *repo*, raising on failure; returns stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_allow_fail(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git_raw(repo: Path, *args: str) -> str:
    """Like ``_git`` but without stripping — for content-sensitive output."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A repo on ``main`` with one baseline commit (``a.txt``)."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "baseline")
    return repo


def make_checkpointer(repo: Path, session_id: str = "test-session") -> Checkpointer:
    return Checkpointer(repo, session_id)


def _tree_files(repo: Path, commit: str) -> dict[str, str]:
    """Map of path → content for every blob in *commit*'s tree."""
    files: dict[str, str] = {}
    for line in _git(repo, "ls-tree", "-r", commit).splitlines():
        meta, path = line.split("\t")
        blob_sha = meta.split()[2]
        files[path] = _git_raw(repo, "cat-file", "-p", blob_sha)
    return files


async def _checkpoint_file(cp: Checkpointer, path: Path) -> object:
    return await cp.checkpoint(
        [path],
        tool_name="fs_write",
        tool_call_id="tc-1",
        decision_class=DecisionClass.A,
        decision_rule="workspace-write",
    )


# ── AC: each meaningful unit of work commits to tst/session/<id> ───────


class TestSessionBranchCommits:
    async def test_checkpoint_commits_to_session_branch(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        head_before = _git(repo, "rev-parse", "HEAD")
        (repo / "b.txt").write_text("hello\n")
        cp = make_checkpointer(repo)

        outcome = await _checkpoint_file(cp, repo / "b.txt")

        assert outcome.status == "committed"
        assert outcome.branch == "tst/session/test-session"
        tip = _git(repo, "rev-parse", "refs/heads/tst/session/test-session")
        assert tip == outcome.commit
        files = _tree_files(repo, tip)
        assert files["b.txt"] == "hello\n"  # the agent's write, captured
        assert files["a.txt"] == "base\n"  # baseline carried forward
        assert _git(repo, "rev-parse", f"{tip}^") == head_before

    async def test_successive_checkpoints_chain(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        cp = make_checkpointer(repo)
        (repo / "b.txt").write_text("one\n")
        first = await _checkpoint_file(cp, repo / "b.txt")
        assert first.status == "committed"

        (repo / "c.txt").write_text("two\n")
        outcome = await cp.checkpoint(
            [repo / "c.txt"],
            tool_name="fs_edit",
            tool_call_id="tc-2",
            decision_class=DecisionClass.B,
            decision_rule="workspace-write",
        )

        assert outcome.status == "committed"
        tip = _git(repo, "rev-parse", "refs/heads/tst/session/test-session")
        assert _git(repo, "rev-parse", f"{tip}^") == first.commit
        files = _tree_files(repo, tip)
        assert files["b.txt"] == "one\n"
        assert files["c.txt"] == "two\n"

    async def test_empty_repo_unborn_head(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        cp = make_checkpointer(repo)
        (repo / "first.txt").write_text("first\n")

        outcome = await _checkpoint_file(cp, repo / "first.txt")

        assert outcome.status == "committed"
        tip = _git(repo, "rev-parse", "refs/heads/tst/session/test-session")
        # A parentless commit: rev-parse of the parent must fail.
        assert _git_allow_fail(repo, "rev-parse", "--verify", "-q", f"{tip}^").returncode != 0
        assert _tree_files(repo, tip) == {"first.txt": "first\n"}


# ── AC: never commits to main ───────────────────────────────────────────


class TestNeverMain:
    async def test_main_ref_untouched(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        main_before = _git(repo, "rev-parse", "refs/heads/main")
        (repo / "b.txt").write_text("hello\n")
        cp = make_checkpointer(repo)

        outcome = await _checkpoint_file(cp, repo / "b.txt")

        assert outcome.status == "committed"
        assert _git(repo, "rev-parse", "refs/heads/main") == main_before
        assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"

    async def test_index_and_worktree_untouched(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "b.txt").write_text("hello\n")
        status_before = _git(repo, "status", "--porcelain")
        cp = make_checkpointer(repo)

        await _checkpoint_file(cp, repo / "b.txt")

        assert _git(repo, "status", "--porcelain") == status_before
        assert (repo / "b.txt").read_text() == "hello\n"

    def test_session_branch_guard(self) -> None:
        assert session_branch("abc-123") == "tst/session/abc-123"
        # Malformed ids and ids that would name a primary branch are all
        # refused; the result can never be ``main`` or ``master``.
        for bad in ("", "a/b", "-flag", "main", "master"):
            with pytest.raises(ValueError):
                session_branch(bad)


# ── AC: commit message references the decision and session ─────────────


class TestCommitMessage:
    async def test_commit_message_references_decision_and_session(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        cp = make_checkpointer(repo, session_id="sess-42")
        (repo / "b.txt").write_text("hello\n")

        outcome = await cp.checkpoint(
            [repo / "b.txt"],
            tool_name="fs_write",
            tool_call_id="call-77",
            decision_class=DecisionClass.A,
            decision_rule="workspace-write",
        )

        message = _git(repo, "log", "-1", "--format=%B", outcome.commit or "")
        assert "sess-42" in message
        assert "call-77" in message
        assert "class A" in message
        assert "fs_write" in message
        assert "workspace-write" in message


# ── AC: non-git workspaces degrade gracefully ───────────────────────────


class TestNoRepo:
    async def test_no_repo_disabled_notifies_once(self, tmp_path: Path) -> None:
        cp = make_checkpointer(tmp_path)
        (tmp_path / "b.txt").write_text("hello\n")

        first = await _checkpoint_file(cp, tmp_path / "b.txt")
        second = await _checkpoint_file(cp, tmp_path / "b.txt")

        assert first.status == "disabled"
        assert first.notice is not None
        assert first.notice.code == NO_GIT
        assert second.status == "disabled"
        assert second.notice is None  # informed once, not twice


# ── AC: pre-existing changes never clobbered — detected and reported ───


class TestDirtyBaseline:
    async def test_dirty_repo_reported_not_clobbered(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        # Pre-existing uncommitted user changes: a modified tracked file
        # and an untracked file.
        (repo / "a.txt").write_text("user's in-progress edit\n")
        (repo / "scratch.txt").write_text("user scratch\n")
        cp = make_checkpointer(repo)
        (repo / "b.txt").write_text("agent write\n")
        # Snapshot after the agent's write so the assertion measures what
        # the checkpoint itself touches: nothing.
        status_before = _git(repo, "status", "--porcelain")

        first = await _checkpoint_file(cp, repo / "b.txt")

        assert first.status == "committed"
        assert first.notice is not None
        assert first.notice.code == DIRTY_BASELINE
        files = _tree_files(repo, first.commit or "")
        # The committed snapshot carries the COMMITTED a.txt, not the
        # user's dirty edit — their work is neither captured nor lost.
        assert files["a.txt"] == "base\n"
        assert files["b.txt"] == "agent write\n"
        assert "scratch.txt" not in files
        # Working tree is byte-identical and still dirty.
        assert _git(repo, "status", "--porcelain") == status_before
        assert (repo / "a.txt").read_text() == "user's in-progress edit\n"

        second = await _checkpoint_file(cp, repo / "b.txt")
        assert second.notice is None  # reported once


# ── AC: matrix — detached HEAD and mid-rebase ───────────────────────────


class TestRepoStates:
    async def test_detached_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        head_sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "--detach")
        cp = make_checkpointer(repo)
        (repo / "b.txt").write_text("hello\n")

        outcome = await _checkpoint_file(cp, repo / "b.txt")

        assert outcome.status == "committed"
        # HEAD is exactly where the user left it: detached, same commit.
        assert _git_allow_fail(repo, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert _git(repo, "rev-parse", "HEAD") == head_sha

    async def test_mid_rebase_skipped(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        _git(repo, "checkout", "-b", "feature")
        (repo / "a.txt").write_text("feature line\n")
        _git(repo, "commit", "-am", "feature change")
        _git(repo, "checkout", "main")
        (repo / "a.txt").write_text("main line\n")
        _git(repo, "commit", "-am", "main change")
        _git(repo, "checkout", "feature")
        rebase = _git_allow_fail(repo, "rebase", "main")
        assert rebase.returncode != 0  # conflict halts the rebase
        assert (repo / ".git" / "rebase-merge").is_dir()

        cp = make_checkpointer(repo)
        (repo / "b.txt").write_text("hello\n")
        first = await _checkpoint_file(cp, repo / "b.txt")
        second = await _checkpoint_file(cp, repo / "b.txt")

        assert first.status == "skipped"
        assert first.notice is not None
        assert first.notice.code == REBASE_IN_PROGRESS
        assert second.status == "skipped"
        assert second.notice is None  # informed once
        # No branch created while skipped.
        probe = _git_allow_fail(repo, "rev-parse", "refs/heads/tst/session/test-session")
        assert probe.returncode != 0
        assert (repo / ".git" / "rebase-merge").is_dir()  # rebase state intact

        _git(repo, "rebase", "--abort")
        resumed = await _checkpoint_file(cp, repo / "b.txt")
        assert resumed.status == "committed"
        # First checkpoint actually executed: the tree is dirty (b.txt
        # untracked), so the dirty-baseline notice fires here — once.
        assert resumed.notice is not None
        assert resumed.notice.code == DIRTY_BASELINE
        again = await _checkpoint_file(cp, repo / "b.txt")
        assert again.status == "committed"
        assert again.notice is None


# ── AC (TD-4816): git children inherit the shell tool's sanitized env ──


class TestChildEnvSanitized:
    """A planted secret-shaped variable never reaches a git child."""

    async def test_default_spawn_env_is_sanitized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``env=None`` path (probe/status/update-ref) is filtered."""
        repo = make_repo(tmp_path)
        monkeypatch.setenv("TST_SECRET_PROBE", "sk-test-probe-key")
        monkeypatch.setenv("TST_PLAIN_PROBE", "visible-value")
        cp = make_checkpointer(repo)
        # A ``!`` alias makes the git child print its own environment.
        rc, out, err = await cp._git("-c", "alias.tstdump=!env", "tstdump")
        assert rc == 0, err
        assert "TST_SECRET_PROBE" not in out
        assert "TST_PLAIN_PROBE=visible-value" in out

    def test_identity_env_drops_secrets_keeps_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The agent identity rides on top of the sanitized base."""
        monkeypatch.setenv("TST_SECRET_PROBE", "sk-test-probe-key")
        env = Checkpointer._identity_env()
        assert "TST_SECRET_PROBE" not in env
        assert env["GIT_AUTHOR_NAME"] == "TST Desk"
        assert env["GIT_COMMITTER_EMAIL"] == "tstdesk@localhost"
