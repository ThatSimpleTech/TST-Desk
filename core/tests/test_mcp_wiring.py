"""Daemon-level tests for MCP server loading over the wire (TD-4401).

Config comes from an isolated HOME so these never touch a real user's
``mcp.servers``; servers are the in-repo fake stdio MCP server.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.client import connect

from tstd.config import cached_config, default_config_yaml
from tstd.daemon import Daemon
from tstd.logging import user_data_dir
from tstd.protocol import PROTOCOL_VERSION

from .fake_mcp_server import spec as fake_spec

ECHO_TOOLS = [
    {
        "name": "echo",
        "description": "Echo the arguments back.",
        "inputSchema": {"type": "object", "properties": {}},
    }
]


async def _connect_and_handshake(uri: str, token: str) -> Any:
    ws = await connect(uri)
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack"
    return ws


async def _start_daemon(tmp: str) -> tuple[Daemon, asyncio.Task[Any]]:
    daemon = Daemon(data_dir=Path(tmp))
    task = asyncio.create_task(daemon.run())
    for _ in range(50):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port > 0
    return daemon, task


def _write_user_config(home: Path, mcp_servers: dict[str, Any]) -> None:
    """Plant a user config whose presets are the shipped defaults."""
    config_dir = user_data_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(default_config_yaml())
    data["mcp"] = {"servers": mcp_servers}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture()
def isolated_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)  # type: ignore[method-assign]
    cached_config.cache_clear()
    yield home
    cached_config.cache_clear()


class TestMcpWiring:
    @pytest.mark.asyncio
    async def test_open_replays_mcp_state_and_registers_tools(
        self, isolated_config_home: Path, tmp_path: Path
    ) -> None:
        with tempfile.TemporaryDirectory() as daemon_tmp:
            _write_user_config(
                isolated_config_home,
                {"git": {"command": fake_spec(ECHO_TOOLS)}},
            )
            workspace = tmp_path / "ws"
            workspace.mkdir()

            daemon, daemon_task = await _start_daemon(daemon_tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
            session_state = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            session_id = session_state["session_id"]

            # Replay from the top: session_state(1), boundary(2),
            # tier_state(3), mcp_state(4).
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            seen = [json.loads(await asyncio.wait_for(ws.recv(), timeout=2))]
            while seen[-1].get("type") != "mcp_state":
                seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
                if len(seen) > 6:
                    break
            assert [e.get("type") for e in seen] == [
                "session_state",
                "boundary_update",
                "tier_state",
                "mcp_state",
            ]
            mcp_state = seen[-1]
            assert mcp_state["session_id"] == session_id
            assert mcp_state["servers"] == [
                {
                    "name": "git",
                    "transport": "stdio",
                    "status": "ready",
                    "detail": "",
                    "tool_count": 1,
                }
            ]

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_dead_server_does_not_block_the_session(
        self, isolated_config_home: Path, tmp_path: Path
    ) -> None:
        with tempfile.TemporaryDirectory() as daemon_tmp:
            _write_user_config(
                isolated_config_home,
                {
                    "git": {"command": fake_spec(ECHO_TOOLS)},
                    "dead": {"command": [sys.executable, "-c", "raise SystemExit(1)"]},
                },
            )
            workspace = tmp_path / "ws"
            workspace.mkdir()

            daemon, daemon_task = await _start_daemon(daemon_tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
            session_state = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            session_id = session_state["session_id"]
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))

            mcp_state: dict[str, Any] | None = None
            for _ in range(6):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                if event.get("type") == "mcp_state":
                    mcp_state = event
                    break
            assert mcp_state is not None, "open must still emit mcp_state"
            by_name = {s["name"]: s for s in mcp_state["servers"]}
            assert by_name["git"]["status"] == "ready"
            assert by_name["dead"]["status"] == "failed"
            assert by_name["dead"]["detail"]

            # The same failure is a doctor row with a fix, not a crash.
            await ws.send(json.dumps({"type": "run_diagnostics"}))
            report = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert report["type"] == "diagnostics_report"
            row = next(c for c in report["checks"] if c["name"] == "mcp")
            assert row["status"] == "fail"
            assert "dead" in row["detail"]
            assert row["fix"]

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_no_servers_configured_emits_no_mcp_state(
        self, isolated_config_home: Path, tmp_path: Path
    ) -> None:
        with tempfile.TemporaryDirectory() as daemon_tmp:
            workspace = tmp_path / "ws"
            workspace.mkdir()

            daemon, daemon_task = await _start_daemon(daemon_tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
            session_state = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            session_id = session_state["session_id"]

            # The unconfigured open sequence stays at seqs 1-3; attaching
            # past it streams nothing until we act.
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 4}))
            await ws.send(json.dumps({"type": "run_diagnostics"}))
            report = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert report["type"] == "diagnostics_report"  # first thing back, no mcp_state
            row = next(c for c in report["checks"] if c["name"] == "mcp")
            assert row["status"] == "skip"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_settings_messages_edit_servers_without_yaml(
        self, isolated_config_home: Path, tmp_path: Path
    ) -> None:
        """Add over the wire, see it in setup_state and in the next open's
        mcp_state, disable, remove (TD-4403)."""
        with tempfile.TemporaryDirectory() as daemon_tmp:
            workspace = tmp_path / "ws"
            workspace.mkdir()

            daemon, daemon_task = await _start_daemon(daemon_tmp)
            uri = f"ws://127.0.0.1:{daemon.ws_server.port}"
            ws = await _connect_and_handshake(uri, daemon.ws_server.token)

            # Add: the ack carries the configured entry, argv joined for display.
            command = fake_spec(ECHO_TOOLS)
            await ws.send(json.dumps({"type": "set_mcp_server", "name": "git", "command": command}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert ack["type"] == "setup_state"
            assert [s["name"] for s in ack["mcp_servers"]] == ["git"]
            assert ack["mcp_servers"][0]["transport"] == "stdio"
            assert ack["mcp_servers"][0]["enabled"] is True
            assert "-c" in ack["mcp_servers"][0]["destination"]

            # A session opened now sees the server and its tools: the open
            # logs mcp_state, and attach replays it with the rest.
            await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
            session_state = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            session_id = session_state["session_id"]
            await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
            mcp_state = None
            for _ in range(6):
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if event.get("type") == "mcp_state":
                    mcp_state = event
                    break
            assert mcp_state is not None
            assert mcp_state["servers"][0]["name"] == "git"
            assert mcp_state["servers"][0]["status"] == "ready"
            assert mcp_state["servers"][0]["tool_count"] == len(ECHO_TOOLS)

            # Disable: still listed, marked disabled.
            await ws.send(json.dumps({"type": "set_mcp_enabled", "name": "git", "enabled": False}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert ack["mcp_servers"][0]["enabled"] is False

            # Remove: gone from the list.
            await ws.send(json.dumps({"type": "remove_mcp_server", "name": "git"}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert ack["mcp_servers"] == []

            # Unknown names are typed errors, never crashes.
            await ws.send(json.dumps({"type": "set_mcp_enabled", "name": "nope", "enabled": True}))
            err = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert err["type"] == "error"

            await ws.close()
            daemon._shutdown_event.set()
            await asyncio.gather(daemon_task, return_exceptions=True)
