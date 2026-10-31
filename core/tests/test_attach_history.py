"""Attach to an older chat whose window was trimmed sends its history (TD-5201)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from tstd.daemon import Daemon
from tstd.protocol import (
    PROTOCOL_VERSION,
    AssistantDelta,
    AssistantReasoning,
    DaemonEvent,
    ToolCall,
    ToolResult,
    TurnComplete,
)
from tstd.provider import ChatMessage, FunctionCall
from tstd.provider import ToolCall as WireCall
from tstd.session_persist import SessionPersist
from tstd.session_store import SessionStore

SECRET = "sk-PROJEXAMPLEKEYfakefake0000abcd"  # tst-secret-ok
SID = "sid-older"


async def _seed(data_dir: Path, workspace: Path) -> None:
    """A three-turn chat whose window kept only the end of its third turn."""
    store = SessionStore(data_dir)
    await store.upsert(SID, str(workspace), "idle")
    persist = SessionPersist(data_dir)
    persist.prepare(SID)
    window: list[DaemonEvent] = [
        AssistantReasoning(session_id=SID, delta="thinking", seq=1),
        ToolCall(session_id=SID, tool_call_id="c3", name="read_file", arguments={}, seq=1),
        ToolResult(session_id=SID, tool_call_id="c3", status="success", output="mon", seq=1),
        AssistantDelta(session_id=SID, delta="Water on Monday.", seq=1),
        TurnComplete(session_id=SID, tokens=1, cost=0.0, tier="brain", duration=1.0, seq=1),
    ]
    for offset, event in enumerate(window):
        event.seq = 500 + offset
        persist.append_event(SID, event)
    call = WireCall(id="c3", function=FunctionCall(name="read_file", arguments="{}"))
    persist.save_conversation(
        SID,
        [
            ChatMessage(role="user", content=f"My key is {SECRET}, plan the beds"),
            ChatMessage(role="assistant", content="Three raised beds."),
            ChatMessage(role="user", content="When should I water?"),
            ChatMessage(role="assistant", content=None, tool_calls=[call]),
            ChatMessage(role="tool", content="mon", tool_call_id="c3"),
            ChatMessage(role="assistant", content="Water on Monday."),
        ],
    )


async def _recv(ws: Any) -> dict[str, Any]:
    return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))


async def _attach(tmp_path: Path, from_seq: int, features: list[str], count: int) -> list[Any]:
    """Seed, start a daemon, attach from *from_seq*, and take *count* frames."""
    data_dir = tmp_path / "data"
    workspace = tmp_path / "ws"
    data_dir.mkdir()
    workspace.mkdir()
    await _seed(data_dir, workspace)
    daemon = Daemon(data_dir=data_dir)
    task = asyncio.create_task(daemon.run())
    try:
        for _ in range(100):
            if daemon.ws_server.port:
                break
            await asyncio.sleep(0.05)
        ws = await connect(f"ws://127.0.0.1:{daemon.ws_server.port}")
        hello = {"type": "hello", "token": daemon.ws_server.token, "version": PROTOCOL_VERSION}
        await ws.send(json.dumps({**hello, "features": features}))
        assert (await _recv(ws))["type"] == "hello_ack"
        await ws.send(json.dumps({"type": "attach", "session_id": SID, "from_seq": from_seq}))
        frames = [await _recv(ws) for _ in range(count)]
        await ws.close()
    finally:
        daemon._shutdown_event.set()
        await asyncio.gather(task, return_exceptions=True)
    return [f for f in frames if f["type"] != "ready"]


@pytest.mark.asyncio
async def test_attach_sends_the_conversation_the_window_lost(tmp_path: Path) -> None:
    frames = await _attach(tmp_path, 1, ["history"], 4)
    assert frames[0]["type"] == "log_trimmed"
    assert frames[0]["earliest_seq"] == 500
    history = frames[1]
    assert history["type"] == "conversation_history"
    assert history["session_id"] == SID
    assert history["user_turns"] == 2
    assert [(m["role"], m["text"]) for m in history["messages"]] == [
        ("user", "My key is [REDACTED], plan the beds"),
        ("assistant", "Three raised beds."),
        ("user", "When should I water?"),
    ]
    assert SECRET not in json.dumps(history)
    assert frames[2]["seq"] == 500


@pytest.mark.asyncio
async def test_attach_from_inside_the_window_sends_no_history(tmp_path: Path) -> None:
    frames = await _attach(tmp_path, 503, ["history"], 3)
    types = [f["type"] for f in frames]
    assert "log_trimmed" not in types
    assert "conversation_history" not in types


@pytest.mark.asyncio
async def test_a_client_that_did_not_ask_gets_only_the_replay(tmp_path: Path) -> None:
    # `tst attach` prints a text stream: a megabyte of history it never
    # reads could only push it past its frame limit.
    frames = await _attach(tmp_path, 1, [], 3)
    assert frames[0]["type"] == "log_trimmed"
    assert frames[1]["seq"] == 500
    assert all(f["type"] != "conversation_history" for f in frames)
