"""The timeline and audit show the class a tool call actually ran with (TD-5148).

The loop classified the ``tool_call`` event on the raw arguments, so a
workspace-relative ``fs_read {"path": "a.txt"}`` resolved against the
daemon's cwd (``/`` in the packaged sidecar) and the event said Class C.
Dispatch joined the path to the workspace first, classified A and ran the
call with no approval.  The audit row copies the event's class, so it said
C too, and a failed read was stored as ``refused``.

Every test here runs with the process cwd at ``/`` and the workspace in a
tmp dir, the way the sidecar runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_dispatch import make_config, start_loop, wait_for_turn
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.autonomy import (
    AmbiguousClassifier,
    Boundary,
    Classification,
    DecisionClass,
    DecisionClassifier,
    DecisionRequest,
)
from tstd.mock import MockProvider, Script
from tstd.policy import PolicyConfig
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.session import Session
from tstd.tools import Tool, create_registry, register_builtin_handlers
from tstd.tools.boundary import PathGuard
from tstd.tools.dispatch import ToolDispatcher, canonicalize_path_fields
from tstd.tools.results import ToolResult


async def _worker_says_b(prompt: str) -> str:
    """An ambiguous call fails toward asking, never toward A."""
    return "B"


class _SpyClassifier(AmbiguousClassifier):
    """Keeps every class it hands out, in order."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[DecisionClass] = []

    async def classify(self, request: DecisionRequest) -> Classification:
        classification = await super().classify(request)
        self.seen.append(classification.decision_class)
        return classification


class _SpyDispatcher(ToolDispatcher):
    """Keeps the class dispatch ran each call with.

    That is the class on the result when dispatch set one (a boundary
    refusal forces C), else the class its own classification returned
    (a handler error carries no class on the result).
    """

    def __init__(self, spy: _SpyClassifier, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, classifier=spy, **kwargs)
        self.spy = spy
        self.ran: dict[str, str] = {}

    async def dispatch(
        self,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
        session: Any = None,
    ) -> ToolResult:
        before = len(self.spy.seen)
        result = await super().dispatch(tool_call_id, name, arguments, session)
        [classified] = self.spy.seen[before:]
        used = result.decision_class if result.decision_class is not None else classified
        self.ran[tool_call_id] = used.value
        return result


def _dispatcher(ws: Path) -> _SpyDispatcher:
    boundary = Boundary(workspace_root=ws)
    dispatcher = _SpyDispatcher(
        _SpyClassifier(static=DecisionClassifier(boundary), call_worker=_worker_says_b),
        create_registry(),
        path_guard=PathGuard(boundary),
        policy=PolicyConfig(),
        workspace=ws,
    )
    register_builtin_handlers(dispatcher)
    return dispatcher


