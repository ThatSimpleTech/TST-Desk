"""Wayland computer-use backend: capture and input stay separate (TD-4901).

X11 is never used here. An XWayland ``DISPLAY`` does not construct this
backend (``get_backend`` only picks it when ``linux_session_kind`` is
``wayland``) and does not make ``health.supported`` true.

``health.supported`` is true only when *both* strategies are available
(ScreenCast *and* RemoteDesktop). Capture-only compositors stay
unsupported rather than silently weakening ``expect_window``.
"""

from __future__ import annotations

from typing import Any

from tst_cu_mcp.backends.base import Rect
from tst_cu_mcp.backends.linux import linux_session_kind
from tst_cu_mcp.backends.linux_keys import parse_key_combo
from tst_cu_mcp.backends.wayland_capture import WaylandCapture, WaylandCaptureError
from tst_cu_mcp.backends.wayland_input import WaylandInput, WaylandInputError
from tst_cu_mcp.displays import DisplayInfo
from tst_cu_mcp.focus import WindowInfo
from tst_cu_mcp.permissions import build_linux_report

__all__ = [
    "WaylandBackend",
    "WaylandCaptureError",
    "WaylandInputError",
    "wayland_supported",
]


def wayland_supported(
    capture: WaylandCapture | None = None,
    input_driver: WaylandInput | None = None,
) -> bool:
    """True only when capture *and* input strategies both work."""
    if linux_session_kind() != "wayland":
        return False
    cap = capture if capture is not None else WaylandCapture()
    inp = input_driver if input_driver is not None else WaylandInput()
    return cap.available() and inp.available()


class WaylandBackend:
    """Eyes from TD-4901a, hands from TD-4901b, focus refuse from TD-4901c."""

    name = "wayland"

    def __init__(
        self,
        *,
        capture: WaylandCapture | None = None,
        input_driver: WaylandInput | None = None,
    ) -> None:
        self.capture = capture if capture is not None else WaylandCapture()
        self.input = input_driver if input_driver is not None else WaylandInput()

    def list_displays(self) -> list[DisplayInfo]:
        return self.capture.list_displays()

    def capture_png(self, rect: Rect) -> bytes:
        return self.capture.capture_png(rect)

    def move_mouse(self, x: float, y: float) -> None:
        self.input.move_mouse(x, y)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        self.input.click_at(x, y, button, count)

    def type_text(self, text: str) -> None:
        self.input.type_at(text)

    def parse_key_combo(self, combo: str) -> tuple[tuple[int, ...], int]:
        return parse_key_combo(combo)

    def press_keys(self, combo: str) -> None:
        self.input.press(combo)

    def scroll(self, dx: int, dy: int) -> None:
        self.input.scroll_by(dx, dy)

    def cursor_position(self) -> tuple[int, int]:
        return self.input.cursor_position()

    def foreground_window(self) -> WindowInfo:
        """Never guess. Empty title/process so ``expect_window`` cannot match.

        Listing toplevels without a compositor focus bit is not foreground
        (TD-4901c). An empty window is a legitimate answer; input_control
        then refuses a mismatch without actuating.
        """
        return WindowInfo(title="", process="", pid=0, x=0, y=0, width=0, height=0)

    def check_permissions(self, *, request: bool = False) -> dict[str, Any]:
        del request
        return build_linux_report(
            session=linux_session_kind() or "wayland",
            display=self.capture.available(),
            xtest=self.input.available(),
        )

    def hit_test(self, x: float, y: float) -> dict[str, Any]:
        del x, y
        return {
            "role": "",
            "xpath": None,
            "attributes": {},
            "box": None,
            "styles": {},
        }
