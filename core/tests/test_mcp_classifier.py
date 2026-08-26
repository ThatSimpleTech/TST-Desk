"""MCP tools hit the classifier (TD-4402).

Security tests: undeclared path/host metadata cannot be Class A, dispatch
without a classifier raises ``UnclassifiedToolCall``, and builtins keep
their TD-702 grants. The chokepoint is ``ToolDispatcher.dispatch`` — the
raw MCP handler is not the gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_dispatch import (
    attach_auto_approver,
    make_config,
    start_loop,
    wait_for_turn,
)
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.autonomy.classifier import RULE_TABLE
from tstd.mock import MockProvider, Script
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import (
    Tool,
    ToolDispatcher,
    ToolRegistry,
    UnclassifiedToolCall,
    create_registry,
    register_builtin_handlers,
)
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import build_decision_request
from tstd.tools.registry import SideEffectClass


def _mcp_tool(
    name: str = "srv__echo",
    *,
    side_effect_class: SideEffectClass = "auto",
    path_fields: tuple[str, ...] = (),
    host_fields: tuple[str, ...] = (),
    host_resolver: Callable[[], tuple[str, ...]] | None = None,
    provenance: str | None = "mcp:srv",
    mutates: bool = False,
) -> Tool:
    """An MCP-shaped tool. Defaults match a loader registration except ``auto``."""
    return Tool(
        name=name,
        description="scripted MCP tool",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "path": {"type": "string"},
                "url": {"type": "string"},
            },
        },
        side_effect_class=side_effect_class,
        parallel_safe=True,
        path_fields=path_fields,
        host_fields=host_fields,
        host_resolver=host_resolver,
        mutates=mutates,
        provenance=provenance,
    )


def _static(workspace: Path) -> DecisionClassifier:
    return DecisionClassifier(Boundary(workspace_root=workspace))


async def _worker_a(_prompt: str) -> str:
    """Poisoned worker: would grant A if consulted."""
    return "A"


def _seed_file(path: Path, content: str) -> None:
    path.write_text(content)


def _file_text(path: Path) -> str:
    return path.read_text()


# ── Static table ───────────────────────────────────────────────────────


def test_mcp_undeclared_rule_precedes_class_a_grants() -> None:
    """A grant after this rule could launder an undeclared MCP call to A."""
    positions = {rule.id: i for i, rule in enumerate(RULE_TABLE)}
    floor = positions["mcp-undeclared-fields"]
    for rule in RULE_TABLE:
        if rule.decision_class is DecisionClass.A:
            assert positions[rule.id] > floor, f"{rule.id} must follow mcp-undeclared-fields"


def test_mcp_auto_without_path_or_host_is_b_never_a(tmp_path: Path) -> None:
    tool = _mcp_tool(side_effect_class="auto")
    request = build_decision_request(tool, {"text": "hi"})
    decision = _static(tmp_path).classify(request)
    assert request.provenance == "mcp:srv"
    assert request.has_path_host_metadata is False
    assert decision.decision_class is DecisionClass.B
    assert decision.decision_class is not DecisionClass.A
    assert decision.rule is not None
    assert decision.rule.id == "mcp-undeclared-fields"


def test_mcp_writes_cannot_launder_to_in_workspace_edit(tmp_path: Path) -> None:
    """Even a write-bearing request is B when path/host metadata was never declared."""
    decision = _static(tmp_path).classify(
        DecisionRequest(
            tool_name="srv__write",
            writes=(tmp_path / "a.txt",),
            is_mutation=True,
            side_effect_class="auto",
            provenance="mcp:srv",
            has_path_host_metadata=False,
        )
    )
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "mcp-undeclared-fields"


def test_mcp_never_and_caps_still_win_c(tmp_path: Path) -> None:
    never = _static(tmp_path).classify(
        DecisionRequest(
            tool_name="srv__echo",
            side_effect_class="never",
            provenance="mcp:srv",
            has_path_host_metadata=False,
        )
    )
    assert never.decision_class is DecisionClass.C
    assert never.rule is not None
    assert never.rule.id == "side-effect-never"

    capped = DecisionClassifier(Boundary(workspace_root=tmp_path, cap_exceeded=True)).classify(
        DecisionRequest(
            tool_name="srv__echo",
            side_effect_class="auto",
            provenance="mcp:srv",
            has_path_host_metadata=False,
        )
    )
    assert capped.decision_class is DecisionClass.C
    assert capped.rule is not None
    assert capped.rule.id == "cap-exceeded"


def test_mcp_declared_host_fields_still_hit_network_new_host(tmp_path: Path) -> None:
    tool = _mcp_tool(side_effect_class="auto", host_fields=("url",))
    request = build_decision_request(tool, {"url": "https://evil.example/x"})
    assert request.has_path_host_metadata is True
    decision = _static(tmp_path).classify(request)
    assert decision.decision_class is DecisionClass.C
    assert decision.rule is not None
    assert decision.rule.id == "network-new-host"


def test_mcp_host_resolver_counts_as_declared_metadata(tmp_path: Path) -> None:
    tool = _mcp_tool(side_effect_class="auto", host_resolver=lambda: ("search.example",))
    request = build_decision_request(tool, {})
    assert request.has_path_host_metadata is True
    decision = _static(tmp_path).classify(request)
    assert decision.decision_class is DecisionClass.C
    assert decision.rule is not None
    assert decision.rule.id == "network-new-host"


def test_builtin_and_desktop_are_not_mcp_provenance(tmp_path: Path) -> None:
    registry = create_registry()
    read_req = build_decision_request(
        registry.require("fs_read"), {"path": str(tmp_path / "a.txt")}
    )
    assert read_req.provenance is None
    assert read_req.has_path_host_metadata is True
    read_decision = _static(tmp_path).classify(read_req)
    assert read_decision.rule is None or read_decision.rule.id != "mcp-undeclared-fields"

    write_req = build_decision_request(
        registry.require("fs_write"),
        {"path": str(tmp_path / "a.txt"), "content": "x"},
    )
    assert write_req.provenance is None
    write_decision = _static(tmp_path).classify(write_req)
    assert write_decision.decision_class is DecisionClass.A
    assert write_decision.rule is not None
    assert write_decision.rule.id == "in-workspace-edit"

    capture = _static(tmp_path).classify(
        DecisionRequest(tool_name="screenshot", actuates=False, side_effect_class="auto")
    )
    assert capture.decision_class is DecisionClass.A
    assert capture.rule is not None
    assert capture.rule.id == "desktop-capture"


async def test_poisoned_worker_cannot_grant_a_to_undeclared_mcp(tmp_path: Path) -> None:
    called = False

    async def spy(_prompt: str) -> str:
        nonlocal called
        called = True
        return "A"

    classifier = AmbiguousClassifier(static=_static(tmp_path), call_worker=spy)
    tool = _mcp_tool(side_effect_class="auto")
    decision = await classifier.classify(build_decision_request(tool, {"text": "hi"}))
    assert decision.decision_class is DecisionClass.B
    assert decision.rule is not None
    assert decision.rule.id == "mcp-undeclared-fields"
    assert called is False


# ── Dispatch chokepoint ────────────────────────────────────────────────


async def test_mcp_dispatch_without_classifier_raises_unclassified() -> None:
    registry = ToolRegistry()
    registry.register(_mcp_tool())
    dispatcher = ToolDispatcher(registry)

    async def boom(session: object = None, tool_call_id: str = "", **_kwargs: object) -> str:
        raise AssertionError("MCP handler ran without a classifier")

    dispatcher.register_handler("srv__echo", boom)
    with pytest.raises(UnclassifiedToolCall):
        await dispatcher.dispatch("c1", "srv__echo", {"text": "hi"})


async def test_mcp_dispatch_classifies_then_runs_handler(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_mcp_tool(side_effect_class="auto"))
    order: list[str] = []

    class RecordingClassifier(DecisionClassifier):
        def classify(self, request: DecisionRequest) -> Classification:
            order.append("classify")
            return super().classify(request)

    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=AmbiguousClassifier(
                static=RecordingClassifier(Boundary(workspace_root=tmp_path)),
                call_worker=_worker_a,
            ),
            path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
        )
    )

    async def handler(session: object = None, tool_call_id: str = "", **_kwargs: object) -> str:
        order.append("handler")
        return "ok"

    dispatcher.register_handler("srv__echo", handler)
    result = await dispatcher.dispatch("c1", "srv__echo", {"text": "hi"})
    assert result.status == "success"
    assert result.output == "ok"
    assert result.decision_class is DecisionClass.B
    assert order == ["classify", "handler"]


async def test_mcp_tool_call_event_carries_class(tmp_path: Path) -> None:
    session = Session(str(tmp_path))
    router = TierRouter(lead_turns=3)
    config = make_config()
    registry = ToolRegistry()
    registry.register(_mcp_tool(side_effect_class="auto"))
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=AmbiguousClassifier(static=_static(tmp_path), call_worker=_worker_a),
            path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
        )
    )

    async def handler(session: object = None, tool_call_id: str = "", **_kwargs: object) -> str:
        return "echoed"

    dispatcher.register_handler("srv__echo", handler)

    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="srv__echo",
                    tool_arguments=json.dumps({"text": "hi"}),
                ),
                Script(kind="stream", content="Done"),
            ]
        }
    )
    runner = await start_loop(session, router, mock, config, registry, dispatcher)
    await session.add_user_message("Call the MCP tool")
    await wait_for_turn(session, 1)

    events = [
        e
        for e in session.event_log.all_events
        if isinstance(e, ToolCallEvent) and e.name == "srv__echo"
    ]
    assert len(events) == 1
    assert events[0].decision_class == "B"
    await runner.cancel()


async def test_builtin_fs_read_and_fs_write_still_class_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_file(tmp_path / "a.txt", "old\n")
    registry = create_registry()
    dispatcher = attach_auto_approver(
        ToolDispatcher(
            registry,
            classifier=AmbiguousClassifier(static=_static(tmp_path), call_worker=_worker_a),
            path_guard=PathGuard(Boundary(workspace_root=tmp_path)),
        )
    )
    register_builtin_handlers(dispatcher)
    written = await dispatcher.dispatch("c1", "fs_write", {"path": "a.txt", "content": "new\n"})
    assert written.status == "success"
    assert written.decision_class is DecisionClass.A
    assert _file_text(tmp_path / "a.txt") == "new\n"
    # fs_read has declared path_fields and no MCP provenance, so the
    # worker may still grant A — the undeclared-MCP floor must not fire.
    read = await dispatcher.dispatch("c2", "fs_read", {"path": "a.txt"})
    assert read.status == "success"
    assert read.decision_class is DecisionClass.A
