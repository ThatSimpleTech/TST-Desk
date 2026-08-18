"""Conversation fork (TD-1708).

Editing a past user turn truncates the model conversation and starts a
sibling. Switching siblings restores the snapshot. A turn in flight
refuses the fork.
"""

from __future__ import annotations

from pathlib import Path

from tstd.provider import ChatMessage
from tstd.session import Session


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def _assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


def _seed(session: Session) -> None:
    session.conversation.extend(
        [
            _user("first"),
            _assistant("answer one"),
            _user("second"),
            _assistant("answer two"),
        ]
    )


class TestForkFrom:
    async def test_replaces_the_user_turn_and_drops_the_tail(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        _seed(session)
        result = await session.fork_from(0, "first, edited")
        assert not isinstance(result, str)
        assert [m.content for m in session.conversation if m.role == "user"] == []
        # The new content is queued, not yet appended — the loop does that.
        assert session.pending_user_messages == 1
        assert result.user_index == 0
        assert result.sibling_index == 1
        assert result.sibling_count == 2
        assert result.content == "first, edited"

    async def test_refuses_an_unknown_user_index(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        _seed(session)
        assert await session.fork_from(5, "nope") == "user_turn_not_found"

    async def test_refuses_while_a_turn_is_in_flight(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        _seed(session)
        session._open_turns = 1
        assert await session.fork_from(0, "edited") == "turn_in_progress"

    async def test_refuses_blank_content(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        _seed(session)
        assert await session.fork_from(0, "   ") == "empty_content"


class TestSetBranch:
    async def test_restores_the_original_sibling(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        _seed(session)
        await session.fork_from(1, "second, edited")
        session.conversation.append(_user("second, edited"))
        session.conversation.append(_assistant("new answer"))
        session.snapshot_branches()
        session._open_turns = 0

        result = await session.set_branch(1, 0)
        assert not isinstance(result, str)
        assert [m.content for m in session.conversation] == [
            "first",
            "answer one",
            "second",
            "answer two",
        ]
        assert result.sibling_index == 0
        assert result.sibling_count == 2

        result = await session.set_branch(1, 1)
        assert not isinstance(result, str)
        assert [m.content for m in session.conversation] == [
            "first",
            "answer one",
            "second, edited",
            "new answer",
        ]
        assert result.sibling_index == 1
