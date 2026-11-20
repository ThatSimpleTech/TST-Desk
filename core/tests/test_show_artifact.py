"""TD-5254: the agent shows text in the Artifacts pane with ``show_artifact``.

Two doors: a text file in the workspace (no copy; the pane shows the file
as it is), or text the model hands over, kept in the session's own data
dir under a name the daemon makes. These pin the contract, the wall, Class
A with no card, the caps, and that a second call replaces the first.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.platform_helpers import assert_owner_only_mode
from tests.test_classifier import WS, boundary, classify, fired_as, req
from tests.test_dispatch import make_classifier
from tstd.artifacts import ArtifactRecord, ArtifactStore
from tstd.autonomy import Boundary, DecisionClass
from tstd.autonomy.classifier import PLAN_READ_ONLY_BUILTINS
from tstd.config import SessionConfig
from tstd.policy import ApprovalOutcome, PolicyConfig
from tstd.protocol import ArtifactReady
from tstd.sent_files import SentFiles
from tstd.session import Session
from tstd.session_persist import SessionPersist
from tstd.shown_artifacts import ShownArtifacts
from tstd.tools import ToolDispatcher
from tstd.tools.boundary import PathGuard
from tstd.tools.delegate import WORKER_CHILD_TOOLS
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.registry import create_registry
from tstd.tools.show_artifact import SHOW_ARTIFACT_TOOL

POSIX = os.name == "posix"


@dataclass
class Env:
    ws: Path
    data: Path
    store: ArtifactStore
    session: Session
    dispatcher: ToolDispatcher
    approvals: list[tuple[Any, ...]] = field(default_factory=list)

    def ready(self) -> list[ArtifactReady]:
        return [e for e in self.session.event_log.all_events if isinstance(e, ArtifactReady)]

    def rows(self) -> list[ArtifactRecord]:
        return self.store.list_records(self.session.id, self.ws)

    def stored(self) -> list[str]:
        folder = self.store.bytes_dir(self.session.id)
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    async def show(self, call_id: str = "call_1", **arguments: Any) -> Any:
        return await self.dispatcher.dispatch(call_id, "show_artifact", arguments, self.session)


def _env(tmp_path: Path, config: SessionConfig | None = None) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    persist = SessionPersist(tmp_path / "data")
    store = ArtifactStore(persist)
    settings = config or SessionConfig()
    service = ShownArtifacts(store, lambda: settings)
    session = Session(str(ws))
    persist.prepare(session.id)
    session.persist_dir = persist.dir_for(session.id)
    dispatcher = ToolDispatcher(
        create_registry(),
        classifier=make_classifier(str(ws)),
        path_guard=PathGuard(Boundary(workspace_root=ws)),
        workspace=ws,
    )
    dispatcher.policy = PolicyConfig()
    service.bind_guard(lambda: dispatcher.path_guard)
    env = Env(ws, tmp_path / "data", store, session, dispatcher)

    async def _approve(*args: Any) -> ApprovalOutcome:
        env.approvals.append(args)
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    register_builtin_handlers(
        dispatcher, sent_files=SentFiles(store, settings), shown_artifacts=service
    )
    return env


def _tree(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# ── Registration ───────────────────────────────────────────────────────


def test_the_tool_is_registered_as_a_read_that_runs_in_order() -> None:
    tool = create_registry().require("show_artifact")
    assert tool is SHOW_ARTIFACT_TOOL
    assert tool.path_fields == ("path",)
    assert tool.side_effect_class == "auto"
    assert tool.parallel_safe is False
    assert tool.mutates is False and tool.provenance is None
    assert tool.parameters["required"] == ["title"]
    assert tool.parameters["additionalProperties"] is False
    assert set(tool.parameters["properties"]) == {"title", "path", "content", "name", "caption"}


def test_only_the_sessions_own_agent_has_it() -> None:
    assert "show_artifact" not in WORKER_CHILD_TOOLS
    assert "show_artifact" in PLAN_READ_ONLY_BUILTINS


# ── The two doors ──────────────────────────────────────────────────────


async def test_a_workspace_file_is_shown_without_a_copy(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.ws / "docs").mkdir()
    (env.ws / "docs" / "report.md").write_text("# Report\n", encoding="utf-8")
    result = await env.show(title="The report", path="docs/report.md", caption="First draft")
    assert result.status == "success", result.output
    assert result.decision_class is DecisionClass.A
    assert env.approvals == []
    assert result.output == 'Showing "The report" in Artifacts (report.md).'
    [row] = env.rows()
    assert (row.kind, row.location, row.path) == ("artifact", "workspace", "docs/report.md")
    assert (row.title, row.name, row.mime) == ("The report", "report.md", "text/markdown")
    assert (row.size, row.sha256) == (None, None)
    assert (row.caption, row.tool_call_id) == ("First draft", "call_1")
    # A pointer, not a copy: nothing was written but the index.
    assert env.stored() == []
    [event] = env.ready()
    assert (event.artifact_id, event.kind, event.name) == (row.id, "artifact", "report.md")


async def test_given_text_is_kept_in_the_session_under_a_name_the_daemon_makes(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    text = "<h1>Chart</h1>\n"
    result = await env.show(title="A chart", content=text, name="chart.html")
    assert result.status == "success", result.output
    assert result.decision_class is DecisionClass.A and env.approvals == []
    [row] = env.rows()
    data = text.encode()
    assert (row.kind, row.location, row.path) == ("artifact", "session", f"artifacts/{row.id}")
    assert (row.name, row.mime, row.size) == ("chart.html", "text/html", len(data))
    assert row.sha256 == hashlib.sha256(data).hexdigest()
    copy = env.store.bytes_dir(env.session.id) / row.id
    assert copy.read_bytes() == data
    assert_owner_only_mode(copy.stat().st_mode & 0o777)
    assert env.stored() == [row.id]
    assert result.output == f'Showing "A chart" in Artifacts (chart.html, {len(data)} bytes).'
    # The workspace is untouched, and the reply names no data-dir path.
    assert _tree(env.ws) == []
    assert str(env.data) not in result.output
    assert str(env.data) not in env.ready()[0].model_dump_json()


@pytest.mark.parametrize(
    ("name", "mime"),
    [("notes.md", "text/markdown"), ("tool.py", "text/plain"), ("data.json", "application/json")],
)
async def test_the_name_picks_the_type_and_unknown_text_is_plain(
    tmp_path: Path, name: str, mime: str
) -> None:
    env = _env(tmp_path)
    assert (await env.show(title="x", content="hello", name=name)).status == "success"
    assert env.rows()[0].mime == mime


# ── What it refuses ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        {"title": "x"},
        {"title": "x", "path": "a.md", "content": "hello", "name": "a.md"},
        {"title": "x", "content": "hello"},
        {"title": "x", "content": "hello", "name": "   "},
    ],
)
async def test_one_door_at_a_time_and_content_needs_a_name(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    env = _env(tmp_path)
    (env.ws / "a.md").write_text("a", encoding="utf-8")
    result = await env.show(**arguments)
    assert result.status != "success"
    assert result.error_code == "bad_request", result.output
    assert env.rows() == [] and env.ready() == [] and env.stored() == []


async def test_only_text_and_only_a_regular_file(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.ws / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00")
    (env.ws / "folder").mkdir()
    binary = await env.show(title="x", path="pic.png")
    assert binary.error_code == "not_text" and "send_file" in binary.output
    assert (await env.show(title="x", path="folder")).error_code == "not_a_file"
    assert (await env.show(title="x", path="gone.md")).error_code == "artifact_not_found"
    if POSIX:
        os.mkfifo(env.ws / "pipe")
        assert (await env.show(title="x", path="pipe")).error_code == "not_a_file"
    assert env.rows() == [] and env.ready() == []


async def test_a_path_outside_the_workspace_is_class_c_and_shows_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")

    async def deny(*args: Any) -> ApprovalOutcome:
        env.approvals.append(args)
        return ApprovalOutcome(False, "no")

    env.dispatcher.approval_handler = deny
    result = await env.show(title="x", path=str(outside))
    assert result.status != "success"
    assert result.decision_class is not DecisionClass.A
    assert env.rows() == [] and env.ready() == []
    if POSIX:
        (env.ws / "link.txt").symlink_to(outside)
        linked = await env.show(title="x", path="link.txt")
        assert linked.status != "success" and env.rows() == []


async def test_a_name_is_a_label_never_a_place(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for hostile in (
        "../../AGENTS.md",
        "/etc/passwd",
        "CLAUDE.md",
        "..\\..\\x.md",
        ".tst/rules/a.md",
    ):
        result = await env.show(title="x", content="text", name=hostile)
        assert result.status == "success", (hostile, result.output)
    # Whatever the name, the only files written are artifacts/<id> in the session dir.
    ids = {row.id for row in env.rows()}
    assert set(env.stored()) == ids
    assert _tree(env.ws) == []
    assert not (tmp_path / "AGENTS.md").exists()
    for row in env.rows():
        assert row.name is not None and "/" not in row.name and "\\" not in row.name
        assert row.path == f"artifacts/{row.id}"


async def test_too_much_text_is_refused_either_way(tmp_path: Path) -> None:
    env = _env(tmp_path, SessionConfig(artifact_max_bytes=100))
    (env.ws / "big.md").write_text("x" * 101, encoding="utf-8")
    assert (await env.show(title="x", path="big.md")).error_code == "artifact_too_large"
    over = await env.show(title="x", content="é" * 51, name="a.md")  # 102 bytes
    assert over.error_code == "artifact_too_large"
    assert (await env.show(title="x", content="é" * 50, name="a.md")).status == "success"
    assert [row.size for row in env.rows()] == [100]


async def test_the_count_and_the_session_quota_are_caps(tmp_path: Path) -> None:
    env = _env(tmp_path, SessionConfig(artifact_max_count=2, send_file_session_max_bytes=25))
    assert (await env.show(title="a", content="0123456789", name="a.md")).status == "success"
    assert (await env.show(title="b", content="0123456789", name="b.md")).status == "success"
    third = await env.show(title="c", content="x", name="c.md")
    assert third.error_code == "artifact_limit"
    # Replacing one is not a third, and is counted once against the quota.
    assert (await env.show(title="a2", content="0123456789abcde", name="a.md")).status == "success"
    grown = await env.show(title="a3", content="0123456789abcdef", name="a.md")
    assert grown.error_code == "session_quota"
    assert sorted(row.size or 0 for row in env.rows()) == [10, 15]


# ── Showing again ──────────────────────────────────────────────────────


async def test_showing_the_same_thing_again_replaces_it_and_keeps_its_id(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.ws / "plan.md").write_text("one", encoding="utf-8")
    await env.show("c1", title="Plan", path="plan.md")
    await env.show("c2", title="Plan, revised", path="./plan.md")
    await env.show("c3", title="Notes", content="first", name="notes.md")
    await env.show("c4", title="Notes v2", content="second, longer", name="notes.md")
    await env.show("c5", title="Other", content="other", name="other.md")
    rows = env.rows()
    assert [row.title for row in rows] == ["Plan, revised", "Notes v2", "Other"]
    events = env.ready()
    assert events[0].artifact_id == events[1].artifact_id == rows[0].id
    assert events[2].artifact_id == events[3].artifact_id == rows[1].id
    assert rows[1].tool_call_id == "c4"
    # One file per content artifact; the replaced text is gone.
    assert set(env.stored()) == {rows[1].id, rows[2].id}
    kept = env.store.bytes_dir(env.session.id) / rows[1].id
    assert kept.read_bytes() == b"second, longer"


async def test_a_sent_file_is_never_replaced_by_a_shown_one(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.ws / "a.md").write_text("sent", encoding="utf-8")
    sent = await env.dispatcher.dispatch("s1", "send_file", {"files": ["a.md"]}, env.session)
    assert sent.status == "success", sent.output
    await env.show(title="shown", content="shown", name="a.md")
    assert sorted(row.kind for row in env.rows()) == ["artifact", "sent_file"]


# ── The classifier ─────────────────────────────────────────────────────


def test_the_rule_is_for_the_builtin_alone() -> None:
    inside = (WS / "a.md",)
    for fine in (req(tool_name="show_artifact"), req(tool_name="show_artifact", reads=inside)):
        decision = classify(boundary(), fine)
        assert decision.decision_class is DecisionClass.A
        assert fired_as(decision, "show-artifact")
    for no_grant in (
        req(tool_name="show_artifact", provenance="mcp:srv"),
        req(tool_name="show_artifact", provenance="plugin:dist"),
        req(tool_name="show_artifact", hosts=frozenset({"example.com"})),
        req(tool_name="show_artifact", writes=inside),
        req(tool_name="show_artifact", is_mutation=True),
        req(tool_name="show_artifact", reads=(WS / "a.md", WS / "b.md")),
    ):
        assert not fired_as(classify(boundary(), no_grant), "show-artifact"), no_grant
    outside = classify(boundary(), req(tool_name="show_artifact", reads=(Path("/etc/passwd"),)))
    assert outside.decision_class is DecisionClass.C


# ── A real daemon session ──────────────────────────────────────────────


async def test_a_daemon_session_shows_with_no_card_and_lists_it(tmp_path: Path) -> None:
    """The daemon wires the service into every session it builds."""
    import json

    from tests.machine_helpers import eventually
    from tests.test_artifacts import _open, _shutdown
    from tests.test_two_daemons import _Steps
    from tstd.daemon import Daemon
    from tstd.mock import Script
    from tstd.protocol import ApprovalRequest, ToolResult

    call = json.dumps({"title": "Summary", "content": "# Done\n", "name": "summary.md"})
    steps = [
        Script(kind="tool_call", tool_name="show_artifact", tool_arguments=call),
        Script(kind="stream", content="Shown."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        message = {"type": "user_message", "session_id": sid, "content": "summarise"}
        await daemon._handle_message(json.dumps(message), None)
        log = session.event_log
        await eventually(lambda: any(isinstance(e, ToolResult) for e in log.all_events), within=20)
        [result] = [e for e in log.all_events if isinstance(e, ToolResult)]
        assert result.status == "success", result.output
        assert not [e for e in log.all_events if isinstance(e, ApprovalRequest)]
        [ready] = [e for e in log.all_events if isinstance(e, ArtifactReady)]
        assert (ready.kind, ready.name, ready.mime) == ("artifact", "summary.md", "text/markdown")
        listed = json.loads(
            await daemon._handle_message(
                json.dumps({"type": "list_artifacts", "session_id": sid}), None
            )
            or "{}"
        )
        assert [row["artifact_id"] for row in listed["artifacts"]] == [ready.artifact_id]
        assert listed["artifacts"][0]["name"] == "summary.md"
        assert list(ws.iterdir()) == [] or [p.name for p in ws.iterdir()] == [".tst"]
    finally:
        await _shutdown(daemon, sid)
