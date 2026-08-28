"""expect_window never degrades on Wayland (TD-4901c)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tst_cu_mcp import input_control, safety
from tst_cu_mcp.backends.wayland import WaylandBackend
from tst_cu_mcp.backends.wayland_capture import WaylandCapture
from tst_cu_mcp.backends.wayland_input import WaylandInput
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowFocusError, window_matches
from tst_cu_mcp.tools.health import health_report

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux session helpers"),
]


@pytest.fixture(autouse=True)
def isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(None)
    yield
    safety.set_config(None)


@pytest.fixture
def wayland_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")


def _backend() -> WaylandBackend:
    clicks: list[object] = []
    display = DisplayInfo(
        display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True
    )
    return WaylandBackend(
        capture=WaylandCapture(grab=lambda _rect: b"png", displays=[display]),
        input_driver=WaylandInput(click=lambda *a: clicks.append(a), move=lambda *_a: None),
    )


class TestWaylandFocus:
    def test_foreground_is_empty(self, wayland_env: None) -> None:
        info = _backend().foreground_window()
        assert info.title == ""
        assert info.process == ""
        assert window_matches("Terminal", info) is False

    def test_expect_window_refuses_without_actuating(
        self, wayland_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = _backend()
        monkeypatch.setattr("tst_cu_mcp.input_control.get_backend", lambda: backend)
        monkeypatch.setattr("tst_cu_mcp.backends.get_backend", lambda platform=None: backend)
        with pytest.raises(WindowFocusError):
            input_control.click(10, 10, expect_window="Terminal")
        assert backend.input.actuations == []

    def test_get_backend_selects_wayland_not_x11(
        self, wayland_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tst_cu_mcp.backends import get_backend
        from tst_cu_mcp.backends.wayland import WaylandBackend as WB

        backend = get_backend()
        assert isinstance(backend, WB)
        assert backend.name == "wayland"

    def test_x11_get_backend_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        from tst_cu_mcp.backends import get_backend
        from tst_cu_mcp.backends.linux import LinuxBackend

        assert isinstance(get_backend(), LinuxBackend)

    def test_health_does_not_claim_support_on_this_host(self, wayland_env: None) -> None:
        report = health_report()
        assert report["session_type"] == "wayland"
        assert report["supported"] is False
        assert report["backend"] is None
