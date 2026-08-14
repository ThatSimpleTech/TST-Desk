"""Tests for the protocol schema — round-trip serialization for every message type."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tstd.protocol import (
    PROTOCOL_VERSION,
    ApprovalRequest,
    Approve,
    AssistantDelta,
    Attach,
    Cancel,
    CostUpdate,
    DaemonEvent,
    DecisionLogged,
    Deny,
    Detach,
    Error,
    GetInstructionStack,
    HandshakeError,
    Hello,
    OpenWorkspace,
    Ready,
    Resume,
    SessionState,
    SetTier,
    ToolCall,
    ToolResult,
    TurnComplete,
    UnknownMessageTypeError,
    UserMessage,
    build_error,
    build_hello_ack,
    parse_client_message,
    parse_daemon_event,
    parse_hello,
)


def _roundtrip(msg: Any) -> Any:
    """Serialize and parse back, then assert the type matches."""
    raw = msg.model_dump_json()
    parsed = parse_daemon_event(raw) if isinstance(msg, DaemonEvent) else parse_client_message(raw)
    assert type(parsed) is type(msg), f"Expected {type(msg).__name__}, got {type(parsed).__name__}"
    return parsed


# ── Client → Daemon ────────────────────────────────────────────────────


class TestClientMessages:
    def test_hello(self) -> None:
        msg = Hello(token="abc123", version=1)
        back = _roundtrip(msg)
        assert isinstance(back, Hello)
        assert back.token == "abc123"
        assert back.version == 1

    def test_open_workspace(self) -> None:
        msg = OpenWorkspace(path="/home/user/project")
        back = _roundtrip(msg)
        assert isinstance(back, OpenWorkspace)
        assert back.path == "/home/user/project"

    def test_user_message(self) -> None:
        msg = UserMessage(session_id="sess-1", content="hello world")
        back = _roundtrip(msg)
        assert isinstance(back, UserMessage)
        assert back.session_id == "sess-1"
        assert back.content == "hello world"

    def test_approve(self) -> None:
        msg = Approve(session_id="sess-1", tool_call_id="tc-1")
        back = _roundtrip(msg)
        assert isinstance(back, Approve)
        assert back.tool_call_id == "tc-1"

    def test_deny(self) -> None:
        msg = Deny(session_id="sess-1", tool_call_id="tc-1", reason="not safe")
        back = _roundtrip(msg)
        assert isinstance(back, Deny)
        assert back.reason == "not safe"

    def test_resume(self) -> None:
        msg = Resume(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Resume)
        assert back.session_id == "sess-1"

    def test_deny_no_reason(self) -> None:
        msg = Deny(session_id="sess-1", tool_call_id="tc-1")
        back = _roundtrip(msg)
        assert isinstance(back, Deny)
        assert back.reason is None

    def test_cancel(self) -> None:
        msg = Cancel(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Cancel)

    def test_attach(self) -> None:
        msg = Attach(session_id="sess-1", from_seq=5)
        back = _roundtrip(msg)
        assert isinstance(back, Attach)
        assert back.from_seq == 5

    def test_attach_default_seq(self) -> None:
        msg = Attach(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Attach)
        assert back.from_seq == 1

    def test_detach(self) -> None:
        msg = Detach(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, Detach)

    def test_set_tier(self) -> None:
        msg = SetTier(session_id="sess-1", tier="brain")
        back = _roundtrip(msg)
        assert isinstance(back, SetTier)
        assert back.tier == "brain"
        for t in ("brain", "worker", "validator"):
            m = SetTier(session_id="sess-1", tier=t)
            r = _roundtrip(m)
            assert isinstance(r, SetTier)
            assert r.tier == t

    def test_set_tier_invalid(self) -> None:
        with pytest.raises(ValidationError):
            SetTier(session_id="sess-1", tier="superbrain")  # type: ignore[arg-type]

    def test_get_instruction_stack(self) -> None:
        msg = GetInstructionStack(session_id="sess-1")
        back = _roundtrip(msg)
        assert isinstance(back, GetInstructionStack)


# ── Daemon → Client ────────────────────────────────────────────────────


class TestDaemonEvents:
    def test_ready(self) -> None:
        evt = Ready(version="0.1.0", protocol_version=PROTOCOL_VERSION)
        back = _roundtrip(evt)
        assert isinstance(back, Ready)
        assert back.version == "0.1.0"

    def test_session_state(self) -> None:
        evt = SessionState(session_id="sess-1", state="running", seq=2)
        back = _roundtrip(evt)
        assert isinstance(back, SessionState)
        assert back.state == "running"
        for s in ("idle", "running", "awaiting_approval", "complete", "failed", "cancelled"):
            m = SessionState(session_id="sess-1", state=s, seq=3)
            r = _roundtrip(m)
            assert isinstance(r, SessionState)
            assert r.state == s

    def test_assistant_delta(self) -> None:
        evt = AssistantDelta(session_id="sess-1", delta="Hello ", seq=1)
        back = _roundtrip(evt)
        assert isinstance(back, AssistantDelta)
        assert back.delta == "Hello "

    def test_tool_call(self) -> None:
        evt = ToolCall(
            session_id="sess-1",
            tool_call_id="tc-1",
            name="fs_read",
            arguments={"path": "/tmp/test.txt"},
            seq=3,
            decision_class="B",
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolCall)
        assert back.name == "fs_read"
        assert back.arguments == {"path": "/tmp/test.txt"}
        assert back.decision_class == "B"

    def test_tool_call_no_decision_class(self) -> None:
        evt = ToolCall(
            session_id="sess-1",
            tool_call_id="tc-1",
            name="fs_read",
            arguments={},
            seq=3,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolCall)
        assert back.decision_class is None

    def test_tool_result(self) -> None:
        evt = ToolResult(
            session_id="sess-1",
            tool_call_id="tc-1",
            status="success",
            output="file contents",
            seq=4,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolResult)
        assert back.status == "success"
        assert back.output == "file contents"
        assert back.truncated is False

    def test_tool_result_truncated(self) -> None:
        evt = ToolResult(
            session_id="sess-1",
            tool_call_id="tc-1",
            status="error",
            output="too long",
            seq=4,
            truncated=True,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ToolResult)
        assert back.truncated is True

    def test_approval_request(self) -> None:
        evt = ApprovalRequest(
            session_id="sess-1",
            tool_call_id="tc-1",
            tool_name="fs_write",
            arguments={"path": "/tmp/test.txt"},
            decision_class="C",
            summary="Write to /tmp/test.txt",
            seq=5,
        )
        back = _roundtrip(evt)
        assert isinstance(back, ApprovalRequest)
        assert back.tool_name == "fs_write"
        assert back.decision_class == "C"
        assert back.summary == "Write to /tmp/test.txt"

    def test_decision_logged(self) -> None:
        evt = DecisionLogged(
            session_id="sess-1",
            decision_class="A",
            what="Formatted file",
            why="Ruff format",
            commit="abc123",
            seq=6,
        )
        back = _roundtrip(evt)
        assert isinstance(back, DecisionLogged)
        assert back.decision_class == "A"
        assert back.commit == "abc123"

    def test_cost_update(self) -> None:
        evt = CostUpdate(
            session_id="sess-1",
            turn_cost=0.05,
            session_cost=0.50,
            total_cost=1.20,
            seq=7,
        )
        back = _roundtrip(evt)
        assert isinstance(back, CostUpdate)
        assert back.turn_cost == 0.05
        assert back.total_cost == 1.20

    def test_turn_complete(self) -> None:
        evt = TurnComplete(
            session_id="sess-1",
            tokens=1500,
            cost=0.03,
            tier="worker",
            duration=2.5,
            seq=8,
        )
        back = _roundtrip(evt)
        assert isinstance(back, TurnComplete)
        assert back.tokens == 1500
        assert back.tier == "worker"
        assert back.duration == 2.5

    def test_error(self) -> None:
        evt = Error(code="test", message="something went wrong", seq=9)
        back = _roundtrip(evt)
        assert isinstance(back, Error)
        assert back.code == "test"
        assert back.session_id is None

    def test_error_with_session(self) -> None:
        evt = Error(session_id="sess-1", code="test", message="fail", seq=10)
        assert isinstance(evt, Error)
        assert evt.session_id == "sess-1"


# ── Discriminated union dispatch ───────────────────────────────────────


class TestDiscriminatedUnion:
    def test_client_message_dispatch(self) -> None:
        cancel = parse_client_message('{"type": "cancel", "session_id": "sess-1"}')
        assert isinstance(cancel, Cancel)
        approve = parse_client_message(
            '{"type": "approve", "session_id": "sess-1", "tool_call_id": "tc-1"}',
        )
        assert isinstance(approve, Approve)

    def test_daemon_event_dispatch(self) -> None:
        ready = parse_daemon_event(
            '{"type": "ready", "version": "0.1.0", "protocol_version": 1, "seq": 1}',
        )
        assert isinstance(ready, Ready)
        error = parse_daemon_event('{"type": "error", "code": "test", "message": "x", "seq": 2}')
        assert isinstance(error, Error)

    def test_unknown_client_message_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError) as exc:
            parse_client_message('{"type": "bogus_type", "session_id": "sess-1"}')
        assert "bogus_type" in str(exc)
        assert exc.value.code == "unknown_message"

    def test_unknown_daemon_event_type(self) -> None:
        with pytest.raises(UnknownMessageTypeError) as exc:
            parse_daemon_event('{"type": "bogus_event", "seq": 1}')
        assert "bogus_event" in str(exc)


# ── Edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_client_message_missing_type(self) -> None:
        with pytest.raises(HandshakeError):
            parse_client_message('{"session_id": "sess-1"}')

    def test_client_message_not_json(self) -> None:
        with pytest.raises(HandshakeError):
            parse_client_message("not json")

    def test_daemon_event_missing_type(self) -> None:
        with pytest.raises(HandshakeError):
            parse_daemon_event('{"seq": 1}')

    def test_daemon_event_missing_seq(self) -> None:
        with pytest.raises(HandshakeError):
            parse_daemon_event(
                '{"type": "session_state", "session_id": "sess-1", "state": "running"}',
            )

    def test_build_hello_ack(self) -> None:
        msg = json.loads(build_hello_ack())
        assert msg["type"] == "hello_ack"
        assert msg["version"] == PROTOCOL_VERSION

    def test_build_error(self) -> None:
        msg = json.loads(build_error("err", "msg"))
        assert msg["code"] == "err"
        assert msg["message"] == "msg"

    def test_hello_parse_roundtrip(self) -> None:
        hello = parse_hello('{"type": "hello", "token": "x", "version": 1}')
        assert hello.token == "x"
        assert hello.version == 1
