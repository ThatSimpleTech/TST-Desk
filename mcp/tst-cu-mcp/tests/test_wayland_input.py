"""Wayland input is RemoteDesktop/libei, not XTEST (TD-4901b)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tst_cu_mcp.backends import linux_x11
from tst_cu_mcp.backends.wayland import wayland_supported
from tst_cu_mcp.backends.wayland_capture import WaylandCapture
from tst_cu_mcp.backends.wayland_input import WaylandInput, WaylandInputError
from tst_cu_mcp.tools.health import health_report

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux session helpers"),
]


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

    for name in ("move_mouse", "click_button", "press_keysym", "scroll_buttons"):
        monkeypatch.setattr(linux_x11, name, boom(name))
    return seen


class TestWaylandInput:
    def test_unavailable_without_injector(self, wayland_env: None) -> None:
        assert WaylandInput().available() is False
        with pytest.raises(WaylandInputError, match="RemoteDesktop"):
            WaylandInput().click_at(1, 2, "left", 1)

    def test_injected_click_does_not_touch_x11(
        self, wayland_env: None, x11_never_opened: list[str]
    ) -> None:
        clicks: list[tuple[float, float, str, int]] = []
        inp = WaylandInput(click=lambda x, y, b, c: clicks.append((x, y, b, c)))
        assert inp.available() is True
        inp.click_at(3, 4, "left", 1)
        assert clicks == [(3, 4, "left", 1)]
        assert inp.actuations == [("click", (3, 4, "left", 1))]
        assert x11_never_opened == []

    def test_health_true_only_when_both_strategies_work(self, wayland_env: None) -> None:
        cap = WaylandCapture(grab=lambda _rect: b"png")
        inp = WaylandInput(click=lambda *_a: None)
        assert wayland_supported(capture=cap, input_driver=inp) is True
        assert wayland_supported(capture=cap) is False
        assert wayland_supported(input_driver=inp) is False
        report = health_report()
        # Default strategies are not injected into health_report.
        assert report["supported"] is False
        assert report["backend"] is None
