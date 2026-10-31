"""The conversation from before a replayed window (TD-5201).

An older chat's event window can hold none of its questions: every streamed
chunk is an event, and the window keeps the newest ones. These pin which
messages of the saved conversation come before the window, for each way a
window can begin.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tstd import chat_history_rows
from tstd.chat_history import history_before_window
from tstd.chat_history_rows import MAX_ROWS
from tstd.compaction_text import compose
from tstd.hooks import with_hook_context
from tstd.plan_mode import with_plan_note
from tstd.protocol import (
    AssistantDelta,
    AssistantReasoning,
    ConversationHistory,
    DaemonEvent,
    SessionState,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from tstd.provider import ChatMessage, FunctionCall
from tstd.provider import ToolCall as WireCall

SID = "s1"


def _call(call_id: str, path: str) -> WireCall:
    return WireCall(
        id=call_id, function=FunctionCall(name="read_file", arguments=json.dumps({"path": path}))
    )


def _garden() -> list[ChatMessage]:
    """Three turns; the third reads a file and then answers."""
    return [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Sketch the beds"),
        ChatMessage(role="assistant", content="Three raised beds."),
        ChatMessage(role="user", content="Which plants?"),
        ChatMessage(role="assistant", content="Checking.", tool_calls=[_call("c2", "notes.md")]),
        ChatMessage(role="tool", content="tomatoes, basil", tool_call_id="c2"),
        ChatMessage(role="assistant", content="Tomatoes and basil."),
        ChatMessage(role="user", content="When to water?"),
        ChatMessage(role="assistant", content=None, tool_calls=[_call("c3", "schedule.txt")]),
        ChatMessage(role="tool", content="mon wed fri", tool_call_id="c3"),
        ChatMessage(role="assistant", content="Monday, Wednesday and Friday."),
    ]


def _turn(text: str, n: int) -> UserTurn:
    return UserTurn(session_id=SID, turn_id=f"t{n}", content=text, seq=1)


def _delta(text: str) -> AssistantDelta:
    return AssistantDelta(session_id=SID, delta=text, seq=1)


def _think() -> AssistantReasoning:
    return AssistantReasoning(session_id=SID, delta="hmm", seq=1)


def _done() -> TurnComplete:
    return TurnComplete(session_id=SID, tokens=1, cost=0.0, tier="brain", duration=1.0, seq=1)


def _noise() -> list[DaemonEvent]:
    return [
        SessionState(session_id=SID, state="cancelled", seq=1),
        SessionState(session_id=SID, state="running", seq=1),
    ]


def _history(conversation: list[ChatMessage], window: list[DaemonEvent]) -> ConversationHistory:
    found = history_before_window(SID, conversation, window)
    assert found.user_turns is not None
    return found


def _unaligned(conversation: list[ChatMessage], window: list[DaemonEvent]) -> bool:
    found = history_before_window(SID, conversation, window)
    return found.user_turns is None and found.messages == []


def _texts(history: ConversationHistory) -> list[str]:
    return [f"{m.role}: {m.text}" for m in history.messages]


class TestWhereTheWindowBegins:
    def test_mid_turn_at_a_tool_call_keeps_that_reply_for_the_window(self) -> None:
        window = [
            _think(),
            ToolCall(session_id=SID, tool_call_id="c3", name="read_file", arguments={}, seq=1),
            ToolResult(session_id=SID, tool_call_id="c3", status="success", output="x", seq=1),
            _delta("Monday, Wednesday and Friday."),
            _done(),
            *_noise(),
        ]
        history = _history(_garden(), window)
        assert _texts(history) == [
            "user: Sketch the beds",
            "assistant: Three raised beds.",
            "user: Which plants?",
            "assistant: Checking.",
            "assistant: Tomatoes and basil.",
            "user: When to water?",
        ]
        assert history.user_turns == 3
        assert [m.user_index for m in history.messages if m.role == "user"] == [0, 1, 2]
        tool = history.messages[3].tools[0]
        assert (tool.tool_call_id, tool.name, tool.arguments) == (
            "c2",
            "read_file",
            {"path": "notes.md"},
        )
        assert not history.truncated

    def test_at_a_user_turn_stops_before_that_question(self) -> None:
        window = [_turn("When to water?", 2), _delta("Monday."), _done()]
        history = _history(_garden(), window)
        assert _texts(history)[-1] == "assistant: Tomatoes and basil."
        assert history.user_turns == 2

    def test_the_end_of_the_previous_reply_stays_with_the_window(self) -> None:
        window = [_delta("and basil."), _done(), _turn("When to water?", 2), _done()]
        history = _history(_garden(), window)
        assert _texts(history)[-1] == "assistant: Checking."
        assert history.user_turns == 2

    def test_at_a_tool_result_keeps_the_call_it_answers(self) -> None:
        window = [
            ToolResult(session_id=SID, tool_call_id="c2", status="success", output="x", seq=1),
            _delta("Tomatoes and basil."),
            _done(),
            _turn("When to water?", 2),
        ]
        history = _history(_garden(), window)
        assert _texts(history)[-1] == "assistant: Checking."
        assert history.messages[-1].tools[0].tool_call_id == "c2"
        assert history.user_turns == 2

    def test_restart_noise_alone_makes_the_whole_conversation_history(self) -> None:
        history = _history(_garden(), _noise())
        assert len(history.messages) == 8
        assert history.user_turns == 3

    def test_the_tail_of_the_last_reply_stays_with_the_window(self) -> None:
        history = _history(_garden(), [_delta("and Friday."), _done(), *_noise()])
        assert _texts(history)[-2:] == ["user: When to water?", "assistant: "]
        assert history.messages[-1].tools[0].tool_call_id == "c3"
        assert history.user_turns == 3

    def test_a_reply_still_streaming_is_in_no_message_yet(self) -> None:
        history = _history(_garden(), [_delta("Let me look")])
        assert _texts(history)[-1] == "assistant: Monday, Wednesday and Friday."

    def test_a_window_holding_the_first_turn_says_nothing_was_lost(self) -> None:
        # Only the session's opening state events went.
        window = [
            _turn("Sketch the beds", 0),
            _delta("Three raised beds."),
            _done(),
            _turn("Which plants?", 1),
            _done(),
            _turn("When to water?", 2),
            _done(),
        ]
        history = _history(_garden(), window)
        assert history.messages == []
        assert history.user_turns == 0


class TestWhenNothingCanBeSaid:
    def test_no_conversation(self) -> None:
        assert _unaligned([], [_delta("the end"), _done()])

    def test_a_tool_call_in_no_turn(self) -> None:
        conversation = [ChatMessage(role="system", content="You are helpful.")]
        window = [ToolCall(session_id=SID, tool_call_id="gone", name="x", arguments={}, seq=1)]
        assert _unaligned(conversation, window)

    def test_more_user_turns_in_the_window_than_the_session_has(self) -> None:
        window = [_turn("a", 0), _turn("b", 1), _turn("c", 2), _turn("d", 3)]
        assert _unaligned(_garden(), window)

    def test_a_question_the_count_points_at_says_something_else(self) -> None:
        # An older summary that miscounts its turns, say.
        window = [_turn("Which plants?", 2), _done()]
        assert _unaligned(_garden(), window)


class TestCompactedConversations:
    def _compacted(self) -> list[ChatMessage]:
        summary = compose("[Earlier conversation compacted] (3 user turns)\nUser: early")
        return [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content=summary),
            ChatMessage(role="user", content="Fourth ask"),
            ChatMessage(role="assistant", content="Fourth answer."),
            ChatMessage(role="user", content="Fifth ask"),
            ChatMessage(role="assistant", content="Fifth answer."),
        ]

    def test_a_summary_stands_for_its_turns_and_numbering_counts_them(self) -> None:
        window = [_turn("Fifth ask", 4), _delta("Fifth answer."), _done()]
        history = _history(self._compacted(), window)
        assert [m.role for m in history.messages] == ["summary", "user", "assistant"]
        assert history.messages[0].folded_turns == 3
        assert history.messages[1].user_index == 3
        assert history.user_turns == 4

    def test_a_summary_also_holding_turns_the_window_shows(self) -> None:
        window = [_turn("Third ask", 2), _done(), _turn("Fourth ask", 3), _turn("Fifth ask", 4)]
        history = _history(self._compacted(), window)
        assert [m.role for m in history.messages] == ["summary"]
        assert history.messages[0].folded_turns == 2
        assert history.user_turns == 2


class TestWhatAMessageCarries:
    def test_notes_the_daemon_added_are_cut_and_images_counted_not_sent(self) -> None:
        content = with_hook_context(with_plan_note("Plan the move"), "branch: main")
        picture = [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        conversation = [
            ChatMessage(role="user", content=content),
            ChatMessage(role="assistant", content="Here is a plan."),
            ChatMessage(role="user", content=picture),
            ChatMessage(role="assistant", content="A cat."),
        ]
        history = _history(conversation, _noise())
        assert _texts(history) == [
            "user: Plan the move",
            "assistant: Here is a plan.",
            "user: What is this?\n\n[1 image attached]",
            "assistant: A cat.",
        ]
        assert "base64" not in history.model_dump_json()

    def test_done_check_feedback_is_no_message_and_no_turn(self) -> None:
        conversation = [
            ChatMessage(role="user", content="Fix it"),
            ChatMessage(role="assistant", content="Fixed."),
            ChatMessage(role="user", content="Tests fail.", steer=True, check=True),
            ChatMessage(role="assistant", content="Fixed for real."),
        ]
        history = _history(conversation, _noise())
        assert _texts(history) == [
            "user: Fix it",
            "assistant: Fixed.",
            "assistant: Fixed for real.",
        ]
        assert history.user_turns == 1

    def test_a_call_comes_back_without_what_it_returned(self) -> None:
        conversation = [
            ChatMessage(role="user", content="Read it"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call("c1", "big.txt")]),
            ChatMessage(role="tool", content="file body " * 50, tool_call_id="c1"),
            ChatMessage(role="assistant", content="Read."),
        ]
        history = _history(conversation, _noise())
        assert [tool.name for tool in history.messages[1].tools] == ["read_file"]
        assert "file body" not in history.model_dump_json()

    def test_the_oldest_rows_go_past_the_cap(self) -> None:
        conversation: list[ChatMessage] = []
        for n in range(MAX_ROWS):
            conversation.append(ChatMessage(role="user", content=f"ask {n}"))
            conversation.append(ChatMessage(role="assistant", content=f"answer {n}"))
        history = _history(conversation, _noise())
        assert history.truncated
        assert len(history.messages) == MAX_ROWS
        assert history.messages[-1].text == f"answer {MAX_ROWS - 1}"
        assert history.user_turns == MAX_ROWS


def test_a_long_conversation_is_walked_only_as_far_as_the_rows_that_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach builds this for every older chat it opens (TD-5201): shaping
    tens of thousands of messages only to keep the newest few hundred
    stalled every window for a moment on each click."""
    shaped: list[int] = []
    real = chat_history_rows._row

    def counting(*args: Any) -> Any:
        shaped.append(1)
        return real(*args)

    monkeypatch.setattr(chat_history_rows, "_row", counting)
    conversation: list[ChatMessage] = []
    for n in range(5 * MAX_ROWS):
        conversation.append(ChatMessage(role="user", content=f"ask {n}"))
        conversation.append(ChatMessage(role="assistant", content=f"answer {n}"))
    history = _history(conversation, _noise())
    assert len(history.messages) == MAX_ROWS
    assert len(shaped) == MAX_ROWS + 1
