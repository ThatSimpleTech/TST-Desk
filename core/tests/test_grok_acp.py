"""ACP client and Grok binary discovery."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from tstd.grok_acp import (
    STDOUT_LINE_LIMIT,
    AcpClient,
    GrokEngineError,
    find_grok_binary,
    persist_prompt_images,
    pick_permission_option,
    prompt_image_supported,
)

FAKE_AGENT = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def main():
    session_id = "grok-sess-1"
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": 1}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": session_id}})
        elif method == "session/load":
            sid = msg["params"]["sessionId"]
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": sid}})
        elif method == "session/prompt":
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello from grok"},
                    },
                },
            })
            send({
                "jsonrpc": "2.0",
                "method": "session/request_permission",
                "id": 9001,
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": "tc-1",
                        "title": "Read",
                        "kind": "read",
                        "rawInput": {"path": "README.md"},
                    },
                    "options": [
                        {"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                        {"optionId": "reject-once", "kind": "reject_once", "name": "Reject"},
                    ],
                },
            })
            # wait for the permission reply before finishing the prompt
            reply = json.loads(sys.stdin.readline())
            assert reply.get("id") == 9001
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Read",
                        "rawInput": {"path": "README.md"},
                        "locations": [{"path": "README.md"}],
                    }
                },
            })
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "status": "completed",
                        "rawOutput": {"lines": 12},
                    }
                },
            })
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            pass

if __name__ == "__main__":
    main()
"""


LONG_LINE_AGENT = r"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

payload = "A" * 70000
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": "s1"}})
    elif method == "session/prompt":
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {
                        "type": "image",
                        "mimeType": "image/jpeg",
                        "data": payload,
                    },
                },
            },
        })
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
"""


def _write_agent(tmp_path: Path) -> Path:
    path = tmp_path / "fake_grok_agent.py"
    path.write_text(FAKE_AGENT, encoding="utf-8")
    return path


class TestFindGrokBinary:
    def test_configured_path(self, tmp_path: Path) -> None:
        binary = tmp_path / "grok"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        assert find_grok_binary(str(binary)) == binary.resolve()

    def test_missing_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("tstd.grok_acp.shutil.which", lambda _: None)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        with pytest.raises(GrokEngineError) as exc:
            find_grok_binary("")
        assert exc.value.code == "grok_not_found"


class TestPickPermission:
    def test_allow_once(self) -> None:
        options = [
            {"optionId": "allow-once", "kind": "allow_once"},
            {"optionId": "reject-once", "kind": "reject_once"},
        ]
        chosen = pick_permission_option(options, approved=True)
        assert chosen["optionId"] == "allow-once"
        denied = pick_permission_option(options, approved=False)
        assert denied["optionId"] == "reject-once"


class TestAcpClient:
    @pytest.mark.asyncio
    async def test_prompt_and_permission(self, tmp_path: Path) -> None:
        agent = _write_agent(tmp_path)
        updates: list[dict[str, object]] = []
        permissions: list[dict[str, object]] = []

        async def on_update(params: dict[str, object]) -> None:
            updates.append(params)

        async def on_permission(params: dict[str, object]) -> dict[str, object]:
            permissions.append(params)
            return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

        client = AcpClient()
        client.on_update(on_update)
        client.on_permission(on_permission)
        await client.start([sys.executable, str(agent)], cwd=str(tmp_path), env=os.environ.copy())
        try:
            await client.initialize("tst-desk", "0.1.0")
            session_id = await client.session_new(str(tmp_path))
            assert session_id == "grok-sess-1"
            result = await asyncio.wait_for(client.prompt(session_id, "hi"), timeout=5)
            assert result.get("stopReason") == "end_turn"
            assert permissions
            kinds = [
                (u.get("update") or {}).get("sessionUpdate")  # type: ignore[union-attr]
                for u in updates
            ]
            assert "agent_message_chunk" in kinds
            assert "tool_call" in kinds
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_prompt_survives_a_70kib_stdout_line(self, tmp_path: Path) -> None:
        # Grok echoes an attached photo as one NDJSON user_message_chunk.
        # The default StreamReader limit is 64KiB; a 53KB JPEG already
        # exceeds that and used to surface as agent_exited.
        agent = tmp_path / "long_line_agent.py"
        agent.write_text(LONG_LINE_AGENT, encoding="utf-8")
        client = AcpClient()
        await client.start([sys.executable, str(agent)], cwd=str(tmp_path), env=os.environ.copy())
        try:
            assert client._proc is not None and client._proc.stdout is not None
            assert client._proc.stdout._limit >= STDOUT_LINE_LIMIT
            await client.initialize("tst-desk", "0.1.0")
            session_id = await client.session_new(str(tmp_path))
            result = await asyncio.wait_for(client.prompt(session_id, "look"), timeout=5)
            assert result.get("stopReason") == "end_turn"
        finally:
            await client.close()


class TestPromptImages:
    def test_image_capability_reads_initialize_result(self) -> None:
        assert prompt_image_supported({}) is False
        assert (
            prompt_image_supported(
                {"agentCapabilities": {"promptCapabilities": {"image": False}}}
            )
            is False
        )
        assert (
            prompt_image_supported(
                {"agentCapabilities": {"promptCapabilities": {"image": True}}}
            )
            is True
        )

    def test_persist_writes_under_workspace_attachments(self, tmp_path: Path) -> None:
        png = b"\x89PNG\r\n\x1a\n"
        saved = persist_prompt_images(str(tmp_path), "sess-1", [("shot.png", png, "image/png")])
        name, rel, media, uri = saved[0]
        assert name == "shot.png"
        assert rel == ".tst/attachments/sess-1/shot.png"
        assert media == "image/png"
        dest = tmp_path / ".tst" / "attachments" / "sess-1" / "shot.png"
        assert dest.read_bytes() == png
        assert uri == dest.resolve().as_uri()
