"""Grok ACP loop maps updates onto TST session events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tstd.desktop.mock import MockDesktopDriver
from tstd.grok_acp import GrokEngineError
from tstd.grok_loop import (
    _is_leaked_tool_json,
    _tool_input,
    _tool_name,
    _update_text,
    _visible_delta,
    grok_loop,
    grok_model_from_payload,
)
from tstd.protocol import (
    ApprovalRequest,
    AssistantDelta,
    CuSession,
    Error,
    GrokMode,
    ToolCall,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from tstd.session import Session


class FakeAcp:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self._update = None
        self._permission = None
        self.prompts: list[tuple[str, str]] = []
        self.mcp_servers: list[dict[str, Any]] = []
        self.alive = True

    def on_update(self, handler: Any) -> None:
        self._update = handler

    def on_permission(self, handler: Any) -> None:
        self._permission = handler

    async def start(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def initialize(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"protocolVersion": 1}

    async def session_new(
        self,
        cwd: str,
        *,
        yolo: bool = False,
        resume_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        self.mcp_servers = mcp_servers or []
        return resume_id or "grok-1"

    async def prompt(
        self, session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        self.prompts.append((session_id, text))
        assert self._update is not None
        await self._update(
            {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "working"},
                }
            }
        )
        await self._update(
            {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-9",
                    "title": "Read",
                    "rawInput": {"path": "a.py"},
                    "locations": [{"path": "a.py"}],
                }
            }
        )
        await self._update(
            {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-9",
                    "status": "completed",
                    "rawOutput": "ok",
                }
            }
        )
        await self._update(
            {
                "update": {
                    "sessionUpdate": "available_commands",
                    "commands": [{"name": "compact", "description": "Compress"}],
                }
            }
        )
        await self._update(
            {
                "update": {
                    "sessionUpdate": "plan",
                    "markdown": "# Plan",
                    "entries": [{"content": "Do it", "status": "pending"}],
                }
            }
        )
        return {"stopReason": "end_turn"}

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def close(self) -> None:
        return None


async def _wait_for_turn_complete(session: Session) -> None:
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_grok_loop_tags_the_computer_use_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grok drives the MCP itself, so the daemon must tag the episode the way
    the native loop does (TD-3407): open on the first computer-use__* tool,
    closed at turn end — diagnostics do not count."""
    session = Session("/tmp")
    await session.set_state("running")
    driver = MockDesktopDriver()
    session._overlay_session = driver.set_overlay_session
    fake = FakeAcp()

    async def prompt_with_cu(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        assert fake._update is not None
        for tc_id, title, raw in (
            ("tc-1", "computer-use__check_permissions", {}),
            # Grok Build wraps MCP calls in use_tool; the tool is in rawInput.
            (
                "tc-2",
                "use_tool",
                {"tool_name": "computer-use__press_keys", "tool_input": {"combo": "cmd+space"}},
            ),
        ):
            await fake._update(
                {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": tc_id,
                        "title": title,
                        "rawInput": raw,
                    }
                }
            )
            await fake._update(
                {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tc_id,
                        "status": "completed",
                        "rawOutput": "ok",
                    }
                }
            )
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(fake, "prompt", prompt_with_cu)
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("open spotlight")
    await _wait_for_turn_complete(session)
    types = [e.type for e in session.event_log.events_from(1)]
    calls = [e for e in session.event_log.events_from(1) if isinstance(e, ToolCall)]
    assert [c.name for c in calls] == [
        "computer-use__check_permissions",
        "computer-use__press_keys",
    ]
    assert calls[1].arguments == {"combo": "cmd+space"}
    tags = [e for e in session.event_log.events_from(1) if isinstance(e, CuSession)]
    assert [t.active for t in tags] == [True, False]
    # Opened by the actuation, not the diagnostic before it.
    assert types.index("cu_session") == types.index("tool_call", types.index("tool_call") + 1) + 1
    # Closed at turn end, not at the last tool.
    assert types[types.index("turn_complete") + 1] == "cu_session"
    assert driver.calls == [
        ("overlay_session", {"active": True}),
        ("overlay_session", {"active": False}),
    ]
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_grok_loop_failed_turn_closes_the_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """An engine fault mid-episode still closes it: the ring must not outlive
    the turn that died."""
    session = Session("/tmp")
    await session.set_state("running")
    driver = MockDesktopDriver()
    session._overlay_session = driver.set_overlay_session
    fake = FakeAcp()

    async def prompt_then_die(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        assert fake._update is not None
        await fake._update(
            {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "computer-use__click",
                    "rawInput": {"x": 10, "y": 10},
                }
            }
        )
        raise GrokEngineError("agent_exited", "grok exited")

    monkeypatch.setattr(fake, "prompt", prompt_then_die)
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("click it")
    await _wait_for_turn_complete(session)
    done = [e for e in session.event_log.events_from(1) if isinstance(e, TurnComplete)]
    assert done and done[0].failed is True
    tags = [e for e in session.event_log.events_from(1) if isinstance(e, CuSession)]
    assert [t.active for t in tags] == [True, False]
    assert driver.calls[-1] == ("overlay_session", {"active": False})
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_grok_loop_emits_transcript_events() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("do the thing")
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    types = [e.type for e in session.event_log.events_from(1)]
    assert "user_turn" in types
    assert "assistant_delta" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "turn_complete" in types
    assert "grok_commands" in types
    assert "grok_plan" in types
    assert fake.prompts == [("grok-1", "do the thing")]
    deltas = [e for e in session.event_log.events_from(1) if isinstance(e, AssistantDelta)]
    assert deltas[0].delta == "working"
    calls = [e for e in session.event_log.events_from(1) if isinstance(e, ToolCall)]
    assert calls[0].name == "Read"
    results = [e for e in session.event_log.events_from(1) if isinstance(e, ToolResult)]
    assert results[0].status == "success"
    users = [e for e in session.event_log.events_from(1) if isinstance(e, UserTurn)]
    assert users[0].content == "do the thing"
    assert "a.py" in session.touched_paths or any("a.py" in str(p) for p in session.touched_paths)


