"""Port file removal on clean shutdown, and the stale-file log (TD-4848).

A file is removed only when its pid names this process. A crash leftover
is replaced at startup and logged at INFO with that dead pid.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import json
import logging
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tstd.console_shutdown import (
    CTRL_BREAK_EVENT,
    CTRL_C_EVENT,
    CTRL_CLOSE_EVENT,
    CTRL_LOGOFF_EVENT,
    CTRL_SHUTDOWN_EVENT,
    install_windows_console_shutdown,
    on_console_close,
    remove_windows_console_shutdown,
)
from tstd.port_file import port_file_path, release_port_file, write_port_file
from tstd.ws import WebSocketServer


def test_release_only_removes_the_named_pid(tmp_path: Path) -> None:
    me = os.getpid()
    cases: list[tuple[str, str | None, int | None, bool]] = [
        ("own", json.dumps({"port": 1, "token": "t", "pid": me}), None, True),
        ("foreign", json.dumps({"port": 1, "token": "t", "pid": me + 1}), None, False),
        ("missing", None, None, False),
        ("not-json", "not-json", None, False),
        ("list", "[]", None, False),
        ("empty-object", "{}", None, False),
        ("bool-pid", '{"pid": true}', None, False),
        ("string-pid", '{"pid": "1"}', None, False),
        ("zero-pid", '{"pid": 0}', None, False),
        ("explicit-match", json.dumps({"pid": 42, "token": "t"}), 42, True),
        ("explicit-miss", json.dumps({"pid": 42, "token": "t"}), 43, False),
    ]
    for name, body, owner, removed in cases:
        directory = tmp_path / name
        directory.mkdir()
        path = port_file_path(directory)
        if body is not None:
            path.write_text(body, encoding="utf-8")
        kwargs: dict[str, int] = {}
        if owner is not None:
            kwargs["pid"] = owner
        assert release_port_file(directory, **kwargs) is removed, name
        if removed:
            assert not path.exists(), name
            assert release_port_file(directory, **kwargs) is False, name
        elif body is None:
            assert not path.exists(), name
        else:
            assert path.is_file(), name


def test_release_does_not_treat_true_as_pid_one(tmp_path: Path) -> None:
    path = port_file_path(tmp_path)
    path.write_text(json.dumps({"pid": 1, "token": "t"}), encoding="utf-8")
    assert release_port_file(tmp_path, pid=True) is False  # type: ignore[arg-type]
    assert path.is_file()


def test_stale_port_file_logs_info_with_the_dead_pid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token = "stale-token-value"
    port_file_path(tmp_path).write_text(
        json.dumps({"port": 1, "token": token, "pid": 4242}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.DEBUG, logger="tstd.ws"):
        write_port_file(tmp_path, 9, "fresh-token")
    stale = [r for r in caplog.records if r.getMessage() == "stale port file detected, replacing"]
    assert len(stale) == 1
    assert stale[0].levelno == logging.INFO
    fields = stale[0].extra_fields
    assert fields["path"] == str(port_file_path(tmp_path))
    assert fields["pid"] == 4242
    assert token not in caplog.text
    assert "fresh-token" not in caplog.text
    body = json.loads(port_file_path(tmp_path).read_text(encoding="utf-8"))
    assert body["port"] == 9
    assert body["pid"] == os.getpid()
    assert not any(r.levelno >= logging.WARNING for r in stale)


def test_unreadable_stale_port_file_logs_info_without_a_pid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    port_file_path(tmp_path).write_text("not-json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="tstd.ws"):
        write_port_file(tmp_path, 9, "fresh-token")
    stale = [r for r in caplog.records if r.getMessage() == "stale port file detected, replacing"]
    assert len(stale) == 1
    assert stale[0].levelno == logging.INFO
    assert stale[0].extra_fields["pid"] is None
    assert "fresh-token" not in caplog.text
    body = json.loads(port_file_path(tmp_path).read_text(encoding="utf-8"))
    assert body["port"] == 9


@pytest.mark.parametrize(
    ("event", "handled"),
    [
        (CTRL_CLOSE_EVENT, True),
        (CTRL_LOGOFF_EVENT, True),
        (CTRL_SHUTDOWN_EVENT, True),
        (CTRL_C_EVENT, False),
        (CTRL_BREAK_EVENT, False),
        (3, False),
    ],
)
def test_console_close_events_release_only_our_file(
    tmp_path: Path, event: int, handled: bool
) -> None:
    write_port_file(tmp_path, 1, "t")
    path = port_file_path(tmp_path)
    calls: list[int] = []
    assert on_console_close(event, tmp_path, lambda: calls.append(1)) is handled
    assert calls == ([1] if handled else [])
    assert path.exists() == (not handled)


def test_console_close_still_shuts_down_when_the_pid_is_foreign(tmp_path: Path) -> None:
    write_port_file(tmp_path, 1, "t")
    path = port_file_path(tmp_path)
    foreign = os.getpid() + 1
    body = json.loads(path.read_text(encoding="utf-8"))
    body["pid"] = foreign
    path.write_text(json.dumps(body), encoding="utf-8")
    calls: list[int] = []
    assert on_console_close(CTRL_CLOSE_EVENT, tmp_path, lambda: calls.append(1)) is True
    assert calls == [1]
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == foreign


def test_windows_console_install_is_a_no_op_off_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("the no-op is the non-Windows branch")
    previous = signal.getsignal(signal.SIGINT)
    install_windows_console_shutdown(lambda: None, Path("."))
    remove_windows_console_shutdown()
    assert signal.getsignal(signal.SIGINT) == previous


def _identity_winfunctype(*_types: object) -> Any:
    def decorate(fn: Any) -> Any:
        return fn

    return decorate


class _Kernel:
    def __init__(self) -> None:
        self.handler: Any = None
        self.calls: list[tuple[Any, bool]] = []

    def SetConsoleCtrlHandler(self, handler: Any, add: bool) -> int:
        self.calls.append((handler, bool(add)))
        self.handler = handler if add else None
        return 1


async def test_installed_console_handler_matches_the_signal_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close releases on the console thread. Ctrl+C only schedules shutdown."""
    monkeypatch.setattr(sys, "platform", "win32")
    kernel = _Kernel()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    # macOS ctypes has no WINFUNCTYPE. The fake returns the Python
    # function so the test can invoke the handler the installer stored.
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", _identity_winfunctype, raising=False)

    watched = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if isinstance(sigbreak, int):
        watched.append(sigbreak)
    previous = {sig: signal.getsignal(sig) for sig in watched}
    hits = 0
    ready = asyncio.Event()

    def callback() -> None:
        nonlocal hits
        hits += 1
        ready.set()

    write_port_file(tmp_path, 1, "t")
    path = port_file_path(tmp_path)
    try:
        install_windows_console_shutdown(callback, tmp_path)
        assert kernel.handler is not None
        assert int(kernel.handler(CTRL_C_EVENT)) == 0
        assert hits == 0
        assert path.is_file()

        sigint = signal.getsignal(signal.SIGINT)
        assert callable(sigint)
        sigint(signal.SIGINT, None)
        await asyncio.wait_for(ready.wait(), 1)
        assert hits == 1
        assert path.is_file()

        ready.clear()
        assert int(kernel.handler(CTRL_CLOSE_EVENT)) == 1
        assert not path.exists()
        await asyncio.wait_for(ready.wait(), 1)
        assert hits == 2
    finally:
        remove_windows_console_shutdown()
    assert [added for _handler, added in kernel.calls] == [True, False]
    for sig, old in previous.items():
        assert signal.getsignal(sig) == old


