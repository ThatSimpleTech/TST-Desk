"""TD-5112: a message sent while tools run steers the running turn.

The loop hands it to that turn at the next tool-batch boundary, as a user
message the model reads before its next step. No new turn starts, Stop
still ends the turn, a phone steers the same way, and the audit database
records where the message went in.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection

from tests.test_dispatch import attach_auto_approver
from tests.test_loop import make_config, mock_factory, wait_for_turn
from tstd.audit import AuditStore, audit_db_path
from tstd.audit_writer import AuditWriter
from tstd.compaction import budget_threshold, estimate_tokens, maybe_compact
from tstd.config import TierConfig, cached_config
from tstd.context.tokens import HeuristicTokenCounter
from tstd.context_fit import fit_inflight_turn
from tstd.daemon import Daemon
from tstd.loop import agent_loop
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig, PolicyRule, save_policy
from tstd.protocol import (
    PROTOCOL_VERSION,
    ContextCompacted,
    SteerPoint,
    TurnComplete,
    UserTurn,
)
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.provider import ChatCompletionRequest, ChatMessage, FunctionCall, content_as_text
from tstd.provider import ToolCall as ProviderToolCall
from tstd.router import TierRouter
from tstd.session import Session, SessionRunner
from tstd.session_persist import chat_message_from_dict, chat_message_to_dict
from tstd.tools import Tool, ToolDispatcher, ToolRegistry

# ── A loop with a tool the test holds open ──────────────────────────────


class _HeldTool:
    """A tool that runs until the test lets it finish."""

    def __init__(self, result: str = "built") -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.result = result

    async def handler(self, session: Session, tool_call_id: str = "") -> str:
        self.started.set()
        await self.release.wait()
        return self.result


async def _start(
    session: Session, scripts: list[Script], held: _HeldTool
) -> tuple[SessionRunner, MockProvider]:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="build",
            parameters={"type": "object", "properties": {}, "required": []},
            side_effect_class="auto",
            parallel_safe=False,
        )
    )
    dispatcher = attach_auto_approver(ToolDispatcher(registry))
    dispatcher.register_handler("build", held.handler)
    mock = MockProvider(sequences={"test-brain": scripts})
    router = TierRouter(lead_turns=80)
    config = make_config()
    factory = mock_factory(mock)
    runner = SessionRunner(
        session,
        loop_factory=lambda s: agent_loop(
            s, router, factory, config, tool_registry=registry, tool_dispatcher=dispatcher
        ),
    )
    await runner.start()
    return runner, mock


def _tool_then_text(reply: str = "done") -> list[Script]:
    return [
        Script(kind="tool_call", tool_name="build", tool_arguments="{}"),
        Script(kind="stream", content=reply),
    ]


def _roles(messages: list[ChatMessage]) -> list[tuple[str, Any]]:
    return [(m.role, m.content) for m in messages if m.role != "system"]


def _brain(mock: MockProvider, slug: str = "test-brain") -> list[Any]:
    """The turn's own model calls; the classifier's go to the worker."""
    return [call for call in mock.calls if call.model == slug]


def _user_turns(session: Session) -> list[UserTurn]:
    return [e for e in session.event_log.all_events if isinstance(e, UserTurn)]


def _turns(session: Session) -> list[TurnComplete]:
    return [e for e in session.event_log.all_events if isinstance(e, TurnComplete)]


async def test_a_message_sent_while_tools_run_joins_the_turn_before_its_next_step() -> None:
    session = Session("/tmp/ws")
    held = _HeldTool()
    runner, mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("Also run the linter", steer=True)
        held.release.set()
        first = await wait_for_turn(session, 1)
        assert first.error_code is None

        # The model's next step saw the message right after the batch's result.
        assert len(_brain(mock)) == 2
        assert _roles(_brain(mock)[1].messages)[-3:] == [
            ("assistant", None),
            ("tool", "built"),
            ("user", "Also run the linter"),
        ]
        # It joined this turn: no second turn ran or is owed.
        await asyncio.sleep(0.2)
        assert len(_turns(session)) == 1
        assert len(_brain(mock)) == 2
        assert session.turn_in_flight is False

        start, steer = _user_turns(session)
        assert start.steer is None
        assert steer.content == "Also run the linter"
        assert steer.steer == SteerPoint(
            turn_id=start.turn_id, step=1, after_tool_call_ids=["call_mock_1"]
        )
        # In the log between the batch's result and the reply that read it.
        events = session.event_log.all_events
        result_at = next(i for i, e in enumerate(events) if isinstance(e, ToolResultEvent))
        assert result_at < events.index(steer) < events.index(first)
        # The conversation keeps it where the model read it.
        assert _roles(session.conversation)[-2:] == [
            ("user", "Also run the linter"),
            ("assistant", "done"),
        ]
    finally:
        await runner.cancel()


async def test_a_message_sent_while_the_turn_sits_paused_after_a_batch_goes_in_on_resume() -> None:
    """A cap pause sits between the batch and the next model call."""
    session = Session("/tmp/ws")
    session.boundary_config.caps.max_iterations = 1
    held = _HeldTool()
    held.release.set()
    runner, mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        for _ in range(200):
            if session.state == "paused":
                break
            await asyncio.sleep(0.01)
        assert session.state == "paused"
        await session.add_user_message("Skip the docs build", steer=True)
        session.boundary_config.caps.max_iterations = 10
        await session.resume()
        first = await wait_for_turn(session, 1)
        assert first.error_code is None
        assert _roles(_brain(mock)[1].messages)[-2:] == [
            ("tool", "built"),
            ("user", "Skip the docs build"),
        ]
        assert _user_turns(session)[1].steer is not None
    finally:
        await runner.cancel()


async def test_a_message_sent_before_the_turn_started_still_waits_its_turn() -> None:
    """Only a message sent while the turn runs steers it; a backlog does not."""
    session = Session("/tmp/ws")
    held = _HeldTool()
    scripts = [*_tool_then_text("first"), Script(kind="stream", content="second")]
    runner, mock = await _start(session, scripts, held)
    try:
        await session.add_user_message("Build it")
        # Asking to steer does not matter: no turn was running yet.
        await session.add_user_message("Then tidy up", steer=True)
        await asyncio.wait_for(held.started.wait(), 3)
        held.release.set()
        await wait_for_turn(session, 1)
        await wait_for_turn(session, 2)
        assert all(m.content != "Then tidy up" for m in _brain(mock)[1].messages)
        assert [t.steer for t in _user_turns(session)] == [None, None]
    finally:
        await runner.cancel()


async def test_a_turn_with_no_further_tool_batch_leaves_the_message_its_own_turn() -> None:
    """Sent during the last step, it starts the next turn, as before."""
    session = Session("/tmp/ws")
    held = _HeldTool()
    scripts = [
        Script(kind="stream", content="one two three four five", chunk_delay=0.05),
        Script(kind="stream", content="answered"),
    ]
    runner, mock = await _start(session, scripts, held)
    try:
        await session.add_user_message("Explain")
        for _ in range(100):
            if _brain(mock):
                break
            await asyncio.sleep(0.01)
        await session.add_user_message("Shorter please", steer=True)
        await wait_for_turn(session, 1)
        second = await wait_for_turn(session, 2)
        assert second.error_code is None
        turns = _user_turns(session)
        assert [t.content for t in turns] == ["Explain", "Shorter please"]
        assert turns[1].steer is None
        assert _brain(mock)[1].messages[-1].content == "Shorter please"
    finally:
        await runner.cancel()


async def test_stop_still_ends_the_turn_with_a_message_waiting() -> None:
    session = Session("/tmp/ws")
    held = _HeldTool()
    scripts = [*_tool_then_text("not reached"), Script(kind="stream", content="next turn")]
    runner, mock = await _start(session, scripts, held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("Use the fast build", steer=True)
        await session.stop_turn()
        held.release.set()
        first = await wait_for_turn(session, 1)
        assert first.error_code == "stopped"
        # The message is not lost: it starts the next turn, unsteered. The
        # stopped turn made no model call after its batch.
        await wait_for_turn(session, 2)
        calls = _brain(mock)
        assert len(calls) == 2
        assert _roles(calls[1].messages)[-2:] == [("tool", "built"), ("user", "Use the fast build")]
        turns = _user_turns(session)
        assert [(t.content, t.steer) for t in turns] == [
            ("Build it", None),
            ("Use the fast build", None),
        ]
        assert runner.is_running is True
    finally:
        await runner.cancel()


async def test_the_queue_file_lets_a_steer_go_once_it_is_logged() -> None:
    """TD-5155 holds: the message is saved until its user_turn is."""
    session = Session("/tmp/ws")
    saved: list[list[str]] = []

    async def hook(rows: list[Any]) -> None:
        saved.append([row.display for row in rows])

    session._queue_hook = hook
    held = _HeldTool()
    runner, _ = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("Also lint", steer=True)
        assert saved[-1] == ["Also lint"]
        held.release.set()
        await wait_for_turn(session, 1)
        assert saved[-1] == []
    finally:
        await runner.cancel()


async def test_a_message_sent_without_asking_to_steer_waits_for_its_own_turn() -> None:
    """A row the window queued, or a client that does not ask, keeps its turn."""
    session = Session("/tmp/ws")
    held = _HeldTool()
    scripts = [*_tool_then_text("first"), Script(kind="stream", content="second")]
    runner, mock = await _start(session, scripts, held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("Then tidy up")
        held.release.set()
        await wait_for_turn(session, 1)
        await wait_for_turn(session, 2)
        assert all(m.content != "Then tidy up" for m in _brain(mock)[1].messages)
        assert [(t.content, t.steer) for t in _user_turns(session)] == [
            ("Build it", None),
            ("Then tidy up", None),
        ]
    finally:
        await runner.cancel()


async def test_messages_sent_to_steer_a_turn_that_ended_each_keep_a_turn() -> None:
    """Sent to steer a turn that ended first, the second does not steer the next."""
    session = Session("/tmp/ws")
    held = _HeldTool()
    held.release.set()
    scripts = [
        Script(kind="stream", content="one two three four five", chunk_delay=0.05),
        *_tool_then_text("first done"),
        Script(kind="stream", content="second done"),
    ]
    runner, mock = await _start(session, scripts, held)
    try:
        await session.add_user_message("Explain")
        for _ in range(100):
            if _brain(mock):
                break
            await asyncio.sleep(0.01)
        await session.add_user_message("First follow-up", steer=True)
        await session.add_user_message("Second follow-up", steer=True)
        third = await wait_for_turn(session, 3)
        assert third.error_code is None
        assert [(t.content, t.steer) for t in _user_turns(session)] == [
            ("Explain", None),
            ("First follow-up", None),
            ("Second follow-up", None),
        ]
        calls = _brain(mock)
        assert len(calls) == 4
        # The first follow-up's turn ran a batch and never read the second.
        assert all(m.content != "Second follow-up" for m in calls[2].messages)
        assert calls[3].messages[-1].content == "Second follow-up"
        assert session.turn_in_flight is False
    finally:
        await runner.cancel()


# ── Context fitting keeps a steered turn whole ──────────────────────────


def _small_tier() -> TierConfig:
    return TierConfig(
        slug="gateway-model",
        base_url="http://mock.local/v1",
        input_price=0.0,
        output_price=0.0,
        cache_read_price=0.0,
        context_window=32768,
        max_output_tokens=4096,
    )


def _batch(call_id: str, body: str) -> list[ChatMessage]:
    call = ProviderToolCall(
        id=call_id, type="function", function=FunctionCall(name="fs_read", arguments="{}")
    )
    return [
        ChatMessage(role="assistant", content=None, tool_calls=[call]),
        ChatMessage(role="tool", content=body, tool_call_id=call_id),
    ]


def _steer(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text, steer=True)


def test_compaction_keeps_the_running_turn_and_its_request_with_two_steers() -> None:
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="Oldest"),
        ChatMessage(role="assistant", content="o" * 40_000),
        ChatMessage(role="user", content="Earlier"),
        ChatMessage(role="assistant", content="answered"),
        ChatMessage(role="user", content="Build it"),
        *_batch("c1", "r" * 4_000),
        _steer("steer one"),
        *_batch("c2", "s" * 4_000),
        _steer("steer two"),
        *_batch("c3", "t" * 60_000),
    ]
    compacted, stats = maybe_compact(messages, _small_tier(), HeuristicTokenCounter())
    assert stats is not None
    users = [m.content for m in compacted if m.role == "user"]
    # Two whole turns stay: the earlier one and the running one, request first.
    assert users == ["Earlier", "Build it", "steer one", "steer two"]
    assert stats.dropped_user_turns == 1


def test_in_turn_elision_reaches_results_from_before_a_steer() -> None:
    tier = _small_tier()
    counter = HeuristicTokenCounter()
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="Build it"),
        *_batch("c1", "P" * 100_000),
        _steer("use the fast build"),
        *_batch("c2", "Q" * 2_000),
    ]
    fitted, fit = fit_inflight_turn(messages, tier, counter)
    assert fit is not None
    assert "chars elided" in content_as_text(fitted[3].content)
    assert estimate_tokens(fitted, counter)[0] <= budget_threshold(tier)
    # The steer and the request stay as sent.
    assert fitted[4] is messages[4]
    assert fitted[1] is messages[1]


def test_a_steer_mark_is_saved_with_the_conversation_and_never_sent() -> None:
    steer = _steer("also lint")
    saved = chat_message_to_dict(steer)
    assert saved["steer"] is True
    assert chat_message_from_dict(json.loads(json.dumps(saved))).steer is True
    assert "steer" not in ChatCompletionRequest._message_to_dict(steer)
    plain = ChatMessage(role="user", content="build it")
    assert "steer" not in chat_message_to_dict(plain)
    assert chat_message_from_dict(chat_message_to_dict(plain)).steer is False


async def test_a_steer_is_counted_before_the_model_call_that_reads_it() -> None:
    """The prompt is fitted again once the steer is in, so it stays in budget."""
    session = Session("/tmp/ws")
    held = _HeldTool(result="B" * 200_000)
    runner, mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("S" * 280_000, steer=True)
        held.release.set()
        first = await wait_for_turn(session, 1)
        assert first.error_code is None

        calls = _brain(mock)
        assert len(calls) == 2
        sent = calls[1].messages
        assert sent[-1].content == "S" * 280_000
        # The result the turn got before the steer was elided to make room.
        result = next(m for m in sent if m.role == "tool")
        assert "chars elided" in content_as_text(result.content)
        tier = make_config().tier("brain")
        assert estimate_tokens(sent, HeuristicTokenCounter())[0] <= budget_threshold(tier)
        # Announced on the timeline after the steer went in.
        events = session.event_log.all_events
        steer_at = events.index(_user_turns(session)[1])
        assert any(
            isinstance(e, ContextCompacted) and e.trigger == "inflight_trim"
            for e in events[steer_at:]
        )
    finally:
        await runner.cancel()


# ── Audit ───────────────────────────────────────────────────────────────


async def test_the_audit_database_records_the_injection_point(tmp_path: Path) -> None:
    store = AuditStore(tmp_path / "audit.db")
    session = Session("/tmp/ws")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    held = _HeldTool()
    runner, _ = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("Also lint", steer=True)
        held.release.set()
        await wait_for_turn(session, 1)
        await writer._queue.join()
        start, steer = _user_turns(session)
        rows = store._conn.execute(
            "SELECT session_id, message_id, into_turn_id, turn_index, step, after_tool_calls"
            " FROM steers"
        ).fetchall()
        assert rows == [(session.id, steer.turn_id, start.turn_id, 1, 1, '["call_mock_1"]')]
        # The same turn_index the running turn's own row got.
        turns = store._conn.execute("SELECT turn_index FROM turns").fetchall()
        assert turns == [(1,)]
    finally:
        await runner.cancel()
        await writer.close()
        store.close()


# ── From the phone ──────────────────────────────────────────────────────

PHONE = "/phone"


def _is_phone(ws: ServerConnection) -> bool:
    return ws.request is not None and ws.request.path == PHONE


async def _recv(ws: ClientConnection, within: float = 10.0) -> dict[str, Any]:
    while True:
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), within))
        if frame.get("type") != "ping":
            return frame


async def _recv_until(ws: ClientConnection, want: str, **fields: Any) -> dict[str, Any]:
    while True:
        frame = await _recv(ws)
        if frame.get("type") == want and all(frame.get(k) == v for k, v in fields.items()):
            return frame
        assert frame.get("type") != "error", frame


async def _client(port: int, token: str, path: str) -> ClientConnection:
    ws = await connect(f"ws://127.0.0.1:{port}{path}")
    await ws.send(json.dumps({"type": "hello", "token": token, "version": PROTOCOL_VERSION}))
    ack = await _recv(ws)
    assert ack["type"] == "hello_ack", ack
    return ws


@pytest.fixture
async def phone_daemon(tmp_path: Path) -> AsyncIterator[tuple[Daemon, MockProvider, Path]]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    save_policy(
        workspace, PolicyConfig(rules=[PolicyRule(tool="fs_write", args="**", effect="ask")])
    )
    slug = cached_config().tier("brain").slug
    target = workspace / "hello.txt"
    mock = MockProvider(
        sequences={
            slug: [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments=json.dumps({"path": str(target), "content": "hi\n"}),
                ),
                Script(kind="stream", content="wrote it, and goodbye"),
                Script(kind="stream", content="a turn of its own"),
            ]
        }
    )
    daemon = Daemon(data_dir=tmp_path / "data", provider=mock)
    daemon.ws_server._is_remote_connection = _is_phone
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if daemon.ws_server.port:
            break
        await asyncio.sleep(0.05)
    assert daemon.ws_server.port
    try:
        yield daemon, mock, workspace
    finally:
        daemon._shutdown_event.set()
        await asyncio.wait_for(task, 10)


async def test_a_paired_phone_steers_a_turn_the_window_started(
    phone_daemon: tuple[Daemon, MockProvider, Path],
) -> None:
    daemon, mock, workspace = phone_daemon
    port = daemon.ws_server.port
    _, key = daemon.remote_devices.add("Pixel")
    window = await _client(port, daemon.ws_server.token, "/")
    phone = await _client(port, key, PHONE)
    try:
        await window.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
        opened = await _recv_until(window, "session_state")
        sid = opened["session_id"]
        for ws in (window, phone):
            await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await window.send(
            json.dumps({"type": "user_message", "session_id": sid, "content": "write hello.txt"})
        )
        # The write waits for approval: the turn's tools are running.
        request = await _recv_until(phone, "approval_request")
        await phone.send(
            json.dumps(
                {
                    "type": "user_message",
                    "session_id": sid,
                    "content": "then say goodbye",
                    "steer": True,
                }
            )
        )
        await phone.send(
            json.dumps(
                {"type": "approve", "session_id": sid, "tool_call_id": request["tool_call_id"]}
            )
        )
        steer = await _recv_until(phone, "user_turn", content="then say goodbye")
        assert steer["steer"]["after_tool_call_ids"] == [request["tool_call_id"]]
        assert steer["steer"]["step"] == 1
        # The window sees the same point, and one turn ends.
        seen = await _recv_until(window, "user_turn", content="then say goodbye")
        assert seen["steer"] == steer["steer"]
        await _recv_until(phone, "turn_complete")

        last = _brain(mock, cached_config().tier("brain").slug or "")[1].messages
        assert [(m.role, m.content) for m in last[-2:]][-1] == ("user", "then say goodbye")
        assert last[-2].role == "tool"
        session = daemon.session_registry.get(sid)
        assert session is not None
        assert session.turn_in_flight is False
        assert len(_brain(mock, cached_config().tier("brain").slug or "")) == 2

        writer = daemon._audit_writer
        assert writer is not None
        await writer._queue.join()
        store = AuditStore(audit_db_path(daemon.data_dir))
        try:
            rows = store._conn.execute(
                "SELECT message_id, step, after_tool_calls FROM steers"
            ).fetchall()
        finally:
            store.close()
        assert rows == [(steer["turn_id"], 1, json.dumps([request["tool_call_id"]]))]
    finally:
        await phone.close()
        await window.close()


async def test_a_message_that_does_not_ask_to_steer_keeps_its_own_turn(
    phone_daemon: tuple[Daemon, MockProvider, Path],
) -> None:
    """The window's queued row goes out without ``steer``: it waits its turn."""
    daemon, mock, workspace = phone_daemon
    port = daemon.ws_server.port
    window = await _client(port, daemon.ws_server.token, "/")
    try:
        await window.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
        sid = (await _recv_until(window, "session_state"))["session_id"]
        await window.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await window.send(
            json.dumps({"type": "user_message", "session_id": sid, "content": "write hello.txt"})
        )
        request = await _recv_until(window, "approval_request")
        await window.send(
            json.dumps({"type": "user_message", "session_id": sid, "content": "and then this"})
        )
        await window.send(
            json.dumps(
                {"type": "approve", "session_id": sid, "tool_call_id": request["tool_call_id"]}
            )
        )
        await _recv_until(window, "turn_complete")
        own = await _recv_until(window, "user_turn", content="and then this")
        assert own["steer"] is None
        await _recv_until(window, "turn_complete")
        # The first turn's step after the write never read it; a turn of its
        # own started with it.
        calls = mock.calls
        assert all(m.content != "and then this" for m in calls[1].messages)
        assert any(call.messages[-1].content == "and then this" for call in calls[2:])
    finally:
        await window.close()


