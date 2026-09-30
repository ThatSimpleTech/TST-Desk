"""Schedule this chat (TD-3816): fill a draft from a session, do not save a job."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tstd.daemon import Daemon
from tstd.protocol import AssistantDelta, SessionJobSource, UserTurn, parse_daemon_event
from tstd.provider import ChatMessage
from tstd.scheduler.chat_source import session_schedule_source
from tstd.scheduler.store import list_jobs
from tstd.session_persist import SessionPersist

_PLANTED = "planted inbox phrase that must not leak"


@pytest.fixture
async def daemon(tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = Daemon(data_dir=tmp_path / "data")
    try:
        yield instance
    finally:
        await instance._shutdown()


async def _handle(daemon: Daemon, payload: dict[str, object]) -> dict[str, object]:
    raw = await daemon._handle_message(json.dumps(payload), None)
    assert raw is not None
    parsed: dict[str, object] = json.loads(raw)
    return parsed


def _source(raw: dict[str, object]) -> SessionJobSource:
    event = parse_daemon_event(json.dumps(raw))
    assert isinstance(event, SessionJobSource)
    return event


def _turn(session_id: str, turn_id: str, content: str, seq: int) -> UserTurn:
    return UserTurn(session_id=session_id, turn_id=turn_id, content=content, seq=seq)


async def test_live_pin_wins_and_skips_a_blank_first_turn(daemon: Daemon, tmp_path: Path) -> None:
    live = tmp_path / "live"
    stored = tmp_path / "stored"
    live.mkdir()
    stored.mkdir()
    sid = "sess-live"
    await daemon._session_store.upsert(sid, str(stored), "idle", engine="native", preset="budget")
    session = await daemon.session_registry.restore(sid, str(live), "idle")
    session.preset = "vllm"
    session.engine = "grok"
    await session.event_log.add(_turn(sid, "t0", "   ", 1))
    await session.event_log.add(_turn(sid, "t1", "first real", 1))
    await session.event_log.add(_turn(sid, "t2", "second", 1))

    source = _source(await _handle(daemon, {"type": "get_session_job_source", "session_id": sid}))
    assert source.seq == 1
    assert source.session_id == sid
    assert source.workspace == str(live)
    assert source.preset == "vllm"
    assert source.engine == "grok"
    assert source.instruction == "first real"
    assert list_jobs(daemon.data_dir) == []
    assert daemon.session_registry.count == 1


async def test_disk_user_turn_does_not_invent_an_engine(daemon: Daemon) -> None:
    sid = "sess-disk"
    workspace = "/tmp/reports"
    await daemon._session_store.upsert(sid, workspace, "idle", engine=None, preset="budget")
    persist = SessionPersist(daemon.data_dir)
    folder = persist.dir_for(sid)
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text(
        "\n".join(
            [
                "{not json",
                _turn(sid, "t0", "   ", 1).model_dump_json(),
                _turn(sid, "t1", "from disk", 2).model_dump_json(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source = _source(await _handle(daemon, {"type": "get_session_job_source", "session_id": sid}))
    assert source.workspace == workspace
    assert source.preset == "budget"
    assert source.engine is None
    assert source.instruction == "from disk"
    assert daemon.session_registry.count == 0
    assert list_jobs(daemon.data_dir) == []


async def test_conversation_file_when_the_log_has_no_user_turn(daemon: Daemon) -> None:
    sid = "sess-convo"
    workspace = "/tmp/reports"
    await daemon._session_store.upsert(sid, workspace, "idle", preset="budget")
    persist = SessionPersist(daemon.data_dir)
    persist.append_event(sid, AssistantDelta(session_id=sid, delta="thinking", seq=1))
    folder = persist.dir_for(sid)
    (folder / "conversation.json").write_text(
        json.dumps(
            [
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": [{"type": "text", "text": "skip me"}]},
                {"role": "user", "content": "   "},
                {"role": "user", "content": "from convo"},
            ]
        ),
        encoding="utf-8",
    )
    source = _source(await _handle(daemon, {"type": "get_session_job_source", "session_id": sid}))
    assert source.instruction == "from convo"
    assert source.engine is None


async def test_live_conversation_when_the_log_has_no_user_turn(
    daemon: Daemon, tmp_path: Path
) -> None:
    workspace = tmp_path / "chat"
    workspace.mkdir()
    session = await daemon.session_registry.create(str(workspace))
    session.preset = "vllm"
    session.engine = "native"
    session.conversation.append(ChatMessage(role="assistant", content="hello"))
    session.conversation.append(
        ChatMessage(role="user", content=[{"type": "text", "text": "skip me"}])
    )
    session.conversation.append(ChatMessage(role="user", content="   "))
    session.conversation.append(ChatMessage(role="user", content="from live"))
    source = _source(
        await _handle(daemon, {"type": "get_session_job_source", "session_id": session.id})
    )
    assert source.workspace == str(workspace)
    assert source.preset == "vllm"
    assert source.engine == "native"
    assert source.instruction == "from live"
    assert list_jobs(daemon.data_dir) == []


async def test_missing_session_and_empty_instruction(daemon: Daemon) -> None:
    missing = await _handle(daemon, {"type": "get_session_job_source", "session_id": "missing"})
    assert missing["type"] == "error"
    assert missing["code"] == "session_not_found"
    assert list_jobs(daemon.data_dir) == []

    sid = "sess-empty"
    await daemon._session_store.upsert(sid, "/tmp/reports", "idle", preset="budget")
    source = _source(await _handle(daemon, {"type": "get_session_job_source", "session_id": sid}))
    assert source.instruction == ""
    assert source.preset == "budget"


def test_path_escape_does_not_read_a_planted_log(tmp_path: Path) -> None:
    planted = tmp_path / "secrets"
    planted.mkdir()
    (planted / "events.jsonl").write_text(
        _turn("x", "t1", _PLANTED, 1).model_dump_json() + "\n",
        encoding="utf-8",
    )
    source = session_schedule_source(
        tmp_path,
        "../secrets",
        stored_workspace="/tmp/reports",
        stored_preset="budget",
        stored_engine=None,
        live=None,
    )
    assert source is not None
    assert source.instruction == ""
    assert _PLANTED not in source.instruction
    assert (
        session_schedule_source(
            tmp_path,
            "../secrets",
            stored_workspace=None,
            stored_preset="",
            stored_engine=None,
            live=None,
        )
        is None
    )


async def test_daemon_refuses_a_path_escape_id(daemon: Daemon) -> None:
    planted = daemon.data_dir / "secrets"
    planted.mkdir()
    (planted / "events.jsonl").write_text(
        _turn("x", "t1", _PLANTED, 1).model_dump_json() + "\n",
        encoding="utf-8",
    )
    missing = await _handle(daemon, {"type": "get_session_job_source", "session_id": "../secrets"})
    assert missing["code"] == "session_not_found"
    assert _PLANTED not in json.dumps(missing)

    await daemon._session_store.upsert("../secrets", "/tmp/reports", "idle", preset="budget")
    source = _source(
        await _handle(daemon, {"type": "get_session_job_source", "session_id": "../secrets"})
    )
    assert source.instruction == ""
    assert source.workspace == "/tmp/reports"
    assert _PLANTED not in source.instruction
