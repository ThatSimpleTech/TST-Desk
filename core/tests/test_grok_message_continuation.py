"""Resumed assistant messages retain attachments and metadata (TD-4833)."""

from pathlib import Path

import pytest

from tstd.grok_loop import _append_assistant
from tstd.provider import ChatMessage, MessageContent, ToolCall, content_as_text
from tstd.session import Session


@pytest.mark.parametrize(
    "content,expected",
    [(None, "next"), ("", "next"), ("first ", "first next")],
)
def test_text_continuation(tmp_path: Path, content: MessageContent | None, expected: str) -> None:
    session = Session(str(tmp_path))
    calls = [ToolCall(id="tc-1")]
    session.conversation.append(ChatMessage(role="assistant", content=content, tool_calls=calls))
    _append_assistant(session, "next")
    assert len(session.conversation) == 1
    assert session.conversation[-1].content == expected
    assert session.conversation[-1].tool_calls == calls


def test_multimodal_continuation_preserves_parts(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    parts = [
        {"type": "text", "text": "first"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    session.conversation.append(ChatMessage(role="assistant", content=parts))
    _append_assistant(session, "next")
    assert session.conversation[-1].content == [*parts, {"type": "text", "text": "next"}]
    assert len(parts) == 2
    assert content_as_text(session.conversation[-1].content) == "first\nnext"


def test_user_message_is_not_overwritten(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    session.conversation.append(ChatMessage(role="user", content="question"))
    _append_assistant(session, "answer")
    assert [(m.role, m.content) for m in session.conversation] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]
