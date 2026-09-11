"""Wayland must not reach Xlib, even when DISPLAY is set (TD-2001).

health.supported is already false on Wayland. These tests pin the other
half of that promise: capture, input, focus, and hit-test refuse before
any X11 symbol is touched. An XWayland DISPLAY is the attack.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tst_cu_mcp import input_control, safety
from tst_cu_mcp.backends import linux_x11
from tst_cu_mcp.backends.linux import (
    LinuxBackend,
    WaylandUnsupportedError,
    require_native_x11,
)
from tst_cu_mcp.backends.wayland_input import WaylandInputError

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux session helpers"),
]

_X11_TOUCHPOINTS = (
    "list_raw_displays",
    "capture_png",
    "move_mouse",
    "click_button",
    "press_keysym",
    "scroll_buttons",
    "cursor_position",
    "foreground_window",
    "probe_display",
)


@pytest.fixture(autouse=True)
def isolate_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(safety.STOP_FILE_ENV, str(tmp_path / "STOP"))
    monkeypatch.delenv(safety.STOP_ENV, raising=False)
    safety.set_config(None)
    yield
    safety.set_config(None)


@pytest.fixture
def wayland_with_xwayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")


@pytest.fixture
def x11_never_opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def boom(name: str) -> Any:
        def _inner(*_args: Any, **_kwargs: Any) -> Any:
            seen.append(name)
            raise AssertionError(f"X11 {name} must not run on Wayland")

        return _inner

    for name in _X11_TOUCHPOINTS:
        monkeypatch.setattr(linux_x11, name, boom(name))
    return seen


class TestRequireNativeX11:
    def test_x11_session_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        require_native_x11()

    def test_wayland_refuses_even_with_display(self, wayland_with_xwayland: None) -> None:
        with pytest.raises(WaylandUnsupportedError, match="XWayland"):
            require_native_x11()

    def test_wayland_display_without_session_type_is_wayland(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", ":0")
        with pytest.raises(WaylandUnsupportedError, match="session_type='wayland'"):
            require_native_x11()


class TestBackendNeverOpensX:
    def test_list_displays(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().list_displays()
        assert x11_never_opened == []

    def test_capture(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().capture_png((0, 0, 64, 64))
        assert x11_never_opened == []

    def test_click(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().click(10, 10, "left", 1)
        assert x11_never_opened == []

    def test_move(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().move_mouse(10, 10)
        assert x11_never_opened == []

    def test_type_text(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().type_text("hi")
        assert x11_never_opened == []

    def test_press_keys(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().press_keys("ctrl+c")
        assert x11_never_opened == []

    def test_scroll(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().scroll(0, 3)
        assert x11_never_opened == []

    def test_cursor(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().cursor_position()
        assert x11_never_opened == []

    def test_foreground(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().foreground_window()
        assert x11_never_opened == []

    def test_hit_test(self, wayland_with_xwayland: None, x11_never_opened: list[str]) -> None:
        with pytest.raises(WaylandUnsupportedError):
            LinuxBackend().hit_test(10, 10)
        assert x11_never_opened == []

    def test_check_permissions_does_not_open_x(
        self, wayland_with_xwayland: None, x11_never_opened: list[str]
    ) -> None:
        report = LinuxBackend().check_permissions()
        assert report["session_type"] == "wayland"
        assert report["all_granted"] is False
        assert "wayland" in report["limits"]
        assert x11_never_opened == []


class TestPolicyLayer:
    def test_click_refuses_without_x(
        self, wayland_with_xwayland: None, x11_never_opened: list[str]
    ) -> None:
        # After TD-4901, get_backend() is WaylandBackend. Empty ScreenCast
        # must not fall through to Xlib or a generic "no displays".
        with pytest.raises(WaylandInputError, match="RemoteDesktop"):
            input_control.click(10, 10)
        assert x11_never_opened == []

    def test_expect_window_does_not_degrade_to_xwayland(
        self, wayland_with_xwayland: None, x11_never_opened: list[str]
    ) -> None:
        with pytest.raises(WaylandInputError, match="RemoteDesktop"):
            input_control.click(10, 10, expect_window="Terminal")
        assert x11_never_opened == []