async def _one_call(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> tuple[ToolCallEvent, ToolResultEvent, str, tuple[Any, ...], Path]:
    """Run one tool call through the real loop and the audit writer."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")

    session = Session(str(ws))
    store = AuditStore(tmp_path / "audit.db")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(session)
    dispatcher = _dispatcher(ws)
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(kind="tool_call", tool_name=tool, tool_arguments=json.dumps(arguments)),
                Script(kind="stream", content="Done"),
            ]
        }
    )
    runner = await start_loop(
        session, TierRouter(lead_turns=3), mock, make_config(), dispatcher.registry, dispatcher
    )
    try:
        await session.add_user_message("go")
        await wait_for_turn(session, 1)
        [event] = [e for e in session.event_log.all_events if isinstance(e, ToolCallEvent)]
        [result] = [e for e in session.event_log.all_events if isinstance(e, ToolResultEvent)]
        await writer._queue.join()
        row = store._conn.execute(
            "SELECT decision_class, status, arguments FROM tool_calls WHERE tool_call_id=?",
            (event.tool_call_id,),
        ).fetchone()
    finally:
        await runner.cancel()
        await writer.close()
    return event, result, dispatcher.ran[event.tool_call_id], row, ws


@pytest.fixture(autouse=True)
def _sidecar_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """The packaged sidecar runs with its cwd at the filesystem root."""
    monkeypatch.chdir("/")


@pytest.mark.parametrize(
    ("tool", "arguments", "status"),
    [
        ("fs_read", {"path": "a.txt"}, "success"),
        ("fs_read", {"path": "missing.txt"}, "success"),  # "Error: file not found" text
        ("fs_write", {"path": "new.txt", "content": "x\n"}, "success"),
        ("fs_edit", {"path": "a.txt", "old_string": "hello", "new_string": "bye"}, "success"),
        ("fs_write", {"path": "sub/deep.txt", "content": "y\n"}, "success"),
    ],
)
async def test_relative_path_event_and_audit_carry_the_class_dispatch_used(
    tmp_path: Path, tool: str, arguments: dict[str, Any], status: str
) -> None:
    event, result, ran, row, _ = await _one_call(tmp_path, tool, arguments)
    assert result.status == status, result.output
    assert ran == "A"
    assert event.decision_class == ran
    decision_class, audit_status, _ = row
    assert (decision_class, audit_status) == ("A", status)


async def test_relative_write_lands_in_the_workspace(tmp_path: Path) -> None:
    *_, ws = await _one_call(tmp_path, "fs_write", {"path": "new.txt", "content": "x\n"})
    assert (ws / "new.txt").read_text(encoding="utf-8") == "x\n"


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "a.txt", "old_string": "not there", "new_string": "x"},
        {"path": "missing.txt", "old_string": "a", "new_string": "b"},
    ],
)
async def test_failed_relative_call_is_an_error_not_a_refusal(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    """A call inside the wall ran as A and failed; the audit must not say refused."""
    event, result, ran, row, _ = await _one_call(tmp_path, "fs_edit", arguments)
    assert result.status == "error"
    assert result.error_code != "boundary_refusal"
    assert ran == "A"
    assert event.decision_class == ran
    assert row[:2] == ("A", "error")


async def test_event_and_audit_keep_the_arguments_the_model_sent(tmp_path: Path) -> None:
    """Only the classification sees the joined path; the record shows what was asked."""
    event, _, _, row, _ = await _one_call(tmp_path, "fs_read", {"path": "a.txt"})
    assert event.arguments == {"path": "a.txt"}
    assert json.loads(row[2]) == {"path": "a.txt"}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("fs_read", {"path": "../outside.txt"}),
        ("fs_write", {"path": "../outside.txt", "content": "x\n"}),
    ],
)
async def test_escape_still_class_c_and_refused(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    event, result, ran, row, _ = await _one_call(tmp_path, tool, arguments)
    assert result.status == "error" and result.error_code == "boundary_refusal"
    assert ran == "C"
    assert event.decision_class == ran
    assert row[:2] == ("C", "refused")
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "secret\n"


@pytest.mark.parametrize(
    "path",
    [
        "C:/x.txt",  # drive-absolute: relative off Windows, so it was joined
        "C:x.txt",  # drive-relative
        "\\\\server\\share\\x.txt",  # UNC: one long relative name off Windows
    ],
)
async def test_windows_unsafe_form_is_refused_not_joined(tmp_path: Path, path: str) -> None:
    """DECISIONS (TD-602 §4): Windows-unsafe forms are refused on every platform.

    Joining the raw value to the workspace hid its drive or UNC prefix, so
    the guard and the classifier both passed ``<ws>/C:/x.txt`` and the write
    ran as A (TD-5158).
    """
    event, result, ran, row, ws = await _one_call(
        tmp_path, "fs_write", {"path": path, "content": "x\n"}
    )
    assert result.status == "error" and result.error_code == "boundary_refusal", result.output
    assert ran == "C"
    assert event.decision_class == ran
    assert row[:2] == ("C", "refused")
    assert sorted(p.name for p in ws.iterdir()) == ["a.txt"]


# ── The shared helper ───────────────────────────────────────────────────


def _path_tool(*fields: str) -> Tool:
    return Tool(
        name="t",
        description="t",
        parameters={"type": "object", "properties": {}},
        side_effect_class="auto",
        parallel_safe=True,
        path_fields=fields,
    )


def test_helper_joins_relative_paths_to_the_workspace_not_the_cwd(tmp_path: Path) -> None:
    guard = PathGuard(Boundary(workspace_root=tmp_path))
    raw = {"path": "a.txt", "files": ["b.txt", str(tmp_path / "c.txt")], "other": "d.txt"}
    out = canonicalize_path_fields(_path_tool("path", "files"), raw, guard)
    root = tmp_path.resolve()
    assert out == {
        "path": str(root / "a.txt"),
        "files": [str(root / "b.txt"), str(root / "c.txt")],
        "other": "d.txt",
    }
    assert raw["path"] == "a.txt"  # a copy; the caller's dict is untouched


def test_helper_leaves_absent_malformed_and_unguarded_values(tmp_path: Path) -> None:
    guard = PathGuard(Boundary(workspace_root=tmp_path))
    tool = _path_tool("path", "files")
    assert canonicalize_path_fields(tool, {}, guard) == {}
    malformed = {"path": 7, "files": ["ok", 3]}
    assert canonicalize_path_fields(tool, malformed, guard) == malformed
    assert canonicalize_path_fields(tool, {"path": "a.txt"}, None) == {"path": "a.txt"}


def test_helper_keeps_a_windows_unsafe_form_as_written(tmp_path: Path) -> None:
    guard = PathGuard(Boundary(workspace_root=tmp_path))
    tool = _path_tool("path", "files")
    raw = {"path": "C:x.txt", "files": ["\\\\srv\\share\\y", "ok.txt"]}
    out = canonicalize_path_fields(tool, raw, guard)
    assert out == {
        "path": "C:x.txt",
        "files": ["\\\\srv\\share\\y", str(tmp_path.resolve() / "ok.txt")],
    }
