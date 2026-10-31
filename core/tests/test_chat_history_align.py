"""Where restored history meets the window, when ids and counts mislead (TD-5201).

A history lined up wrong is worse than none: its user turns carry the
numbers Edit, Retry and Summarize from here fork by. These pin the windows
whose events a plain count or a plain id misreads: a fork the window shows
both sides of, a message a hook blocked, a reply a retry withdrew or a
done-check redid, servers that repeat or omit tool-call ids, and the Grok
engine, whose conversation keeps no tool calls.
"""

from __future__ import annotations

import json

from tstd.chat_history import history_before_window
from tstd.hooks import HOOK_BLOCKED_NOTE
from tstd.plan_mode import with_plan_note
from tstd.protocol import (
    AssistantDelta,
    AssistantRetract,
    ConversationHistory,
    ConversationReset,
    DaemonEvent,
    SessionState,
    SteerPoint,
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


def _turn(text: str, n: int, *, steer: SteerPoint | None = None) -> UserTurn:
    return UserTurn(session_id=SID, turn_id=f"t{n}", content=text, steer=steer, seq=1)


def _delta(text: str, response: str | None = None) -> AssistantDelta:
    return AssistantDelta(session_id=SID, delta=text, response_id=response, seq=1)


def _done(code: str | None = None) -> TurnComplete:
    return TurnComplete(
        session_id=SID, tokens=1, cost=0.0, tier="brain", duration=1.0, error_code=code, seq=1
    )


def _calls(call_id: str) -> list[DaemonEvent]:
    return [
        ToolCall(session_id=SID, tool_call_id=call_id, name="read_file", arguments={}, seq=1),
        ToolResult(session_id=SID, tool_call_id=call_id, status="success", output="x", seq=1),
    ]


def _reset(user_index: int, content: str) -> ConversationReset:
    return ConversationReset(
        session_id=SID,
        user_index=user_index,
        sibling_index=1,
        sibling_count=2,
        content=content,
        seq=1,
    )


def _texts(history: ConversationHistory) -> list[str]:
    return [f"{m.role}: {m.text}" for m in history.messages]


def _chat(turns: int) -> list[ChatMessage]:
    conversation = [ChatMessage(role="system", content="You are helpful.")]
    for n in range(turns):
        conversation.append(ChatMessage(role="user", content=f"Ask {n}"))
        conversation.append(ChatMessage(role="assistant", content=f"Answer {n}."))
    return conversation


def _aligned(conversation: list[ChatMessage], window: list[DaemonEvent]) -> ConversationHistory:
    history = history_before_window(SID, conversation, window)
    assert history.user_turns is not None
    return history


def _unaligned(conversation: list[ChatMessage], window: list[DaemonEvent]) -> bool:
    history = history_before_window(SID, conversation, window)
    return history.user_turns is None and history.messages == []


class TestForksInTheWindow:
    def test_a_retry_the_window_shows_both_sides_of(self) -> None:
        # Retry of the last turn: its first answer and the retried one.
        window: list[DaemonEvent] = [
            _turn("Ask 8", 8),
            _delta("Answer 8."),
            _done(),
            _turn("Ask 9", 9),
            _delta("First try."),
            _done(),
            _reset(9, "Ask 9"),
            _turn("Ask 9", 10),
            _delta("Answer 9."),
            _done(),
        ]
        assert _unaligned(_chat(10), window)

    def test_an_edit_inside_the_window(self) -> None:
        conversation = _chat(18)
        conversation.append(ChatMessage(role="user", content="Ask 18, edited"))
        conversation.append(ChatMessage(role="assistant", content="Edited answer."))
        window: list[DaemonEvent] = [_turn(f"Ask {n}", n) for n in range(15, 21)]
        window += [_reset(18, "Ask 18, edited"), _turn("Ask 18, edited", 22), _done()]
        assert _unaligned(conversation, window)

    def test_a_fork_whose_turn_never_ran(self) -> None:
        # The log took the reset; the saved conversation never took the fork.
        window: list[DaemonEvent] = [_turn("Ask 5", 5), _turn("Ask 6", 6), _turn("Ask 7", 7)]
        window += [SessionState(session_id=SID, state="cancelled", seq=1), _reset(2, "fork")]
        assert _unaligned(_chat(8), window)


class TestMessagesAHookBlocked:
    def test_a_blocked_message_is_no_turn(self) -> None:
        window: list[DaemonEvent] = [
            _turn("delete everything", 0),
            _delta(f"{HOOK_BLOCKED_NOTE}not allowed"),
            _done("hook_blocked"),
            _turn("Ask 1", 1),
            _delta("Answer 1."),
            _done(),
        ]
        history = _aligned(_chat(2), window)
        assert _texts(history) == ["user: Ask 0", "assistant: Answer 0."]
        assert history.user_turns == 1

    def test_a_blocked_steer_is_no_turn_and_its_note_no_reply(self) -> None:
        point = SteerPoint(turn_id="t0", step=1, after_tool_call_ids=[])
        window: list[DaemonEvent] = [
            _turn("stop that", 0, steer=point),
            _delta(f"{HOOK_BLOCKED_NOTE}not allowed\n\n"),
            _delta("Answer 1."),
            _done(),
            _turn("Ask 2", 2),
            _done(),
        ]
        history = _aligned(_chat(3), window)
        assert _texts(history)[-1] == "user: Ask 1"
        assert history.user_turns == 2


class TestWordsMustAgree:
    def test_notes_and_images_do_not_count_as_a_difference(self) -> None:
        picture = [
            {"type": "text", "text": with_plan_note("What is this?")},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        conversation = [*_chat(1), ChatMessage(role="user", content=picture)]
        conversation.append(ChatMessage(role="assistant", content="A cat."))
        shown = "What is this?\n\n[Images attached: cat.png (12 bytes)]"
        history = _aligned(conversation, [_turn(shown, 1), _delta("A cat."), _done()])
        assert history.user_turns == 1


class TestRepliesTheWindowShowsTheEndOf:
    def test_a_reply_a_retry_withdrew_is_no_part_of_it(self) -> None:
        conversation = [*_chat(1), ChatMessage(role="user", content="Ask 1")]
        conversation.append(ChatMessage(role="assistant", content="The full answer, second try."))
        window: list[DaemonEvent] = [
            _delta("junk junk", "r1"),
            AssistantRetract(session_id=SID, response_id="r1", reason="stream_stalled", seq=1),
            _delta("The full answer, second try.", "r2"),
            _done(),
        ]
        history = _aligned(conversation, window)
        assert _texts(history)[-1] == "user: Ask 1"

    def test_a_done_check_retry_ends_a_turn_in_two_messages(self) -> None:
        conversation = [
            ChatMessage(role="user", content="Fix it"),
            ChatMessage(role="assistant", content="Fixed."),
            ChatMessage(role="user", content="Tests fail.", steer=True, check=True),
            ChatMessage(role="assistant", content="Fixed for real."),
        ]
        history = _aligned(conversation, [_delta("ed."), _delta("Fixed for real."), _done()])
        assert _texts(history) == ["user: Fix it"]

    def test_a_secret_the_log_redacted_still_matches(self) -> None:
        key = "sk-" + "PROJEXAMPLEKEY" + "fakefake0000abcd"
        conversation = [*_chat(1), ChatMessage(role="user", content="Ask 1")]
        conversation.append(ChatMessage(role="assistant", content=f"Your key {key} is set."))
        history = _aligned(conversation, [_delta("Your key [REDACTED] is set."), _done()])
        assert _texts(history)[-1] == "user: Ask 1"

    def test_a_finished_reply_that_matches_nothing_stops_at_its_question(self) -> None:
        history = _aligned(_chat(2), [_delta("text the conversation never had"), _done()])
        assert _texts(history)[-1] == "user: Ask 1"
        assert history.user_turns == 2


class TestToolCallIdsThatAreNoKey:
    def _steps(self, call_id: str) -> list[ChatMessage]:
        """One turn, two steps, both calls under the same id."""
        return [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Read both"),
            ChatMessage(role="assistant", content=None, tool_calls=[_call(call_id, "a.txt")]),
            ChatMessage(role="tool", content="a", tool_call_id=call_id),
            ChatMessage(role="assistant", content=None, tool_calls=[_call(call_id, "b.txt")]),
            ChatMessage(role="tool", content="b", tool_call_id=call_id),
            ChatMessage(role="assistant", content="Done."),
        ]

    def test_an_id_every_response_repeats(self) -> None:
        window = [*_calls("call_0"), *_calls("call_0"), _delta("Done."), _done()]
        history = _aligned(self._steps("call_0"), window)
        assert _texts(history) == ["user: Read both"]
        assert history.user_turns == 1

    def test_no_id_at_all(self) -> None:
        window = [*_calls(""), *_calls(""), _delta("Done."), _done()]
        assert _texts(_aligned(self._steps(""), window)) == ["user: Read both"]


class TestTheGrokEngine:
    def _conversation(self) -> list[ChatMessage]:
        # No system prompt and no tool calls: only what was said.
        return [
            ChatMessage(role="user", content="Fix the bug"),
            ChatMessage(role="assistant", content="Fixed it."),
            ChatMessage(role="user", content="Now the tests"),
            ChatMessage(role="assistant", content="They pass."),
        ]

    def test_a_window_starting_at_a_tool_result(self) -> None:
        window = [_calls("t9")[1], _delta("They pass."), _done()]
        history = _aligned(self._conversation(), window)
        assert _texts(history) == [
            "user: Fix the bug",
            "assistant: Fixed it.",
            "user: Now the tests",
        ]
        assert history.user_turns == 2

    def test_a_window_starting_at_a_tool_call(self) -> None:
        window = [*_calls("t9"), _delta("They pass."), _done()]
        assert _aligned(self._conversation(), window).user_turns == 2
