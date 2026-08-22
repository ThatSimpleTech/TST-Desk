"""TD-3702: a remote hello can resolve a parked approval, once.

The protocol is TD-802's. These tests prove the 3602 remote hello
(injected ``is_remote_connection`` + rotating token) can approve / deny /
always-allow, that the loop unparks, and that two attached clients cannot
double-resolve (TD-1014): the second gets ``no_pending_approval`` and
does not flip the outcome.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.autonomy.classifier import DecisionClass
from tstd.config import cached_config
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, load_policy, save_policy
from tstd.protocol import PROTOCOL_VERSION
from tstd.session import Session
from tstd.tools import Tool


async def _wait_port(daemon: Daemon) -> None:
    for _ in range(50):
        if daemon.ws_server.port:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("daemon did not bind")


async def _recv_until(ws: Any, target_type: str, _timeout: float = 3.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _timeout
    while loop.time() < deadline:
        msg: dict[str, Any] = json.loads(
            await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time())
        )
        if msg.get("type") == target_type:
            return msg
    raise TimeoutError(f"no {target_type!r} message received")


async def _recv_session_state(ws: Any, wanted: str, _timeout: float = 3.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _timeout
    while loop.time() < deadline:
        msg = await _recv_until(ws, "session_state", deadline - loop.time())
        if msg.get("state") == wanted:
            return msg
    raise TimeoutError(f"no session_state {wanted!r}")


async def _hello(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _park(
    session: Session,
    tool_call_id: str,
    tool: Tool,
    arguments: dict[str, Any],
    cls: DecisionClass = DecisionClass.B,
) -> asyncio.Task[Any]:
    parked = asyncio.create_task(
        session.request_approval(
            tool_call_id,
            tool,
            arguments,
            cls,
            f"Call {tool.name}",
            f"decision class {cls.value} requires approval",
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        if session.state == "awaiting_approval":
            break
        await asyncio.sleep(0.02)
    assert session.state == "awaiting_approval"
    return parked


class _MixedRemote:
    """First hello is loopback; later hellos are remote (test injection)."""

    def __init__(self) -> None:
        self.remote_next = False
        self._classified: dict[int, bool] = {}

    def __call__(self, ws: object) -> bool:
        key = id(ws)
        if key not in self._classified:
            self._classified[key] = self.remote_next
        return self._classified[key]


async def _start_daemon(
    tmp_path: Path,
    *,
    classify: Any,
    provider: MockProvider | None = None,
) -> tuple[Daemon, asyncio.Task[None]]:
    daemon = Daemon(data_dir=tmp_path / "data", provider=provider)
    daemon.ws_server._is_remote_connection = classify
    task = asyncio.create_task(daemon.run())
    await _wait_port(daemon)
    assert daemon.ws_server.remote_token
    return daemon, task


async def _stop(daemon: Daemon, task: asyncio.Task[None]) -> None:
    daemon._shutdown_event.set()
    await asyncio.wait_for(task, timeout=3)


async def _open_workspace(ws: Any, workspace: Path) -> str:
    await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
    opened = await _recv_until(ws, "session_state")
    return str(opened["session_id"])


async def _attach(ws: Any, session_id: str) -> None:
    await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))


# ── Remote resolve (criterion 1) ────────────────────────────────────────


class TestRemoteResolve:
    @pytest.mark.asyncio
    async def test_remote_approve_unparks(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path, classify=lambda _ws: True)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _hello(uri, daemon.ws_server.remote_token)
            session_id = await _open_workspace(ws, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(ws, session_id)
            req = await _recv_until(ws, "approval_request")
            assert req["tool_call_id"] == "tc-1"

            await ws.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "tc-1"})
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved
            assert session.state == "running"
            state = await _recv_session_state(ws, "running")
            assert state["state"] == "running"
            await ws.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_remote_deny_unparks(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path, classify=lambda _ws: True)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _hello(uri, daemon.ws_server.remote_token)
            session_id = await _open_workspace(ws, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(ws, session_id)
            await _recv_until(ws, "approval_request")

            await ws.send(
                json.dumps(
                    {
                        "type": "deny",
                        "session_id": session_id,
                        "tool_call_id": "tc-1",
                        "reason": "not from the phone",
                    }
                )
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert not outcome.approved
            assert "not from the phone" in outcome.message
            assert session.state == "running"
            await ws.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_remote_always_allow_unparks_and_writes_rule(self, tmp_path: Path) -> None:
        daemon, task = await _start_daemon(tmp_path, classify=lambda _ws: True)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _hello(uri, daemon.ws_server.remote_token)
            session_id = await _open_workspace(ws, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(ws, session_id)
            await _recv_until(ws, "approval_request")

            await ws.send(
                json.dumps(
                    {"type": "always_allow", "session_id": session_id, "tool_call_id": "tc-1"}
                )
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved
            assert session.state == "running"
            assert load_policy(tmp_path).rules == [
                PolicyRule(tool="shell", args="npm test", effect="auto")
            ]
            await ws.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_remote_approve_unparks_the_agent_loop(self, tmp_path: Path) -> None:
        save_policy(
            tmp_path,
            PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")]),
        )
        target = tmp_path / "hello.txt"
        slug = cached_config().tier("brain").require_slug()
        mock = MockProvider(
            sequences={
                slug: [
                    Script(
                        kind="tool_call",
                        tool_name="fs_write",
                        tool_arguments=json.dumps(
                            {"path": str(target), "content": "from the phone\n"}
                        ),
                    ),
                    Script(kind="stream", content="wrote it"),
                ]
            }
        )
        daemon, task = await _start_daemon(tmp_path, classify=lambda _ws: True, provider=mock)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _hello(uri, daemon.ws_server.remote_token)
            session_id = await _open_workspace(ws, tmp_path)
            await _attach(ws, session_id)
            await ws.send(
                json.dumps(
                    {
                        "type": "user_message",
                        "session_id": session_id,
                        "content": "write hello.txt",
                    }
                )
            )
            req = await _recv_until(ws, "approval_request")
            assert req["tool_name"] == "fs_write"
            await ws.send(
                json.dumps(
                    {
                        "type": "approve",
                        "session_id": session_id,
                        "tool_call_id": req["tool_call_id"],
                    }
                )
            )
            result = await _recv_until(ws, "tool_result")
            assert result["status"] == "success"
            await _recv_until(ws, "turn_complete")
            assert target.read_text() == "from the phone\n"
            session = daemon.session_registry.get(session_id)
            assert session is not None
            assert session.state == "running"
            await ws.close()
        finally:
            await _stop(daemon, task)


# ── Double-resolve (criterion 2) ────────────────────────────────────────


class TestDoubleResolve:
    @pytest.mark.asyncio
    async def test_second_client_cannot_flip_an_approve(self, tmp_path: Path) -> None:
        classify = _MixedRemote()
        daemon, task = await _start_daemon(tmp_path, classify=classify)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            classify.remote_next = False
            local = await _hello(uri, daemon.ws_server.token)
            session_id = await _open_workspace(local, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(local, session_id)

            classify.remote_next = True
            remote = await _hello(uri, daemon.ws_server.remote_token)
            await _attach(remote, session_id)

            local_req = await _recv_until(local, "approval_request")
            remote_req = await _recv_until(remote, "approval_request")
            assert local_req["tool_call_id"] == remote_req["tool_call_id"] == "tc-1"

            await remote.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "tc-1"})
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved
            local_state = await _recv_session_state(local, "running")
            remote_state = await _recv_session_state(remote, "running")
            assert local_state["state"] == remote_state["state"] == "running"

            await local.send(
                json.dumps(
                    {
                        "type": "deny",
                        "session_id": session_id,
                        "tool_call_id": "tc-1",
                        "reason": "too late",
                    }
                )
            )
            err = await _recv_until(local, "error")
            assert err["code"] == "no_pending_approval"
            assert outcome.approved
            await local.close()
            await remote.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_second_client_cannot_flip_a_deny(self, tmp_path: Path) -> None:
        classify = _MixedRemote()
        daemon, task = await _start_daemon(tmp_path, classify=classify)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            classify.remote_next = False
            local = await _hello(uri, daemon.ws_server.token)
            session_id = await _open_workspace(local, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(local, session_id)

            classify.remote_next = True
            remote = await _hello(uri, daemon.ws_server.remote_token)
            await _attach(remote, session_id)
            await _recv_until(local, "approval_request")
            await _recv_until(remote, "approval_request")

            await local.send(
                json.dumps(
                    {
                        "type": "deny",
                        "session_id": session_id,
                        "tool_call_id": "tc-1",
                        "reason": "local said no",
                    }
                )
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert not outcome.approved

            await remote.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "tc-1"})
            )
            err = await _recv_until(remote, "error")
            assert err["code"] == "no_pending_approval"
            assert not outcome.approved
            await local.close()
            await remote.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_always_allow_after_resolve_writes_nothing(self, tmp_path: Path) -> None:
        classify = _MixedRemote()
        daemon, task = await _start_daemon(tmp_path, classify=classify)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            classify.remote_next = False
            local = await _hello(uri, daemon.ws_server.token)
            session_id = await _open_workspace(local, tmp_path)
            session = daemon.session_registry.get(session_id)
            assert session is not None

            parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "npm test"})
            await _attach(local, session_id)

            classify.remote_next = True
            remote = await _hello(uri, daemon.ws_server.remote_token)
            await _attach(remote, session_id)
            await _recv_until(local, "approval_request")
            await _recv_until(remote, "approval_request")

            await local.send(
                json.dumps({"type": "approve", "session_id": session_id, "tool_call_id": "tc-1"})
            )
            outcome = await asyncio.wait_for(parked, timeout=2)
            assert outcome.approved

            await remote.send(
                json.dumps(
                    {"type": "always_allow", "session_id": session_id, "tool_call_id": "tc-1"}
                )
            )
            err = await _recv_until(remote, "error")
            assert err["code"] == "no_pending_approval"
            assert load_policy(tmp_path).rules == []
            await local.close()
            await remote.close()
        finally:
            await _stop(daemon, task)

    @pytest.mark.asyncio
    async def test_both_clients_see_resolving_tool_result(self, tmp_path: Path) -> None:
        save_policy(
            tmp_path,
            PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")]),
        )
        target = tmp_path / "hello.txt"
        slug = cached_config().tier("brain").require_slug()
        mock = MockProvider(
            sequences={
                slug: [
                    Script(
                        kind="tool_call",
                        tool_name="fs_write",
                        tool_arguments=json.dumps({"path": str(target), "content": "once\n"}),
                    ),
                    Script(kind="stream", content="wrote it"),
                ]
            }
        )
        classify = _MixedRemote()
        daemon, task = await _start_daemon(tmp_path, classify=classify, provider=mock)
        try:
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            classify.remote_next = False
            local = await _hello(uri, daemon.ws_server.token)
            session_id = await _open_workspace(local, tmp_path)
            await _attach(local, session_id)

            classify.remote_next = True
            remote = await _hello(uri, daemon.ws_server.remote_token)
            await _attach(remote, session_id)

            await local.send(
                json.dumps(
                    {
                        "type": "user_message",
                        "session_id": session_id,
                        "content": "write hello.txt",
                    }
                )
            )
            local_req = await _recv_until(local, "approval_request")
            remote_req = await _recv_until(remote, "approval_request")
            assert local_req["tool_call_id"] == remote_req["tool_call_id"]

            await remote.send(
                json.dumps(
                    {
                        "type": "approve",
                        "session_id": session_id,
                        "tool_call_id": remote_req["tool_call_id"],
                    }
                )
            )
            local_result = await _recv_until(local, "tool_result")
            remote_result = await _recv_until(remote, "tool_result")
            assert local_result["status"] == remote_result["status"] == "success"
            assert local_result["tool_call_id"] == remote_result["tool_call_id"]

            await local.send(
                json.dumps(
                    {
                        "type": "deny",
                        "session_id": session_id,
                        "tool_call_id": local_req["tool_call_id"],
                        "reason": "ghost click",
                    }
                )
            )
            err = await _recv_until(local, "error")
            assert err["code"] == "no_pending_approval"
            assert target.read_text() == "once\n"
            await local.close()
            await remote.close()
        finally:
            await _stop(daemon, task)


class TestPendingLooksGone:
    @pytest.mark.asyncio
    async def test_resolved_future_is_not_pending(self, tmp_path: Path) -> None:
        """The waiter has not popped yet; get_pending_approval still says gone."""
        session = Session(str(tmp_path))
        await session.set_state("running")
        parked = await _park(session, "tc-1", Tool(name="shell"), {"command": "ls"})
        assert session.get_pending_approval("tc-1") is not None
        assert session.resolve_approval("tc-1", True)
        assert session.get_pending_approval("tc-1") is None
        await asyncio.wait_for(parked, timeout=2)
