"""A scripted "other computer" for the lane tests (not collected).

A real websockets server on 127.0.0.1 that speaks just enough of the
daemon protocol: hello with one key, ``list_sessions``, ``attach`` with
replay from a seq, ``user_message`` (one event), lifecycle verbs (a
``session_list``), and approvals (nothing, or one id-less ``error``).
``stall`` makes it read and never answer, the slow computer the hub must
never wait on.
"""

from __future__ import annotations

import contextlib
import itertools
import json
from typing import Any

import websockets.exceptions
from websockets.asyncio.server import Server, ServerConnection, serve

from tstd.protocol import PROTOCOL_VERSION

LIFECYCLE = frozenset({"archive_session", "rename_session", "set_session_star", "delete_session"})


def row(sid: str, **extra: Any) -> dict[str, Any]:
    """A valid session row."""
    return {
        "session_id": sid,
        "workspace_path": "/w",
        "state": "idle",
        "created_at": "2026-10-14T00:00:00Z",
        "updated_at": "2026-10-14T00:00:00Z",
        "event_count": 0,
        **extra,
    }


class FakeRemote:
    def __init__(self, key: str, machine_id: str = "mid_00000000000000aa") -> None:
        self.key = key
        self.machine_id = machine_id
        self.stall = False
        self.approve_error = False
        self.rows: list[dict[str, Any]] = [row("s1", title="one")]
        self.log: dict[str, list[dict[str, Any]]] = {}
        self.received: list[tuple[int, dict[str, Any]]] = []
        self.conns: dict[int, ServerConnection] = {}
        self.attached: dict[int, set[str]] = {}
        self._ids = itertools.count(1)
        self._server: Server | None = None
        self.port = 0

    async def start(self, port: int = 0) -> None:
        self._server = await serve(self._handler, "127.0.0.1", port, ping_interval=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop(self) -> None:
        """Close every socket (the computer's network blipped)."""
        for ws in list(self.conns.values()):
            await ws.close(1001)

    def got(self, kind: str) -> list[tuple[int, dict[str, Any]]]:
        return [(i, f) for i, f in self.received if f.get("type") == kind]

    async def emit(self, sid: str, **fields: Any) -> dict[str, Any]:
        """Log one event for *sid* and send it to every socket attached to it."""
        events = self.log.setdefault(sid, [])
        frame = {"type": "assistant_delta", "delta": "x", **fields}
        frame.update(session_id=sid, seq=len(events) + 1)
        events.append(frame)
        for idx, ws in list(self.conns.items()):
            if sid in self.attached.get(idx, set()):
                with contextlib.suppress(websockets.exceptions.ConnectionClosed):
                    await ws.send(json.dumps(frame))
        return frame

    def _session_list(self) -> str:
        return json.dumps({"type": "session_list", "seq": 1, "sessions": self.rows})

    async def _handler(self, ws: ServerConnection) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("token") != self.key:
            await ws.send(json.dumps({"type": "error", "code": "auth_failed", "message": "no"}))
            await ws.close(1008, "auth_failed")
            return
        ack = {"type": "hello_ack", "version": PROTOCOL_VERSION}
        ack.update(machine_id=self.machine_id, desktop_name="ezer")
        await ws.send(json.dumps(ack))
        idx = next(self._ids)
        self.conns[idx] = ws
        self.attached[idx] = set()
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.received.append((idx, frame))
                if not self.stall:
                    await self._answer(idx, ws, frame)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.conns.pop(idx, None)
            self.attached.pop(idx, None)

    async def _answer(self, idx: int, ws: ServerConnection, frame: dict[str, Any]) -> None:
        kind, sid = frame.get("type"), frame.get("session_id")
        if kind == "list_sessions" or kind in LIFECYCLE:
            await ws.send(self._session_list())
        elif kind == "attach" and isinstance(sid, str):
            self.attached[idx].add(sid)
            for event in self.log.get(sid, []):
                if event["seq"] >= frame.get("from_seq", 1):
                    await ws.send(json.dumps(event))
        elif kind == "detach" and isinstance(sid, str):
            self.attached[idx].discard(sid)
        elif kind == "user_message" and isinstance(sid, str):
            await self.emit(sid, delta=str(frame.get("content", "")))
        elif kind in ("approve", "deny", "always_allow") and self.approve_error:
            error = {
                "type": "error",
                "code": "no_pending_approval",
                "message": "Nothing to approve.",
            }
            await ws.send(json.dumps(error))