async def _silent_websocket(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Complete the HTTP upgrade and then never read, so close is not acked."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    key = base64.b64encode(b"0123456789abcdef").decode("ascii")
    writer.write(
        (
            "GET / HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), 2)
    if b" 101 " not in status:
        raise AssertionError(f"websocket upgrade failed: {status!r}")
    while True:
        line = await asyncio.wait_for(reader.readline(), 2)
        if line in (b"\r\n", b""):
            break
    return reader, writer


async def test_stop_bounds_a_client_that_never_acks_close(tmp_path: Path) -> None:
    server = WebSocketServer(tmp_path)
    await server.start()
    writer: asyncio.StreamWriter | None = None
    try:
        _reader, writer = await _silent_websocket(server.port)
        await asyncio.wait_for(server.stop(), 3)
        assert not port_file_path(tmp_path).exists()
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
        await server.stop()


async def test_stop_leaves_a_foreign_port_file(tmp_path: Path) -> None:
    server = WebSocketServer(tmp_path)
    await server.start()
    path = port_file_path(tmp_path)
    foreign = os.getpid() + 1
    body = json.loads(path.read_text(encoding="utf-8"))
    body["pid"] = foreign
    path.write_text(json.dumps(body), encoding="utf-8")
    await server.stop()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == foreign
