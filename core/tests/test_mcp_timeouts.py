"""MCP timeouts per server, progress and cancellation (TD-5105).

A scripted stdio server stands in for a long TOWER tool.  The timeouts are
scaled down (a 0.3 s idle timeout instead of 30 s) so the suite stays fast;
the claim is the same: a tool that keeps reporting progress outlives the
idle timeout, silence ends a call, the hard cap ends one that only reports
progress, and every ended call tells the server with
``notifications/cancelled``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tstd.config import McpConfig, McpServerConfig, cached_config, ensure_user_config, load_config
from tstd.config_write import save_mcp_server
from tstd.daemon import Daemon
from tstd.mcp.errors import McpError
from tstd.mcp.http import HttpMcpClient
from tstd.mcp.loader import PROGRESS_EVENT_INTERVAL, McpSupervisor, _handler
from tstd.mcp.stdio import StdioMcpClient
from tstd.protocol import ToolProgress
from tstd.tools import ToolDispatcher, create_registry

_SERVER = r"""
import json
import sys
import time

LOG = sys.argv[1]
# Seconds to wait before answering initialize, like a server loading a model.
START_DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "notifications/cancelled":
        with open(LOG, "a") as f:
            f.write(json.dumps(msg["params"]) + "\n")
        continue
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        time.sleep(START_DELAY)
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "slow", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
            {"name": "slow", "inputSchema": {"type": "object"}},
            {"name": "silent", "inputSchema": {"type": "object"}},
            {"name": "echo", "inputSchema": {"type": "object"}}]}})
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        token = msg["params"].get("_meta", {}).get("progressToken")
        if name == "silent":
            continue  # never answers
        if name == "slow":
            steps, every = args.get("steps", 10), args.get("every", 0.1)
            for i in range(1, steps + 1):
                time.sleep(every)
                # Progress for another token must not keep this call alive.
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": "someone-else", "progress": i}})
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": i, "total": steps,
                                 "message": f"step {i}"}})
        send({"jsonrpc": "2.0", "id": msg["id"],
              "result": {"content": [{"type": "text", "text": f"{name} done"}]}})
