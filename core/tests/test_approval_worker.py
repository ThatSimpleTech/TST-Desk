"""An approval card says which child is asking (TD-5268).

A ``delegate`` or ``workflow`` child's call parks on its parent's session.
The card used to read as if the session's own agent had asked. These cases
run a real child against the session's own approval path and read the
event a window would get.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.test_delegate import _bind_runtime, _parent_dispatcher, _workspace
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig
from tstd.protocol import ApprovalRequest, ApprovalWorker, parse_daemon_event
from tstd.session import Session
from tstd.tools.delegate import ChildTask, run_child_task, worker_summary

SHELL = Script(kind="tool_call", tool_name="shell", tool_arguments='{"command": "make test"}')
DONE = Script(kind="text", content="ran it")


async def _asking_parent(tmp_path: Path) -> tuple[Session, Any]:
    """A parent whose dispatcher asks through the session, as the daemon wires it."""
    ws = _workspace(tmp_path)
    parent = Session(str(ws))
    dispatcher = _parent_dispatcher(ws)
    dispatcher.policy = PolicyConfig()
    parent.policy = dispatcher.policy
    dispatcher.approval_handler = parent.request_approval
    # A card parks a session that is in a turn.
    await parent.set_state("running")
    return parent, dispatcher


async def _first_card(parent: Session) -> ApprovalRequest:
    for _ in range(300):
        cards = [e for e in parent.event_log.all_events if isinstance(e, ApprovalRequest)]
        if cards:
            return cards[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval card was raised")


async def _run_and_approve(parent: Session, spec: ChildTask) -> ApprovalRequest:
    running = asyncio.create_task(run_child_task(parent, spec))
    try:
        card = await _first_card(parent)
        parent.resolve_approval(card.tool_call_id, approved=True)
        await asyncio.wait_for(running, 10)
    finally:
        if not running.done():
            running.cancel()
    return card


class TestTheCard:
    async def test_a_delegate_childs_card_names_the_child(self, tmp_path: Path) -> None:
        parent, dispatcher = await _asking_parent(tmp_path)
        mock = MockProvider(sequences={"test-worker": [SHELL, DONE]})
        _bind_runtime(parent, dispatcher, mock)
        spec = ChildTask(task="Run the test suite\nand report what fails", call_id="call_ab12")

        card = await _run_and_approve(parent, spec)

        assert card.worker is not None
        assert card.worker.via == "delegate" and card.worker.kind == "worker"
        assert card.worker.id == "call_ab12" and card.worker.parent_call_id == "call_ab12"
        assert card.worker.index is None and card.worker.agent is None
        assert card.worker.label == "Run the test suite and report what fails"
        assert card.worker.tier == "worker"
        assert card.worker.worktree_branch is None
        # The child's call id still starts with the child's own.
        assert card.tool_call_id.startswith("call_ab12/")
        # Every surface that only shows the summary says who asked.
        assert card.summary.startswith("Worker: ")
        assert "make test" in card.summary

    async def test_a_workflow_tasks_card_has_its_number_and_label(self, tmp_path: Path) -> None:
        parent, dispatcher = await _asking_parent(tmp_path)
        mock = MockProvider(sequences={"test-worker": [SHELL, DONE]})
        _bind_runtime(parent, dispatcher, mock)
        spec = ChildTask(
            task="Check the API module",
            call_id="call_wf.2",
            parent_call_id="call_wf",
            index=2,
            label="API module",
        )

        card = await _run_and_approve(parent, spec)

        assert card.worker is not None
        assert card.worker.via == "workflow"
        assert card.worker.index == 2 and card.worker.label == "API module"
        assert card.worker.parent_call_id == "call_wf" and card.worker.id == "call_wf.2"
        assert card.summary.startswith("Worker 2: ")

    async def test_the_sessions_own_card_is_as_it_was(self, tmp_path: Path) -> None:
        parent, dispatcher = await _asking_parent(tmp_path)
        tool = dispatcher.registry.require("shell")
        from tstd.autonomy.classifier import DecisionClass

        asking = asyncio.create_task(
            parent.request_approval(
                "call_own", tool, {"command": "make test"}, DecisionClass.B, "Run `make test`", "B"
            )
        )
        card = await _first_card(parent)
        parent.resolve_approval("call_own", approved=True)
        await asyncio.wait_for(asking, 5)
        assert card.worker is None
        assert card.summary == "Run `make test`"

    async def test_a_long_task_is_cut_and_stays_one_line(self, tmp_path: Path) -> None:
        parent, dispatcher = await _asking_parent(tmp_path)
        mock = MockProvider(sequences={"test-worker": [SHELL, DONE]})
        _bind_runtime(parent, dispatcher, mock)
        card = await _run_and_approve(parent, ChildTask(task="word " * 200, call_id="c1"))
        assert card.worker is not None
        assert len(card.worker.label) <= 60 and "\n" not in card.worker.label
        assert len(card.worker.task) <= 300 and card.worker.task.endswith("…")

    async def test_the_event_crosses_the_wire_and_an_older_one_still_parses(
        self, tmp_path: Path
    ) -> None:
        parent, dispatcher = await _asking_parent(tmp_path)
        mock = MockProvider(sequences={"test-worker": [SHELL, DONE]})
        _bind_runtime(parent, dispatcher, mock)
        card = await _run_and_approve(parent, ChildTask(task="t", call_id="c1"))
        back = parse_daemon_event(card.model_dump_json())
        assert isinstance(back, ApprovalRequest) and back.worker == card.worker
        old = card.model_dump(mode="json")
        del old["worker"]
        import json

        again = parse_daemon_event(json.dumps(old))
        assert isinstance(again, ApprovalRequest) and again.worker is None


class TestTheSummary:
    @pytest.mark.parametrize(
        ("worker", "expected"),
        [
            ({"kind": "worker"}, "Worker: Run `x`"),
            ({"kind": "research"}, "Worker (research): Run `x`"),
            ({"kind": "worker", "index": 3}, "Worker 3: Run `x`"),
            ({"kind": "research", "index": 3}, "Worker 3 (research): Run `x`"),
            ({"kind": "worker", "agent": "db-migrator"}, 'Worker "db-migrator": Run `x`'),
            ({"kind": "check", "index": 2}, "Check 2: Run `x`"),
            (
                {"kind": "worker", "index": 1, "worktree_branch": "tst/wt/abc"},
                "Worker 1: Run `x` in its worktree",
            ),
        ],
    )
    def test_who_is_asking_comes_first(self, worker: dict[str, Any], expected: str) -> None:
        full = ApprovalWorker(id="c", parent_call_id="c", via="workflow", **worker)
        assert worker_summary(full, "Run `x`") == expected


class TestAlwaysAllow:
    async def _proposal(self, tmp_path: Path, worker: ApprovalWorker | None) -> str | None:
        parent, dispatcher = await _asking_parent(tmp_path)

        async def writable() -> bool:
            return True

        parent._config_writable = writable  # type: ignore[method-assign]
        tool = dispatcher.registry.require("fs_write")
        from tstd.autonomy.classifier import DecisionClass

        asking = asyncio.create_task(
            parent.request_approval(
                "c1",
                tool,
                {"path": "notes.md", "content": "x"},
                DecisionClass.B,
                "Write notes.md",
                "B",
                worker=worker,
            )
        )
        card = await _first_card(parent)
        parent.resolve_approval("c1", approved=False)
        await asyncio.wait_for(asking, 5)
        return card.proposed_always_allow

    def _worker(self, **over: Any) -> ApprovalWorker:
        return ApprovalWorker(id="c", parent_call_id="c", via="delegate", kind="worker", **over)

    async def test_the_sessions_own_card_and_a_plain_childs_offer_it(self, tmp_path: Path) -> None:
        assert await self._proposal(tmp_path, None) is not None

    async def test_a_plain_child_offers_it_too(self, tmp_path: Path) -> None:
        assert await self._proposal(tmp_path, self._worker()) is not None

    async def test_a_named_workers_card_does_not(self, tmp_path: Path) -> None:
        # Its own rule may be why it asked, and a workspace rule cannot lift that.
        assert await self._proposal(tmp_path, self._worker(agent="db-migrator")) is None

    async def test_a_path_in_a_childs_own_worktree_does_not(self, tmp_path: Path) -> None:
        # The folder goes when the child finishes: the rule would never match again.
        assert await self._proposal(tmp_path, self._worker(worktree_branch="tst/wt/x")) is None
