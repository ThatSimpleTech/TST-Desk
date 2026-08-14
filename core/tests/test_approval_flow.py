"""Tests for the approval flow (TD-802).

Covers: the approval_request payload (tool, arguments, decision class,
summary, reason); parking without spin or poll; approve resuming execution;
denial returning a structured message the model can route around;
configurable timeout defaulting to indefinite; client disconnect leaving
the session parked and resumable; and policy `never` refusing without an
approval round-trip.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tests.test_dispatch import make_classifier, make_config, start_loop, wait_for_turn
from tstd.autonomy.classifier import DecisionClass
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule
from tstd.protocol import PROTOCOL_VERSION, AssistantDelta
from tstd.protocol import ApprovalRequest as ApprovalRequestEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry
from tstd.tools.dispatch import UnclassifiedToolCall


def make_echo() -> tuple[ToolRegistry, ToolDispatcher]:
    """An echo tool with a bare dispatcher — the loop wires classifier,
    guard, policy, and the session's approval handler (production path)."""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Echo arguments back",
            parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        )
    )

    async def echo_handler(session: object, message: str, tool_call_id: str = "") -> str:
        return f"Echo: {message}"

    dispatcher = ToolDispatcher(registry)
    dispatcher.register_handler("echo", echo_handler)
    return registry, dispatcher


def ask_policy(**kwargs: Any) -> PolicyConfig:
    """A policy whose only rule asks for every echo call."""
    return PolicyConfig(rules=[PolicyRule(tool="echo", args="**", effect="ask")], **kwargs)


def tool_call_then(reply: str) -> dict[str, list[Script]]:
    """Two-script sequence: echo tool call, then a streamed text reply."""
    return {
        "test-brain": [
            Script(kind="tool_call", tool_name="echo", tool_arguments='{"message": "hello"}'),
            Script(kind="stream", content=reply),
        ]
    }


