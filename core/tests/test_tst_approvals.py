"""TTY approval cards for ``tst run`` / ``tst attach`` (TD-3103)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed

from tstd import cli
from tstd.cli_approvals import (
    CLASS_C_NOT_ALWAYS_ALLOWABLE,
    NON_TTY_COPY,
    parse_approval_answer,
    prompt_approval,
)


class _Stdin:
    def __init__(self, text: str, *, tty: bool) -> None:
        self._buf = io.StringIO(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._buf.readline()


class _Ws:
    def __init__(self, incoming: list[dict[str, Any]]) -> None:
        self._incoming = [json.dumps(event) for event in incoming]
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._incoming:
            raise ConnectionClosed(None, None)
        return self._incoming.pop(0)


_SESSION = "sess-1"

_APPROVAL_B: dict[str, Any] = {
    "type": "approval_request",
    "session_id": _SESSION,
    "tool_call_id": "tc-1",
    "tool_name": "shell",
    "arguments": {"command": "ls"},
    "decision_class": "B",
    "summary": "Run `ls`",
    "reason": "class B",
    "proposed_always_allow": {"tool": "shell", "args": "ls", "effect": "auto"},
}

_APPROVAL_C: dict[str, Any] = {
    "type": "approval_request",
    "session_id": _SESSION,
    "tool_call_id": "tc-2",
    "tool_name": "fs_write",
    "arguments": {"path": "/tmp/x"},
    "decision_class": "C",
    "summary": "Write /tmp/x",
    "reason": "class C",
    "proposed_always_allow": None,
}

_TURN_DONE: dict[str, Any] = {
    "type": "turn_complete",
    "session_id": _SESSION,
    "failed": False,
}


def _streams() -> tuple[io.StringIO, io.StringIO]:
    return io.StringIO(), io.StringIO()


class TestParseAnswer:
    def test_y_n_always_on_b(self) -> None:
        y, err = parse_approval_answer(_APPROVAL_B, _SESSION, " y \n")
        assert err is None
        assert y == {"type": "approve", "session_id": _SESSION, "tool_call_id": "tc-1"}
        n, err = parse_approval_answer(_APPROVAL_B, _SESSION, "N")
        assert err is None
        assert n == {"type": "deny", "session_id": _SESSION, "tool_call_id": "tc-1"}
        always, err = parse_approval_answer(_APPROVAL_B, _SESSION, "Always")
        assert err is None
        assert always == {
            "type": "always_allow",
            "session_id": _SESSION,
            "tool_call_id": "tc-1",
        }

    def test_always_rejected_on_c(self) -> None:
        message, err = parse_approval_answer(_APPROVAL_C, _SESSION, "always")
        assert message is None
        assert err == CLASS_C_NOT_ALWAYS_ALLOWABLE

    def test_always_rejected_when_proposal_absent(self) -> None:
        event = {**_APPROVAL_B, "proposed_always_allow": None}
        message, err = parse_approval_answer(event, _SESSION, "always")
        assert message is None
        assert err == CLASS_C_NOT_ALWAYS_ALLOWABLE

    def test_unrecognized_is_neither_send_nor_refuse(self) -> None:
        message, err = parse_approval_answer(_APPROVAL_B, _SESSION, "maybe")
        assert message is None
        assert err is None


class TestPromptApproval:
    async def test_tty_y_prints_card_and_approves(self) -> None:
        out, err = _streams()
        reply = await prompt_approval(
            _APPROVAL_B,
            _SESSION,
            stdin=_Stdin("y\n", tty=True),
            stdout=out,
            stderr=err,
        )
        assert reply == {"type": "approve", "session_id": _SESSION, "tool_call_id": "tc-1"}
        card = out.getvalue()
        assert "approval: Run `ls`" in card
        assert "tool: shell" in card
        assert "class: B" in card
        assert err.getvalue() == ""

    async def test_tty_n_denies(self) -> None:
        out, err = _streams()
        reply = await prompt_approval(
            _APPROVAL_B,
            _SESSION,
            stdin=_Stdin("n\n", tty=True),
            stdout=out,
            stderr=err,
        )
        assert reply == {"type": "deny", "session_id": _SESSION, "tool_call_id": "tc-1"}

    async def test_tty_always_on_b(self) -> None:
        out, err = _streams()
        reply = await prompt_approval(
            _APPROVAL_B,
            _SESSION,
            stdin=_Stdin("always\n", tty=True),
            stdout=out,
            stderr=err,
        )
        assert reply == {
            "type": "always_allow",
            "session_id": _SESSION,
            "tool_call_id": "tc-1",
        }

    async def test_tty_always_on_c_refuses_then_accepts_y(self) -> None:
        out, err = _streams()
        reply = await prompt_approval(
            _APPROVAL_C,
            _SESSION,
            stdin=_Stdin("always\ny\n", tty=True),
            stdout=out,
            stderr=err,
        )
        assert reply == {"type": "approve", "session_id": _SESSION, "tool_call_id": "tc-2"}
        assert CLASS_C_NOT_ALWAYS_ALLOWABLE in err.getvalue()
        assert "always_allow" not in json.dumps(reply)

    async def test_non_tty_refuses_without_reading(self) -> None:
        out, err = _streams()
        stdin = _Stdin("y\n", tty=False)
        reply = await prompt_approval(_APPROVAL_B, _SESSION, stdin=stdin, stdout=out, stderr=err)
        assert reply is None
        assert NON_TTY_COPY in err.getvalue()
        assert "window" in err.getvalue()
        assert stdin.readline() == "y\n"


class TestStreamWiring:
    async def test_run_tty_y_sends_approve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("y\n", tty=True))
        ws = _Ws([_APPROVAL_B, _TURN_DONE])
        code = await cli._stream_turn(ws, _SESSION)
        assert code == 0
        assert ws.sent == [{"type": "approve", "session_id": _SESSION, "tool_call_id": "tc-1"}]

    async def test_run_tty_always_on_b(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("always\n", tty=True))
        ws = _Ws([_APPROVAL_B, _TURN_DONE])
        code = await cli._stream_turn(ws, _SESSION)
        assert code == 0
        assert ws.sent == [{"type": "always_allow", "session_id": _SESSION, "tool_call_id": "tc-1"}]

    async def test_run_non_tty_exits_without_approve(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("y\n", tty=False))
        ws = _Ws([_APPROVAL_B, _TURN_DONE])
        code = await cli._stream_turn(ws, _SESSION)
        assert code == 1
        assert ws.sent == []
        captured = capsys.readouterr()
        assert "approval: Run `ls`" in captured.out
        assert NON_TTY_COPY in captured.err
        assert "window" in captured.err

    async def test_run_class_c_always_does_not_send_always_allow(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("always\nn\n", tty=True))
        ws = _Ws([_APPROVAL_C, _TURN_DONE])
        code = await cli._stream_turn(ws, _SESSION)
        assert code == 0
        assert ws.sent == [{"type": "deny", "session_id": _SESSION, "tool_call_id": "tc-2"}]
        assert CLASS_C_NOT_ALWAYS_ALLOWABLE in capsys.readouterr().err

    async def test_attach_tty_n_sends_deny(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("n\n", tty=True))
        ws = _Ws([_APPROVAL_B])
        code = await cli._stream_attached(ws, _SESSION)
        assert code == 0
        assert ws.sent == [{"type": "deny", "session_id": _SESSION, "tool_call_id": "tc-1"}]

    async def test_attach_non_tty_keeps_following(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli.sys, "stdin", _Stdin("y\n", tty=False))
        ws = _Ws([_APPROVAL_B])
        code = await cli._stream_attached(ws, _SESSION)
        assert code == 0
        assert ws.sent == []
        captured = capsys.readouterr()
        assert NON_TTY_COPY in captured.err
        assert "window" in captured.err
