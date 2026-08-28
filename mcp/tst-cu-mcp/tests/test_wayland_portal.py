"""ScreenCast and RemoteDesktop listings can diverge (TD-4901 split)."""

from __future__ import annotations

import sys

import pytest

from tst_cu_mcp.backends.wayland_portal import (
    REMOTE_DESKTOP_IFACE,
    SCREENCAST_IFACE,
    portal_interfaces,
    remotedesktop_listed,
    screencast_listed,
)

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux session helpers"),
]


@pytest.fixture
def wayland_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")


class TestPortalListingSplit:
    def test_x11_session_lists_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("DISPLAY", ":0")
        assert portal_interfaces(introspect=lambda: SCREENCAST_IFACE) == frozenset()

    def test_screencast_without_remotedesktop(self, wayland_env: None) -> None:
        text = f"interface {SCREENCAST_IFACE}"
        assert screencast_listed(introspect=lambda: text) is True
        assert remotedesktop_listed(introspect=lambda: text) is False

    def test_both_listed(self, wayland_env: None) -> None:
        text = f"{SCREENCAST_IFACE}\n{REMOTE_DESKTOP_IFACE}"
        assert portal_interfaces(introspect=lambda: text) == frozenset(
            {SCREENCAST_IFACE, REMOTE_DESKTOP_IFACE}
        )

    def test_xwayland_display_does_not_count_on_x11(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("DISPLAY", ":0")
        # linux_session_kind prefers explicit x11 over WAYLAND_DISPLAY.
        assert portal_interfaces(introspect=lambda: SCREENCAST_IFACE) == frozenset()
