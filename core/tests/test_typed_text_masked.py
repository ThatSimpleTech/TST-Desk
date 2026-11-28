"""Text the agent types is never kept, only its length (CU-08).

desktop_type said "the typed text is never logged", but the call's
arguments, with the full text, went to the event log, the saved
conversation, the audit log and the workspace's decisions ledger.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tstd.audit import AuditStore
from tstd.provider import ChatMessage, FunctionCall, ToolCall
from tstd.session_persist import chat_message_to_dict
from tstd.tools.typed_text import mask_typed_json, mask_typed_text

SECRET = "correct horse battery staple"


def test_the_typing_tools_are_masked_and_others_are_not() -> None:
    for name in ("desktop_type", "browser_type", "cu__type_text"):
        masked = mask_typed_text(name, {"text": SECRET, "settle_ms": 10})
        assert masked == {"text": "[28 characters typed]", "settle_ms": 10}
    assert mask_typed_text("fs_write", {"text": SECRET}) == {"text": SECRET}
    assert SECRET not in mask_typed_json("desktop_type", json.dumps({"text": SECRET}))


def test_the_saved_conversation_keeps_only_the_length() -> None:
    message = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(
                id="tc-1",
                function=FunctionCall(name="desktop_type", arguments=json.dumps({"text": SECRET})),
            )
        ],
    )
    row = chat_message_to_dict(message)
    assert SECRET not in json.dumps(row)
    assert "28 characters typed" in json.dumps(row)
    # The message the loop still holds, and runs, is unchanged.
    assert message.tool_calls is not None
    assert SECRET in message.tool_calls[0].function.arguments


def test_the_audit_log_keeps_only_the_length(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    store.append_session("s1", str(tmp_path), 0.0)
    store.append_tool_call(
        session_id="s1",
        tool_call_id="tc-1",
        name="desktop_type",
        arguments={"text": SECRET},
        decision_class="B",
        status="success",
    )
    rows = (
        sqlite3.connect(tmp_path / "audit.db")
        .execute("SELECT arguments FROM tool_calls")
        .fetchall()
    )
    assert rows and SECRET not in rows[0][0]
