"""TD-5143: ``read_artifact_chunk`` serves the bytes of a file the agent sent.

Driven through the daemon's own message handler (the reply funnel that
scrubs every direct reply), and over real sockets for the phone and the
bridged viewer. The wall is the index row plus the disk: only a
well-formed ``sent_file`` copy is ever read, by id, never by path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.machine_helpers import FakeKeychain, Fleet, ask, eventually, window
from tests.test_artifacts import _open, _shutdown
from tests.test_two_daemons import VIEWER, _pair, _Steps
from tstd import peer_policy
from tstd.artifacts import ArtifactRecord
from tstd.daemon import Daemon
from tstd.mock import Script
from tstd.protocol import (
    CHUNK_MAX,
    ApprovalRequest,
    ArtifactChunk,
    ArtifactReady,
    ReadArtifactChunk,
    ToolResult,
    parse_daemon_event,
)
from tstd.sent_files import SentFiles

POSIX = os.name == "posix"
MARKER = b"TD5143-CHUNK-MARKER-41d7"
# Bytes whose base64 starts with an AWS key id shape: the scrub would eat it.
AKIA_BYTES = base64.b64decode("AKIA" + "ABCDEFGHIJKLMNOP")


def _send(daemon: Daemon, sid: str, ws: Path, name: str, data: bytes) -> ArtifactRecord:
    (ws / name).write_bytes(data)
    service = SentFiles(daemon._artifacts, daemon.config.session)
    [record] = service.copy_all(sid, [name], workspace=ws)
    return record


async def _chunk(daemon: Daemon, sid: str, aid: str, offset: int, **extra: Any) -> dict[str, Any]:
    frame = {"type": "read_artifact_chunk", "session_id": sid, "artifact_id": aid}
    frame |= {"offset": offset, **extra}
    raw = await daemon._handle_message(json.dumps(frame), None)
    assert raw is not None
    reply: dict[str, Any] = json.loads(raw)
    return reply


@pytest.fixture
async def one(tmp_path: Path) -> AsyncIterator[tuple[Daemon, str, Path]]:
    daemon = Daemon(data_dir=tmp_path / "data")
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = await _open(daemon, ws)
    try:
        yield daemon, sid, ws
    finally:
        await _shutdown(daemon, sid)


async def test_reads_in_chunks_and_hash_matches(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, ws = one
    data = AKIA_BYTES + MARKER + os.urandom(CHUNK_MAX * 2 + 1_000)
    record = _send(daemon, sid, ws, "app-release.apk", data)
    got = b""
    offset = 0
    replies: list[dict[str, Any]] = []
    while True:
        reply = await _chunk(daemon, sid, record.id, offset)
        assert reply["type"] == "artifact_chunk" and reply["error_code"] is None, reply
        assert parse_daemon_event(json.dumps(reply)) == ArtifactChunk.model_validate(reply)
        replies.append(reply)
        piece = base64.b64decode(reply["data_b64"], validate=True)
        assert len(piece) <= CHUNK_MAX
        got += piece
        offset += len(piece)
        if reply["eof"]:
            break
    assert len(replies) == 3
    assert replies[0]["data_b64"].startswith("AKIA" + "ABCDEFGHIJKLMNOP")  # survived the scrub
    assert got == data
    assert hashlib.sha256(got).hexdigest() == record.sha256
    assert {(r["size"], r["sha256"], r["seq"]) for r in replies} == {(len(data), record.sha256, 1)}
    # A short length is honoured, mid-file.
    mid = await _chunk(daemon, sid, record.id, 10, length=7)
    assert base64.b64decode(mid["data_b64"]) == data[10:17]
    assert mid["eof"] is False


async def test_eof_and_offset_at_size_is_empty_eof(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "notes.txt", b"0123456789")
    tail = await _chunk(daemon, sid, record.id, 4, length=6)
    assert (base64.b64decode(tail["data_b64"]), tail["eof"]) == (b"456789", True)
    at_end = await _chunk(daemon, sid, record.id, 10)
    assert (at_end["data_b64"], at_end["eof"], at_end["error_code"]) == ("", True, None)
    empty = _send(daemon, sid, ws, "empty.log", b"")
    first = await _chunk(daemon, sid, empty.id, 0)
    assert (first["data_b64"], first["eof"], first["size"]) == ("", True, 0)
    assert first["sha256"] == hashlib.sha256(b"").hexdigest()


async def test_offset_past_size_is_bad_range(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "notes.txt", b"0123456789")
    reply = await _chunk(daemon, sid, record.id, 11)
    assert reply["type"] == "artifact_chunk"
    assert (reply["error_code"], reply["data_b64"], reply["size"]) == ("bad_range", "", 0)


async def test_length_over_cap_rejected_by_model(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, _ws = one
    aid = "0" * 32
    with pytest.raises(ValidationError):
        ReadArtifactChunk(session_id=sid, artifact_id=aid, offset=0, length=CHUNK_MAX + 1)
    for bad in ({"length": CHUNK_MAX + 1}, {"length": 0}, {"offset": -1}):
        frame = {"offset": 0} | bad
        reply = await _chunk(daemon, sid, aid, frame.pop("offset"), **frame)
        assert reply["type"] == "error", bad
    # The verb takes no path: an extra one is ignored, and the id decides.
    ignored = await _chunk(daemon, sid, aid, 0, path="/etc/passwd")
    assert (ignored["error_code"], ignored["data_b64"]) == ("artifact_not_found", "")


async def test_unknown_id_and_unsafe_session_are_artifact_not_found(
    one: tuple[Daemon, str, Path],
) -> None:
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "notes.txt", b"notes")
    assert (await _chunk(daemon, sid, "f" * 32, 0))["error_code"] == "artifact_not_found"
    for unsafe in ("..", ".", "../" + sid, f"{sid}/..", "/etc", "no-such-session"):
        reply = await _chunk(daemon, unsafe, record.id, 0)
        assert reply["type"] == "artifact_chunk", unsafe
        assert reply["error_code"] == "artifact_not_found", unsafe
    # A malformed id never reaches the store: the model refuses it.
    assert (await _chunk(daemon, sid, "../artifacts.json", 0))["type"] == "error"


async def test_td3201_path_and_content_artifacts_are_not_a_sent_file(
    one: tuple[Daemon, str, Path],
) -> None:
    daemon, sid, ws = one
    (ws / "doc.md").write_text("# doc\n", encoding="utf-8")
    by_path = await daemon.record_artifact(sid, title="Doc", mime="text/markdown", path="doc.md")
    by_content = await daemon.record_artifact(
        sid, title="Preview", mime="text/html", content=b"<p>hi</p>"
    )
    for record in (by_path, by_content):
        reply = await _chunk(daemon, sid, record.id, 0)
        assert (reply["error_code"], reply["data_b64"]) == ("not_a_sent_file", ""), record.path


async def test_list_and_open_carry_sent_file_fields(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "app.apk", b"apk")
    raw = await daemon._handle_message(
        json.dumps({"type": "list_artifacts", "session_id": sid}), None
    )
    assert raw is not None
    [entry] = json.loads(raw)["artifacts"]
    assert (entry["kind"], entry["name"], entry["size"], entry["sha256"]) == (
        "sent_file",
        "app.apk",
        3,
        record.sha256,
    )
    opened = await daemon._handle_message(
        json.dumps({"type": "open_artifact", "session_id": sid, "artifact_id": record.id}), None
    )
    assert opened is not None
    reply = json.loads(opened)
    assert (reply["type"], reply["kind"], reply["name"]) == ("artifact", "sent_file", "app.apk")
    assert reply["path"] == f"artifacts/{record.id}"


@pytest.mark.skipif(not POSIX, reason="symlinks need privileges on Windows")
async def test_symlink_swapped_in_refused(one: tuple[Daemon, str, Path], tmp_path: Path) -> None:
    daemon, sid, ws = one
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    record = _send(daemon, sid, ws, "secret.txt", b"sent!!")
    folder = daemon._artifacts.bytes_dir(sid) / record.id
    copy = folder / "secret.txt"
    copy.unlink()
    copy.symlink_to(outside)  # same size, so only the type check stops it
    reply = await _chunk(daemon, sid, record.id, 0)
    assert (reply["error_code"], reply["data_b64"]) == ("not_a_sent_file", "")
    # The folder swapped for a link to another folder holding the same name.
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "secret.txt").write_bytes(b"sent!!")
    copy.unlink()
    folder.rmdir()
    folder.symlink_to(other, target_is_directory=True)
    reply = await _chunk(daemon, sid, record.id, 0)
    assert (reply["error_code"], reply["data_b64"]) == ("not_a_sent_file", "")


async def test_size_change_is_changed(one: tuple[Daemon, str, Path]) -> None:
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "app.apk", b"original")
    copy = daemon._artifacts.bytes_dir(sid) / record.id / "app.apk"
    with copy.open("ab") as f:
        f.write(b"+tampered")
    assert (await _chunk(daemon, sid, record.id, 0))["error_code"] == "changed"
    copy.write_bytes(b"x")
    assert (await _chunk(daemon, sid, record.id, 0))["error_code"] == "changed"
    copy.unlink()
    assert (await _chunk(daemon, sid, record.id, 0))["error_code"] == "artifact_not_found"


async def test_works_without_a_loaded_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = Daemon(data_dir=data_dir)
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = await _open(first, ws)
    record = _send(first, sid, ws, "app.apk", b"kept on disk")
    await _shutdown(first, sid)
    fresh = Daemon(data_dir=data_dir)  # never started: no session is loaded
    assert fresh.session_registry.get(sid) is None
    reply = await _chunk(fresh, sid, record.id, 0)
    assert reply["error_code"] is None, reply
    assert base64.b64decode(reply["data_b64"]) == b"kept on disk"
    await fresh._shutdown()


async def test_chunks_not_in_event_log_and_never_logged(
    one: tuple[Daemon, str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    daemon, sid, ws = one
    record = _send(daemon, sid, ws, "notes.txt", MARKER * 3)
    caplog.clear()
    for offset in (0, 5, 9_999):
        await _chunk(daemon, sid, record.id, offset)
    await _chunk(daemon, sid, "f" * 32, 0)
    session = daemon.session_registry.get(sid)
    assert session is not None
    assert not any(e.type == "artifact_chunk" for e in session.event_log.all_events)
    events = daemon._session_persist.dir_for(sid) / "events.jsonl"
    if events.exists():
        assert "artifact_chunk" not in events.read_text(encoding="utf-8")
    texts = [r.getMessage() + json.dumps(getattr(r, "extra_fields", {})) for r in caplog.records]
    assert not any(MARKER.decode() in t for t in texts)
    assert not any(base64.b64encode(MARKER).decode()[:16] in t for t in texts)
    # Success and bad_range log nothing; a store refusal logs its code and id only.
    chunk_lines = [r for r in caplog.records if "artifact chunk" in r.getMessage()]
    assert [getattr(r, "extra_fields", {}) for r in chunk_lines] == [
        {"artifact_id": "f" * 32, "code": "artifact_not_found"}
    ]


# ── over real sockets ─────────────────────────────────────────────────


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


async def _open_on(ws_conn: Any, workspace: Path) -> str:
    opened = await ask(ws_conn, {"type": "open_workspace", "path": str(workspace)}, "session_state")
    return str(opened["session_id"])


def _read(sid: str, aid: str) -> dict[str, Any]:
    return {"type": "read_artifact_chunk", "session_id": sid, "artifact_id": aid, "offset": 0}


async def test_phone_peer_can_read(tmp_path: Path, fleet: Fleet) -> None:
    daemon = await fleet.start(tmp_path / "a")
    work = tmp_path / "ws"
    work.mkdir()
    async with window(daemon) as local:
        sid = await _open_on(local, work)
    record = _send(daemon, sid, work, "app-release.apk", b"apk for the phone")
    phone_key = daemon.remote_devices.add("Pixel")[1]
    async with window(daemon, VIEWER, token=phone_key, path="/phone") as phone:
        reply = await ask(phone, _read(sid, record.id), "artifact_chunk")
    assert reply["type"] == "artifact_chunk", reply
    assert reply["error_code"] is None
    assert base64.b64decode(reply["data_b64"]) == b"apk for the phone"
    assert reply["eof"] is True


async def test_bridged_viewer_forwarded_and_old_list_gets_not_on_remote(
    tmp_path: Path, fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeKeychain().install(monkeypatch)
    work = tmp_path / "ws-b"
    work.mkdir()
    b = await fleet.start(tmp_path / "b")
    a = await fleet.start(tmp_path / "a")
    async with window(b) as b_screen, window(a, VIEWER) as hub:
        sid = await _open_on(b_screen, work)
        record = _send(b, sid, work, "app.apk", AKIA_BYTES + b" from B")
        mch = await _pair(hub, b)
        remote = f"{sid}:{mch}"
        reply = await ask(hub, _read(remote, record.id), "artifact_chunk")
        assert reply["type"] == "artifact_chunk", reply
        assert reply["session_id"] == remote
        assert reply["error_code"] is None
        assert base64.b64decode(reply["data_b64"]) == AKIA_BYTES + b" from B"
        assert reply["sha256"] == record.sha256
        # A hub whose forward list predates the verb refuses it by name.
        monkeypatch.setattr(
            peer_policy, "FORWARDED_VERBS", peer_policy.FORWARDED_VERBS - {"read_artifact_chunk"}
        )
        old = await ask(hub, _read(remote, record.id), "artifact_chunk")
        assert (old["type"], old["code"], old["session_id"]) == ("error", "not_on_remote", remote)


async def test_a_daemon_session_sends_with_no_card_and_serves_the_bytes(tmp_path: Path) -> None:
    """The daemon wires ``SentFiles`` into every session it builds.

    A scripted ``send_file`` runs in the default approval mode with no card,
    and the bytes it sent come back over ``read_artifact_chunk``.
    """
    call = json.dumps({"files": ["app-release.apk"], "caption": "build"})
    steps = [
        Script(kind="tool_call", tool_name="send_file", tool_arguments=call),
        Script(kind="stream", content="Sent."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    ws = tmp_path / "ws"
    ws.mkdir()
    data = MARKER + os.urandom(2_000)
    (ws / "app-release.apk").write_bytes(data)
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        message = {"type": "user_message", "session_id": sid, "content": "send me the build"}
        await daemon._handle_message(json.dumps(message), None)
        log = session.event_log
        await eventually(lambda: any(isinstance(e, ToolResult) for e in log.all_events), within=20)
        events = log.all_events
        [result] = [e for e in events if isinstance(e, ToolResult)]
        assert result.status == "success", result.output
        assert not [e for e in events if isinstance(e, ApprovalRequest)]
        [ready] = [e for e in events if isinstance(e, ArtifactReady)]
        assert (ready.kind, ready.name, ready.caption) == ("sent_file", "app-release.apk", "build")
        reply = await _chunk(daemon, sid, ready.artifact_id, 0)
        assert (base64.b64decode(reply["data_b64"]), reply["eof"]) == (data, True)
    finally:
        await _shutdown(daemon, sid)


async def test_a_daemon_session_still_sends_after_a_live_move(tmp_path: Path) -> None:
    """TD-1715 moves a live session; send_file judges paths as the dispatcher does."""
    call = json.dumps({"files": ["app-release.apk"]})
    steps = [
        Script(kind="tool_call", tool_name="send_file", tool_arguments=call),
        Script(kind="stream", content="Sent."),
    ]
    daemon = Daemon(data_dir=tmp_path / "data", provider=_Steps(steps))
    ws, ws2 = tmp_path / "ws", tmp_path / "ws2"
    for root in (ws, ws2):
        root.mkdir()
        (root / "app-release.apk").write_bytes(MARKER + root.name.encode())
    sid = await _open(daemon, ws)
    try:
        session = daemon.session_registry.get(sid)
        assert session is not None
        move = {"type": "move_session", "session_id": sid, "workspace_path": str(ws2)}
        await daemon._handle_message(json.dumps(move), None)
        assert session.workspace_path == str(ws2)
        message = {"type": "user_message", "session_id": sid, "content": "send me the build"}
        await daemon._handle_message(json.dumps(message), None)
        log = session.event_log
        await eventually(lambda: any(isinstance(e, ToolResult) for e in log.all_events), within=20)
        [result] = [e for e in log.all_events if isinstance(e, ToolResult)]
        assert result.status == "success", result.output
        [ready] = [e for e in log.all_events if isinstance(e, ArtifactReady)]
        reply = await _chunk(daemon, sid, ready.artifact_id, 0)
        assert base64.b64decode(reply["data_b64"]).startswith(MARKER)
    finally:
        await _shutdown(daemon, sid)