async def test_every_refused_user_message_names_it(
    phone_daemon: tuple[Daemon, MockProvider, Path],
) -> None:
    """The window tells a refused send from other errors by in_reply_to."""
    daemon, _, workspace = phone_daemon
    port = daemon.ws_server.port
    window = await _client(port, daemon.ws_server.token, "/")
    try:
        await window.send(
            json.dumps({"type": "user_message", "session_id": "nope", "content": "hi"})
        )
        missing = await _recv(window)
        assert (missing["type"], missing["code"]) == ("error", "session_not_found")
        assert missing["in_reply_to"] == "user_message"

        await window.send(json.dumps({"type": "open_workspace", "path": str(workspace)}))
        sid = (await _recv_until(window, "session_state"))["session_id"]
        await window.send(
            json.dumps(
                {
                    "type": "user_message",
                    "session_id": sid,
                    "content": "see the file",
                    "steer": True,
                    "attachments": [{"name": "a.txt", "content_b64": "not base64!"}],
                }
            )
        )
        refused = await _recv(window)
        assert refused["type"] == "error"
        assert refused["code"].startswith("attachment_")
        assert refused["in_reply_to"] == "user_message"
    finally:
        await window.close()


async def test_a_skill_named_in_a_steer_is_in_the_prompt_of_the_step_that_reads_it(
    tmp_path: Path,
) -> None:
    """TD-5160: the prompt was assembled before the steer loaded the skill,
    so the step reading "/review" went without it."""
    skill = tmp_path / ".tst" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\ndescription: Review a diff\n---\nREVIEW-SKILL-BODY\n")
    session = Session(str(tmp_path))
    held = _HeldTool()
    runner, mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        await session.add_user_message("/review the diff", steer=True)
        held.release.set()
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    first, second = _brain(mock)
    assert "REVIEW-SKILL-BODY" not in str(first.messages[0].content)
    assert second.messages[0].role == "system"
    assert "REVIEW-SKILL-BODY" in str(second.messages[0].content)
    assert session.loaded_skills == ["review"]


async def test_line_comments_that_join_a_running_turn_keep_their_anchors() -> None:
    """TD-5132: the window marks a comment resolved from the user_turn's anchors."""
    from tstd.protocol import LineCommentAnchor
    from tstd.session import QueuedUserMessage

    session = Session("/tmp/ws")
    held = _HeldTool()
    runner, _mock = await _start(session, _tool_then_text(), held)
    try:
        await session.add_user_message("Build it")
        await asyncio.wait_for(held.started.wait(), 3)
        anchor = LineCommentAnchor(id="c1", path="/tmp/ws/a.py", start_line=2, end_line=2, body="x")
        await session.add_user_message(
            QueuedUserMessage(
                display="Review comments", provider="Review comments", line_comments=[anchor]
            ),
            steer=True,
        )
        held.release.set()
        await wait_for_turn(session, 1)
        _start_turn, steer = _user_turns(session)
        assert steer.steer is not None
        assert steer.line_comments == [anchor]
    finally:
        await runner.cancel()
