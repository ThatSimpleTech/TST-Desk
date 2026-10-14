"""TD-5023: ``tst drain`` against a real daemon, and with none running."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.machine_helpers import Fleet, ask, recv_until, window
from tstd import cli
from tstd.cli import CliError
from tstd.cli_drain import drain_frame
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
