"""The platform contract.

Everything that differs between operating systems lives behind this Protocol,
and nothing above it (``server.py``, the tools, the coordinate mapper, the
kill-switch) knows which platform it is running on.

Two rules make the split testable, and both matter more than they look:

1. **Backend modules import on every platform.** OS handles are acquired inside
   methods, never at module scope, so ``backends.windows`` imports cleanly on a
   Mac and ``backends.darwin`` imports cleanly on Windows. Only the actual OS
   calls are platform-bound. That is what lets one host prove both selection
   branches instead of leaving half the logic to a CI leg nobody reads.
2. **Backends carry no policy.** Bounds checks, the kill-switch, length caps and
   argument validation stay in ``input_control``, so a new platform cannot
   accidentally ship without them.

Coordinates crossing this boundary are always *global* coordinates in the
backend's own space, top-left origin: logical points on macOS, physical pixels
on Windows. Callers never need to know which, because the only thing that reads
them is the same backend that produced the display bounds they came from, and
the image-pixel mapping above is purely proportional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tst_cu_mcp.displays import DisplayInfo
    from tst_cu_mcp.focus import WindowInfo

# A global rectangle: (x, y, width, height), top-left origin.
Rect = tuple[int, int, int, int]


@runtime_checkable
class Backend(Protocol):
    """One operating system's implementation of eyes and hands."""

    #: Short identifier reported by the ``health`` tool, e.g. ``"darwin"``.
    name: str

    # --- eyes ---------------------------------------------------------------

    def list_displays(self) -> list[DisplayInfo]:
        """Enumerate active displays. Empty list if none are available."""
        ...

    def capture_png(self, rect: Rect) -> bytes:
        """Capture a global rectangle and return encoded PNG bytes.

        Implementations must not leave the image on disk. Raise ``RuntimeError``
        with an actionable message if the platform refused the capture.
        """
        ...

    # --- hands --------------------------------------------------------------

    def move_mouse(self, x: float, y: float) -> None:
        """Move the cursor to a global coordinate."""
        ...

    def click(self, x: float, y: float, button: str, count: int) -> None:
        """Click at a global coordinate. ``count`` >= 2 is a multi-click."""
        ...

    def type_text(self, text: str) -> None:
        """Type a Unicode string at the current focus. Never log the text."""
        ...

    def parse_key_combo(self, combo: str) -> object:
        """Parse ``"ctrl+shift+t"`` into a platform-specific pressable form.

        Pure and side-effect free so the key vocabulary is testable without a
        desktop. Raises ``ValueError`` on unknown tokens.
        """
        ...

    def press_keys(self, combo: str) -> None:
        """Press and release a key combo."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by lines: dy>0 up, dy<0 down; dx>0 right, dx<0 left."""
        ...

    # --- state read-back ----------------------------------------------------
    #
    # Both of these are reads, not actions: they carry no kill-switch check
    # because stopping the hands should not blind the eyes. A model that has just
    # been refused an action still needs to see where things stand.

    def cursor_position(self) -> tuple[int, int]:
        """Current pointer position in global coordinates."""
        ...

    def foreground_window(self) -> WindowInfo:
        """The window currently in front.

        Title and process are best-effort per platform; an empty string is a
        legitimate answer, not a failure. Raise only if the OS query itself fails.
        """
        ...

    # --- environment --------------------------------------------------------

    def check_permissions(self, *, request: bool = False) -> dict[str, Any]:
        """Report whatever gates this platform puts in front of capture/input.

        Platforms with no gate must still report the limits that make actuation
        silently ineffective, rather than an unqualified "granted".
        """
        ...