"""


@pytest.fixture
def server(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    log = tmp_path / "cancelled.jsonl"
    return [sys.executable, str(script), str(log)], log


def _read_notices(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines() if x]


async def _cancel_notices(log: Path, *, want: int = 1) -> list[dict[str, Any]]:
    """The cancel notices the server logged, waiting up to 5 s for them.

    The scripted server is single-threaded, so it reads the notice only
    after the step it is in (or its whole progress loop) finishes.
    """
    for _ in range(250):
        notices = _read_notices(log)
        if len(notices) >= want:
            return notices
        await asyncio.sleep(0.02)
    return _read_notices(log)


def _text(result: Any) -> str:
    return str(result["content"][0]["text"])


# ── stdio client ─────────────────────────────────────────────────────────


class TestStdioProgressAndTimeouts:
    async def test_progress_keeps_a_slow_call_alive(self, server: tuple[list[str], Path]) -> None:
        command, log = server
        client = StdioMcpClient(command, timeout=0.3, max_call_seconds=10.0)
        seen: list[tuple[float, float | None, str | None]] = []

        async def on_progress(progress: float, total: float | None, message: str | None) -> None:
            seen.append((progress, total, message))

        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            # Ten steps of 0.1 s: about 1 s in all, over three idle timeouts.
            result = await client.call_tool(
                "slow", {"steps": 10, "every": 0.1}, on_progress=on_progress
            )
            elapsed = loop.time() - started
        finally:
            await client.aclose()
        assert _text(result) == "slow done"
        assert elapsed > 0.9
        assert [p for p, _, _ in seen] == [float(i) for i in range(1, 11)]
        assert seen[-1] == (10.0, 10.0, "step 10")
        assert not log.exists()

    async def test_silence_times_out_and_sends_cancel(self, server: tuple[list[str], Path]) -> None:
        command, log = server
        client = StdioMcpClient(command, timeout=0.3, max_call_seconds=10.0)
        try:
            with pytest.raises(McpError, match=r"no result or progress for 0.3 s"):
                await client.call_tool("silent")
            (notice,) = await _cancel_notices(log)
            # The client still works after a timed-out call.
            assert _text(await client.call_tool("echo")) == "echo done"
        finally:
            await client.aclose()
        assert isinstance(notice["requestId"], int)
        assert "timeout_seconds" in notice["reason"]

    async def test_hard_cap_ends_a_call_that_only_reports_progress(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, log = server
        client = StdioMcpClient(command, timeout=0.3, max_call_seconds=0.6)
        try:
            with pytest.raises(McpError, match=r"no result after 0.6 s \(max_call_seconds\)"):
                await client.call_tool("slow", {"steps": 30, "every": 0.05})
            (notice,) = await _cancel_notices(log)
        finally:
            await client.aclose()
        assert "max_call_seconds" in notice["reason"]

    async def test_stopping_the_turn_sends_cancel(self, server: tuple[list[str], Path]) -> None:
        command, log = server
        client = StdioMcpClient(command, timeout=30.0)
        try:
            task = asyncio.create_task(client.call_tool("silent"))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            (notice,) = await _cancel_notices(log)
        finally:
            await client.aclose()
        assert notice["reason"] == "the user stopped the turn"

    def test_hard_cap_is_never_below_the_idle_timeout(self) -> None:
        client = StdioMcpClient(["true"], timeout=60.0, max_call_seconds=5.0)
        assert client._max_call == 60.0


# ── supervisor: per-server keys and progress events ──────────────────────


class _EventLog:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def add(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _Session:
    def __init__(self) -> None:
        self.id = "sess-1"
        self.event_log = _EventLog()
        self.stop = asyncio.Event()

    async def wait_for_turn_abort(self) -> None:
        await self.stop.wait()


class TestSupervisor:
    async def test_per_server_timeout_overrides_the_default(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, log = server
        config = McpConfig(
            servers={
                "srv": McpServerConfig(
                    transport="stdio",
                    command=command,
                    timeout_seconds=0.3,
                    startup_timeout_seconds=10.0,
                )
            }
        )
        supervisor = McpSupervisor(config, handshake_timeout=0.001, call_timeout=30.0)
        registry = create_registry()
        dispatcher = ToolDispatcher(registry)
        try:
            # A 1 ms default handshake would fail; the server's own key wins.
            (row,) = await supervisor.ensure_loaded()
            assert row.status == "ok", row.detail
            supervisor.attach(registry, dispatcher)
            handler = dispatcher._handlers["srv__silent"]
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(McpError, match="timeout_seconds"):
                await handler(session=None, tool_call_id="tc-1")
            assert loop.time() - started < 5.0
            assert await _cancel_notices(log)
        finally:
            await supervisor.aclose()

    async def test_startup_timeout_can_outlast_the_call_timeout(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, _log = server
        slow_start = [*command, "1.0"]  # initialize answers after 1 s
        config = McpConfig(
            servers={
                "srv": McpServerConfig(
                    transport="stdio",
                    command=slow_start,
                    timeout_seconds=0.3,
                    startup_timeout_seconds=10.0,
                )
            }
        )
        supervisor = McpSupervisor(config)
        try:
            (row,) = await supervisor.ensure_loaded()
        finally:
            await supervisor.aclose()
        assert row.status == "ok", row.detail

    async def test_startup_timeout_bounds_the_handshake(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, _log = server
        slow_start = [*command, "5.0"]
        config = McpConfig(
            servers={
                "srv": McpServerConfig(
                    transport="stdio",
                    command=slow_start,
                    timeout_seconds=30.0,
                    startup_timeout_seconds=0.3,
                )
            }
        )
        supervisor = McpSupervisor(config)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            (row,) = await supervisor.ensure_loaded()
        finally:
            await supervisor.aclose()
        assert row.status == "fail"
        assert loop.time() - started < 4.0

    async def test_progress_becomes_thinned_tool_progress_events(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, _log = server
        client = StdioMcpClient(command, timeout=0.3, max_call_seconds=10.0)
        session = _Session()
        try:
            out = await _handler(client, "slow")(
                session=session, tool_call_id="tc-7", steps=12, every=0.1
            )
        finally:
            await client.aclose()
        assert "slow done" in out
        events = session.event_log.events
        assert all(isinstance(e, ToolProgress) for e in events)
        assert all(e.session_id == "sess-1" and e.tool_call_id == "tc-7" for e in events)
        # 12 notifications over ~1.2 s, at most one event per interval.
        assert 1 <= len(events) <= int(1.2 / PROGRESS_EVENT_INTERVAL) + 2
        assert events[0].progress == 1.0 and events[0].total == 12.0
        assert events[0].message == "step 1"

    async def test_stopping_the_turn_ends_the_call_and_tells_the_server(
        self, server: tuple[list[str], Path]
    ) -> None:
        command, log = server
        client = StdioMcpClient(command, timeout=30.0)
        session = _Session()
        try:
            call = asyncio.create_task(
                _handler(client, "silent")(session=session, tool_call_id="t")
            )
            await asyncio.sleep(0.3)
            assert not call.done()
            session.stop.set()  # what Session.stop_turn() does for the waiter
            with pytest.raises(McpError, match="the user stopped the turn"):
                await asyncio.wait_for(call, timeout=5.0)
            (notice,) = await _cancel_notices(log)
            # The server and client stay usable for the next turn.
            assert _text(await client.call_tool("echo")) == "echo done"
        finally:
            await client.aclose()
        assert notice["reason"] == "the user stopped the turn"

    async def test_no_session_means_no_events(self, server: tuple[list[str], Path]) -> None:
        command, _log = server
        client = StdioMcpClient(command, timeout=0.3)
        try:
            out = await _handler(client, "slow")(session=None, tool_call_id="", steps=2, every=0.05)
        finally:
            await client.aclose()
        assert "slow done" in out


# ── HTTP client ──────────────────────────────────────────────────────────


class TestHttp:
    async def test_handshake_requests_wait_for_the_startup_timeout(self) -> None:
        read_timeouts: dict[str, float] = {}

        async def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            read_timeouts[body["method"]] = request.extensions["timeout"]["read"]
            if "id" not in body:
                return httpx.Response(202)
            result: dict[str, Any] = {"tools": []} if body["method"] == "tools/list" else {}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = HttpMcpClient("http://127.0.0.1:9/mcp", timeout=0.3, startup_timeout=60.0)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            await client.start()
            assert await client.list_tools() == []
            await client.call_tool("echo")
        finally:
            await client.aclose()
        # The handshake may take the whole startup timeout; a call keeps
        # its own.
        assert read_timeouts["initialize"] >= 60.0
        assert read_timeouts["notifications/initialized"] >= 60.0
        assert read_timeouts["tools/list"] >= 60.0
        assert read_timeouts["tools/call"] < 60.0

    async def test_timeout_posts_the_cancel_notice(self) -> None:
        posted: list[dict[str, Any]] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            posted.append(body)
            if body.get("method") == "tools/call":
                await asyncio.sleep(5.0)
            if "id" not in body:
                return httpx.Response(202)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

        client = HttpMcpClient("http://127.0.0.1:9/mcp", timeout=0.3)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(McpError, match="timeout_seconds"):
                await client.call_tool("slow")
            for _ in range(100):
                if any(p.get("method") == "notifications/cancelled" for p in posted):
                    break
                await asyncio.sleep(0.02)
        finally:
            await client.aclose()
        call = next(p for p in posted if p.get("method") == "tools/call")
        notice = next(p for p in posted if p.get("method") == "notifications/cancelled")
        assert notice["params"]["requestId"] == call["id"]

    async def test_concurrent_timeouts_cancel_their_own_requests(self) -> None:
        """TD-5154: ``call_tool`` read ``_next_id`` and trusted ``request()``
        to take that id. On Python 3.11 ``wait_for`` starts the request in a
        new task, so a second call read the same id first, and its cancel
        notice named the other call's request."""
        posted: list[dict[str, Any]] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            posted.append(body)
            if body.get("method") == "tools/call":
                await asyncio.sleep(5.0)
            if "id" not in body:
                return httpx.Response(202)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

        client = HttpMcpClient("http://127.0.0.1:9/mcp", timeout=0.3)
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            outcomes = await asyncio.gather(
                client.call_tool("slow_a"),
                client.call_tool("slow_b"),
                return_exceptions=True,
            )
            assert all(isinstance(o, McpError) for o in outcomes), outcomes
            for _ in range(100):
                if sum(p.get("method") == "notifications/cancelled" for p in posted) == 2:
                    break
                await asyncio.sleep(0.02)
        finally:
            await client.aclose()
        calls = {p["params"]["name"]: p["id"] for p in posted if p.get("method") == "tools/call"}
        assert len(set(calls.values())) == 2
        cancelled = sorted(
            p["params"]["requestId"] for p in posted if p.get("method") == "notifications/cancelled"
        )
        assert cancelled == sorted(calls.values())


