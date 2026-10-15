"""TD-5143: the agent sends files to the chat with ``send_file``.

The tool copies each workspace file into the session's own data dir and
emits one ``artifact_ready`` per file. These tests pin the contract (§1.2
result text and refusal codes, §1.4 names, §1.5 MIME), the wall (every path
guarded, outside paths Class C), Class A with no card in any mode, all or
nothing, and that file bytes never reach a log, the event log or the audit
DB.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.platform_helpers import assert_owner_only_mode
from tests.test_dispatch import make_classifier, make_config, start_loop, wait_for_turn
from tests.test_plugins import _entry
from tstd import sent_files
from tstd.artifacts import ArtifactError, ArtifactRecord, ArtifactStore
from tstd.audit import AuditStore
from tstd.audit_writer import AuditWriter
from tstd.autonomy import Boundary, DecisionClass, DecisionClassifier
from tstd.config import SessionConfig
from tstd.mcp.jsonrpc import local_tool_name
from tstd.mock import MockProvider, Script
from tstd.policy import ApprovalOutcome, PolicyConfig, PolicyRule
from tstd.protocol import ArtifactReady, DaemonEvent
from tstd.protocol import ToolCall as ToolCallEvent
from tstd.protocol import ToolResult as ToolResultEvent
from tstd.router import TierRouter
from tstd.sent_files import (
    FREE_MARGIN,
    SendError,
    SentFiles,
    mime_for,
    read_chunk,
    safe_file_name,
)
from tstd.session import Session, SessionEventLog
from tstd.session_persist import SessionPersist
from tstd.tools import Tool, ToolDispatcher, build_decision_request
from tstd.tools.boundary import PathGuard
from tstd.tools.delegate import WORKER_CHILD_TOOLS
from tstd.tools.handlers import register_builtin_handlers
from tstd.tools.plugins import load_plugins
from tstd.tools.registry import create_registry
from tstd.tools.send_file import SEND_FILE_TOOL, send_file

POSIX = os.name == "posix"
MARKER = "TD5143-BYTES-MARKER-9c41"


@dataclass
class Env:
    ws: Path
    persist: SessionPersist
    store: ArtifactStore
    service: SentFiles
    session: Session
    dispatcher: ToolDispatcher
    approvals: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def bytes_dir(self) -> Path:
        return self.store.bytes_dir(self.session.id)

    def ready(self) -> list[ArtifactReady]:
        return [e for e in self.session.event_log.all_events if isinstance(e, ArtifactReady)]

    def rows(self) -> list[ArtifactRecord]:
        return self.store.list_records(self.session.id, self.ws)

    def on_disk(self) -> list[str]:
        if not self.bytes_dir.exists():
            return []
        return sorted(p.relative_to(self.bytes_dir).as_posix() for p in self.bytes_dir.rglob("*"))

    async def send(self, files: Any, caption: str | None = None) -> Any:
        arguments: dict[str, Any] = {"files": files}
        if caption is not None:
            arguments["caption"] = caption
        return await self.dispatcher.dispatch("call_1", "send_file", arguments, self.session)


def _env(tmp_path: Path, config: SessionConfig | None = None) -> Env:
    ws = tmp_path / "ws"
    ws.mkdir()
    persist = SessionPersist(tmp_path / "data")
    store = ArtifactStore(persist)
    service = SentFiles(store, config or SessionConfig())
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
    service.bind_guard(lambda: dispatcher.path_guard)  # as the daemon does
    env = Env(ws, persist, store, service, session, dispatcher)

    async def _approve(*args: Any) -> ApprovalOutcome:
        env.approvals.append(args)
        return ApprovalOutcome(True)

    dispatcher.approval_handler = _approve
    register_builtin_handlers(dispatcher, sent_files=service)
    return env


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


# ── Registration ───────────────────────────────────────────────────────


def test_tool_registered_with_files_path_field_class_auto_parallel_safe() -> None:
    tool = create_registry().require("send_file")
    assert tool is SEND_FILE_TOOL
    assert tool.path_fields == ("files",)
    assert tool.side_effect_class == "auto"
    assert tool.parallel_safe is True
    assert tool.mutates is False
    assert tool.provenance is None
    files = tool.parameters["properties"]["files"]
    assert (files["type"], files["minItems"], files["maxItems"]) == ("array", 1, 10)
    assert tool.parameters["properties"]["caption"]["maxLength"] == 500
    assert tool.parameters["required"] == ["files"]
    assert tool.parameters["additionalProperties"] is False


def test_not_a_delegate_child_tool() -> None:
    assert "send_file" not in WORKER_CHILD_TOOLS


def test_builtin_not_shadowed_by_plugin_or_mcp() -> None:
    registry = create_registry()

    def register(reg: Any, dispatcher: Any) -> None:
        reg.register(Tool(name="send_file", description="hijacked", parameters={}))

    load_plugins(registry, entries=[_entry("send_file", register=register)])
    kept = registry.require("send_file")
    assert kept is SEND_FILE_TOOL
    assert kept.provenance is None
    # An MCP server's own send_file can only ever be <server>__send_file.
    assert local_tool_name("srv", "send_file") == "srv__send_file"


# ── Copies and records ─────────────────────────────────────────────────


async def test_sends_copies_with_0600_files_and_0700_folders(tmp_path: Path) -> None:
    env = _env(tmp_path)
    data = b"PK\x03\x04" + os.urandom(3000)
    _write(env.ws / "out" / "app-release.apk", data)
    result = await env.send(["out/app-release.apk"], caption="0.1.9 release build")
    assert result.status == "success", result.output
    assert result.decision_class is DecisionClass.A
    [record] = env.rows()
    assert record.kind == "sent_file"
    assert record.name == record.title == "app-release.apk"
    assert record.size == len(data)
    assert record.sha256 == hashlib.sha256(data).hexdigest()
    assert record.mime == "application/vnd.android.package-archive"
    assert record.location == "session"
    assert record.path == f"artifacts/{record.id}"
    assert record.caption == "0.1.9 release build"
    assert record.tool_call_id == "call_1"
    copy = env.bytes_dir / record.id / "app-release.apk"
    assert copy.read_bytes() == data
    assert os.listdir(env.bytes_dir / record.id) == ["app-release.apk"]
    assert_owner_only_mode(copy.stat().st_mode & 0o777)
    if POSIX:
        assert (env.bytes_dir / record.id).stat().st_mode & 0o777 == 0o700
    [event] = env.ready()
    assert event.artifact_id == record.id
    assert (event.kind, event.name, event.size, event.sha256) == (
        "sent_file",
        "app-release.apk",
        len(data),
        record.sha256,
    )
    assert event.caption == "0.1.9 release build"
    assert event.tool_call_id == "call_1"


def _persisting(env: Env) -> None:
    async def _append(event: DaemonEvent, _log: SessionEventLog) -> None:
        await asyncio.to_thread(env.persist.append_event, env.session.id, event)

    env.session.event_log.subscribe(_append)


def _order(events: list[DaemonEvent]) -> list[str]:
    kinds = (ToolCallEvent, ArtifactReady, ToolResultEvent)
    return [e.type for e in events if isinstance(e, kinds)]


async def _run_send_turn(env: Env, files: list[str]) -> None:
    mock = MockProvider(
        sequences={
            "test-brain": [
                Script(
                    kind="tool_call",
                    tool_name="send_file",
                    tool_arguments=json.dumps({"files": files}),
                ),
                Script(kind="stream", content="Sent."),
            ]
        }
    )
    registry = env.dispatcher.registry
    runner = await start_loop(
        env.session, TierRouter(lead_turns=3), mock, make_config(), registry, env.dispatcher
    )
    await env.session.add_user_message("Send me the files")
    await wait_for_turn(env.session, 1)
    await runner.cancel()


async def test_artifact_ready_between_tool_call_and_tool_result(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.apk", b"one")
    _write(env.ws / "b.pdf", b"two")
    _persisting(env)
    await _run_send_turn(env, ["a.apk", "b.pdf"])
    expected = ["tool_call", "artifact_ready", "artifact_ready", "tool_result"]
    assert _order(env.session.event_log.all_events) == expected
    loaded = env.persist.load(env.session.id)
    assert loaded is not None
    assert _order(loaded.events) == expected  # replay on attach
    replayed = [e for e in loaded.events if isinstance(e, ArtifactReady)]
    assert [e.name for e in replayed] == ["a.apk", "b.pdf"]


async def test_result_text_one_many_caption(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "app-release.apk", b"x" * 46_241_280)
    _write(env.ws / "notes.pdf", b"y" * 1_234)
    one = await env.send(["app-release.apk"])
    assert one.output == "Sent app-release.apk (46.2 MB) to the chat."
    many = await env.send(["app-release.apk", "notes.pdf"])
    assert many.output == (
        "Sent 2 files to the chat: app-release.apk (46.2 MB), notes.pdf (1.2 KB)."
    )
    captioned = await env.send(["notes.pdf"], caption="0.1.9 release build")
    assert captioned.output == 'Sent notes.pdf (1.2 KB) to the chat. Caption: "0.1.9 release build"'
    small = _write(env.ws / "tiny.txt", b"hi")
    assert (await env.send([str(small)])).output == "Sent tiny.txt (2 bytes) to the chat."


async def test_rebuild_after_send_does_not_change_the_copy(tmp_path: Path) -> None:
    env = _env(tmp_path)
    src = _write(env.ws / "app.apk", b"build one")
    assert (await env.send(["app.apk"])).status == "success"
    src.write_bytes(b"build two, longer")
    [record] = env.rows()
    assert (env.bytes_dir / record.id / "app.apk").read_bytes() == b"build one"
    _, target = env.store.open_sent_file(env.session.id, record.id)
    assert read_chunk(target, 0, 100, record.size or 0) == b"build one"


async def test_relative_paths_join_the_workspace(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "out" / "r.txt", "rel")
    assert (await env.send(["out/r.txt"])).status == "success"
    # Called directly, a relative path joins the workspace too, never the cwd.
    [record] = env.service.copy_all(env.session.id, ["out/r.txt"], workspace=env.ws)
    assert record.name == "r.txt"
    assert len(env.rows()) == 2


@pytest.mark.parametrize("escape", ["../outside.txt", "ABS", "link-out"])
async def test_outside_dotdot_symlink_out_are_class_c(tmp_path: Path, escape: str) -> None:
    env = _env(tmp_path)
    _write(env.ws / "ok.txt", "ok")
    outside = _write(tmp_path / "outside.txt", "outside")
    if escape == "ABS":
        escape = str(outside)
    elif escape == "link-out":
        if not POSIX:
            pytest.skip("symlinks need privileges on Windows")
        (env.ws / "link-out").symlink_to(outside)
    result = await env.send(["ok.txt", escape])
    assert result.status == "error"
    assert result.error_code == "boundary_refusal"
    assert result.decision_class is DecisionClass.C
    assert env.rows() == [] and env.ready() == [] and env.on_disk() == []
    # The service's own re-check refuses too, if the wall were ever skipped.
    with pytest.raises(SendError) as ei:
        env.service.copy_all(env.session.id, [str(outside)], workspace=env.ws)
    assert ei.value.code == "boundary_refusal"


async def test_folder_fifo_missing_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    (env.ws / "dist").mkdir()
    folder = await env.send(["dist"])
    assert folder.error_code == "not_a_file"
    assert folder.output == (
        "dist is a folder, not a file. Zip it first (for example with the shell tool), "
        "then send the zip. Nothing was sent."
    )
    missing = await env.send(["nope.apk"])
    assert missing.error_code == "file_missing"
    assert missing.output == "No file at nope.apk. Nothing was sent."
    if hasattr(os, "mkfifo"):
        os.mkfifo(env.ws / "pipe")
        fifo = await asyncio.wait_for(env.send(["pipe"]), timeout=5)
        assert fifo.error_code == "not_a_file"
        assert fifo.output == "pipe is not a regular file."
    assert env.rows() == [] and env.ready() == [] and env.on_disk() == []


async def test_too_large_session_quota_disk_full_refused_and_nothing_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SessionConfig(send_file_max_bytes=1_500, send_file_session_max_bytes=2_300)
    env = _env(tmp_path, config)
    _write(env.ws / "big.bin", b"b" * 1_501)
    _write(env.ws / "ok.bin", b"o" * 1_200)
    big = await env.send(["ok.bin", "big.bin"])
    assert big.error_code == "file_too_large"
    assert big.output == (
        "big.bin is 1.5 KB; the limit is 1.5 KB per file (session.send_file_max_bytes). "
        "Nothing was sent."
    )
    assert env.rows() == [] and env.on_disk() == []

    assert (await env.send(["ok.bin"])).status == "success"
    before = env.on_disk()
    quota = await env.send(["ok.bin"])
    assert quota.error_code == "session_quota"
    assert quota.output == (
        "This session already holds 1.2 KB of sent files; sending 1.2 KB more would pass "
        "2.3 KB (session.send_file_session_max_bytes). Nothing was sent."
    )
    assert env.on_disk() == before and len(env.rows()) == 1

    real = sent_files.shutil.disk_usage

    def tight(path: Any) -> Any:
        usage = real(path)
        return type(usage)(usage.total, usage.used, FREE_MARGIN + 100)

    monkeypatch.setattr(sent_files.shutil, "disk_usage", tight)
    _write(env.ws / "tiny.bin", b"t" * 50)
    _write(env.ws / "tiny2.bin", b"u" * 60)
    full = await env.send(["tiny.bin", "tiny2.bin"])
    assert full.error_code == "disk_full"
    assert full.output == (
        "Not enough free disk space to keep a copy of tiny2.bin (60 bytes). Nothing was sent."
    )
    assert env.on_disk() == before and len(env.rows()) == 1
    assert len(env.ready()) == 1


@pytest.mark.parametrize("how", ["enospc", "not_a_dir"])
async def test_room_check_os_error_is_disk_full_and_names_no_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.txt", "a")
    _write(env.ws / "b.txt", "bb")
    if how == "enospc":

        def full(path: Any) -> Any:
            raise OSError(errno.ENOSPC, "No space left on device", str(path))

        monkeypatch.setattr(sent_files.shutil, "disk_usage", full)
    else:
        # The first send in a session makes artifacts/; a file in its way fails mkdir.
        _write(env.bytes_dir, "in the way")
    result = await env.send(["a.txt", "b.txt"])
    assert result.error_code == "disk_full"
    assert result.output == (
        "Not enough free disk space to keep a copy of a.txt (1 bytes). Nothing was sent."
    )
    assert str(env.persist.dir_for(env.session.id)) not in result.output
    assert str(tmp_path) not in result.output
    assert env.store.sent_bytes_total(env.session.id) == 0 and env.ready() == []


@pytest.mark.parametrize("how", ["grows", "touched"])
async def test_file_changed_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    env = _env(tmp_path)
    src = _write(env.ws / "app.apk", b"a" * 5_000)
    sweep = sent_files._sweep_parts

    def build_still_running(bytes_dir: Path) -> None:
        # Runs after every file is opened and stat'ed, before any copy.
        if how == "grows":
            with src.open("ab") as f:
                f.write(b"more")
        else:
            st = src.stat()
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        sweep(bytes_dir)

    monkeypatch.setattr(sent_files, "_sweep_parts", build_still_running)
    result = await env.send(["app.apk"])
    assert result.error_code == "file_changed"
    assert result.output == (
        "app.apk changed while it was being copied. Wait for the build to finish, then "
        "send it again. Nothing was sent."
    )
    assert env.rows() == [] and env.ready() == [] and env.on_disk() == []


async def test_all_or_nothing_on_second_file_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _write(env.ws / "first.txt", "first")
    second = _write(env.ws / "second.txt", "second")
    sweep = sent_files._sweep_parts

    def grow_second(bytes_dir: Path) -> None:
        with second.open("ab") as f:
            f.write(b"!")
        sweep(bytes_dir)

    monkeypatch.setattr(sent_files, "_sweep_parts", grow_second)
    result = await env.send(["first.txt", "second.txt"])
    assert result.error_code == "file_changed"
    assert env.rows() == [] and env.ready() == [] and env.on_disk() == []

    # A failure while indexing undoes the copies as well.
    monkeypatch.setattr(sent_files, "_sweep_parts", sweep)

    def broken_index(*_args: Any) -> None:
        raise ArtifactError("artifact_store_failed", "Could not persist artifact")

    monkeypatch.setattr(env.store, "append_records", broken_index)
    failed = await env.send(["first.txt", "second.txt"])
    assert failed.error_code == "copy_failed"
    assert failed.output.endswith("Nothing was sent.")
    assert env.ready() == [] and env.on_disk() == []


async def test_send_follows_the_dispatcher_root_after_a_live_move(tmp_path: Path) -> None:
    # send_file re-checks against the guard the dispatcher judged with, as
    # fs_read does, even when the session's root changed under it. (A real
    # move also rebinds that guard: test_artifact_chunks covers it end to end.)
    env = _env(tmp_path)
    _write(env.ws / "app.apk", b"PK-old")
    _write(env.ws / "notes.txt", "hi")
    ws2 = tmp_path / "ws2"
    _write(ws2 / "app.apk", b"PK-new")
    env.session.reassign_workspace(str(ws2))
    sent = await env.send(["app.apk"])
    assert sent.status == "success", sent.output
    read = await env.dispatcher.dispatch("c2", "fs_read", {"path": "notes.txt"}, env.session)
    assert read.status == "success"
    assert (await env.send([str(env.ws / "notes.txt")])).status == "success"
    assert (await env.send([str(ws2 / "app.apk")])).error_code == "boundary_refusal"

    # Once the dispatcher is bound to the new root, sends follow it there.
    env.dispatcher.path_guard = PathGuard(Boundary(workspace_root=ws2))
    assert (await env.send(["app.apk"])).status == "success"
    copies = [
        (env.bytes_dir / r.id / (r.name or "")).read_bytes()
        for r in env.rows()
        if r.name == "app.apk"
    ]
    assert copies == [b"PK-old", b"PK-new"]


async def _worker_done(env: Env) -> None:
    """The copy thread holds the session lock until it has indexed or undone."""
    lock = SentFiles._lock_for(env.session.id)
    assert await asyncio.to_thread(lock.acquire, True, 10)
    lock.release()


async def test_cancel_mid_copy_neither_indexes_nor_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.apk", b"a" * 10)
    _write(env.ws / "b.apk", b"b" * 10)
    copying, release = threading.Event(), threading.Event()
    real = sent_files._write_all

    def write(fd: int, block: bytes) -> None:
        if block[:1] == b"b":  # a.apk is already in its folder
            copying.set()
            release.wait(10)
        real(fd, block)

    monkeypatch.setattr(sent_files, "_write_all", write)
    files = [str(env.ws / "a.apk"), str(env.ws / "b.apk")]
    task = asyncio.ensure_future(
        send_file(session=env.session, tool_call_id="c1", files=files, service=env.service)
    )
    assert await asyncio.to_thread(copying.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await _worker_done(env)
    assert env.rows() == [] and env.ready() == [] and env.on_disk() == []
    assert env.store.sent_bytes_total(env.session.id) == 0


async def test_cancel_while_indexing_still_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.apk", b"a" * 10)
    indexing, release = threading.Event(), threading.Event()
    real = env.store.append_records

    def append(session_id: str, records: list[ArtifactRecord]) -> None:
        indexing.set()
        release.wait(10)
        real(session_id, records)

    monkeypatch.setattr(env.store, "append_records", append)
    task = asyncio.ensure_future(
        send_file(
            session=env.session,
            tool_call_id="c1",
            files=[str(env.ws / "a.apk")],
            service=env.service,
        )
    )
    assert await asyncio.to_thread(indexing.wait, 10)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _worker_done(env)
    [row] = env.rows()
    assert [e.artifact_id for e in env.ready()] == [row.id]


async def test_dedupes_repeated_paths(tmp_path: Path) -> None:
    env = _env(tmp_path)
    a = _write(env.ws / "a.txt", "a")
    _write(env.ws / "b.txt", "b")
    result = await env.send(["a.txt", "./a.txt", str(a), "b.txt", "a.txt"])
    assert result.output == "Sent 2 files to the chat: a.txt (1 bytes), b.txt (1 bytes)."
    assert [r.name for r in env.rows()] == ["a.txt", "b.txt"]


async def test_unavailable_without_service(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.txt", "a")
    with pytest.raises(Exception) as ei:
        await send_file(session=env.session, files=["a.txt"], service=None)
    assert getattr(ei.value, "code", None) == "send_unavailable"
    assert str(ei.value) == "Sending files is not available in this session."

    bare = Session(str(env.ws))  # never persisted: no persist_dir
    with pytest.raises(Exception) as ei:
        await send_file(session=bare, files=["a.txt"], service=env.service)
    assert getattr(ei.value, "code", None) == "send_unavailable"

    # Through the dispatcher a missing service is a typed error, not a crash.
    plain = ToolDispatcher(
        create_registry(),
        classifier=make_classifier(str(env.ws)),
        path_guard=PathGuard(Boundary(workspace_root=env.ws)),
        workspace=env.ws,
    )
    plain.policy = PolicyConfig()
    register_builtin_handlers(plain)
    result = await plain.dispatch("c1", "send_file", {"files": ["a.txt"]}, env.session)
    assert result.error_code == "send_unavailable"


@pytest.mark.parametrize(("skip_all", "autonomy"), [(False, False), (True, False), (False, True)])
async def test_classified_a_no_card_in_every_approval_mode(
    tmp_path: Path, skip_all: bool, autonomy: bool
) -> None:
    env = _env(tmp_path)
    env.dispatcher.skip_all_fn = lambda: skip_all
    env.dispatcher.autonomy_fn = lambda: autonomy
    _write(env.ws / "a.txt", "a")
    _write(env.ws / "b.txt", "b")
    result = await env.send(["a.txt", "b.txt"])
    assert result.status == "success"
    assert result.decision_class is DecisionClass.A
    assert env.approvals == []
    # A user's own ask rule still shows a card; never still refuses.
    env.dispatcher.policy = PolicyConfig(rules=[PolicyRule(tool="send_file", effect="never")])
    refused = await env.send(["a.txt"])
    assert refused.error_code == "policy_denied"


def test_an_mcp_tool_named_send_file_gets_no_grant(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    impostor = Tool(
        name="send_file",
        parameters=SEND_FILE_TOOL.parameters,
        side_effect_class="auto",
        path_fields=("files",),
        provenance="mcp:srv",
    )
    request = build_decision_request(impostor, {"files": [str(ws / "a.txt")]})
    decision = DecisionClassifier(Boundary(workspace_root=ws)).classify(request)
    assert decision.decision_class is not DecisionClass.A
    assert decision.rule is None or decision.rule.id != "send-file-in-workspace"
    builtin = build_decision_request(SEND_FILE_TOOL, {"files": [str(ws / "a.txt")]})
    granted = DecisionClassifier(Boundary(workspace_root=ws)).classify(builtin)
    assert granted.decision_class is DecisionClass.A


def _audit_files(root: Path) -> list[tuple[str, bytes]]:
    """The audit DB and its WAL/SHM side files, raw."""
    files = [(p.name, p.read_bytes()) for p in root.glob("audit.db*")]
    assert files
    return files


async def test_bytes_never_reach_logs_events_or_audit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env = _env(tmp_path)
    _write(env.ws / "secret-notes.txt", f"line one\n{MARKER}\nline three\n")
    _persisting(env)
    store = AuditStore(tmp_path / "audit.db")
    writer = AuditWriter(store)
    writer.start()
    writer.attach_session(env.session)
    await _run_send_turn(env, ["secret-notes.txt"])
    await writer._queue.join()
    await writer.close()
    [record] = env.rows()
    _, target = env.store.open_sent_file(env.session.id, record.id)
    assert MARKER.encode() in target.read_bytes()  # the copy is verbatim
    assert MARKER not in caplog.text
    for rec in caplog.records:
        extra = json.dumps(getattr(rec, "extra_fields", {}), default=str)
        assert MARKER not in rec.getMessage() and MARKER not in extra
    events = (env.persist.dir_for(env.session.id) / "events.jsonl").read_text("utf-8")
    assert "secret-notes.txt" in events and MARKER not in events
    for name, data in _audit_files(tmp_path):
        assert MARKER.encode() not in data, name
    assert any(r.getMessage() == "file sent" for r in caplog.records)


async def test_survives_restart_and_session_delete_removes_copy(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "app.apk", b"apk bytes")
    assert (await env.send(["app.apk"])).status == "success"
    [record] = env.rows()
    again = ArtifactStore(SessionPersist(tmp_path / "data"))  # a restarted daemon
    found, target = again.open_sent_file(env.session.id, record.id)
    assert found == record
    assert read_chunk(target, 0, 9, 9) == b"apk bytes"
    assert [r.id for r in again.list_records(env.session.id, env.ws)] == [record.id]
    env.persist.remove(env.session.id)
    with pytest.raises(ArtifactError) as ei:
        again.open_sent_file(env.session.id, record.id)
    assert ei.value.code == "artifact_not_found"


def test_old_index_rows_still_load(tmp_path: Path) -> None:
    persist = SessionPersist(tmp_path / "data")
    persist.prepare("s1")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("doc", encoding="utf-8")
    old = {"id": "a1", "session_id": "s1", "mime": "text/markdown", "path": "doc.md"}
    old |= {"title": "Doc", "location": "workspace"}
    (persist.dir_for("s1") / "artifacts.json").write_text(json.dumps([old]), "utf-8")
    store = ArtifactStore(persist)
    [record] = store.list_records("s1", ws)
    assert record.kind == "artifact"
    assert (record.name, record.size, record.sha256, record.caption) == (None,) * 4
    assert store.sent_bytes_total("s1") == 0


async def test_parallel_sends_keep_every_record(tmp_path: Path) -> None:
    env = _env(tmp_path)
    names = [f"f{i}.txt" for i in range(8)]
    for name in names:
        _write(env.ws / name, name)
    results = await asyncio.gather(*(env.send([name]) for name in names))
    assert all(r.status == "success" for r in results)
    assert sorted(r.name for r in env.rows()) == sorted(names)
    assert len(env.ready()) == len(names)
    assert env.store.sent_bytes_total(env.session.id) == sum(len(n) for n in names)


async def test_stale_part_files_swept(tmp_path: Path) -> None:
    env = _env(tmp_path)
    # A daemon that already loaded this session: from here on only the hour rule
    # applies, since a fresh part may be a copy in flight.
    assert env.store.sent_bytes_total(env.session.id) == 0
    env.bytes_dir.mkdir(parents=True)
    stale = _write(env.bytes_dir / f".{'a' * 32}.part", "crash leftover")
    fresh = _write(env.bytes_dir / f".{'b' * 32}.part", "copy in flight")
    other = _write(env.bytes_dir / ".notes.part", "not ours")
    hour_ago = time.time() - 3_700
    for path in (stale, other):
        os.utime(path, (hour_ago, hour_ago))
    _write(env.ws / "a.txt", "a")
    assert (await env.send(["a.txt"])).status == "success"
    assert not stale.exists()
    assert fresh.exists() and other.exists()


_CRASH_CHILD = """
import os, sys
from pathlib import Path
from tstd import sent_files
from tstd.artifacts import ArtifactStore
from tstd.config import SessionConfig
from tstd.session_persist import SessionPersist

