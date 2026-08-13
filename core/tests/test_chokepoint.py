"""Tests for the decision-classifier chokepoint (TD-702, prime §2.6).

Covers the four acceptance criteria:

1. Every tool dispatch routes through the classifier — no bypass path —
   asserted by a test that enumerates dispatch call sites.
2. Classification precedes execution (and any approval decision).
3. The class is attached to the audit record and the ``tool_call`` event.
4. A tool reaching execution unclassified raises immediately.

The chokepoint is enforced in ``ToolDispatcher.dispatch``, which refuses
to execute a tool without a classifier attached; the loop wires the
classifier and attaches the class to the ``tool_call`` event it appends to
the session's append-only event log (the audit trail).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import tstd
from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, ToolDispatcher, ToolRegistry, UnclassifiedToolCall


def _write_file(path: str, content: str) -> None:
    """Sync file write helper — avoids Path I/O inside async test handlers."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def make_tool(
    name: str,
    path_fields: tuple[str, ...] | None = None,
    host_fields: tuple[str, ...] | None = None,
    mutates: bool = False,
) -> Tool:
    """A registered, handler-backed tool with classifier metadata."""
    return Tool(
        name=name,
        description=f"{name} test tool",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        side_effect_class="auto",
        parallel_safe=False,
        path_fields=path_fields or (),
        host_fields=host_fields or (),
        mutates=mutates,
    )


async def _stub_worker(prompt: str) -> str:
    """Stub worker-tier classifier: always answers B (fail toward asking)."""
    return "B"


def make_dispatcher(
    registry: ToolRegistry, workspace: Path, path_field: str = "path"
) -> ToolDispatcher:
    """A dispatcher with a classifier over *workspace* and a writing handler."""
    dispatcher = ToolDispatcher(
        registry,
        classifier=AmbiguousClassifier(
            static=DecisionClassifier(Boundary(workspace_root=workspace)),
            call_worker=_stub_worker,
        ),
    )

    async def write_handler(session: object, path: str, content: str = "") -> str:
        _write_file(path, content)
        return f"wrote {path}"

    for tool in registry.list_tools():
        dispatcher.register_handler(tool.name, write_handler)
    return dispatcher


# ── Criterion 4: unclassified reaching execution raises ────────────────


