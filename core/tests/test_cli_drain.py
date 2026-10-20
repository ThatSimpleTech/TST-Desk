"""TD-5023: ``tst drain`` against a real daemon, and with none running.

TD-5155: a turn paused for the person does not hold a deploy up unless
``--strict``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tests.test_queued_restart import _same_read, paused_session
from tstd import cli
from tstd.cli import CliError
from tstd.cli_drain import drain_frame, exit_code
from tstd.mock import MockProvider, Script
from tstd.ws import create_port_file_path


@pytest.fixture
async def fleet(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fleet]:
    made = Fleet(monkeypatch)
    try:
        yield made
    finally:
        await made.close()


def _slow(seconds: float) -> MockProvider:
    words = " ".join(f"w{i}" for i in range(10))
    return MockProvider(default=Script(kind="stream", content=words, chunk_delay=seconds / 10))


async def _tst(*argv: str) -> int:
    """The real entry point, on its own thread: it runs its own event loop."""
    return await asyncio.to_thread(cli.main, list(argv))


class TestParser:
    def test_defaults(self) -> None:
        ns = cli.parse_args(["drain"])
        assert ns.command == "drain"
        assert (ns.timeout, ns.hold, ns.release, ns.json) == (30, 120, False, False)
        assert ns.strict is False
        assert cli.parse_args(["drain", "--strict"]).strict is True

    def test_frames(self) -> None:
        assert drain_frame(120, 60, False) == {"type": "drain", "timeout_s": 120, "hold_s": 60}
        assert drain_frame(120, 60, True) == {"type": "drain", "timeout_s": 0, "hold_s": 0}
        with pytest.raises(CliError):
            drain_frame(601, 0, False)
        with pytest.raises(CliError):
            drain_frame(0, -1, False)


def test_no_daemon_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir = tmp_path / "nobody"
    assert cli.main(["drain", "--data-dir", str(data_dir)]) == 1
    assert "No TST Desk daemon is running" in capsys.readouterr().err
    # It never starts one.
    assert not create_port_file_path(data_dir).exists()


async def test_idle_daemon_exits_0(
    tmp_path: Path, fleet: Fleet, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "d"
    daemon = await fleet.start(data_dir, provider=_slow(0.1))
    async with window(daemon) as ws:
        await ask(ws, {"type": "open_workspace", "path": str(tmp_path)}, "session_state")
    assert await _tst("drain", "--timeout", "5", "--hold", "30", "--data-dir", str(data_dir)) == 0
    assert capsys.readouterr().out.startswith("Drained. New turns are held until ")
    assert daemon._drain.active

    assert await _tst("drain", "--release", "--json", "--data-dir", str(data_dir)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["type"] == "drain_result" and result["hold_until"] is None
    assert not daemon._drain.active


async def test_turn_outliving_the_timeout_exits_2_with_its_id(
    tmp_path: Path, fleet: Fleet, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "d"
    daemon = await fleet.start(data_dir, provider=_slow(30.0))
    async with window(daemon) as ws:
        opened = await ask(ws, {"type": "open_workspace", "path": str(tmp_path)}, "session_state")
        sid = str(opened["session_id"])
        await ws.send(json.dumps({"type": "attach", "session_id": sid, "from_seq": 1}))
        await ws.send(json.dumps({"type": "user_message", "session_id": sid, "content": "go"}))
        await recv_until(ws, lambda f: f.get("type") == "assistant_delta")
        code = await _tst("drain", "--timeout", "1", "--hold", "0", "--data-dir", str(data_dir))
    assert code == 2
    out = capsys.readouterr().out
    assert f"{sid}  running" in out
    # Ids only: the message never shows.
    assert "go" not in out.replace(sid, "")


class TestExitCode:
    """Only a running turn holds a deploy up, unless --strict (TD-5155)."""

    def test_waiting_on_the_user_is_0_unless_strict(self) -> None:
        for waiting in ({"paused": ["s1"]}, {"awaiting_approval": ["s2"]}):
            result = {"drained": False, "running": [], **waiting}
            assert exit_code(result, strict=False) == 0
            assert exit_code(result, strict=True) == 2

    def test_a_running_turn_is_2_either_way(self) -> None:
        result = {"drained": False, "running": ["s1"], "paused": [], "awaiting_approval": []}
        assert exit_code(result, strict=False) == 2
        assert exit_code(result, strict=True) == 2

    def test_an_older_daemon_lists_a_paused_turn_as_running(self) -> None:
        assert exit_code({"drained": False, "running": ["s1"]}, strict=False) == 2

    def test_drained_is_0_either_way(self) -> None:
        result = {"drained": True, "running": [], "paused": [], "awaiting_approval": []}
        assert exit_code(result, strict=False) == 0
        assert exit_code(result, strict=True) == 0


async def test_a_guard_paused_turn_does_not_hold_up_a_deploy(
    tmp_path: Path, fleet: Fleet, capsys: pytest.CaptureFixture[str]
) -> None:
    """2026-10-19: drain called the guard-paused session running, so every deploy waited."""
    work = tmp_path / "ws"
    work.mkdir()
    data_dir = tmp_path / "d"
    daemon = await fleet.start(data_dir, provider=_same_read(work))
    flags = ("--timeout", "1", "--hold", "0", "--data-dir", str(data_dir))
    async with window(daemon) as ws:
        sid = await paused_session(ws, work)
        assert await _tst("drain", *flags) == 0
        out = capsys.readouterr().out
        assert out.startswith("Drained.")
        assert f"{sid}  paused, waiting on the user" in out
        assert f"{sid}  running" not in out

        assert await _tst("drain", "--strict", *flags) == 2
        out = capsys.readouterr().out
        assert out.startswith("Still open:")
        assert f"{sid}  paused, waiting on the user" in out
