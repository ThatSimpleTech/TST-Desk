"""Artifact record (TD-3201): store, wall, and list/open protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tstd.artifacts import ArtifactError, ArtifactStore
from tstd.daemon import Daemon
from tstd.protocol import Artifact, ArtifactList, ArtifactReady, parse_daemon_event
from tstd.session_persist import SessionPersist
from tstd.tools.registry import create_registry


def _store(tmp_path: Path) -> tuple[ArtifactStore, Path, Path, SessionPersist]:
    data = tmp_path / "data"
    persist = SessionPersist(data)
    persist.prepare("s1")
    ws = tmp_path / "ws"
    ws.mkdir()
    return ArtifactStore(persist), ws, persist.dir_for("s1"), persist


async def _open(daemon: Daemon, path: Path) -> str:
    raw = json.dumps({"type": "open_workspace", "path": str(path)})
    response = await daemon._handle_message(raw, None)
    assert response is not None
    session_id: str = json.loads(response)["session_id"]
    return session_id


async def _shutdown(daemon: Daemon, session_id: str) -> None:
    runner = daemon.session_registry.get_runner(session_id)
    if runner is not None:
        await runner.cancel()
    await daemon._shutdown()


class TestStore:
    def test_workspace_path_round_trips(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, persist = _store(tmp_path)
        (ws / "notes.md").write_text("# hi\n", encoding="utf-8")
        rec = store.record("s1", "Notes", "text/markdown", ws, path="notes.md")
        assert rec.session_id == "s1"
        assert rec.mime == "text/markdown"
        assert rec.title == "Notes"
        assert rec.path == "notes.md"
        assert rec.location == "workspace"
        assert rec.id
        listed = store.list_records("s1", ws)
        assert [r.id for r in listed] == [rec.id]
        assert store.get("s1", rec.id, ws).path == "notes.md"
        # Survives a new store over the same persist dir.
        again = ArtifactStore(persist).list_records("s1", ws)
        assert again[0].id == rec.id
        assert again[0].title == "Notes"

    def test_bytes_land_in_session_dir(self, tmp_path: Path) -> None:
        store, ws, persist_dir, _persist = _store(tmp_path)
        rec = store.record("s1", "Preview", "text/html", ws, content=b"<p>ok</p>")
        assert rec.location == "session"
        assert rec.path == f"artifacts/{rec.id}"
        dest = persist_dir / "artifacts" / rec.id
        assert dest.read_bytes() == b"<p>ok</p>"
        assert dest.stat().st_mode & 0o777 == 0o600

    def test_absolute_persist_path_is_accepted(self, tmp_path: Path) -> None:
        store, ws, persist_dir, _persist = _store(tmp_path)
        dest = persist_dir / "artifacts" / "already"
        dest.parent.mkdir()
        dest.write_bytes(b"kept")
        rec = store.record("s1", "Kept", "text/plain", ws, path=str(dest))
        assert rec.location == "session"
        assert rec.path == "artifacts/already"

    def test_path_and_content_together_refused(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        (ws / "notes.md").write_text("x\n", encoding="utf-8")
        with pytest.raises(ArtifactError) as ei:
            store.record("s1", "x", "text/plain", ws, path="notes.md", content=b"no")
        assert ei.value.code == "bad_request"

    def test_unknown_id(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        with pytest.raises(ArtifactError) as ei:
            store.get("s1", "missing", ws)
        assert ei.value.code == "artifact_not_found"


class TestWall:
    """Table-driven path escape — these are security tests."""

    @pytest.mark.parametrize(
        "raw",
        [
            "../escape.md",
            "../../etc/passwd",
            "/etc/passwd",
            r"\\server\share\file",
        ],
        ids=["dotdot", "deep-dotdot", "absolute", "unc"],
    )
    def test_workspace_escape_refused(self, tmp_path: Path, raw: str) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        (tmp_path / "escape.md").write_text("no\n", encoding="utf-8")
        with pytest.raises(ArtifactError) as ei:
            store.record("s1", "Nope", "text/plain", ws, path=raw)
        assert ei.value.code in {"outside_workspace", "windows_unsafe"}
        assert store.list_records("s1", ws) == []

    def test_sibling_file_refused(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        outsider = tmp_path / "other" / "x.md"
        outsider.parent.mkdir()
        outsider.write_text("nope\n", encoding="utf-8")
        with pytest.raises(ArtifactError) as ei:
            store.record("s1", "Nope", "text/plain", ws, path=str(outsider))
        assert ei.value.code == "outside_workspace"

    def test_symlink_out_refused(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        outside = tmp_path / "secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (ws / "link.md").symlink_to(outside)
        with pytest.raises(ArtifactError) as ei:
            store.record("s1", "Nope", "text/plain", ws, path="link.md")
        assert ei.value.code == "outside_workspace"

    def test_other_session_persist_dir_refused(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, persist = _store(tmp_path)
        persist.prepare("s2")
        other = persist.dir_for("s2") / "artifacts" / "x"
        other.parent.mkdir()
        other.write_bytes(b"x")
        with pytest.raises(ArtifactError) as ei:
            store.record("s1", "Nope", "text/plain", ws, path=str(other))
        assert ei.value.code == "outside_workspace"

    def test_session_id_cannot_leave_sessions_dir(self, tmp_path: Path) -> None:
        store, ws, _persist_dir, _persist = _store(tmp_path)
        with pytest.raises(ArtifactError) as ei:
            store.record("../evil", "Nope", "text/plain", ws, content=b"x")
        assert ei.value.code == "session_not_found"

    def test_tampered_workspace_path_dropped_from_list(self, tmp_path: Path) -> None:
        store, ws, persist_dir, _persist = _store(tmp_path)
        (ws / "ok.md").write_text("ok\n", encoding="utf-8")
        rec = store.record("s1", "Ok", "text/plain", ws, path="ok.md")
        meta = persist_dir / "artifacts.json"
        rows = json.loads(meta.read_text(encoding="utf-8"))
        rows[0]["path"] = "../escape.md"
        meta.write_text(json.dumps(rows), encoding="utf-8")
        assert store.list_records("s1", ws) == []
        with pytest.raises(ArtifactError) as ei:
            store.get("s1", rec.id, ws)
        assert ei.value.code == "outside_workspace"


class TestDaemonProtocol:
    async def test_record_emits_artifact_ready(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "doc.md").write_text("# doc\n", encoding="utf-8")
        sid = await _open(daemon, ws)
        rec = await daemon.record_artifact(sid, title="Doc", mime="text/markdown", path="doc.md")
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        ready = [e for e in sess.event_log.events_from(1) if isinstance(e, ArtifactReady)]
        assert len(ready) == 1
        assert ready[0].artifact_id == rec.id
        assert ready[0].title == "Doc"
        assert ready[0].path == "doc.md"
        assert ready[0].mime == "text/markdown"
        await _shutdown(daemon, sid)

    async def test_list_and_open(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        sid = await _open(daemon, ws)
        rec = await daemon.record_artifact(
            sid, title="Preview", mime="text/html", content=b"<p>hi</p>"
        )

        listed_raw = await daemon._handle_message(
            json.dumps({"type": "list_artifacts", "session_id": sid}),
            None,
        )
        assert listed_raw is not None
        listed = parse_daemon_event(listed_raw)
        assert isinstance(listed, ArtifactList)
        assert listed.session_id == sid
        assert listed.seq == 1
        assert [a.artifact_id for a in listed.artifacts] == [rec.id]
        assert listed.artifacts[0].path == f"artifacts/{rec.id}"

        opened_raw = await daemon._handle_message(
            json.dumps(
                {
                    "type": "open_artifact",
                    "session_id": sid,
                    "artifact_id": rec.id,
                }
            ),
            None,
        )
        assert opened_raw is not None
        opened = parse_daemon_event(opened_raw)
        assert isinstance(opened, Artifact)
        assert opened.artifact_id == rec.id
        assert opened.path == rec.path
        assert "content" not in json.loads(opened_raw)

        missing = await daemon._handle_message(
            json.dumps(
                {
                    "type": "open_artifact",
                    "session_id": sid,
                    "artifact_id": "no-such",
                }
            ),
            None,
        )
        assert missing is not None
        err: dict[str, Any] = json.loads(missing)
        assert err["type"] == "error"
        assert err["code"] == "artifact_not_found"
        assert err["session_id"] == sid
        await _shutdown(daemon, sid)

    async def test_list_unknown_session(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        raw = await daemon._handle_message(
            json.dumps({"type": "list_artifacts", "session_id": "missing"}),
            None,
        )
        assert raw is not None
        err = json.loads(raw)
        assert err["code"] == "session_not_found"
        await daemon._shutdown()

    async def test_record_unknown_session(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        with pytest.raises(ArtifactError) as ei:
            await daemon.record_artifact("missing", title="x", mime="text/plain", content=b"x")
        assert ei.value.code == "session_not_found"
        await daemon._shutdown()

    async def test_wall_refusal_does_not_emit(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        sid = await _open(daemon, ws)
        with pytest.raises(ArtifactError) as ei:
            await daemon.record_artifact(sid, title="Nope", mime="text/plain", path="../escape.md")
        assert ei.value.code == "outside_workspace"
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        assert not any(isinstance(e, ArtifactReady) for e in sess.event_log.events_from(1))
        await _shutdown(daemon, sid)

    async def test_secret_in_title_is_redacted_on_ready(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        ws = tmp_path / "ws"
        ws.mkdir()
        sid = await _open(daemon, ws)
        key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        rec = await daemon.record_artifact(
            sid, title=f"key {key}", mime="text/plain", content=b"body"
        )
        sess = daemon.session_registry.get(sid)
        assert sess is not None
        ready = [e for e in sess.event_log.events_from(1) if isinstance(e, ArtifactReady)]
        assert ready[0].title == "key [REDACTED]"
        assert key not in ready[0].title
        # Disk keeps the real title so open can still name it after redact.
        assert rec.title.startswith("key sk-")
        await _shutdown(daemon, sid)


def test_not_a_tool() -> None:
    names = {tool.name for tool in create_registry().list_tools()}
    assert "record_artifact" not in names
    assert "list_artifacts" not in names
    assert "open_artifact" not in names
