"""Restored history never carries a secret the replay hides (TD-5201).

The replay's copy of a message was scrubbed whole when it was logged. The
saved conversation keeps the raw text, so restoring it has to scrub it as
well, and before any cap cuts it: the patterns match whole secrets, and a
secret cut in half by a cap would get past them. Tool results are left out
altogether: a token an MCP server echoed was scrubbed by its exact text,
which a later process may never have read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

import tstd.logging as tstd_logging
from tstd.chat_history import restorable_history
from tstd.chat_history_rows import ARGUMENT_CAP, TEXT_CAP
from tstd.logging import register_secret_value
from tstd.protocol import ConversationHistory, SessionState
from tstd.provider import ChatMessage, FunctionCall
from tstd.provider import ToolCall as WireCall

SID = "s1"
# Built here, not written out, so no line of this file looks like a key.
PAT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
REGISTERED = "mcp-header-token-" + "x7Kq2Lm9Pw4Zr8Tv6Yb3Nc5Hd1Jf0Gs"


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """A process that has read no exact-text secret yet, restored afterwards."""
    saved = tstd_logging._KNOWN_SECRETS
    tstd_logging._KNOWN_SECRETS = ()
    yield
    tstd_logging._KNOWN_SECRETS = saved


def _straddling(secret: str, cap: int, opening: str = "") -> str:
    """*opening*, then text whose cap falls twenty characters into *secret*."""
    return opening + "a" * (cap - 20 - len(opening)) + secret + " and the rest"


def _leaks(secret: str, frame: str) -> bool:
    """Whether *frame* carries what a cut would have left of *secret*."""
    return secret[:16] in frame


def _restored(conversation: list[ChatMessage]) -> ConversationHistory:
    window = [SessionState(session_id=SID, state="running", seq=1)]
    return restorable_history(SID, conversation, window)


def _frame(conversation: list[ChatMessage]) -> str:
    return _restored(conversation).model_dump_json()


def _call(arguments: str) -> ChatMessage:
    call = WireCall(id="c1", function=FunctionCall(name="shell", arguments=arguments))
    return ChatMessage(role="assistant", content=None, tool_calls=[call])


class TestASecretACapWouldCut:
    def test_in_a_question(self) -> None:
        frame = _frame([ChatMessage(role="user", content=_straddling(PAT, TEXT_CAP))])
        assert not _leaks(PAT, frame)
        assert "[REDACTED]" in frame

    def test_in_an_answer(self) -> None:
        conversation = [
            ChatMessage(role="user", content="Show it"),
            ChatMessage(role="assistant", content=_straddling(PAT, TEXT_CAP)),
        ]
        assert not _leaks(PAT, _frame(conversation))

    def test_in_a_summary(self) -> None:
        from tstd.compaction_text import compose

        summary = compose(
            _straddling(PAT, TEXT_CAP, "[Earlier conversation compacted] (1 user turns)\n")
        )
        conversation = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="system", content=summary),
            ChatMessage(role="user", content="Next"),
        ]
        restored = _restored(conversation)
        assert [m.role for m in restored.messages] == ["summary", "user"]
        assert not _leaks(PAT, restored.model_dump_json())

    def test_in_an_argument(self) -> None:
        arguments = json.dumps({"command": _straddling(PAT, ARGUMENT_CAP)})
        conversation = [ChatMessage(role="user", content="Run it"), _call(arguments)]
        assert not _leaks(PAT, _frame(conversation))

    def test_in_arguments_that_are_not_json(self) -> None:
        raw = _straddling(PAT, ARGUMENT_CAP)
        conversation = [ChatMessage(role="user", content="Run it"), _call(raw)]
        assert not _leaks(PAT, _frame(conversation))

    def test_registered_by_its_exact_text(self, fresh_registry: None) -> None:
        register_secret_value(REGISTERED)
        conversation = [
            ChatMessage(role="user", content="Show it"),
            ChatMessage(role="assistant", content=_straddling(REGISTERED, TEXT_CAP)),
        ]
        assert not _leaks(REGISTERED, _frame(conversation))


class TestToolResults:
    def test_never_go_out_even_when_no_longer_registered(self, fresh_registry: None) -> None:
        # Logged while the token was registered, the replay's copy reads
        # [REDACTED]; this process never read that token.
        conversation = [
            ChatMessage(role="user", content="Call the server"),
            _call("{}"),
            ChatMessage(role="tool", content=f"401: bad token {REGISTERED}", tool_call_id="c1"),
            ChatMessage(role="assistant", content="The server refused."),
        ]
        restored = _restored(conversation)
        assert [tool.name for tool in restored.messages[1].tools] == ["shell"]
        assert REGISTERED not in restored.model_dump_json()
        assert "401" not in restored.model_dump_json()
