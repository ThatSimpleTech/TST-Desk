"""Restore files to a user turn (TD-5125): the git side.

A scripted agent writes through a real ``Checkpointer`` (pre-image, write,
checkpoint, as the dispatcher does) with a counter standing in for the
session log's seq, and a user turn is the seq the counter had when it was
"sent". Every test checks the files on disk and the refs, not the plan's
own bookkeeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_checkpoint import _git, make_repo
from tstd.autonomy import Checkpointer
from tstd.autonomy.checkpoint import DecisionClass, session_branch
from tstd.autonomy.checkpoint_message import agent_message, parse_trailers
from tstd.file_rewind import RewindError, apply_rewind, plan_rewind

SID = "sess-rewind"


class Agent:
    """Writes like the dispatcher: pre-image, the write, then the checkpoint."""

    def __init__(self, repo: Path, *, with_seq: bool = True) -> None:
        self.repo = repo
        self.seq = 0
        self.cp = Checkpointer(repo, SID, log_seq=(lambda: self.seq) if with_seq else None)

    def turn(self) -> int:
        """A user message lands in the log; its seq is the turn's."""
        self.seq += 1
        return self.seq

    async def write(self, rel: str, content: str | None) -> None:
        self.seq += 1  # the tool_call event
        path = self.repo / rel
        await self.cp.preimage([path], tool_name="fs_write", tool_call_id=f"c{self.seq}")
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        outcome = await self.cp.checkpoint(
            [path],
            tool_name="fs_write",
            tool_call_id=f"c{self.seq}",
            decision_class=DecisionClass.A,
            decision_rule="workspace-write",
        )
        assert outcome.status == "committed", outcome

    async def restore(self, turn: int, *, include: list[str] | None = None) -> object:
        plan = await plan_rewind(self.repo, SID, turn)
        self.seq += 1
        return await apply_rewind(
            self.repo,
            SID,
            turn,
            turn_id=f"turn-{turn}",
            plan_id=plan.plan_id,
            include_hand_edited=include or [],
            log_seq=self.seq,
        )


def _read(repo: Path, rel: str) -> str | None:
    path = repo / rel
    return path.read_text() if path.exists() else None


def _changes(plan: object) -> dict[str, tuple[str, bool]]:
    files = getattr(plan, "files", ())
    return {f.path: (f.change, f.hand_edited) for f in files}