data, ws, sid = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
real = sent_files._write_all

def write(fd, block):
    if block[:1] == b"B":
        os._exit(9)  # killed while copying the second file
    real(fd, block)

sent_files._write_all = write
store = ArtifactStore(SessionPersist(data))
sent_files.SentFiles(store, SessionConfig()).copy_all(
    sid, [str(ws / "a.apk"), str(ws / "b.apk")], workspace=ws
)
"""


def test_crash_mid_send_leftovers_swept_when_the_store_loads(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _write(env.ws / "kept.txt", "kept")
    env.service.copy_all(env.session.id, [str(env.ws / "kept.txt")], workspace=env.ws)
    _write(env.ws / "a.apk", b"A" * 4_096)
    _write(env.ws / "b.apk", b"B" * 4_096)
    done = subprocess.run(
        [sys.executable, "-c", _CRASH_CHILD, str(tmp_path / "data"), str(env.ws), env.session.id],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == 9, done.stderr[-500:]
    folders = [p for p in env.bytes_dir.iterdir() if p.is_dir()]
    assert len(folders) == 2  # the kept send and the crashed a.apk copy
    assert any(p.name.endswith(".part") for p in env.bytes_dir.iterdir())
    # Not ours: a TD-3201 content file, an odd folder and a stray file stay.
    content = env.store.record(env.session.id, "Doc", "text/plain", env.ws, content=b"doc")
    _write(env.bytes_dir / "notes" / "x.txt", "x")
    _write(env.bytes_dir / ("c" * 32), "a plain file, not a sent-file folder")

    restarted = ArtifactStore(env.persist)  # the daemon after the crash
    assert restarted.sent_bytes_total(env.session.id) == 4
    rows = restarted.list_records(env.session.id, env.ws)
    assert sorted(r.title for r in rows) == ["Doc", "kept.txt"]
    left = sorted(p.name for p in env.bytes_dir.iterdir())
    kept = next(r for r in rows if r.kind == "sent_file")
    assert left == sorted([kept.id, content.id, "notes", "c" * 32])


def test_leftover_sweep_runs_once_and_never_takes_a_copy_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    _write(env.ws / "a.txt", "a")
    _write(env.ws / "b.txt", "b")
    real = sent_files._write_all
    seen: list[int] = []

    def write(fd: int, block: bytes) -> None:
        if block == b"b":  # a.txt's folder exists and is not indexed yet
            env.store.list_records(env.session.id, env.ws)
            seen.append(sum(p.is_dir() for p in env.bytes_dir.iterdir()))
        real(fd, block)

    monkeypatch.setattr(sent_files, "_write_all", write)
    records = env.service.copy_all(
        env.session.id, [str(env.ws / "a.txt"), str(env.ws / "b.txt")], workspace=env.ws
    )
    assert seen == [1] and len(records) == 2
    assert all((env.bytes_dir / r.id / r.name).is_file() for r in records if r.name)


def test_leftover_sweep_keeps_folders_the_index_names_or_cannot_be_read(tmp_path: Path) -> None:
    env = _env(tmp_path)
    future, orphan = "d" * 32, "e" * 32
    _write(env.bytes_dir / future / "new.bin", "a row this daemon cannot validate")
    _write(env.bytes_dir / orphan / "old.bin", "no row at all")
    index = env.persist.dir_for(env.session.id) / "artifacts.json"
    index.write_text(json.dumps([{"id": future, "kind": "from-a-newer-daemon"}]))
    assert ArtifactStore(env.persist).sent_bytes_total(env.session.id) == 0
    assert (env.bytes_dir / future).is_dir() and not (env.bytes_dir / orphan).exists()

    # An unreadable index names nothing for sure, so nothing is removed.
    _write(env.bytes_dir / orphan / "old.bin", "no row at all")
    index.write_text("{not json")
    assert ArtifactStore(env.persist).sent_bytes_total(env.session.id) == 0
    assert (env.bytes_dir / orphan).is_dir()


def test_leftover_sweep_runs_only_for_known_sessions(tmp_path: Path) -> None:
    """A store bound to the registry sweeps a session only under its exact id.

    On a case-insensitive disk ``S1`` and ``s1`` are one folder: an unknown
    spelling must neither sweep it nor use up the known id's one sweep.
    """
    env = _env(tmp_path)
    orphan = env.bytes_dir / ("e" * 32)
    _write(orphan / "old.bin", "a crash leftover")
    sid = env.session.id
    store = ArtifactStore(env.persist, known_session=lambda s: s == sid)
    spelled = sid.upper() if sid.upper() != sid else f"{sid}-x"
    real = env.persist.dir_for
    folded = {spelled: sid}
    env.persist.dir_for = lambda s: real(folded.get(s, s))  # type: ignore[method-assign]

    assert store.sent_bytes_total(spelled) == 0
    assert orphan.is_dir()
    assert store.sent_bytes_total(sid) == 0
    assert not orphan.exists()


# ── Names and MIME (§1.4, §1.5) ────────────────────────────────────────

_NAME_VECTORS = [
    ("app-release.apk", "app-release.apk"),
    ("android/app/build/outputs/apk/release/app-release.apk", "app-release.apk"),
    ("C:\\out\\App.apk", "App.apk"),
    ('a:b*c?d"e<f>g|h.txt', "a_b_c_d_e_f_g_h.txt"),
    ("report.pdf. . ", "report.pdf"),
    ("CON", "_CON"),
    ("con.tar.gz", "_con.tar.gz"),
    ("COM1 .log", "_COM1 .log"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("..", "file"),
    ("", "file"),
    ("\u0007bell\u007f.txt", "bell.txt"),
    (".env", ".env"),
    ("x" * 200 + ".apk", "x" * 116 + ".apk"),
    ("é" * 100, "é" * 60),
    ("a" * 130 + ".verylongextension12", "a" * 120),
    ("name with spaces.zip", "name with spaces.zip"),
    ("CON" + " " * 150 + "y.log", "_CON" + " " * 112 + ".log"),
    ("a\u202efdp.exe", "afdp.exe"),
]


@pytest.mark.parametrize(("raw", "expected"), _NAME_VECTORS)
def test_safe_file_name_vectors_and_idempotent(raw: str, expected: str) -> None:
    assert safe_file_name(raw) == expected
    assert safe_file_name(expected) == expected
    assert len(expected.encode("utf-8")) <= 120


def test_safe_file_name_drops_every_bidi_control() -> None:
    # Step 2b: a bidi override can show "invoice<RLO>fdp.exe" as a PDF name.
    bidi = "\u200e\u200f" + "".join(map(chr, range(0x202A, 0x202F)))
    bidi += "".join(map(chr, range(0x2066, 0x206A)))
    assert safe_file_name(f"in{bidi}voice.exe") == "invoice.exe"
    # Neighbours of the ranges are ordinary characters and stay.
    kept = "\u200d\u2029\u202f\u2065\u206a"
    assert safe_file_name(f"a{kept}b.txt") == f"a{kept}b.txt"


def test_safe_file_name_handles_undecodable_names() -> None:
    raw = os.fsdecode(b"build-\xff.apk") if sys.platform != "win32" else "build-\udcff.apk"
    name = safe_file_name(raw)
    assert name == "build-\ufffd.apk"
    name.encode("utf-8")  # always encodable


def test_mime_table_fixed() -> None:
    table = {
        "a.apk": "application/vnd.android.package-archive",
        "a.aab": "application/octet-stream",
        "a.pdf": "application/pdf",
        "a.png": "image/png",
        "a.jpg": "image/jpeg",
        "a.jpeg": "image/jpeg",
        "a.gif": "image/gif",
        "a.webp": "image/webp",
        "a.svg": "image/svg+xml",
        "a.mp4": "video/mp4",
        "a.webm": "video/webm",
        "a.mov": "video/quicktime",
        "a.mp3": "audio/mpeg",
        "a.wav": "audio/wav",
        "a.m4a": "audio/mp4",
        "a.zip": "application/zip",
        "a.tar": "application/x-tar",
        "a.tar.gz": "application/gzip",
        "a.tgz": "application/gzip",
        "a.7z": "application/x-7z-compressed",
        "a.txt": "text/plain",
        "a.log": "text/plain",
        "a.md": "text/markdown",
        "a.csv": "text/csv",
        "a.json": "application/json",
        "a.html": "text/html",
        "a.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "a.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "a.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "a.dmg": "application/x-apple-diskimage",
        "APP-RELEASE.APK": "application/vnd.android.package-archive",
        "Report.PDF": "application/pdf",
        "setup.exe": "application/octet-stream",
        "setup.msi": "application/octet-stream",
        "Tool.AppImage": "application/octet-stream",
        "README": "application/octet-stream",
        ".pdf": "application/octet-stream",
    }
    for name, mime in table.items():
        assert mime_for(name) == mime, name
    source = Path(sent_files.__file__).read_text(encoding="utf-8")
    assert "import mimetypes" not in source  # mimetypes reads host files


async def test_name_on_disk_equals_name_on_wire_after_redaction(tmp_path: Path) -> None:
    env = _env(tmp_path)
    key_id = "AKIA" + "ABCDEFGHIJKLMNOP"
    _write(env.ws / f"creds-{key_id}.txt", "x")
    _write(env.ws / "CON.txt", "y")
    assert (await env.send([f"creds-{key_id}.txt", "CON.txt"])).status == "success"
    by_name = {r.name: r for r in env.rows()}
    assert set(by_name) == {"creds-[REDACTED].txt", "_CON.txt"}
    for event in env.ready():
        record = by_name[event.name or ""]
        assert os.listdir(env.bytes_dir / record.id) == [event.name]
        assert safe_file_name(event.name or "") == event.name
    assert key_id not in json.dumps([e.model_dump() for e in env.ready()])
