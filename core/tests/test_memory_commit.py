"""Memory writes commit on HEAD (TD-2104).

Not the session checkpoint branch. ``git revert`` restores the previous
bytes. A non-git workspace still gets the write, once, with a notice.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_checkpoint import _git, make_repo
from tests.test_security_suite import make_dispatcher
from tstd.autonomy import Checkpointer
from tstd.memory_commit import MEMORY_COMMIT_SUBJECT, MEMORY_NO_GIT, MemoryCommitter
from tstd.session import Session


def _head_subject(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%s")


class TestHeadCommit:
    async def test_memory_write_commits_on_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        head_before = _git(repo, "rev-parse", "HEAD")
        sess = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.checkpointer = Checkpointer(repo, sess.id)
        dispatcher.memory_committer = MemoryCommitter(repo)
        path = repo / ".tst" / "memory" / "gotchas.md"

        result = await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": str(path), "content": "the cache key includes the preset\n"},
            session=sess,
        )

        assert result.status == "success"
        assert path.read_text(encoding="utf-8") == "the cache key includes the preset\n"
        assert _head_subject(repo) == MEMORY_COMMIT_SUBJECT
        head = _git(repo, "rev-parse", "HEAD")
        assert head != head_before
        branch = _git(repo, "rev-parse", f"refs/heads/tst/session/{sess.id}")
        assert result.checkpoint_commit == branch
        assert branch != head
        shown = _git(repo, "show", f"HEAD:{path.relative_to(repo).as_posix()}")
        assert shown == "the cache key includes the preset"

    async def test_git_revert_restores_previous_bytes(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        sess = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.memory_committer = MemoryCommitter(repo)
        path = repo / ".tst" / "memory" / "MEMORY.md"

        await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": str(path), "content": "first\n"},
            session=sess,
        )
        await dispatcher.dispatch(
            "c2",
            "fs_write",
            {"path": str(path), "content": "second\n"},
            session=sess,
        )
        assert path.read_text(encoding="utf-8") == "second\n"

        _git(repo, "revert", "--no-edit", "HEAD")
        assert path.read_text(encoding="utf-8") == "first\n"

    async def test_non_memory_write_does_not_move_head(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        head_before = _git(repo, "rev-parse", "HEAD")
        sess = Session(str(repo))
        dispatcher = make_dispatcher(repo)
        dispatcher.checkpointer = Checkpointer(repo, sess.id)
        dispatcher.memory_committer = MemoryCommitter(repo)

        result = await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": str(repo / "src" / "a.py"), "content": "print(1)\n"},
            session=sess,
        )
        assert result.status == "success"
        assert _git(repo, "rev-parse", "HEAD") == head_before
        assert result.checkpoint_commit is not None
        assert result.checkpoint_commit != head_before


class TestNoGit:
    async def test_write_lands_and_notifies_once(self, tmp_path: Path) -> None:
        sess = Session(str(tmp_path))
        dispatcher = make_dispatcher(tmp_path)
        committer = MemoryCommitter(tmp_path)
        dispatcher.memory_committer = committer
        path = tmp_path / ".tst" / "memory" / "gotchas.md"

        first = await dispatcher.dispatch(
            "c1",
            "fs_write",
            {"path": str(path), "content": "note\n"},
            session=sess,
        )
        second = await dispatcher.dispatch(
            "c2",
            "fs_write",
            {"path": str(path), "content": "note two\n"},
            session=sess,
        )

        assert first.status == "success"
        assert path.read_text(encoding="utf-8") == "note two\n"
        assert first.memory_notice is not None
        assert first.memory_notice.code == MEMORY_NO_GIT
        assert "no commit" in first.memory_notice.message
        assert second.memory_notice is None