class TestRestoreToTurn:
    async def test_undoes_only_what_the_agent_did_since_that_turn(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        first = agent.turn()
        await agent.write("a.txt", "one\n")
        await agent.write("b.txt", "made in turn one\n")
        second = agent.turn()
        await agent.write("a.txt", "two\n")
        await agent.write("c.txt", "made in turn two\n")
        await agent.write("b.txt", None)

        plan = await plan_rewind(repo, SID, second)
        assert _changes(plan) == {
            "a.txt": ("modify", False),
            "b.txt": ("recreate", False),
            "c.txt": ("delete", False),
        }
        # The diff is what goes: the agent's turn-two edits.
        assert "-one" in plan.diff and "+two" in plan.diff
        assert "+made in turn two" in plan.diff

        await agent.restore(second)
        assert _read(repo, "a.txt") == "one\n"
        assert _read(repo, "b.txt") == "made in turn one\n"
        assert _read(repo, "c.txt") is None

        await agent.restore(first)
        assert _read(repo, "a.txt") == "base\n"
        assert _read(repo, "b.txt") is None

    async def test_never_moves_head_main_or_the_index(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")
        await agent.restore(turn)

        assert _git(repo, "rev-parse", "HEAD") == head
        assert _git(repo, "rev-parse", "refs/heads/main") == head
        assert _git(repo, "diff", "--cached", "--name-only") == ""
        assert _git(repo, "status", "--porcelain") == ""
        # The restore is on the session branch, so the next one starts from it.
        subject = _git(repo, "log", "-1", "--format=%s", f"refs/heads/{session_branch(SID)}")
        assert subject.startswith("restore:")
        assert (await plan_rewind(repo, SID, turn)).files == ()

    async def test_a_turn_with_nothing_after_it_has_nothing_to_restore(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        agent.turn()
        await agent.write("a.txt", "agent\n")
        later = agent.turn()
        plan = await plan_rewind(repo, SID, later)
        assert plan.files == ()
        assert plan.diff == ""

    async def test_no_session_branch_is_nothing_to_restore(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        assert (await plan_rewind(repo, SID, 1)).files == ()


class TestHandEdits:
    async def test_a_file_changed_by_hand_after_the_agent_is_flagged_and_left_alone(
        self, tmp_path: Path
    ) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")
        await agent.write("b.txt", "agent b\n")
        (repo / "a.txt").write_text("mine\n")

        plan = await plan_rewind(repo, SID, turn)
        assert _changes(plan) == {"a.txt": ("modify", True), "b.txt": ("delete", False)}

        result = await agent.restore(turn)
        assert getattr(result, "left_alone", None) == ("a.txt",)
        assert _read(repo, "a.txt") == "mine\n"
        assert _read(repo, "b.txt") is None

    async def test_a_ticked_hand_edited_file_is_restored(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")
        (repo / "a.txt").write_text("mine\n")

        await agent.restore(turn, include=["a.txt"])
        assert _read(repo, "a.txt") == "base\n"

    async def test_a_hand_edit_between_agent_writes_is_flagged(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent one\n")
        (repo / "a.txt").write_text("mine\n")
        await agent.write("a.txt", "agent two\n")  # disk matches the tip again

        plan = await plan_rewind(repo, SID, turn)
        assert _changes(plan) == {"a.txt": ("modify", True)}
        await agent.restore(turn)
        assert _read(repo, "a.txt") == "agent two\n"

    async def test_uncommitted_work_from_before_the_turn_comes_back(self, tmp_path: Path) -> None:
        # The branch starts at HEAD, which has "base". Without the pre-image
        # the restore would write "base" and lose the person's own edit.
        repo = make_repo(tmp_path)
        (repo / "a.txt").write_text("my unsaved work\n")
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")

        plan = await plan_rewind(repo, SID, turn)
        assert _changes(plan) == {"a.txt": ("modify", False)}
        await agent.restore(turn)
        assert _read(repo, "a.txt") == "my unsaved work\n"

    async def test_a_preview_goes_stale_when_a_file_changes(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")
        plan = await plan_rewind(repo, SID, turn)
        (repo / "a.txt").write_text("edited after the preview\n")

        with pytest.raises(RewindError) as caught:
            await apply_rewind(
                repo,
                SID,
                turn,
                turn_id="t",
                plan_id=plan.plan_id,
                include_hand_edited=["a.txt"],
                log_seq=99,
            )
        assert caught.value.code == "restore_stale"
        assert _read(repo, "a.txt") == "edited after the preview\n"


class TestRefusals:
    async def test_not_a_git_repository(self, tmp_path: Path) -> None:
        with pytest.raises(RewindError) as caught:
            await plan_rewind(tmp_path, SID, 1)
        assert caught.value.code == "rewind_unavailable"

    async def test_checkpoints_without_a_log_seq_are_refused(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        agent = Agent(repo, with_seq=False)
        turn = agent.turn()
        await agent.write("a.txt", "agent\n")
        with pytest.raises(RewindError) as caught:
            await plan_rewind(repo, SID, turn)
        assert caught.value.code == "rewind_predates"
        assert _read(repo, "a.txt") == "agent\n"

    async def test_another_sessions_branch_is_not_read(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        other = Checkpointer(repo, "other-session", log_seq=lambda: 5)
        (repo / "a.txt").write_text("other\n")
        await other.checkpoint(
            [repo / "a.txt"],
            tool_name="fs_write",
            tool_call_id="x",
            decision_class=DecisionClass.A,
        )
        assert (await plan_rewind(repo, SID, 0)).files == ()


class TestMessages:
    def test_a_file_name_cannot_write_a_trailer(self) -> None:
        message = agent_message(
            session_id="s1",
            tool_name="fs_write",
            tool_call_id="c1",
            decision="A",
            via="rule",
            rels=["evil\nLog-Seq: 0\nKind: restore"],
            log_seq=42,
        )
        trailers = parse_trailers(message)
        assert trailers is not None
        assert trailers.log_seq == 42
        assert trailers.kind == "agent"
        assert all(line.count(": ") >= 1 for line in message.splitlines()[2:])
