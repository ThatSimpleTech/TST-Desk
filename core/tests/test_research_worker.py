"""The read-only research worker (TD-5117).

``delegate`` with ``kind: research`` runs a child on the worker tier that
holds only fs_read, fs_list, fs_grep, web_search and web_fetch. The
parent gets a summary capped in tokens; the child's transcript rides on
the ``delegate`` result event for the window and never reaches the
parent model. Plan mode sends delegation to this worker.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_delegate import _bind_runtime, _parent_dispatcher, _workspace
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.context.tokens import HeuristicTokenCounter
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.protocol import WorkerStep
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import create_registry
from tstd.tools.delegate import (
    RESEARCH_CHILD_TOOLS,
    SUMMARY_TOKEN_CAP,
    handle_delegate,
    run_worker_child,
    worker_child_registry,
)
from tstd.tools.delegate_child import (
    CUT_NOTE,
    STEP_CHAR_CAP,
    TRANSCRIPT_CHAR_BUDGET,
    TranscriptRecorder,
)
from tstd.tools.registry import Tool
from tstd.tools.results import HandlerOutput

READ_ONLY = {"fs_read", "fs_list", "fs_grep", "web_search", "web_fetch"}
#: In a workspace file, never in the worker's summary: if the parent model
#: ever sees it, the transcript leaked into the parent context.
MARKER = "zebra-quokka-7731"


def _offered(mock: MockProvider, model: str) -> list[set[str]]:
    return [
        {t.function.name for t in (call.tools or []) if t.function is not None}
        for call in mock.calls
        if call.model == model
    ]


def _brain_text(mock: MockProvider) -> str:
    return json.dumps(
        [
            [m.content if isinstance(m.content, str) else str(m.content) for m in call.messages]
            for call in mock.calls
            if call.model == "test-brain"
        ]
    )


# ── Tools ───────────────────────────────────────────────────────────────


def test_research_child_registry_is_read_only() -> None:
    parent = create_registry()
    # A plugin or MCP tool in the parent never reaches the research child.
    parent.register(
        Tool(
            name="github__create_issue",
            description="x",
            parameters={"type": "object", "properties": {}},
        )
    )
    child = worker_child_registry(parent, "research")
    assert {t.name for t in child.list_tools()} == READ_ONLY
    assert set(RESEARCH_CHILD_TOOLS) == READ_ONLY


def test_delegate_schema_offers_the_two_kinds() -> None:
    tool = create_registry().require("delegate")
    kind = tool.parameters["properties"]["kind"]
    assert kind["enum"] == ["worker", "research"]
    assert "kind" not in tool.parameters["required"]


async def test_unknown_kind_fails_schema_validation(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    dispatcher = _parent_dispatcher(ws)
    session = Session(str(ws))
    result = await dispatcher.dispatch("c1", "delegate", {"task": "x", "kind": "admin"}, session)
    assert result.status == "error"
    assert "kind" in result.output


async def test_research_child_runs_on_worker_tier_with_read_only_tools(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "notes.md"}'
                ),
                Script(kind="text", content="notes.md says hello from notes."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="What is in notes.md?", kind="research")

    assert "hello from notes" in summary
    offered = _offered(mock, "test-worker")
    assert offered and all(names == READ_ONLY for names in offered)
    assert {c.model for c in mock.calls} == {"test-worker"}
    first = mock.calls[0].messages[1].content
    assert isinstance(first, str) and "read-only research worker" in first


async def test_research_child_cannot_write_or_run_shell(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call",
                    tool_name="fs_write",
                    tool_arguments='{"path": "out.md", "content": "written"}',
                ),
                Script(
                    kind="tool_call",
                    tool_name="shell",
                    tool_arguments='{"command": "touch ran.txt"}',
                ),
                Script(kind="text", content="I could not write."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)
    steps: list[WorkerStep] = []

    await run_worker_child(parent=parent, task="Write out.md", kind="research", transcript=steps)

    assert not (ws / "out.md").exists()
    assert not (ws / "ran.txt").exists()
    results = [s for s in steps if s.kind == "tool_result"]
    assert [s.status for s in results] == ["error", "error"]
    assert all("Unknown tool" in s.text for s in results)


async def test_summary_is_capped_in_tokens_of_the_worker_tier(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    long_reply = "finding " * (SUMMARY_TOKEN_CAP * 2)
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content=long_reply)]})
    _bind_runtime(parent, dispatcher, mock)

    summary = await run_worker_child(parent=parent, task="Explore", kind="research")

    # make_config's slugs are unknown to tiktoken, so the worker tier's
    # counter is the heuristic, the same one this checks with.
    assert HeuristicTokenCounter().count(summary).count <= SUMMARY_TOKEN_CAP
    assert summary.endswith(f"capped at {SUMMARY_TOKEN_CAP} tokens]")


# ── Transcript ──────────────────────────────────────────────────────────


async def test_handler_returns_summary_text_and_transcript(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    (ws / "animals.md").write_text(f"pair: {MARKER}\n", encoding="utf-8")
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-worker": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "animals.md"}'
                ),
                Script(kind="text", content="animals.md names one animal pair."),
            ]
        }
    )
    _bind_runtime(parent, dispatcher, mock)

    out = await handle_delegate(parent, task="What pair?", kind="research")

    assert isinstance(out, HandlerOutput)
    assert MARKER not in out.text
    assert [s.kind for s in out.transcript] == ["task", "tool_call", "tool_result", "reply"]
    assert out.transcript[0].text == "What pair?"
    assert out.transcript[1].tool == "fs_read"
    assert MARKER in out.transcript[2].text
    assert out.transcript[2].status == "success"


async def test_parent_loop_keeps_transcript_out_of_context_and_on_the_event(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    (ws / "animals.md").write_text(f"pair: {MARKER}\n", encoding="utf-8")
    session = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="delegate",
                    tool_arguments='{"task": "Which pair is in animals.md?", "kind": "research"}',
                ),
                Script(kind="stream", content="Done."),
            ],
            "test-worker": [
                Script(
                    kind="tool_call", tool_name="fs_read", tool_arguments='{"path": "animals.md"}'
                ),
                Script(kind="text", content="animals.md names one animal pair."),
            ],
        }
    )
    runner = await start_loop(
        session,
        TierRouter(),
        mock,
        make_config(),
        registry=dispatcher.registry,
        dispatcher=dispatcher,
    )
    try:
        await session.add_user_message("Research the pair")
        await wait_for_turn(session, 1)
    finally:
        await runner.cancel()

    results = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    event = results[0]
    assert event.output == "animals.md names one animal pair."
    assert any(MARKER in step.text for step in event.transcript)
    # The brain saw the summary as the delegate result, never the read.
    brain = _brain_text(mock)
    assert "animals.md names one animal pair." in brain
    assert MARKER not in brain
    assert _offered(mock, "test-worker")[0] == READ_ONLY


def test_recorder_caps_each_step_and_the_whole_transcript() -> None:
    steps: list[WorkerStep] = []
    record = TranscriptRecorder(steps)
    record.add("tool_result", "y" * (STEP_CHAR_CAP + 10), tool="fs_read", status="success")
    assert len(steps[0].text) == STEP_CHAR_CAP
    assert steps[0].truncated is True

    for _ in range(TRANSCRIPT_CHAR_BUDGET // STEP_CHAR_CAP + 5):
        record.add("reply", "z" * STEP_CHAR_CAP)
    assert steps[-1].kind == "note"
    assert steps[-1].text == CUT_NOTE
    assert sum(len(s.text) for s in steps) <= TRANSCRIPT_CHAR_BUDGET + STEP_CHAR_CAP + len(CUT_NOTE)
    assert [s.text for s in steps].count(CUT_NOTE) == 1


def test_recorder_without_a_sink_records_nothing() -> None:
    TranscriptRecorder(None).add("reply", "x")


# ── Plan mode ───────────────────────────────────────────────────────────


async def test_plan_mode_defaults_delegate_to_research(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    parent.router = TierRouter()
    parent.router.set_plan(True)
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content="explored")]})
    _bind_runtime(parent, dispatcher, mock)

    out = await handle_delegate(parent, task="Map the repo")

    assert out.text == "explored"
    assert _offered(mock, "test-worker") == [READ_ONLY]


async def test_plan_mode_refuses_a_writing_worker(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    session = Session(str(ws))
    session.router = TierRouter()
    session.router.set_plan(True)
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(default=Script(kind="text", content="should not run"))
    _bind_runtime(session, dispatcher, mock)

    result = await dispatcher.dispatch(
        "c1", "delegate", {"task": "Edit notes.md", "kind": "worker"}, session
    )

    assert result.status == "error"
    assert result.error_code == "delegate_plan_mode"
    assert "research" in result.output
    assert mock.calls == []


async def test_outside_plan_mode_the_default_is_still_the_writing_worker(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    parent.router = TierRouter()
    dispatcher = _parent_dispatcher(ws)
    mock = MockProvider(sequences={"test-worker": [Script(kind="text", content="done")]})
    _bind_runtime(parent, dispatcher, mock)

    await handle_delegate(parent, task="Edit notes.md")

    offered = _offered(mock, "test-worker")
    assert offered and "fs_write" in offered[0] and "shell" in offered[0]
