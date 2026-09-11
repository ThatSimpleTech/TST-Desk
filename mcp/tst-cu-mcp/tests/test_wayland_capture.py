"""Wayland capture is ScreenCast, not X11 (TD-4901a)."""

from __future__ import annotations

import sys
from io import BytesIO
from typing import Any

import pytest
from PIL import Image

from tst_cu_mcp.backends import linux_x11
from tst_cu_mcp.backends.wayland import wayland_supported
from tst_cu_mcp.backends.wayland_capture import WaylandCapture, WaylandCaptureError
from tst_cu_mcp.tools.health import health_report

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux session helpers"),
]


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (128, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def wayland_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")


@pytest.fixture
def x11_never_opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def boom(name: str) -> Any:
        def _inner(*_a: Any, **_k: Any) -> Any:
            seen.append(name)
            raise AssertionError(f"X11 {name} must not run on Wayland")

        return _inner

    for name in ("list_raw_displays", "capture_png", "move_mouse"):
        monkeypatch.setattr(linux_x11, name, boom(name))
    return seen


class TestWaylandCapture:
    def test_unavailable_without_grabber(self, wayland_env: None) -> None:
        assert WaylandCapture().available() is False
        with pytest.raises(WaylandCaptureError, match="ScreenCast"):
            WaylandCapture().capture_png((0, 0, 8, 8))

    def test_x11_session_is_not_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        cap = WaylandCapture(grab=lambda _rect: _png())
        assert cap.available() is False

    def test_injected_grab_returns_png(
        self, wayland_env: None, x11_never_opened: list[str]
    ) -> None:
        png = _png()
        cap = WaylandCapture(grab=lambda _rect: png)
        assert cap.available() is True
        got = cap.capture_png((0, 0, 8, 8))
        assert got == png
        Image.open(BytesIO(got)).verify()
        assert x11_never_opened == []

    def test_health_stays_false_without_input(
        self, wayland_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        png = _png()
        cap = WaylandCapture(grab=lambda _rect: png)
        monkeypatch.setattr("tst_cu_mcp.backends.wayland.WaylandCapture", lambda: cap)
        # Capture alone must not flip supported (split from 4901b).
        assert wayland_supported(capture=cap) is False
        report = health_report()
        assert report["session_type"] == "wayland"
        assert report["supported"] is False
        assert report["backend"] is None
