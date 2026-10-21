"""TD-5129: the rail searches what sessions said; the model recalls them.

A real in-process daemon with the mock provider: turns write the index as
they log, ``search_sessions`` answers the rail, a restart catches up and a
delete forgets. ``session_search`` goes through the real dispatcher and
classifier, scoped to the caller's workspace and citing session and turn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_artifacts import _open, _shutdown
from tests.test_dispatch import make_classifier
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.autonomy.classifier import RULE_TABLE, DecisionRequest
from tstd.daemon import Daemon
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import TurnComplete
from tstd.session import Session
from tstd.session_index import SessionIndex
from tstd.session_store import SessionRecord
from tstd.tools import ToolDispatcher
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import create_registry
from tstd.tools.session_search import SESSION_SEARCH_NAME, SessionRecall

REPLY = "The date parser now keeps the zoneinfo offset."


def _daemon(data_dir: Path) -> Daemon:
    return Daemon(
        data_dir=data_dir, provider=MockProvider(default=Script(kind="stream", content=REPLY))
    )


async def _turn(daemon: Daemon, sid: str, text: str) -> None:
    session = daemon.session_registry.get(sid)
    assert session is not None
    before = sum(isinstance(e, TurnComplete) for e in session.event_log.all_events)
    reply = await daemon._handle_message(
        json.dumps({"type": "user_message", "session_id": sid, "content": text}), None
    )
    assert reply is None or json.loads(reply).get("type") != "error", reply
    for _ in range(400):
        done = sum(isinstance(e, TurnComplete) for e in session.event_log.all_events)
        if done > before:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("turn did not complete")


async def _search(daemon: Daemon, query: str) -> dict[str, Any]:
    reply = await daemon._handle_message(
        json.dumps({"type": "search_sessions", "query": query}), None
    )
    assert reply is not None
    frame: dict[str, Any] = json.loads(reply)
    assert frame["type"] == "session_search_results"
    assert frame["query"] == query
    return frame


async def _stop_all(daemon: Daemon) -> None:
    for sess in await daemon.session_registry.list_sessions():
        runner = daemon.session_registry.get_runner(sess.id)
        if runner is not None:
            await runner.cancel()
    await daemon._shutdown()


async def test_the_rail_finds_message_text_as_sessions_write(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    daemon = _daemon(tmp_path / "data")
    sid = await _open(daemon, ws)
    try:
        assert (await _search(daemon, "zoneinfo"))["hits"] == []
        await _turn(daemon, sid, "Why does the quarterly invoice export fail?")
        found = await _search(daemon, "invoice export")
        assert found["available"] is True
        assert [(h["session_id"], h["turn"], h["role"]) for h in found["hits"]] == [
            (sid, 1, "user")
        ]
        reply = await _search(daemon, "zoneinfo")
        assert [(h["turn"], h["role"]) for h in reply["hits"]] == [(1, "assistant")]
        assert "zoneinfo" in reply["hits"][0]["snippet"]
    finally:
        await _shutdown(daemon, sid)


async def test_a_restart_keeps_the_index_and_a_delete_empties_it(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    data_dir = tmp_path / "data"
    daemon = _daemon(data_dir)
    sid = await _open(daemon, ws)
    await _turn(daemon, sid, "remember the walrus migration")
    await _shutdown(daemon, sid)

    # A log written before the index existed: drop the file, restore rebuilds.
    (data_dir / "session_index.db").unlink()
    for extra in ("-wal", "-shm"):
        (data_dir / f"session_index.db{extra}").unlink(missing_ok=True)
    again = _daemon(data_dir)
    await again._restore_sessions()
    try:
        [hit] = (await _search(again, "walrus"))["hits"]
        assert (hit["session_id"], hit["turn"]) == (sid, 1)
        reply = await again._handle_message(
            json.dumps({"type": "delete_session", "session_id": sid}), None
        )
        assert reply is not None and json.loads(reply)["type"] == "session_list"
        assert (await _search(again, "walrus"))["hits"] == []
    finally:
        await _stop_all(again)


async def test_an_unopenable_index_says_so_instead_of_failing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "session_index.db").mkdir()
    daemon = _daemon(data_dir)
    try:
        frame = await _search(daemon, "anything")
        assert frame == {
            "type": "session_search_results",
            "seq": 1,
            "query": "anything",
            "hits": [],
            "available": False,
        }
    finally:
        await daemon._shutdown()


async def test_session_search_recalls_earlier_sessions_in_the_same_workspace(
    tmp_path: Path,
) -> None:
    ws, other = tmp_path / "ws", tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    daemon = _daemon(tmp_path / "data")
    earlier = await _open(daemon, ws)
    elsewhere = await _open(daemon, other)
    now = await _open(daemon, ws)
    try:
        await daemon._session_store.set_title(earlier, "Parser fix")
        await _turn(daemon, earlier, "the walrus parser drops the offset")
        await _turn(daemon, elsewhere, "the walrus parser in another project")
        await _turn(daemon, now, "walrus again")

        recall = SessionRecall(daemon._session_index, daemon._session_store.records)
        session = daemon.session_registry.get(now)
        assert session is not None
        dispatcher = ToolDispatcher(create_registry(), classifier=make_classifier(str(ws)))
        dispatcher.policy = PolicyConfig()
        asked: list[Any] = []

        async def _approve(*args: Any) -> ApprovalOutcome:
            asked.append(args)
            return ApprovalOutcome(True)

        dispatcher.approval_handler = _approve
        register_builtin_handlers(dispatcher, session_recall=recall)
        result = await dispatcher.dispatch(
            "c1", SESSION_SEARCH_NAME, {"query": "walrus parser"}, session
        )
        assert result.status == "success", result
        assert asked == [], "Class A: no approval card"
        assert f'[session {earlier[:8]} "Parser fix", turn 1,' in result.output
        assert elsewhere[:8] not in result.output
        assert now[:8] not in result.output
        assert "drops the offset" in result.output
    finally:
        for sid in (earlier, elsewhere):
            runner = daemon.session_registry.get_runner(sid)
            if runner is not None:
                await runner.cancel()
        await _shutdown(daemon, now)


async def test_session_search_says_so_when_nothing_matches(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path)
    rows = [SessionRecord(session_id="me", workspace_path="/w", state="idle")]
    recall = SessionRecall(index, lambda: rows)
    dispatcher = ToolDispatcher(create_registry(), classifier=make_classifier("/w"))
    dispatcher.policy = PolicyConfig()
    register_builtin_handlers(dispatcher, session_recall=recall)
    session = Session("/w")
    session.id = "me"
    try:
        result = await dispatcher.dispatch("c1", SESSION_SEARCH_NAME, {"query": "x"}, session)
        assert "No earlier session in this workspace mentions 'x'" in result.output
    finally:
        index.close()


async def test_without_an_index_the_tool_refuses(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(create_registry(), classifier=make_classifier(str(tmp_path)))
    dispatcher.policy = PolicyConfig()
    register_builtin_handlers(dispatcher)
    result = await dispatcher.dispatch(
        "c1", SESSION_SEARCH_NAME, {"query": "x"}, Session(str(tmp_path))
    )
    assert result.status != "success"
    assert "session_search_unavailable" in json.dumps(result.__dict__, default=str)


# ── The classifier ───────────────────────────────────────────────────────


def _classify(request: DecisionRequest) -> Any:
    return DecisionClassifier(Boundary(workspace_root=Path("/work/proj"))).classify(request)


def test_the_builtin_is_class_a() -> None:
    decision = _classify(DecisionRequest(tool_name=SESSION_SEARCH_NAME))
    assert decision.decision_class is DecisionClass.A
    assert decision.rule is not None and decision.rule.id == "session-search"


@pytest.mark.parametrize(
    "request_",
    [
        DecisionRequest(tool_name=SESSION_SEARCH_NAME, provenance="mcp:srv"),
        DecisionRequest(tool_name=SESSION_SEARCH_NAME, provenance="plugin:dist"),
        DecisionRequest(tool_name=SESSION_SEARCH_NAME, side_effect_class="ask"),
        DecisionRequest(tool_name=SESSION_SEARCH_NAME, reads=(Path("/work/proj/a"),)),
        DecisionRequest(tool_name=SESSION_SEARCH_NAME, hosts=("example.com",)),
    ],
)
def test_only_the_builtin_shape_matches(request_: DecisionRequest) -> None:
    decision = _classify(request_)
    assert decision.rule is None or decision.rule.id != "session-search"


def test_rule_order_pinned() -> None:
    ids = [rule.id for rule in RULE_TABLE]
    at = ids.index("session-search")
    assert ids.index("cap-exceeded") < at
    assert ids.index("side-effect-never") < at
    assert at < ids.index("side-effect-ask-floor")


def test_the_tool_is_offered_and_read_only() -> None:
    tool = create_registry().get(SESSION_SEARCH_NAME)
    assert tool is not None
    assert tool.side_effect_class == "auto"
    assert tool.mutates is False
