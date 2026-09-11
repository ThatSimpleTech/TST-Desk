"""Computer-use must run inside the sidecar, not checkout Python."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from tstd import cu_host


def test_checkout_src_finds_this_repo() -> None:
    src = cu_host._checkout_cu_src()
    assert src is not None
    assert (src / "tst_cu_mcp" / "__init__.py").is_file()


def test_prepare_imports_this_repo() -> None:
    assert cu_host._prepare_cu_imports() is True


def test_cu_capture_usage_without_args() -> None:
    assert cu_host.run_cu_capture(["tstd", "--cu-capture"]) == 2


def test_ctypes_capture_png_refuses_non_positive_rect() -> None:
    assert cu_host._ctypes_capture_png(0, 0, 0, 10) is None


def test_permissions_dict_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cu_host, "_ax_trusted_ctypes", lambda: True)
    monkeypatch.setattr(cu_host, "_cg_preflight_ctypes", lambda: True)
    cu_host._screen_capable = None
    report = cu_host._permissions_dict()
    assert report["platform"] == "macos"
    screen = report["screen_recording"]
    access = report["accessibility"]
    assert isinstance(screen, dict) and screen["granted"] is True
    assert isinstance(access, dict) and access["granted"] is True
    assert report["actuation_path"] == "daemon"


def test_permissions_probe_does_not_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cu_host, "_ax_trusted_ctypes", lambda: False)
    monkeypatch.setattr(cu_host, "_cg_preflight_ctypes", lambda: False)

    def fail_capture(*_a: object) -> None:
        raise AssertionError("permissions probe must not capture")

    monkeypatch.setattr(cu_host, "_ctypes_capture_png", fail_capture)
    cu_host._screen_capable = None
    report = cu_host._permissions_dict()
    assert report["screen_recording"]["granted"] is False


def test_dispatch_quit() -> None:
    assert cu_host.dispatch("quit") == (b"ok\n", None)


def test_dispatch_move_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cu_host, "_mouse_move", lambda *_a: True)
    monkeypatch.setattr(cu_host, "_press_combo", lambda *_a: True)
    assert cu_host.dispatch("move 1.5 2") == (b"ok\n", None)
    assert cu_host.dispatch("key cmd+space") == (b"ok\n", None)


@pytest.mark.asyncio
async def test_unix_socket_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("Unix CU socket is Darwin-only")
    # macOS AF_UNIX paths cap at ~104 bytes; pytest's tmp_path is often longer.
    data = Path("/tmp") / "tst-cu-sock-test"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cu_host, "_ctypes_capture_png", lambda *_a: None)
    monkeypatch.setattr(cu_host, "_ax_trusted_ctypes", lambda: False)
    monkeypatch.setattr(cu_host, "_cg_preflight_ctypes", lambda: False)
    cu_host._screen_capable = False
    server = await cu_host.start_cu_socket(data)
    assert server is not None
    try:
        reader, writer = await asyncio.open_unix_connection(str(cu_host.sock_path(data)))
        writer.write(b"permissions\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        writer.close()
        await writer.wait_closed()
        payload = json.loads(line.decode())
        assert payload["platform"] == "macos"
        assert payload["actuation_path"] == "daemon"
    finally:
        await cu_host.stop_cu_socket(data)


def test_dispatch_permissions_request_does_not_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cu_host, "_ctypes_capture_png", lambda *_a: b"\x89PNG\r\n\x1a\nxx")
    monkeypatch.setattr(cu_host, "_ax_trusted_ctypes", lambda: True)
    monkeypatch.setattr(cu_host, "_cg_preflight_ctypes", lambda: True)
    cu_host._screen_capable = True
    header, blob = cu_host.dispatch("permissions request")
    payload = json.loads(header.decode())
    assert payload["actuation_path"] == "daemon"
    assert blob is None


@pytest.mark.asyncio
async def test_start_cu_socket_skips_live_host() -> None:
    if sys.platform != "darwin":
        pytest.skip("Unix CU socket is Darwin-only")
    import socket as socklib

    data = Path("/tmp") / "tst-cu-sock-live"
    data.mkdir(parents=True, exist_ok=True)
    path = cu_host.sock_path(data)
    path.unlink(missing_ok=True)
    listener = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    try:
        owned = await cu_host.start_cu_socket(data)
        assert owned is None
        assert os.environ.get(cu_host.SOCK_ENV) == str(path)
        await cu_host.stop_cu_socket(data)
        assert path.exists()
    finally:
        listener.close()
        path.unlink(missing_ok=True)
        os.environ.pop(cu_host.SOCK_ENV, None)


def test_dispatch_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\nhello"
    monkeypatch.setattr(cu_host, "_ctypes_capture_png", lambda *_a: png)
    header, blob = cu_host.dispatch("capture 0 0 10 10")
    assert header == f"png {len(png)}\n".encode("ascii")
    assert blob == png
