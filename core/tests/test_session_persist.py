"""On-disk session transcript and conversation (honest revive)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tstd.protocol import AssistantDelta, UserTurn, parse_daemon_event
from tstd.provider import ChatMessage, FunctionCall, ToolCall
from tstd.session_persist import SessionPersist, chat_message_from_dict, chat_message_to_dict


def _persist(tmp_path: Path) -> SessionPersist:
    return SessionPersist(tmp_path)


class TestSessionPersist:
    def test_missing_directory_is_none(self, tmp_path: Path) -> None:
        assert _persist(tmp_path).load("nope") is None

    def test_prepare_then_round_trip_events_and_conversation(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        persist.append_event(
            "s1",
            UserTurn(session_id="s1", turn_id="t1", content="Fix the tests", seq=1),
        )
        persist.append_event(
            "s1",
            AssistantDelta(session_id="s1", delta="Done.", seq=2),
        )
        persist.save_conversation(
            "s1",
            [
                ChatMessage(role="user", content="Fix the tests"),
                ChatMessage(role="assistant", content="Done."),
            ],
        )

        loaded = persist.load("s1")
        assert loaded is not None
        assert loaded.conversation is not None
        assert [m.role for m in loaded.conversation] == ["user", "assistant"]
        assert [m.content for m in loaded.conversation] == ["Fix the tests", "Done."]
        assert isinstance(loaded.events[0], UserTurn)
        assert loaded.events[0].content == "Fix the tests"
        assert isinstance(loaded.events[1], AssistantDelta)
        assert loaded.events[1].delta == "Done."

        events_path = persist.dir_for("s1") / "events.jsonl"
        convo_path = persist.dir_for("s1") / "conversation.json"
        assert oct(events_path.stat().st_mode)[-3:] == "600"
        assert oct(convo_path.stat().st_mode)[-3:] == "600"

    def test_prepare_writes_empty_conversation_not_missing(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        loaded = persist.load("s1")
        assert loaded is not None
        assert loaded.conversation == []
        assert loaded.events == []

    def test_malformed_event_lines_are_dropped(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        persist.append_event(
            "s1",
            UserTurn(session_id="s1", turn_id="t1", content="keep", seq=1),
        )
        events = persist.dir_for("s1") / "events.jsonl"
        events.write_text(
            events.read_text(encoding="utf-8") + "not-json\n" + '{"type":"unknown_event"}\n',
            encoding="utf-8",
        )
        loaded = persist.load("s1")
        assert loaded is not None
        assert len(loaded.events) == 1
        assert isinstance(loaded.events[0], UserTurn)

    def test_malformed_conversation_is_none_not_invented(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        (persist.dir_for("s1") / "conversation.json").write_text("{", encoding="utf-8")
        loaded = persist.load("s1")
        assert loaded is not None
        assert loaded.conversation is None

    def test_conversation_that_is_not_a_list_is_none(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        (persist.dir_for("s1") / "conversation.json").write_text(
            json.dumps({"role": "user"}), encoding="utf-8"
        )
        loaded = persist.load("s1")
        assert loaded is not None
        assert loaded.conversation is None

    def test_remove_deletes_the_directory(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        persist.remove("s1")
        assert persist.load("s1") is None
        persist.remove("already-gone")

    def test_tool_message_round_trip(self, tmp_path: Path) -> None:
        persist = _persist(tmp_path)
        persist.prepare("s1")
        persist.save_conversation(
            "s1",
            [
                ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            function=FunctionCall(name="fs_read", arguments='{"path":"a"}'),
                        )
                    ],
                ),
                ChatMessage(role="tool", content="hello", tool_call_id="c1"),
            ],
        )
        loaded = persist.load("s1")
        assert loaded is not None
        assert loaded.conversation is not None
        assert loaded.conversation[0].tool_calls is not None
        assert loaded.conversation[0].tool_calls[0].function is not None
        assert loaded.conversation[0].tool_calls[0].function.name == "fs_read"
        assert loaded.conversation[1].tool_call_id == "c1"


class TestChatMessageCodec:
    def test_unknown_role_is_a_load_error(self) -> None:
        with pytest.raises(ValueError, match="role"):
            chat_message_from_dict({"role": "narrator", "content": "nope"})

    def test_to_dict_then_from_dict(self) -> None:
        msg = ChatMessage(role="user", content="hi")
        assert chat_message_from_dict(chat_message_to_dict(msg)).content == "hi"


class TestParseUserTurn:
    def test_user_turn_is_a_known_daemon_event(self) -> None:
        raw = UserTurn(session_id="s1", turn_id="t1", content="hello", seq=4).model_dump_json()
        parsed = parse_daemon_event(raw)
        assert isinstance(parsed, UserTurn)
        assert parsed.content == "hello"
