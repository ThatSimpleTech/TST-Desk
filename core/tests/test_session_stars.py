"""Machine-wide session stars (TD-3003)."""

from __future__ import annotations

import json
from pathlib import Path

from tstd.daemon import Daemon
from tstd.session_stars import load_session_stars, save_session_stars


def test_absent_is_empty(tmp_path: Path) -> None:
    assert load_session_stars(tmp_path) == []


def test_roundtrip(tmp_path: Path) -> None:
    save_session_stars(tmp_path, ["s-one", "s-two"])
    assert load_session_stars(tmp_path) == ["s-one", "s-two"]


def test_junk_is_empty(tmp_path: Path) -> None:
    (tmp_path / "session_stars.yaml").write_text("nope\n", encoding="utf-8")
    assert load_session_stars(tmp_path) == []


def _ensure(workspace: Path) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


async def _open(daemon: Daemon, workspace: Path) -> str:
    workspace = _ensure(workspace)
    reply = await daemon._handle_message(
        json.dumps({"type": "open_workspace", "path": str(workspace)}),
        None,
    )
    assert reply is not None
    return str(json.loads(reply)["session_id"])


async def _list(daemon: Daemon) -> list[dict[str, object]]:
    raw = await daemon._handle_list_sessions()
    return list(json.loads(raw)["sessions"])


class TestDaemonStars:
    async def test_star_persists_and_sorts_above_newer(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        older = await _open(daemon, tmp_path / "older")
        newer = await _open(daemon, tmp_path / "newer")
        listed = await _list(daemon)
        assert next(s["session_id"] for s in listed) == newer

        reply = await daemon._handle_message(
            json.dumps({"type": "set_session_star", "session_id": older, "starred": True}),
            None,
        )
        assert reply is not None
        rows = json.loads(reply)["sessions"]
        assert rows[0]["session_id"] == older
        assert rows[0]["starred"] is True
        assert rows[1]["session_id"] == newer
        assert rows[1]["starred"] is False
        assert load_session_stars(tmp_path / "data") == [older]

        runner = daemon.session_registry.get_runner(older)
        if runner is not None:
            await runner.cancel()
        runner = daemon.session_registry.get_runner(newer)
        if runner is not None:
            await runner.cancel()
        await daemon._shutdown()

    async def test_unknown_id_is_session_not_found(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        reply = await daemon._handle_message(
            json.dumps({"type": "set_session_star", "session_id": "missing", "starred": True}),
            None,
        )
        assert reply is not None
        body = json.loads(reply)
        assert body["type"] == "error"
        assert body["code"] == "session_not_found"
        await daemon._shutdown()

    async def test_delete_drops_the_star(self, tmp_path: Path) -> None:
        daemon = Daemon(data_dir=tmp_path / "data")
        sid = await _open(daemon, tmp_path / "ws")
        await daemon._handle_message(
            json.dumps({"type": "set_session_star", "session_id": sid, "starred": True}),
            None,
        )
        await daemon._handle_message(
            json.dumps({"type": "delete_session", "session_id": sid}),
            None,
        )
        assert load_session_stars(tmp_path / "data") == []
        await daemon._shutdown()