@pytest.mark.asyncio
async def test_grok_loop_permission_parks_approval() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()

    async def prompt_with_permission(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        assert fake._permission is not None
        task = asyncio.create_task(
            fake._permission(
                {
                    "toolCall": {
                        "toolCallId": "perm-1",
                        "title": "Bash",
                        "rawInput": {"command": "ls"},
                    },
                    "options": [
                        {"optionId": "allow-once", "kind": "allow_once"},
                        {"optionId": "reject-once", "kind": "reject_once"},
                    ],
                }
            )
        )
        for _ in range(40):
            if any(isinstance(e, ApprovalRequest) for e in session.event_log.events_from(1)):
                break
            await asyncio.sleep(0.02)
        pending = session.get_pending_approval("perm-1")
        assert pending is not None
        pending.future.set_result((True, None))
        result = await task
        assert result["outcome"]["optionId"] == "allow-once"
        return {"stopReason": "end_turn"}

    fake.prompt = prompt_with_permission  # type: ignore[method-assign]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("run ls")
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    approvals = [e for e in session.event_log.events_from(1) if isinstance(e, ApprovalRequest)]
    assert approvals[0].tool_name == "Bash"


@pytest.mark.asyncio
async def test_grok_loop_emits_preview_and_mode(tmp_path: Path) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    session = Session(str(tmp_path))
    await session.set_state("running")
    fake = FakeAcp()

    async def prompt_with_preview(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        assert fake._update is not None
        await fake._update(
            {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "up at http://127.0.0.1:5173/app"},
                }
            }
        )
        await fake._update(
            {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-img",
                    "title": "Write",
                    "locations": [{"path": "shot.png"}],
                }
            }
        )
        await fake._update(
            {
                "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "plan",
                    "availableModes": [{"id": "agent"}, {"id": "plan"}],
                }
            }
        )
        return {"stopReason": "end_turn", "usage": {"total_tokens": 12, "cost": 0.02}}

    fake.prompt = prompt_with_preview  # type: ignore[method-assign]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("show it")
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    types = [e.type for e in session.event_log.events_from(1)]
    assert "grok_preview" in types
    assert "grok_mode" in types
    previews = [e for e in session.event_log.events_from(1) if e.type == "grok_preview"]
    urls = [getattr(e, "url", None) for e in previews]
    paths = [getattr(e, "path", None) for e in previews]
    assert "http://127.0.0.1:5173/app" in urls
    assert any(p and p.endswith("shot.png") for p in paths)
    modes = [e for e in session.event_log.events_from(1) if e.type == "grok_mode"]
    assert modes[0].mode == "plan"  # type: ignore[attr-defined]
    costs = [e for e in session.event_log.events_from(1) if e.type == "cost_update"]
    assert costs[0].turn_cost == 0.02  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_grok_loop_forwards_pending_images(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    await session.set_state("running")
    fake = FakeAcp()
    received: list[
        tuple[list[tuple[str, bytes, str]] | None, list[tuple[str, str, str]] | None]
    ] = []

    async def prompt_images(
        session_id: str,
        text: str,
        images: list[tuple[str, bytes, str]] | None = None,
        files: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, Any]:
        received.append((images, files))
        return {"stopReason": "end_turn"}

    fake.prompt = prompt_images  # type: ignore[method-assign]
    png = b"\x89PNG\r\n\x1a\n"
    session.pending_images = [("shot.png", png, "image/png")]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("look")
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    # Grok advertises image: false (FakeAcp initialize has no caps), so
    # the photo is written to the workspace and sent as a resource_link.
    images, files = received[0]
    assert images is None
    assert files is not None
    assert files[0][0] == "shot.png"
    assert files[0][2] == "image/png"
    dest = tmp_path / ".tst" / "attachments" / session.id / "shot.png"
    assert dest.read_bytes() == png
    assert session.pending_images == []


@pytest.mark.asyncio
async def test_grok_loop_sends_image_blocks_when_agent_supports_them() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()

    async def initialize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"agentCapabilities": {"promptCapabilities": {"image": True}}}

    fake.initialize = initialize  # type: ignore[method-assign]
    received: list[list[tuple[str, bytes, str]] | None] = []

    async def prompt_images(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        received.append(images)
        return {"stopReason": "end_turn"}

    fake.prompt = prompt_images  # type: ignore[method-assign]
    session.pending_images = [("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("look")
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    assert received[0] == [("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")]
    assert session.pending_images == []


def test_use_tool_wrapper_is_unwrapped() -> None:
    wrapped = {
        "title": "use_tool",
        "rawInput": {"tool_name": "computer-use__click", "tool_input": {"x": 1, "y": 2}},
    }
    assert _tool_name(wrapped) == "computer-use__click"
    assert _tool_input(wrapped) == {"x": 1, "y": 2}
    plain = {"title": "Read", "rawInput": {"path": "a.py"}}
    assert _tool_name(plain) == "Read"
    assert _tool_input(plain) == {"path": "a.py"}


def test_update_text_keeps_only_prose() -> None:
    assert _update_text({"content": {"type": "text", "text": "hi"}}) == "hi"
    assert _update_text({"content": {"type": "image", "data": "xx"}}) == ""
    assert (
        _update_text(
            {
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "diff", "diff": "x"},
                ]
            }
        )
        == "a"
    )


def test_leaked_tool_json_is_not_prose() -> None:
    assert _is_leaked_tool_json('"is_background": false')
    assert _is_leaked_tool_json("}")
    assert not _is_leaked_tool_json("Hello there")


def test_grok_model_from_payload() -> None:
    assert grok_model_from_payload(None) is None
    assert grok_model_from_payload({"_meta": {"modelId": "grok-4.6"}}) == "grok-4.6"
    assert (
        grok_model_from_payload(
            {"update": {"sessionUpdate": "agent_message_chunk", "_meta": {"modelId": "grok-4.6"}}}
        )
        == "grok-4.6"
    )
    assert grok_model_from_payload({"modelId": "custom"}) == "custom"
    assert grok_model_from_payload({"model": "not-this"}) is None


@pytest.mark.asyncio
async def test_grok_loop_records_model_from_update_meta() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()

    async def prompt_with_model(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        assert fake._update is not None
        await fake._update(
            {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hi"},
                    "_meta": {"modelId": "grok-4.6"},
                }
            }
        )
        return {"stopReason": "end_turn"}

    fake.prompt = prompt_with_model  # type: ignore[method-assign]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("hi")
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    modes = [e for e in session.event_log.events_from(1) if e.type == "grok_mode"]
    assert modes
    last = modes[-1]
    assert isinstance(last, GrokMode)
    assert last.model == "grok-4.6"


@pytest.mark.asyncio
async def test_grok_loop_followup_interrupts_in_flight_prompt() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()
    entered = asyncio.Event()
    released = asyncio.Event()

    async def hang(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        fake.prompts.append((session_id, text))
        entered.set()
        await released.wait()
        released.clear()
        return {"stopReason": "cancelled"}

    orig_cancel = fake.cancel

    async def cancel_and_release(session_id: str) -> None:
        await orig_cancel(session_id)
        released.set()

    fake.prompt = hang  # type: ignore[method-assign]
    fake.cancel = cancel_and_release  # type: ignore[method-assign]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("first")
    await asyncio.wait_for(entered.wait(), timeout=2)
    entered.clear()
    await session.add_user_message("stop that")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await session.cancel()
    released.set()
    await asyncio.wait_for(task, timeout=2)
    assert fake.cancelled
    assert [text for _, text in fake.prompts] == ["first", "stop that"]
    users = [e for e in session.event_log.events_from(1) if isinstance(e, UserTurn)]
    assert [u.content for u in users] == ["first", "stop that"]


def test_visible_delta_separates_sentences_not_tokens() -> None:
    assert _visible_delta("today?", "I'll load", after_tool=False) == " I'll load"
    assert _visible_delta("today? ", "I'll load", after_tool=False) == "I'll load"
    assert _visible_delta("Hel", "lo", after_tool=False) == "lo"
    assert _visible_delta("done.", "Next", after_tool=True) == "\n\nNext"
    assert _visible_delta("hi", '"is_background": false', after_tool=False) == ""


@pytest.mark.asyncio
async def test_grok_loop_engine_error_is_not_assistant_prose() -> None:
    session = Session("/tmp")
    await session.set_state("running")
    fake = FakeAcp()

    async def boom(
        session_id: str, text: str, images: list[tuple[str, bytes, str]] | None = None
    ) -> dict[str, Any]:
        raise GrokEngineError("already_started", "ACP client already started")

    fake.prompt = boom  # type: ignore[method-assign]
    task = asyncio.create_task(grok_loop(session, client=fake, binary=""))  # type: ignore[arg-type]
    await session.add_user_message("hi")
    for _ in range(50):
        if any(isinstance(e, TurnComplete) for e in session.event_log.events_from(1)):
            break
        await asyncio.sleep(0.02)
    await session.cancel()
    await asyncio.wait_for(task, timeout=2)
    deltas = [e for e in session.event_log.events_from(1) if isinstance(e, AssistantDelta)]
    assert deltas == []
    errors = [e for e in session.event_log.events_from(1) if isinstance(e, Error)]
    assert errors[0].code == "already_started"
    failed = [e for e in session.event_log.events_from(1) if isinstance(e, TurnComplete)]
    assert failed[0].failed is True
