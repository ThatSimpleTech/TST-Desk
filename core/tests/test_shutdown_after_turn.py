"""SIGTERM after a finished turn exits inside the shutdown budget (TD-4844).

The provider read inside quit-distill used to sit until that budget
fired. These tests spawn a daemon, finish one turn, drop the client,
and signal it. Readiness is a log line, not a sleep.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import websockets
import yaml

from tstd.cli import daemon_argv
from tstd.config import default_config_yaml
from tstd.daemon import Daemon
from tstd.logging import LOG_FILE_NAME, log_directory, setup_logging
from tstd.mock import MockProvider, Script
from tstd.protocol import PROTOCOL_VERSION
from tstd.provider import ChatCompletionRequest, ChatCompletionResponse, ProviderError
from tstd.ws import create_port_file_path, read_port_file

_CORE = Path(__file__).resolve().parent.parent


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(_CORE),
        env=env,
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except PermissionError:
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return
    if proc.stdout is not None:
        proc.stdout.close()


def _read_log(data_dir: Path) -> str:
    return (log_directory(data_dir) / LOG_FILE_NAME).read_text(encoding="utf-8")


class _HangDistill(MockProvider):
    """Stream a turn, then sit in the non-streaming call quit-distill makes."""

    def __init__(self) -> None:
        super().__init__(default=Script(kind="stream", content="hi"))

    async def chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse | ProviderError:
        del request
        await asyncio.Event().wait()
        return ProviderError(code="cancelled", message="unused", retryable=False)


class _Stdout:
    """One reader for the daemon pipe. Waits are on events, not a clock."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self._chunks: list[bytes] = []
        self._ready = threading.Event()
        self._disconnected = threading.Event()
        threading.Thread(target=self._read, name="tstd-stdout", daemon=True).start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                return
            self._chunks.append(line)
            if b"ws server started" in line:
                self._ready.set()
            if b"client disconnected" in line:
                self._disconnected.set()

    def wait_ready(self, seconds: float = 10.0) -> None:
        if self._ready.wait(seconds):
            return
        tail = b"".join(self._chunks).decode(errors="replace")[-2000:]
        raise RuntimeError(f"daemon not ready (exit={self._proc.poll()}): {tail}")

    def wait_disconnected(self, seconds: float = 10.0) -> None:
        if self._disconnected.wait(seconds):
            return
        tail = b"".join(self._chunks).decode(errors="replace")[-2000:]
        raise RuntimeError(f"client disconnect was not logged: {tail}")


def _spawn_injected(mode: str, data_dir: Path) -> subprocess.Popen[bytes]:
    """A daemon whose provider is the test double named by *mode*."""
    return _spawn([sys.executable, str(Path(__file__).resolve()), mode, str(data_dir)])


async def _finish_one_turn(data_dir: Path, workspace: Path) -> None:
    """Hello, open, attach, one user message, then drop the socket."""
    info = read_port_file(data_dir)
    assert info is not None
    ws = await websockets.connect(f"ws://127.0.0.1:{info['port']}")
    await ws.send(
        json.dumps({"type": "hello", "token": info["token"], "version": PROTOCOL_VERSION})
    )
    ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert ack["type"] == "hello_ack"
    await ws.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
    opened = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert opened["type"] == "session_state"
    session_id = opened["session_id"]
    await ws.send(json.dumps({"type": "attach", "session_id": session_id, "from_seq": 1}))
    await ws.send(json.dumps({"type": "user_message", "session_id": session_id, "content": "hi"}))
    kinds: list[str] = []
    while "turn_complete" not in kinds:
        raw = await asyncio.wait_for(ws.recv(), 10)
        kinds.append(str(json.loads(raw)["type"]))
        assert len(kinds) < 40
    await ws.close()


def _signal_and_join(proc: subprocess.Popen[bytes]) -> int:
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except PermissionError:
        pytest.skip("the OS vetoed the test's own kill (TD-605)")
    try:
        return proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise AssertionError("daemon still running 2s after the signal") from None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps the websocket shutdown path; SIGTERM is not installed",
)
@pytest.mark.parametrize("mode", ["mock", "hang"])
def test_finished_session_stops_inside_the_budget(tmp_path: Path, mode: str) -> None:
    """One completed turn, the client gone, SIGTERM. Exit 0 inside the budget.

    ``mock`` answers distill. ``hang`` sits in that call the way a provider
    read does, which used to trip the 5s budget.
    """
    data_dir = tmp_path / "scratch"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    proc = _spawn_injected(mode, data_dir)
    out = _Stdout(proc)
    try:
        out.wait_ready()
        port = create_port_file_path(data_dir)
        assert port.is_file()
        asyncio.run(_finish_one_turn(data_dir, workspace))
        out.wait_disconnected()
        code = _signal_and_join(proc)
        assert code == 0
        assert not port.exists()
        text = _read_log(data_dir)
        assert "signal received" in text
        assert "shutdown complete" in text
        assert "shutdown exceeded" not in text
        if mode == "hang":
            assert "distill skipped; provider call still in flight" in text
        else:
            assert "distill skipped; provider call still in flight" not in text
    finally:
        _stop(proc)


def _write_loopback_config(data_dir: Path, base_url: str) -> None:
    loaded = yaml.safe_load(default_config_yaml())
    assert isinstance(loaded, dict)
    loaded["active_preset"] = "local"
    for tier in loaded["presets"]["local"].values():
        tier["base_url"] = base_url
        tier["slug"] = "test-model"
    loaded["embeddings"]["base_url"] = ""
    loaded["provider_retry"]["max_retries"] = 0
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(yaml.safe_dump(loaded), encoding="utf-8")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows keeps the websocket shutdown path; SIGTERM is not installed",
)
def test_stuck_provider_read_does_not_burn_the_shutdown_budget(tmp_path: Path) -> None:
    """The production client, against a server that accepts distill and never answers."""
    hold = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _body(self) -> bytes:
            count = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(count) if count else b""

        def do_POST(self) -> None:
            try:
                payload = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                payload = {}
            if payload.get("stream"):
                data = (
                    b'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant",'
                    b'"content":"hi"},"finish_reason":null}]}\n\n'
                    b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            hold.wait(30)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    data_dir = tmp_path / "scratch"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_loopback_config(data_dir, f"http://127.0.0.1:{port}/v1")
    proc = _spawn(daemon_argv(data_dir))
    out = _Stdout(proc)
    try:
        out.wait_ready()
        port_file = create_port_file_path(data_dir)
        asyncio.run(_finish_one_turn(data_dir, workspace))
        out.wait_disconnected()
        code = _signal_and_join(proc)
        assert code == 0
        assert not port_file.exists()
        text = _read_log(data_dir)
        assert "shutdown complete" in text
        assert "distill skipped; provider call still in flight" in text
        assert "shutdown exceeded" not in text
    finally:
        hold.set()
        server.shutdown()
        _stop(proc)


def _child() -> None:
    mode, raw_dir = sys.argv[1], sys.argv[2]
    data_dir = Path(raw_dir)
    setup_logging(level="INFO", log_dir=log_directory(data_dir))
    if mode == "mock":
        provider: MockProvider = MockProvider(default=Script(kind="stream", content="hi"))
    elif mode == "hang":
        provider = _HangDistill()
    else:
        raise SystemExit(f"unknown shutdown child mode {mode}")

    async def _run() -> None:
        await Daemon(data_dir=data_dir, provider=provider).run()

    asyncio.run(_run())


if __name__ == "__main__":
    _child()
