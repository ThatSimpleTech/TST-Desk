"""One kill state for the whole machine (TD-5223).

Stop in TST Desk creates the tst-cu-mcp stop-file, which every computer-use
client checks before it acts; Resume removes it. A stop-file made outside
TST Desk shows as engaged. Every path here is under ``tmp_path`` (and the
autouse throwaway HOME): no test touches a real ``~/.tst-cu-mcp/STOP``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tstd import cu_stopfile
from tstd.cu_host import dispatch
from tstd.daemon import Daemon
from tstd.desktop import MockDesktopDriver
from tstd.protocol import CuKillState, parse_daemon_event


@pytest.fixture
def stop_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the tst-cu-mcp config at a temp file whose stop_file is ours."""
    target = tmp_path / "custom" / "STOP"
    config = tmp_path / "cu-config.yaml"
    config.write_text(f"killswitch:\n  stop_file: {target}\n", encoding="utf-8")
    monkeypatch.setenv("TST_CU_MCP_CONFIG", str(config))
    return target


def _exists(path: Path) -> bool:
    return path.exists()


def _make(path: Path) -> None:
    """Create the stop-file by hand, as an outside tool would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _remove(path: Path) -> None:
    path.unlink()


def _daemon(tmp_path: Path) -> tuple[Daemon, MockDesktopDriver]:
    daemon = Daemon(data_dir=tmp_path / "data")
    mock = MockDesktopDriver()
    daemon.desktop_driver = mock
    return daemon, mock


async def _set_kill(daemon: Daemon, killed: bool) -> CuKillState:
    raw = await daemon._handle_message(json.dumps({"type": "set_cu_kill", "killed": killed}), None)
    assert raw is not None
    event = parse_daemon_event(raw)
    assert isinstance(event, CuKillState)
    return event


class TestPath:
    def test_default_is_under_home(self) -> None:
        assert cu_stopfile.stop_file_path() == Path.home() / ".tst-cu-mcp" / "STOP"

    def test_env_beats_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TST_CU_MCP_STOP_FILE", str(tmp_path / "env-stop"))
        assert cu_stopfile.stop_file_path() == tmp_path / "env-stop"

    def test_config_beats_env(
        self, stop_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TST_CU_MCP_STOP_FILE", str(tmp_path / "env-stop"))
        assert cu_stopfile.stop_file_path() == stop_file

    def test_env_flag_counts_as_engaged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert cu_stopfile.stop_engaged() is False
        monkeypatch.setenv("TST_CU_MCP_STOP", "1")
        assert cu_stopfile.stop_engaged() is True


class TestProtocol:
    async def test_stop_creates_and_resume_removes_the_file(
        self, stop_file: Path, tmp_path: Path
    ) -> None:
        daemon, mock = _daemon(tmp_path)
        assert not _exists(stop_file)

        event = await _set_kill(daemon, True)
        assert event.killed is True
        assert _exists(stop_file)
        assert mock.killed is True

        event = await _set_kill(daemon, False)
        assert event.killed is False
        assert not _exists(stop_file)
        assert mock.killed is False

    async def test_resume_with_no_file_is_fine(self, stop_file: Path, tmp_path: Path) -> None:
        daemon, _ = _daemon(tmp_path)
        assert (await _set_kill(daemon, False)).killed is False
        assert not _exists(stop_file)

    async def test_env_flag_keeps_it_engaged_on_resume(
        self, stop_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TST_CU_MCP_STOP", "yes")
        daemon, mock = _daemon(tmp_path)
        assert (await _set_kill(daemon, False)).killed is True
        assert mock.killed is True


class _Server:
    """Records broadcasts instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def broadcast(self, payload: str, **_: Any) -> int:
        self.sent.append(payload)
        return 1


class TestOutsideFile:
    async def test_outside_file_engages_and_broadcasts(
        self, stop_file: Path, tmp_path: Path
    ) -> None:
        daemon, mock = _daemon(tmp_path)
        server = _Server()
        daemon.ws_server = server  # type: ignore[assignment]
        watch = asyncio.create_task(daemon._cu_stop_watch(interval=0.01))
        try:
            _make(stop_file)  # by hand, not through TST Desk
            for _ in range(200):
                if server.sent:
                    break
                await asyncio.sleep(0.01)
            assert mock.killed is True
            first = parse_daemon_event(server.sent[0])
            assert isinstance(first, CuKillState) and first.killed is True

            _remove(stop_file)
            for _ in range(200):
                if len(server.sent) > 1:
                    break
                await asyncio.sleep(0.01)
            assert mock.killed is False
            second = parse_daemon_event(server.sent[1])
            assert isinstance(second, CuKillState) and second.killed is False
        finally:
            watch.cancel()
        # The watcher never rewrote the file: the change came from outside.
        assert not _exists(stop_file)

    async def test_resume_from_ui_clears_an_outside_file(
        self, stop_file: Path, tmp_path: Path
    ) -> None:
        _make(stop_file)
        daemon, _ = _daemon(tmp_path)
        await _set_kill(daemon, False)
        assert not _exists(stop_file)

    async def test_new_window_hears_engaged(self, stop_file: Path, tmp_path: Path) -> None:
        daemon, _ = _daemon(tmp_path)

        class _Conn:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, payload: str) -> None:
                self.sent.append(payload)

        quiet = _Conn()
        await daemon._on_handshake(quiet)
        assert not any('"cu_kill_state"' in p for p in quiet.sent)

        daemon.set_computer_use_killed(True, write_file=False)
        told = _Conn()
        await daemon._on_handshake(told)
        events = [parse_daemon_event(p) for p in told.sent if '"cu_kill_state"' in p]
        assert len(events) == 1
        assert isinstance(events[0], CuKillState) and events[0].killed is True


class TestFallbackSocket:
    """The daemon's own cu-agent fallback refuses the same way (TD-5223)."""

    ACTING = (
        "move 1 2",
        "click 1 2 left 1",
        "key cmd+space",
        "scroll 0 -1",
        "type aGk=",
        'json {"op":"ui_act","pid":1}',
    )

    def test_actuation_refused_while_stopped(self, stop_file: Path) -> None:
        _make(stop_file)
        for line in self.ACTING:
            header, body = dispatch(line)
            assert header.strip() != b"ok", line
            assert body is None
            if line.startswith("json "):
                assert json.loads(header)["ok"] is False
            else:
                assert header == b"err stopped\n"

    def test_reads_still_served_while_stopped(self, stop_file: Path) -> None:
        _make(stop_file)
        header, _ = dispatch("permissions")
        assert header.startswith(b"{")
        assert dispatch("quit") == (b"ok\n", None)
