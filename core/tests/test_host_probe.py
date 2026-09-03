"""Host permission probe over ``cu-agent.sock`` (TD-4823).

The daemon asks the Tauri host for its diagnosis before it trusts the
driver. No socket → ``None`` (driver fallback). A live socket answers one
JSON line; ``reset`` sends ``permissions reset``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from tstd.cu_host import SOCK_ENV
from tstd.desktop.host_probe import host_permissions, host_socket, transact

_DARWIN = sys.platform == "darwin"


def _short_dir(name: str) -> Path:
    """A fresh dir under /tmp, not tmp_path: AF_UNIX paths cap at 104 bytes on macOS."""
    base = Path("/tmp") / name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir()
    return base


async def test_no_socket_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOCK_ENV, raising=False)
    assert host_socket(tmp_path) is None
    assert await host_permissions(tmp_path) is None
    assert await host_permissions(tmp_path, reset=True) is None


@pytest.mark.skipif(not _DARWIN, reason="the host socket only exists on macOS")
async def test_round_trip_sends_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _short_dir("tst-cu-probe-test")
    sock = base / "cu-agent.sock"
    seen: list[str] = []
    reply = {
        "platform": "macos",
        "screen_recording": {"granted": False},
        "accessibility": {"granted": True},
        "all_granted": False,
        "actuation_path": "host",
        "stale_grant_suspected": {"screen_recording": True, "accessibility": False},
    }

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        seen.append(line.decode().strip())
        writer.write(json.dumps(reply).encode() + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    monkeypatch.delenv(SOCK_ENV, raising=False)
    try:
        assert host_socket(base) == sock
        report = await host_permissions(base, reset=True)
        assert report is not None
        assert report["stale_grant_suspected"]["screen_recording"] is True
        assert seen == ["permissions reset"]
        # The env override wins over the data dir.
        monkeypatch.setenv(SOCK_ENV, str(sock))
        assert host_socket(Path("/nowhere")) == sock
        assert await host_permissions(Path("/nowhere")) is not None
        assert seen == ["permissions reset", "permissions"]
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not _DARWIN, reason="the host socket only exists on macOS")
async def test_non_json_reply_is_none() -> None:
    base = _short_dir("tst-cu-probe-junk")
    sock = base / "cu-agent.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"err\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        assert await asyncio.to_thread(transact, sock, "permissions") is None
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(base, ignore_errors=True)