async def wait_for_approval(session: Session, _timeout: float = 3.0) -> ApprovalRequestEvent:
    """Wait until the session emits an approval_request event."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _timeout
    while loop.time() < deadline:
        for event in session.event_log.all_events:
            if isinstance(event, ApprovalRequestEvent):
                return event
        await asyncio.sleep(0.02)
    raise TimeoutError("no approval_request emitted")


def tool_results(session: Session) -> list[ToolResultEvent]:
    return [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]


# ── Payload (criterion 1) ───────────────────────────────────────────────


class TestPayload:
    async def test_approval_request_carries_full_payload(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy()
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("done"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        req = await wait_for_approval(session)

        assert req.tool_call_id
        assert req.tool_name == "echo"
        assert req.arguments == {"message": "hello"}
        assert req.decision_class in ("A", "B", "C")
        assert req.summary  # human-readable
        assert req.reason == "policy rule `echo: **` → ask"
        assert session.state == "awaiting_approval"

        # Resolve so the runner unwinds cleanly.
        assert session.resolve_approval(req.tool_call_id, True)
        await wait_for_turn(session, 1)


# ── Park without spin or poll (criterion 2) ─────────────────────────────


class TestPark:
    async def test_parked_session_is_quiescent(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy()
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("done"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        req = await wait_for_approval(session)

        # Parked: no provider calls, no new events, no state change.
        calls_at_park = len(mock.calls)
        seq_at_park = session.event_log.last_seq
        await asyncio.sleep(0.3)
        assert len(mock.calls) == calls_at_park
        assert session.event_log.last_seq == seq_at_park
        assert session.state == "awaiting_approval"

        assert session.resolve_approval(req.tool_call_id, True)
        await wait_for_turn(session, 1)


# ── Approve / deny (criterion 3) ────────────────────────────────────────


class TestApproveDeny:
    async def test_approve_runs_the_tool(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy()
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("done"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        req = await wait_for_approval(session)
        assert session.resolve_approval(req.tool_call_id, True)
        await wait_for_turn(session, 1)

        results = tool_results(session)
        assert results[0].status == "success"
        assert results[0].output == "Echo: hello"
        assert results[0].error_code is None  # a successful run carries no code
        assert session.state == "running"

    async def test_deny_returns_structured_message_to_model(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy()
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("took another path"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        req = await wait_for_approval(session)
        assert session.resolve_approval(req.tool_call_id, False, "not safe")
        await wait_for_turn(session, 1)

        # The tool never ran; the denial went back as the tool result.
        results = tool_results(session)
        assert results[0].status == "error"
        assert results[0].output == "Denied by user: not safe"
        # A denial is distinguishable from a generic error on the wire (TD-1007).
        assert results[0].error_code == "approval_denied"

        # The model observed the structured denial and chose another path:
        # the follow-up turn carries it as the tool-role message (calls also
        # include the TD-703 classifier worker call, so search them all).
        tool_messages = [m for c in mock.calls for m in c.messages if m.role == "tool"]
        assert any("Denied by user: not safe" in (m.content or "") for m in tool_messages)
        deltas = [e for e in session.event_log.all_events if isinstance(e, AssistantDelta)]
        assert "took another path" in "".join(d.delta for d in deltas)  # mock streams per-word


# ── Timeout (criterion 4) ───────────────────────────────────────────────


class TestTimeout:
    async def test_default_waits_indefinitely(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy()  # approval_timeout_seconds=None
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("done"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        req = await wait_for_approval(session)

        await asyncio.sleep(0.3)
        assert session.state == "awaiting_approval"  # no timeout fired

        assert session.resolve_approval(req.tool_call_id, True)
        await wait_for_turn(session, 1)

    async def test_configured_timeout_denies(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = ask_policy(approval_timeout_seconds=0.2)
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("gave up gracefully"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        await wait_for_approval(session)
        await wait_for_turn(session, 1)  # timeout resolves the turn itself

        results = tool_results(session)
        assert results[0].status == "error"
        assert "Approval timed out after 0.2s — treated as denial" in results[0].output
        # A late resolution finds nothing pending.
        assert not session.resolve_approval(results[0].tool_call_id, True)


# ── Disconnect / resume (criterion 5) ───────────────────────────────────


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


class TestDisconnectResume:
    @pytest.mark.asyncio
    async def test_disconnect_leaves_session_parked_and_resumable(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        daemon_task = asyncio.create_task(daemon.run())
        for _ in range(50):
            if daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)

        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)
            await ws.send(json.dumps({"type": "open_workspace", "path": str(tmp_path)}))
            opened = json.loads(await ws.recv())
            session_id = opened["session_id"]
            session = daemon.session_registry.get(session_id)
            assert session is not None

            # Park an approval directly (daemon-opened loops carry no tools
            # at this commit; the session API is the parking surface).
            tool = Tool(name="fs_write", path_fields=("path",), mutates=True)
            parked = asyncio.create_task(
                session.request_approval(
                    "tc-9",
                    tool,
                    {"path": "src/app.py"},
                    DecisionClass.B,
                    "Write src/app.py",
                    "decision class B requires approval",
                )
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while loop.time() < deadline:
                if session.state == "awaiting_approval":
                    break
                await asyncio.sleep(0.02)
            assert session.state == "awaiting_approval"

            # Client disconnects mid-approval: the session stays parked.
            await ws.close()
            await asyncio.sleep(0.1)
            assert session.state == "awaiting_approval"
            assert not parked.done()

            # A new client attaches: replay shows the parked state and the
            # outstanding request.
            ws2 = await _connect_and_handshake(uri, daemon.ws_server.token)
            await ws2.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            replayed = [json.loads(await asyncio.wait_for(ws2.recv(), timeout=2)) for _ in range(4)]
            types = [e["type"] for e in replayed]
            assert types == [
                "session_state",
                "boundary_update",
                "approval_request",
                "session_state",
            ]
            assert replayed[2]["tool_call_id"] == "tc-9"
            assert replayed[3]["state"] == "awaiting_approval"

            # The reattached client approves; the parked call resumes.
            await ws2.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "tc-9"})
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved
            assert session.state == "running"

            # Unknown approval ids get a typed error, not a crash.  (The
            # attached socket may first stream the running-state event from
            # the resolution above — read until the error arrives.)
            await ws2.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "bogus"})
            )
            err: dict[str, Any] = {}
            for _ in range(3):
                msg = json.loads(await asyncio.wait_for(ws2.recv(), timeout=2))
                if msg.get("type") == "error":
                    err = msg
                    break
            assert err.get("code") == "no_pending_approval"
            await ws2.close()
        finally:
            daemon._shutdown_event.set()
            await asyncio.wait_for(daemon_task, timeout=3)


# ── Never refuses without a round-trip ──────────────────────────────────


class TestNever:
    async def test_never_rule_refuses_without_approval(self, tmp_path: Path) -> None:
        session = Session(str(tmp_path))
        session.policy = PolicyConfig(rules=[PolicyRule(tool="echo", args="**", effect="never")])
        registry, dispatcher = make_echo()
        mock = MockProvider(sequences=tool_call_then("understood, skipping"))
        await start_loop(session, TierRouter(), mock, make_config(), registry, dispatcher)

        await session.add_user_message("echo hello")
        await wait_for_turn(session, 1)

        # No approval was ever requested; the refusal is structured.
        assert not any(isinstance(e, ApprovalRequestEvent) for e in session.event_log.all_events)
        results = tool_results(session)
        assert results[0].status == "error"
        assert results[0].output.startswith("Refused by policy: policy rule `echo: **` → never")
        assert results[0].error_code == "policy_denied"
        assert session.state == "running"

    async def test_ask_without_handler_is_a_chokepoint_bypass(self, tmp_path: Path) -> None:
        """A call resolving to ask with no approval handler raises — the
        gate is part of the chokepoint, not an optional add-on."""
        _registry, dispatcher = make_echo()
        dispatcher.policy = ask_policy()
        dispatcher.classifier = make_classifier(str(tmp_path))
        # No approval_handler attached: the ask must raise, not execute.
        with pytest.raises(UnclassifiedToolCall, match="no approval handler"):
            await dispatcher.dispatch("tc-1", "echo", {"message": "hi"})
