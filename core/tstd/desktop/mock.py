"""In-process desktop driver for CI (TD-102 / TD-3301).

Records calls and returns a tiny PNG. It never talks to the OS pointer.
Tests script a focus mismatch by setting ``foreground_title`` /
``foreground_app``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .permissions import normalize_cu_platform, windows_report
from .permissions_linux import linux_report
from .protocol import TINY_PNG, DesktopError, scripted_ax_hit_node, window_matches


class MockDesktopDriver:
    """The TD-102 mock path: observe, never actuate a real desktop."""

    def __init__(
        self,
        *,
        foreground_title: str = "Mock Window",
        foreground_app: str = "mock",
        permission_denied: bool = False,
        platform: str = "macos",
        elevated: bool = False,
        uipi_blocked: bool = False,
        secure_desktop_blocked: bool = False,
        session_type: str = "x11",
        display_available: bool = True,
        xtest_available: bool = True,
    ) -> None:
        self.foreground_title = foreground_title
        self.foreground_app = foreground_app
        self.killed = False
        # Scripted TCC denial: raise before any record or actuation.
        self.permission_denied = permission_denied
        # Default mock stays macOS-shaped on every host so TD-3302 tests
        # pin identically. ``platform="win32"`` is the Windows first-run path.
        self.platform = platform
        self.elevated = elevated
        self.uipi_blocked = uipi_blocked
        self.secure_desktop_blocked = secure_desktop_blocked
        self.session_type = session_type
        self.display_available = display_available
        self.xtest_available = xtest_available
        # Successful operations only — a refusal must not appear here.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.actuations: list[str] = []

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)

    async def set_overlay_session(self, active: bool) -> None:
        self.calls.append(("overlay_session", {"active": bool(active)}))

    def _refuse_if_denied(self) -> None:
        if self.secure_desktop_blocked:
            raise DesktopError(
                DesktopError.SECURE_DESKTOP,
                "The secure desktop cannot be captured or driven. Nothing was sent.",
            )
        if self.uipi_blocked:
            raise DesktopError(
                DesktopError.UIPI,
                "Synthetic input was discarded by UIPI "
                "(target is a higher integrity level). Nothing was sent.",
            )
        if self.permission_denied:
            raise DesktopError(
                DesktopError.PERMISSION_DENIED,
                "Screen Recording or Accessibility is not granted. Nothing was sent.",
            )

    async def check_permissions(self) -> dict[str, Any]:
        plat = normalize_cu_platform(self.platform)
        if plat == "windows":
            return windows_report(elevated=self.elevated)
        if plat == "linux":
            return linux_report(
                session=self.session_type,
                display=self.display_available,
                xtest=self.xtest_available,
            )
        granted = not self.permission_denied
        return {
            "platform": "macos",
            "screen_recording": {"granted": granted},
            "accessibility": {"granted": granted},
            "all_granted": granted,
        }

    def _guard(self, expect_window: str | None, *, actuating: bool) -> None:
        self._refuse_if_denied()
        if actuating and self.killed:
            raise DesktopError("cu_killed", "computer-use kill-switch is engaged")
        if expect_window is not None and not window_matches(
            expect_window, self.foreground_title, self.foreground_app
        ):
            raise DesktopError(
                "focus_mismatch",
                f"expected the foreground window to match {expect_window!r}, "
                f"but it is {self.foreground_title!r} ({self.foreground_app}). "
                "Nothing was sent.",
            )

    def _record(self, name: str, actuating: bool, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if actuating:
            self.actuations.append(name)

    async def screenshot(self, display: int | None = None) -> str:
        # Capture is not actuation: the kill-switch must not blind the eyes.
        # Screen Recording still applies — a TCC deny is not a hang.
        self._refuse_if_denied()
        self._record("screenshot", False, display=display)
        return json.dumps(
            {
                "png_base64": base64.b64encode(TINY_PNG).decode("ascii"),
                "width": 1,
                "height": 1,
            }
        )

    async def move(self, x: float, y: float, expect_window: str | None = None) -> str:
        self._guard(expect_window, actuating=True)
        self._record("move", True, x=x, y=y, expect_window=expect_window)
        return json.dumps({"moved_to": {"x": x, "y": y}})

    async def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        expect_window: str | None = None,
    ) -> str:
        self._guard(expect_window, actuating=True)
        self._record(
            "click",
            True,
            x=x,
            y=y,
            button=button,
            count=count,
            expect_window=expect_window,
        )
        return json.dumps({"clicked": {"x": x, "y": y}, "button": button, "count": count})

    async def type_text(self, text: str, expect_window: str | None = None) -> str:
        self._guard(expect_window, actuating=True)
        # Do not echo *text* in the tool result — same contract as the MCP.
        self._record("type", True, chars=len(text), expect_window=expect_window)
        return json.dumps({"typed_chars": len(text)})

    async def scroll(
        self,
        dx: int = 0,
        dy: int = 0,
        x: float | None = None,
        y: float | None = None,
        expect_window: str | None = None,
    ) -> str:
        self._guard(expect_window, actuating=True)
        self._record("scroll", True, dx=dx, dy=dy, x=x, y=y, expect_window=expect_window)
        return json.dumps({"scrolled": {"dx": dx, "dy": dy}})

    async def hit_test(self, x: float, y: float) -> dict[str, Any]:
        # Observe only: the kill-switch must not blind Design mode.
        self._refuse_if_denied()
        self._record("hit_test", False, x=x, y=y)
        return scripted_ax_hit_node(x, y)

    async def aclose(self) -> None:
        return None