class TestUnclassifiedExecutionRaises:
    async def test_dispatch_without_classifier_raises(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(make_tool("fs_edit", path_fields=("path",), mutates=True))
        # No classifier attached — the chokepoint must refuse to execute.
        dispatcher = ToolDispatcher(registry)

        async def handler(session: object, path: str) -> str:
            return "should not run"

        dispatcher.register_handler("fs_edit", handler)

        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch("c1", "fs_edit", {"path": str(tmp_path / "a.txt")})

    async def test_handler_not_called_when_unclassified(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(make_tool("touch", mutates=True))
        dispatcher = ToolDispatcher(registry)
        called = False

        async def handler(session: object, **kwargs: object) -> str:
            nonlocal called
            called = True
            return "ran"

        dispatcher.register_handler("touch", handler)
        with pytest.raises(UnclassifiedToolCall):
            await dispatcher.dispatch("c1", "touch", {"path": str(tmp_path)})
        assert called is False


# ── Criterion 2: classification precedes execution ─────────────────────


class TestClassificationPrecedesExecution:
    async def test_classifier_runs_before_handler(self, tmp_path: Path) -> None:
        class RecordingClassifier(DecisionClassifier):
            handler_ran: bool
            classified_before: bool

            def __init__(self, boundary: Boundary) -> None:
                super().__init__(boundary)
                self.handler_ran = False
                self.classified_before = False

            def classify(self, request: DecisionRequest) -> Classification:
                # Called by dispatch; flag that classification happened.
                self.classified_before = True
                return super().classify(request)

        registry = ToolRegistry()
        registry.register(make_tool("fs_edit", path_fields=("path",), mutates=True))
        spy = RecordingClassifier(Boundary(workspace_root=tmp_path))
        classifier = AmbiguousClassifier(static=spy, call_worker=_stub_worker)
        dispatcher = ToolDispatcher(registry, classifier=classifier)

        async def handler(session: object, path: str) -> str:
            # The handler must observe classification already happened.
            spy.handler_ran = True
            assert spy.classified_before
            return "ok"

        dispatcher.register_handler("fs_edit", handler)

        result = await dispatcher.dispatch("c1", "fs_edit", {"path": str(tmp_path / "a.txt")})
        assert result.status == "success"
        assert spy.handler_ran


# ── Criterion 1 + 3: class attached to result and to the event ─────────


class TestClassAttached:
    async def test_in_workspace_edit_result_carries_class(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(make_tool("fs_edit", path_fields=("path",), mutates=True))
        dispatcher = make_dispatcher(registry, tmp_path)

        result = await dispatcher.dispatch("c1", "fs_edit", {"path": str(tmp_path / "a.txt")})
        assert result.status == "success"
        assert result.decision_class is DecisionClass.A

    async def test_outside_workspace_edit_result_carries_c(self, tmp_path: Path) -> None:
        registry = ToolRegistry()
        registry.register(make_tool("fs_edit", path_fields=("path",), mutates=True))
        dispatcher = make_dispatcher(registry, tmp_path)
        outside = tmp_path.parent / "outside.txt"

        result = await dispatcher.dispatch("c1", "fs_edit", {"path": str(outside)})
        assert result.status == "success"
        assert result.decision_class is DecisionClass.C

    async def test_tool_call_event_carries_class(self, tmp_path: Path) -> None:
        """The class lands on the tool_call event in the audit log."""
        ws = tmp_path
        session = Session(str(ws))
        router = TierRouter(lead_turns=3)
        config = make_config()
        registry = ToolRegistry()
        registry.register(make_tool("fs_edit", path_fields=("path",), mutates=True))
        dispatcher = make_dispatcher(registry, ws)

        mock = MockProvider(
            sequences={
                "test-brain": [
                    Script(
                        kind="tool_call",
                        tool_name="fs_edit",
                        tool_arguments=f'{{"path": "{ws / "a.txt"}"}}',
                    ),
                    Script(kind="stream", content="Done"),
                ]
            }
        )

        runner = await start_loop(session, router, mock, config, registry, dispatcher)
        await session.add_user_message("Write a file")
        await wait_for_turn(session, 1)

        events = [
            e
            for e in session.event_log.all_events
            if isinstance(e, ToolCallEvent) and e.name == "fs_edit"
        ]
        assert len(events) == 1
        # In-workspace write ⟹ Class A, recorded on the audit-log event.
        assert events[0].decision_class == "A"

        await runner.cancel()


# ── Criterion 1: no bypass — enumerate dispatch call sites ─────────────


def test_no_bypass_enumerates_dispatch_call_sites() -> None:
    """Tool execution is possible only in guarded modules.

    Every call to ``dispatch``/``dispatch_many`` in the production package
    is confined to ``loop.py`` (the orchestrator that wires the classifier)
    and ``dispatch.py`` (where the classifier guard lives).  Any other
    module getting a tool to execution is a bypass and fails this test.
    """
    pkg = Path(tstd.__file__).parent
    allowed = {"loop.py", "dispatch.py"}
    offenders: list[tuple[str, int, str]] = []

    for path in pkg.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        rel = path.relative_to(pkg)
        if rel.name in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.dispatch\(|dispatch_many\(", line):
                offenders.append((str(rel), lineno, line.strip()))

    assert not offenders, f"dispatch call sites outside loop.py/dispatch.py: {offenders}"

    # The loop's dispatch path and the engine guard must exist.
    loop_src = (pkg / "loop.py").read_text()
    dispatch_src = (pkg / "tools" / "dispatch.py").read_text()
    assert "dispatch_many(" in loop_src
    assert "_dispatch_and_append_results" in loop_src
    assert "UnclassifiedToolCall" in dispatch_src
    assert "self.classifier" in dispatch_src
