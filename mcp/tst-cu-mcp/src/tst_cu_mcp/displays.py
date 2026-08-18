"""Display enumeration and geometry.

All coordinates are in a single global space with a **top-left origin**, shared
by display bounds, capture rectangles and input events. The unit is the active
backend's native one — logical points on macOS, physical pixels on Windows — and
nothing above this layer needs to know which, because the image-pixel mapping in
:mod:`tst_cu_mcp.coordinates` is proportional over the captured region.

Displays left of or above the main display have negative origins on both
platforms; that is expected and preserved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DisplayInfo:
    """One active display, in the backend's global coordinate space."""

    display_id: int
    index: int
    x: int
    y: int
    width: int
    height: int
    scale: float
    is_main: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "display_id": self.display_id,
            "is_main": self.is_main,
            "scale": self.scale,
            "bounds_points": {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            },
        }


def list_displays() -> list[DisplayInfo]:
    """Enumerate active displays. Empty list if none or unavailable."""
    from tst_cu_mcp.backends import get_backend

    return get_backend().list_displays()


def screen_info() -> dict[str, Any]:
    """Structured summary of all displays for the ``get_screen_info`` tool."""
    displays = list_displays()
    return {
        "count": len(displays),
        "main_index": next((d.index for d in displays if d.is_main), None),
        "displays": [d.to_dict() for d in displays],
    }
