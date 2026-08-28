"""Wayland capture strategy: portal ScreenCast + PipeWire (TD-4901a).

This module is capture *only*. It does not synthesise input. Availability
is a portal ScreenCast listing, not an XWayland ``DISPLAY``.

Grabbing frames from PipeWire is not implemented on this checkout — this
host has no Wayland session to live-verify against. Tests inject a
capture function. ``available`` never becomes true just because ``DISPLAY``
is set.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.backends.linux import linux_session_kind
from tst_cu_mcp.displays import DisplayInfo

CaptureFn = Callable[[Rect], bytes]


class WaylandCaptureError(RuntimeError):
    """ScreenCast is listed but this host cannot grab frames yet."""


@dataclass
class WaylandCapture:
    """ScreenCast-shaped capture. PipeWire grab is injected or refused."""

    grab: CaptureFn | None = None
    displays: list[DisplayInfo] = field(default_factory=list)

    def available(self) -> bool:
        """True when this is Wayland and a grabber is actually wired.

        A portal listing is not enough (TD-4901: ``health.supported``
        only when the strategy works). Live PipeWire is injected in
        tests; this host has no Wayland session to verify against.
        """
        if linux_session_kind() != "wayland":
            return False
        return self.grab is not None

    def list_displays(self) -> list[DisplayInfo]:
        if not self.available():
            return []
        if self.displays:
            return list(self.displays)
        return [
            DisplayInfo(
                display_id=1,
                index=0,
                x=0,
                y=0,
                width=1,
                height=1,
                scale=1.0,
                is_main=True,
            )
        ]

    def capture_png(self, rect: Rect) -> bytes:
        if not self.available():
            raise WaylandCaptureError(
                "Wayland capture requires portal ScreenCast (TD-4901a). "
                "An XWayland DISPLAY is not a substitute."
            )
        if self.grab is None:
            raise WaylandCaptureError(
                "ScreenCast is listed but PipeWire frame grab is not live-"
                "verified on this host. See docs/wayland-computer-use.md."
            )
        return self.grab(rect)
