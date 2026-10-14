"""The pure half of TD-5018: spotting a cut turn and answering its open calls."""

from __future__ import annotations

from tstd.protocol import AssistantDelta, SessionState, TurnComplete, UserTurn
from tstd.provider import ChatMessage, FunctionCall, ToolCall
from tstd.turn_seal import CUT_BY_RESTART, open_turn, repair_cut_conversation


def _call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name="fs_read", arguments='{"path": "a"}'))


def _complete(seq: int) -> TurnComplete:
    return TurnComplete(session_id="s", tokens=1, cost=0, tier="brain", duration=0.1, seq=seq)


class TestOpenTurn:
    def test_open_after_user_turn_and_delta(self) -> None:
        events = [
            SessionState(session_id="s", state="running", seq=1),
            UserTurn(session_id="s", turn_id="t1", content="hi", seq=2),
            AssistantDelta(session_id="s", delta="he", seq=3),
        ]
        assert open_turn(events)

    def test_closed_after_turn_complete(self) -> None:
        events = [
            UserTurn(session_id="s", turn_id="t1", content="hi", seq=1),
            AssistantDelta(session_id="s", delta="hello", seq=2),
            _complete(3),
        ]
        assert not open_turn(events)

    def test_second_turn_open_after_first_closed(self) -> None:
        events = [
            UserTurn(session_id="s", turn_id="t1", content="hi", seq=1),
            _complete(2),
            UserTurn(session_id="s", turn_id="t2", content="again", seq=3),
        ]
        assert open_turn(events)

    def test_no_turns_is_not_open(self) -> None:
        assert not open_turn([])
        assert not open_turn([SessionState(session_id="s", state="idle", seq=1)])


class TestRepairCutConversation:
    def test_replies_only_for_unanswered_ids(self) -> None:
        messages = [
            ChatMessage(role="user", content="read both"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1"), _call("c2")]),
            ChatMessage(role="tool", content="a's text", tool_call_id="c1"),
        ]
        repaired = repair_cut_conversation(messages)
        assert repaired[:3] == messages
        assert len(repaired) == 4
        assert repaired[3].role == "tool"
        assert repaired[3].tool_call_id == "c2"
        assert repaired[3].content == CUT_BY_RESTART
        # The input is left alone.
        assert len(messages) == 3

    def test_every_open_call_answered_in_order(self) -> None:
        messages = [
            ChatMessage(role="user", content="go"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1"), _call("c2")]),
        ]
        repaired = repair_cut_conversation(messages)
        assert [m.tool_call_id for m in repaired[2:]] == ["c1", "c2"]

    def test_finished_conversation_unchanged(self) -> None:
        messages = [
            ChatMessage(role="user", content="read"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="tool", content="text", tool_call_id="c1"),
            ChatMessage(role="assistant", content="done"),
        ]
        assert repair_cut_conversation(messages) == messages

    def test_earlier_assistant_calls_are_not_reopened(self) -> None:
        messages = [
            ChatMessage(role="user", content="read"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1")]),
            ChatMessage(role="tool", content="text", tool_call_id="c1"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
        ]
        assert repair_cut_conversation(messages) == messages

    def test_empty_conversation_unchanged(self) -> None:
        assert repair_cut_conversation([]) == []
