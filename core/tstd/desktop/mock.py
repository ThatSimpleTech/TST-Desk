"""In-process desktop driver for CI (TD-102 / TD-3301).

Records calls and returns a tiny PNG. It never talks to the OS pointer.
Tests script a focus mismatch by setting ``foreground_title`` /
``foreground_app``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .protocol import TINY_PNG, DesktopError, window_matches


class MockDesktopDriver:
    """The TD-102 mock path: observe, never actuate a real desktop."""

    def __init__(
        self,
        *,
        foreground_title: str = "Mock Window",
        foreground_app: str = "mock",
    ) -> None:
        self.foreground_title = foreground_title
        self.foreground_app = foreground_app
        self.killed = False
        # Successful operations only — a refusal must not appear here.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.actuations: list[str] = []

    def set_killed(self, killed: bool) -> None:
        self.killed = bool(killed)

    def _guard(self, expect_window: str | None, *, actuating: bool) -> None:
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

    async def aclose(self) -> None:
        return None
