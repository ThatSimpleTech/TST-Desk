"""Conversation fork (TD-1708, TD-5147).

Editing a past user turn truncates the model conversation and starts a
sibling. Switching siblings restores the snapshot. A turn in flight
refuses the fork. After a compaction the window's turn numbers are mapped
through the summaries, and a turn inside one is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_compaction import tiny_tier
from tstd.compaction import ContextMeter, maybe_compact, summary_user_turns, window_user_turns
from tstd.context.tokens import HeuristicTokenCounter
from tstd.daemon import Daemon
from tstd.provider import ChatMessage, content_as_text
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


# ── After a compaction (TD-5147) ─────────────────────────────────────────
#
# The window numbers user turns from the start of the session.  A
# compaction folds older turns into a summary that says how many it stands
# for, so Edit and the branch arrows map the window's number through it.

_COUNTER = HeuristicTokenCounter()
# A brain window small enough that five long turns compact.
_AUTO_TIER = tiny_tier(context_window=400, max_output_tokens=25)


def _q(n: int) -> str:
    return f"question {n} " + "x" * 230


def _a(n: int) -> str:
    return f"answer {n} " + "y" * 230


def _long_session(tmp_path: Path, turns: int) -> Session:
    session = Session(str(tmp_path))
    session.conversation.append(ChatMessage(role="system", content="You are helpful."))
    for n in range(turns):
        session.conversation.extend([_user(_q(n)), _assistant(_a(n))])
    session.context_meter = ContextMeter("brain", _AUTO_TIER, _COUNTER, None)
    return session


def _auto_compact(session: Session) -> None:
    """What the loop does before a request that is over budget."""
    compacted, stats = maybe_compact(session.conversation, _AUTO_TIER, _COUNTER)
    assert stats is not None
    session.conversation[:] = compacted


def _land(session: Session, user: str, answer: str) -> None:
    """What the loop does with a queued (forked) turn: append it and its reply."""
    session.conversation.extend([_user(user), _assistant(answer)])
    session._drain_user_queue()
    session.snapshot_branches()


def _users(session: Session) -> list[str]:
    return [content_as_text(m.content) for m in session.conversation if m.role == "user"]


def _summaries(session: Session) -> list[int | None]:
    return [summary_user_turns(m) for m in session.conversation if m.role == "system"][1:]


class TestForkAfterCompaction:
    async def test_auto_compaction_edits_the_turn_the_window_named(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=5)
        _auto_compact(session)
        assert _summaries(session) == [3]  # turns 0-2; 3 and 4 kept verbatim
        result = await session.fork_from(4, "question 4, edited")
        assert not isinstance(result, str)
        assert result.user_index == 4
        assert result.content == "question 4, edited"
        # Cut at turn 4: the summary and turn 3 stay, turn 4 is replaced.
        assert _users(session) == [_q(3)]
        assert content_as_text(session.conversation[-1].content) == _a(3)

    async def test_auto_compaction_refuses_a_summarized_turn(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=5)
        _auto_compact(session)
        before = list(session.conversation)
        # Turn 1 is inside the summary. The old mapping counted live user
        # messages only and forked turn 4 here instead.
        assert await session.fork_from(1, "question 1, edited") == "user_turn_compacted"
        assert await session.fork_from(2, "question 2, edited") == "user_turn_compacted"
        assert await session.fork_from(5, "nope") == "user_turn_not_found"
        assert session.conversation == before
        assert session.pending_user_messages == 0

    async def test_slash_compact_keeps_the_latest_turn_editable(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=5)
        assert await session.compact_now() is None  # /compact: turns 0-3
        assert _summaries(session) == [4]
        assert await session.fork_from(3, "question 3, edited") == "user_turn_compacted"
        result = await session.fork_from(4, "question 4, edited")
        assert not isinstance(result, str)
        assert _users(session) == []
        assert _summaries(session) == [4]

    async def test_from_here_compaction_keeps_later_numbers(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=5)
        assert await session.compact_now(user_index=2) is None  # turns 2-4
        session.conversation.extend([_user(_q(5)), _assistant(_a(5))])
        # Turn 2 is summarized; the old mapping forked turn 5 for it.
        assert await session.fork_from(2, "question 2, edited") == "user_turn_compacted"
        assert await session.fork_from(4, "question 4, edited") == "user_turn_compacted"
        result = await session.fork_from(5, "question 5, edited")
        assert not isinstance(result, str)
        assert result.user_index == 5
        assert _users(session) == [_q(0), _q(1)]
        assert _summaries(session) == [3]

    async def test_from_here_compaction_leaves_earlier_turns_editable(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=5)
        assert await session.compact_now(user_index=2) is None
        session.conversation.extend([_user(_q(5)), _assistant(_a(5))])
        result = await session.fork_from(1, "question 1, edited")
        assert not isinstance(result, str)
        assert _users(session) == [_q(0)]
        assert _summaries(session) == []

    async def test_a_conversation_with_no_system_prompt_counts_from_its_first_message(
        self, tmp_path: Path
    ) -> None:
        # A Grok-engine session keeps no system prompt in its conversation.
        session = Session(str(tmp_path))
        _seed(session)
        assert window_user_turns(session.conversation) == [0, 2]
        result = await session.fork_from(1, "second, edited")
        assert not isinstance(result, str)
        assert _users(session) == ["first"]


class TestSetBranchAfterCompaction:
    async def test_auto_compaction_restores_the_named_turn(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=4)
        result = await session.fork_from(3, "question 3, edited")
        assert not isinstance(result, str)
        _land(session, "question 3, edited", "answer 3, edited")
        session.conversation.extend([_user(_q(4)), _assistant(_a(4))])
        _auto_compact(session)  # turns 0-2; 3 and 4 kept verbatim
        session.snapshot_branches()
        assert _summaries(session) == [3]

        back = await session.set_branch(3, 0)
        assert not isinstance(back, str)
        assert back.content == _q(3)
        assert _users(session) == [_q(0), _q(1), _q(2), _q(3)]

        # The edited sibling was compacted; the old mapping sent "" here.
        again = await session.set_branch(3, 1)
        assert not isinstance(again, str)
        assert again.user_index == 3
        assert again.sibling_index == 1
        assert again.sibling_count == 2
        assert again.content == "question 3, edited"
        assert _users(session) == ["question 3, edited", _q(4)]
        assert _summaries(session) == [3]

    async def test_auto_compaction_refuses_a_summarized_turn(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=3)
        result = await session.fork_from(1, "question 1, edited")
        assert not isinstance(result, str)
        _land(session, "question 1, edited", "answer 1, edited")
        for n in range(2, 5):
            session.conversation.extend([_user(_q(n)), _assistant(_a(n))])
        _auto_compact(session)  # turns 0-2, the edited turn 1 among them
        session.snapshot_branches()
        before = list(session.conversation)

        assert await session.set_branch(1, 0) == "user_turn_compacted"
        assert session.conversation == before
        assert session._branch_cursor[1] == 1

    async def test_from_here_compaction_restores_the_named_turn(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=3)
        assert await session.compact_now(user_index=1) is None  # turns 1-2
        session.conversation.extend([_user(_q(3)), _assistant(_a(3))])
        session.conversation.extend([_user(_q(4)), _assistant(_a(4))])
        result = await session.fork_from(4, "question 4, edited")
        assert not isinstance(result, str)
        _land(session, "question 4, edited", "answer 4, edited")

        back = await session.set_branch(4, 0)
        assert not isinstance(back, str)
        assert back.content == _q(4)
        assert _users(session) == [_q(0), _q(3), _q(4)]
        again = await session.set_branch(4, 1)
        assert not isinstance(again, str)
        assert again.content == "question 4, edited"
        assert _users(session) == [_q(0), _q(3), "question 4, edited"]

    async def test_from_here_compaction_refuses_a_summarized_turn(self, tmp_path: Path) -> None:
        session = _long_session(tmp_path, turns=4)
        result = await session.fork_from(3, "question 3, edited")
        assert not isinstance(result, str)
        _land(session, "question 3, edited", "answer 3, edited")
        assert await session.compact_now(user_index=1) is None  # turns 1-3
        session.snapshot_branches()
        before = list(session.conversation)

        assert await session.set_branch(3, 0) == "user_turn_compacted"
        assert session.conversation == before


async def test_daemon_explains_an_edit_of_a_summarized_message(tmp_path: Path) -> None:
    daemon = Daemon(data_dir=tmp_path / "data")
    session = await daemon.session_registry.create(str(tmp_path))
    session.conversation.extend(_long_session(tmp_path, turns=3).conversation)
    result = await session.fork_from(1, "question 1, edited")
    assert not isinstance(result, str)
    _land(session, "question 1, edited", "answer 1, edited")
    for n in range(2, 5):
        session.conversation.extend([_user(_q(n)), _assistant(_a(n))])
    _auto_compact(session)  # turns 0-2
    session.snapshot_branches()

    for payload in (
        {"type": "fork_from", "session_id": session.id, "user_index": 1, "content": "x"},
        {"type": "set_branch", "session_id": session.id, "user_index": 1, "sibling_index": 0},
    ):
        raw = await daemon._handle_message(json.dumps(payload), None)
        assert raw is not None
        reply = json.loads(raw)
        assert reply["type"] == "error"
        assert reply["code"] == "user_turn_compacted"
        assert reply["session_id"] == session.id
        # Words for the window's toast, not the code with spaces.
        assert reply["message"].startswith("That message was folded into a summary")

    raw = await daemon._handle_message(
        json.dumps(
            {"type": "set_branch", "session_id": session.id, "user_index": 3, "sibling_index": 0}
        ),
        None,
    )
    assert raw is not None
    assert json.loads(raw)["message"] == "That version of the message is no longer available."
