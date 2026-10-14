"""TD-5014: ``tst pair`` and ``tst devices [remove <id>]`` against a real daemon."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.test_pairing import pair, running_daemon
from tstd import cli
from tstd.cli_pairing import list_devices, pair_device, remove_device
from tstd.daemon import Daemon


@contextlib.asynccontextmanager
async def daemon_in(tmp_path: Path) -> AsyncIterator[tuple[Daemon, Path]]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    async with running_daemon(data_dir) as daemon:
        yield daemon, data_dir


class TestParser:
    def test_pair_and_no_wait(self, tmp_path: Path) -> None:
        ns = cli.parse_args(["pair", "--no-wait", "--data-dir", str(tmp_path)])
        assert ns.command == "pair" and ns.no_wait is True and ns.data_dir == tmp_path
        assert cli.parse_args(["pair"]).no_wait is False

    def test_devices_lists_by_default(self) -> None:
        ns = cli.parse_args(["devices"])
        assert ns.command == "devices" and ns.devices_command is None

    @pytest.mark.parametrize(
        "argv",
        [
            ["devices", "remove", "dev_0123456789ab", "--data-dir", "{d}"],
            ["devices", "--data-dir", "{d}", "remove", "dev_0123456789ab"],
        ],
    )
    def test_devices_remove_keeps_data_dir_on_either_side(
        self, tmp_path: Path, argv: list[str]
    ) -> None:
        ns = cli.parse_args([a.replace("{d}", str(tmp_path)) for a in argv])
        assert ns.devices_command == "remove"
        assert ns.device_id == "dev_0123456789ab"
        assert ns.data_dir == tmp_path


async def test_pair_no_wait_prints_the_code_and_leaves_it_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with daemon_in(tmp_path) as (daemon, data_dir):
        assert await pair_device(data_dir, wait=False) == 0
        assert daemon.pairing.active
    out = capsys.readouterr().out
    assert re.search(r"^Code:    [0-9A-Z]{4}-[0-9A-Z]{4}$", out, re.M), out
    assert f"Address: 100.64.1.5:{daemon.ws_server.port}" in out
    assert "Link:    tstdesk://pair?h=100.64.1.5&p=" in out
    assert "The code expires in 10 minutes." in out


async def _printed_code(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Wait for ``tst pair`` to print its code. Returns (code, output so far)."""
    seen = ""
    for _ in range(200):
        seen += capsys.readouterr().out
        found = re.search(r"^Code:    (\w{4})-(\w{4})$", seen, re.M)
        if found and "Waiting for the phone" in seen:
            return found[1] + found[2], seen
        await asyncio.sleep(0.02)
    raise AssertionError(seen)


async def test_pair_waits_for_the_phone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async with daemon_in(tmp_path) as (daemon, data_dir):
        waiting = asyncio.create_task(pair_device(data_dir))
        code, _ = await _printed_code(capsys)
        reply, _ = await pair(daemon.ws_server.port, code, name="Pixel")
        assert reply["type"] == "pair_ok"
        assert await asyncio.wait_for(waiting, 5) == 0
    assert "Paired: Pixel" in capsys.readouterr().out


async def test_cancelling_the_wait_cancels_the_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with daemon_in(tmp_path) as (daemon, data_dir):
        waiting = asyncio.create_task(pair_device(data_dir))
        code, _ = await _printed_code(capsys)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        for _ in range(100):
            if not daemon.pairing.active:
                break
            await asyncio.sleep(0.02)
        assert not daemon.pairing.active
        reply, _ = await pair(daemon.ws_server.port, code)
        assert reply["code"] == "pair_failed"


async def test_devices_lists_and_removes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with daemon_in(tmp_path) as (daemon, data_dir):
        assert await list_devices(data_dir) == 0
        assert "No paired devices. Run `tst pair` to add one." in capsys.readouterr().out

        phone, _ = daemon.remote_devices.add("Pixel")
        assert await list_devices(data_dir) == 0
        listed = capsys.readouterr().out
        assert phone.id in listed and "Pixel" in listed and "last seen" in listed

        assert await remove_device(data_dir, "dev_000000000000") == 1
        assert "No paired device dev_000000000000." in capsys.readouterr().err
        assert await remove_device(data_dir, phone.id) == 0
        assert "Removed: Pixel" in capsys.readouterr().out
        assert phone.id not in daemon.remote_devices


async def test_no_tailscale_is_a_plain_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    async with running_daemon(data_dir, dict):
        assert await pair_device(data_dir, wait=False) == 1
    assert "Tailscale isn't running" in capsys.readouterr().err
