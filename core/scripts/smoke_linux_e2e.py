#!/usr/bin/env python3
"""Packaged-sidecar protocol smoke for a clean guest (TD-1302, TD-4906).

Stdlib only — runs on Linux, macOS, and Windows (no ``websockets`` dep).
Two modes:

* ``--serve`` — OpenAI-compatible loopback on 127.0.0.1:11434 (the shipped
  ``local`` preset). First completion is ``fs_write``; the follow-up is text.
* ``--client`` — handshake the already-running bundled ``tstd``, switch to
  ``local``, open a workspace, send one turn, assert the write and the reply.
* ``--probe-keychain`` — on a clean guest, ``set_preset`` + ``validate_api_key``
  must return actionable copy, not hang (TD-4906).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_REPLY = "Wrote hello.txt as requested."
_WRITE_NAME = "hello.txt"
_WRITE_BODY = "hello from clean guest\n"


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


class _MockHandler(BaseHTTPRequestHandler):
    server: _MockServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s\n" % (fmt % args))

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps(
                {"object": "list", "data": [{"id": "smoke-model", "object": "model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode())
        except ValueError:
            self.send_error(400)
            return
        messages = req.get("messages") if isinstance(req, dict) else None
        follow_up = _has_tool_result(messages)
        workspace = self.server.workspace
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if follow_up:
            self.wfile.write(_text_stream(_REPLY))
        else:
            args = json.dumps({"path": str(workspace / _WRITE_NAME), "content": _WRITE_BODY})
            self.wfile.write(_tool_stream("fs_write", args))
        self.wfile.flush()


def _has_tool_result(messages: object) -> bool:
    if not isinstance(messages, list):
        return False
    return any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages)


def _tool_stream(name: str, arguments: str) -> bytes:
    start = {
        "id": "smoke-1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_smoke1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    done = {
        "id": "smoke-1",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }
    return _sse(start) + _sse(done) + b"data: [DONE]\n\n"


def _text_stream(text: str) -> bytes:
    delta = {
        "id": "smoke-2",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    done = {
        "id": "smoke-2",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
    }
    return _sse(delta) + _sse(done) + b"data: [DONE]\n\n"


class _MockServer(ThreadingHTTPServer):
    def __init__(self, workspace: Path) -> None:
        super().__init__(("127.0.0.1", 11434), _MockHandler)
        self.workspace = workspace


def serve(workspace: Path) -> None:
    httpd = _MockServer(workspace)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    sys.stderr.write("mock openai listening on 127.0.0.1:11434\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()


# ── Minimal RFC 6455 client (stdlib; guest may not have websockets) ──


class _Ws:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self._sock.sendall(req.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            header += chunk
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket upgrade failed: {header[:200]!r}")

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + masked)

    def recv_text(self) -> str:
        self._sock.settimeout(60)
        while True:
            opcode, data = self._read_frame()
            if opcode == 0x1:
                return data.decode()
            if opcode == 0x8:
                raise RuntimeError("websocket closed")
            if opcode == 0x9:
                self._send_pong(data)
                continue
            # ignore binary / continuation for this smoke

    def _send_pong(self, data: bytes) -> None:
        header = bytearray([0x8A, 0x80 | len(data)])
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        hdr = self._read_exact(2)
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        masked = bool(hdr[1] & 0x80)
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("websocket closed mid-frame")
            buf += chunk
        return buf

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


def _send(ws: _Ws, message: dict[str, Any]) -> None:
    ws.send_text(json.dumps(message))


def _recv_until(ws: _Ws, typ: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        ws._sock.settimeout(remaining)
        raw = ws.recv_text()
        event = json.loads(raw)
        last = event
        if event.get("type") == "error":
            raise RuntimeError(f"daemon error {event.get('code')}: {event.get('message')}")
        if event.get("type") == typ:
            return event
    raise TimeoutError(f"timed out waiting for {typ}; last={last!r}")


def client(port_file: Path, workspace: Path) -> None:
    info = json.loads(port_file.read_text())
    port = int(info["port"])
    token = str(info["token"])
    ws = _Ws("127.0.0.1", port)
    try:
        _send(ws, {"type": "hello", "token": token, "version": 1})
        _recv_until(ws, "hello_ack", 10.0)
        _send(ws, {"type": "set_preset", "name": "local"})
        _recv_until(ws, "setup_state", 15.0)
        _send(ws, {"type": "open_workspace", "path": str(workspace)})
        state = _recv_until(ws, "session_state", 15.0)
        session_id = str(state["session_id"])
        _send(ws, {"type": "attach", "session_id": session_id, "from_seq": 2})
        _send(
            ws,
            {
                "type": "user_message",
                "session_id": session_id,
                "content": "Write a short greeting to hello.txt.",
            },
        )
        texts: list[str] = []
        deadline = time.monotonic() + 60.0
        saw_tool = False
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            ws._sock.settimeout(remaining)
            event = json.loads(ws.recv_text())
            typ = event.get("type")
            if typ == "error":
                raise RuntimeError(f"daemon error {event.get('code')}: {event.get('message')}")
            if typ == "tool_call" and event.get("name") == "fs_write":
                saw_tool = True
            if typ == "assistant_delta" and event.get("delta"):
                texts.append(str(event["delta"]))
            if typ == "turn_complete":
                if event.get("failed"):
                    raise RuntimeError(f"turn failed: {event.get('error_code')}")
                break
        else:
            raise TimeoutError("turn_complete not received")
        written = workspace / _WRITE_NAME
        if not written.is_file():
            raise RuntimeError(f"{written} was not written")
        if written.read_text() != _WRITE_BODY:
            raise RuntimeError(f"{written} content mismatch: {written.read_text()!r}")
        if not saw_tool:
            raise RuntimeError("fs_write tool_call never arrived")
        joined = "".join(texts)
        if _REPLY not in joined:
            raise RuntimeError(f"assistant reply missing {_REPLY!r}: {joined!r}")
        print(
            json.dumps(
                {
                    "ok": True,
                    "session_id": session_id,
                    "wrote": str(written),
                    "reply": joined,
                }
            )
        )
    finally:
        ws.close()


def probe_keychain(port_file: Path) -> None:
    """Missing helper / empty keychain must fail typed, not hang (TD-4906)."""
    info = json.loads(port_file.read_text())
    port = int(info["port"])
    token = str(info["token"])
    ws = _Ws("127.0.0.1", port)
    try:
        _send(ws, {"type": "hello", "token": token, "version": 1})
        _recv_until(ws, "hello_ack", 10.0)
        _send(ws, {"type": "set_preset", "name": "tst-default"})
        state = _recv_until(ws, "setup_state", 15.0)
        if not state.get("key_required"):
            raise RuntimeError("tst-default must require a key on this preset")
        if state.get("has_api_key"):
            raise RuntimeError("clean guest must not report a stored API key")
        _send(ws, {"type": "validate_api_key"})
        resp = _recv_until(ws, "api_key_validated", 15.0)
        if resp.get("ok") is not False:
            raise RuntimeError(f"expected validate_api_key ok=false, got {resp!r}")
        detail = str(resp.get("detail", "")).lower()
        if "key" not in detail and "keychain" not in detail:
            raise RuntimeError(f"expected actionable key copy, got {resp!r}")
        print(json.dumps({"ok": True, "probe": "keychain"}))
    finally:
        ws.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--client", action="store_true")
    parser.add_argument("--probe-keychain", action="store_true")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port-file", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if args.serve:
        serve(workspace)
        return 0
    if args.client:
        if args.port_file is None:
            print("--port-file is required with --client", file=sys.stderr)
            return 2
        client(args.port_file, workspace)
        return 0
    if args.probe_keychain:
        if args.port_file is None:
            print("--port-file is required with --probe-keychain", file=sys.stderr)
            return 2
        probe_keychain(args.port_file)
        return 0
    print("pass --serve, --client, or --probe-keychain", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"e2e failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
