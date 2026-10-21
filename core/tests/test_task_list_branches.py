"""TD-5161: the task list follows the branch, and an attach shows it.

An edit (``fork_from``) or a sibling switch (``set_branch``) changes the
conversation. The list must follow: the newest ``task_list`` call the
branch made, or none. Before TD-5161 the abandoned branch's list stayed,
and the next compaction told the model to keep working from it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tstd.compaction import compact_on_request
from tstd.compaction_text import compose
from tstd.context.tokens import HeuristicTokenCounter
from tstd.mock import MockProvider, Script
from tstd.protocol import TaskList, TaskListItem
from tstd.provider import ChatMessage, FunctionCall, ToolCall, content_as_text
from tstd.session import Session
from tstd.task_list import carry_text

PLAN = [
    TaskListItem(content="Read the failing test", status="done"),
    TaskListItem(content="Fix the date parser", status="in_progress"),
]
EARLY = [TaskListItem(content="Look around", status="pending")]


def _call(call_id: str, items: list[TaskListItem], saved: bool = True) -> list[ChatMessage]:
    arguments = json.dumps({"tasks": [i.model_dump(include={"content", "status"}) for i in items]})
    call = ToolCall(id=call_id, function=FunctionCall(name="task_list", arguments=arguments))
    result = "Task list saved: 1 of 2 done." if saved else "Every task needs some text."
    return [
        ChatMessage(role="assistant", content=None, tool_calls=[call]),
        ChatMessage(role="tool", content=result, tool_call_id=call_id),
    ]


async def _session(conversation: list[ChatMessage], items: list[TaskListItem]) -> Session:
    session = Session("/tmp/ws")
    session.conversation[:] = conversation
    session.task_list = list(items)
    return session


async def test_an_edit_before_the_list_drops_it() -> None:
    """The list was made after the edited turn: the new branch has none."""
    session = await _session([ChatMessage(role="user", content="fix it"), *_call("c1", PLAN)], PLAN)
    assert not isinstance(await session.fork_from(0, "Actually, do something else"), str)
    assert session.task_list == []
    logged = [e for e in session.event_log.all_events if isinstance(e, TaskList)]
    assert logged[-1].items == []  # the Plan pane and the strip clear too
    summary = compact_on_request(
        session.conversation, HeuristicTokenCounter(), carry=carry_text(session.task_list)
    )
    assert summary is None or carry_text(PLAN) not in content_as_text(summary[0].content)


async def test_an_edit_after_the_list_keeps_the_branchs_own() -> None:
    session = await _session(
        [
            ChatMessage(role="user", content="plan it"),
            *_call("c1", EARLY),
            ChatMessage(role="user", content="now fix it"),
            *_call("c2", PLAN),
        ],
        PLAN,
    )
    assert not isinstance(await session.fork_from(1, "now test it"), str)
    assert session.task_list == EARLY


async def test_a_refused_call_does_not_count() -> None:
    session = await _session(
        [
            ChatMessage(role="user", content="plan it"),
            *_call("c1", EARLY),
            *_call("c2", PLAN, saved=False),
            ChatMessage(role="user", content="go on"),
        ],
        PLAN,
    )
    assert not isinstance(await session.fork_from(1, "go on, carefully"), str)
    assert session.task_list == EARLY


async def test_a_branch_after_a_summary_keeps_the_list() -> None:
    """A summary carried a list this module does not parse: keep it."""
    text = compose("[Earlier conversation compacted] earlier")
    summary = ChatMessage(role="system", content=text)
    session = await _session([summary, ChatMessage(role="user", content="go")], PLAN)
    assert not isinstance(await session.fork_from(0, "go, differently"), str)
    assert session.task_list == PLAN


async def test_a_sibling_switch_restores_that_siblings_list() -> None:
    session = await _session([ChatMessage(role="user", content="fix it"), *_call("c1", PLAN)], PLAN)
    await session.fork_from(0, "Actually, do something else")
    assert session.task_list == []
    session.conversation.append(ChatMessage(role="user", content="Actually, do something else"))
    session.snapshot_branches()
    session._open_turns = 0
    assert not isinstance(await session.set_branch(0, 0), str)
    assert session.task_list == PLAN


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def test_an_attach_logs_a_list_the_window_lost(tmp_path: Path, fleet: Fleet) -> None:
    """The attach handler itself re-logs the list, not only the helper."""
    work = tmp_path / "ws"
    work.mkdir()
    daemon = await fleet.start(tmp_path / "d", provider=MockProvider(default=Script(kind="stream")))
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(work)}, "session_state")
        sid = str(opened["session_id"])
        session = daemon.session_registry.get(sid)
        assert session is not None
        session.task_list = list(PLAN)  # no task_list event in the window
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        frame = await recv_until(ws, lambda f: f.get("type") == "task_list")
        assert [i["content"] for i in frame["items"]] == [i.content for i in PLAN]
    await fleet.stop(daemon)