# ── config: keys load, write and survive a Settings toggle ───────────────


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    cached_config.cache_clear()
    return tmp_path


class TestConfigKeys:
    def test_keys_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            McpServerConfig(transport="stdio", command=["x"], timeout_seconds=0)

    def test_written_keys_load_back(self, tmp_path: Path) -> None:
        path = ensure_user_config(tmp_path / "config.yaml")
        spec = McpServerConfig(
            transport="stdio",
            command=["render-server"],
            timeout_seconds=120,
            startup_timeout_seconds=20,
            max_call_seconds=1800,
        )
        save_mcp_server("render", spec, path)
        loaded = load_config(path).mcp.servers["render"]
        assert loaded.timeout_seconds == 120
        assert loaded.startup_timeout_seconds == 20
        assert loaded.max_call_seconds == 1800
        row = yaml.safe_load(path.read_text())["mcp"]["servers"]["render"]
        assert "env" not in row

    async def test_settings_toggle_keeps_the_timeouts(
        self, isolated_home: Path, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_path = ensure_user_config(data_dir / "config.yaml")
        save_mcp_server(
            "render",
            McpServerConfig(
                transport="stdio", command=["true"], timeout_seconds=120, max_call_seconds=1800
            ),
            config_path,
        )
        cached_config.cache_clear()
        daemon = Daemon(data_dir=data_dir)
        assert daemon.config_path == config_path
        raw = await daemon._handle_message(
            json.dumps(
                {
                    "type": "set_mcp_server",
                    "id": "render",
                    "transport": "stdio",
                    "command": ["true"],
                    "enabled": False,
                }
            ),
            None,
        )
        assert raw is not None and json.loads(raw)["type"] == "setup_state"
        row = yaml.safe_load(daemon.config_path.read_text())["mcp"]["servers"]["render"]
        assert row["enabled"] is False
        assert row["timeout_seconds"] == 120
        assert row["max_call_seconds"] == 1800
