"""Screen capture (the vision core).

The platform-specific part is one call — ``backend.capture_png`` — and everything
here is the platform-free frame around it: choosing a display, clamping a region,
downscaling, and recording what the image actually covers. Screenshots are never
left on disk; each backend guarantees that its own way.

The result carries the exact global rectangle it covers plus its final pixel
size, so callers can map image-pixel coordinates back to global coordinates
without assuming anything about the display's scale factor (Retina backing scale
or Windows per-monitor DPI) — the mapping is purely proportional.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any

from tst_cu_mcp.displays import DisplayInfo, list_displays

DEFAULT_MAX_LONG_EDGE = 1568
_REGION_KEYS = ("x", "y", "width", "height")


@dataclass(frozen=True)
class ScreenshotResult:
    """A captured, possibly-downscaled PNG plus its coordinate metadata."""

    png_bytes: bytes
    image_px_width: int
    image_px_height: int
    region_points: tuple[int, int, int, int]  # global x, y, width, height (points)
    display: DisplayInfo
    downscaled: bool

    def metadata(self) -> dict[str, Any]:
        gx, gy, gw, gh = self.region_points
        return {
            "display": self.display.to_dict(),
            "captured_region_points": {"x": gx, "y": gy, "width": gw, "height": gh},
            "image_px": {"width": self.image_px_width, "height": self.image_px_height},
            "downscaled": self.downscaled,
            "coordinate_note": (
                f"Click/move coordinates use THIS image's pixel space: "
                f"x in [0,{self.image_px_width}], y in [0,{self.image_px_height}], "
                f"origin top-left."
            ),
        }


def compute_downscale(width: int, height: int, max_long_edge: int) -> tuple[int, int, bool]:
    """Return (new_w, new_h, downscaled) honoring a maximum long edge."""
    long_edge = max(width, height)
    if max_long_edge <= 0 or long_edge <= max_long_edge:
        return width, height, False
    ratio = max_long_edge / long_edge
    return max(1, round(width * ratio)), max(1, round(height * ratio)), True


def parse_region_dict(region: dict[str, int] | None) -> tuple[int, int, int, int] | None:
    """Validate a region dict into a local (x, y, width, height) tuple, or None."""
    if region is None:
        return None
    missing = [k for k in _REGION_KEYS if k not in region]
    if missing:
        raise ValueError(f"region requires keys {_REGION_KEYS}; missing {missing}")
    return (int(region["x"]), int(region["y"]), int(region["width"]), int(region["height"]))


def resolve_region(
    display: DisplayInfo, region_local: tuple[int, int, int, int] | None
) -> tuple[int, int, int, int]:
    """Map a display-local region (points) to a clamped global rectangle (points)."""
    if region_local is None:
        return (display.x, display.y, display.width, display.height)

    rx, ry, rw, rh = region_local
    if rw <= 0 or rh <= 0:
        raise ValueError("region width and height must be positive")

    rx = max(0, min(rx, display.width - 1))
    ry = max(0, min(ry, display.height - 1))
    rw = min(rw, display.width - rx)
    rh = min(rh, display.height - ry)
    return (display.x + rx, display.y + ry, rw, rh)


def select_display(displays: list[DisplayInfo], display_index: int | None) -> DisplayInfo:
    """Pick the target display: the main one by default, else by index."""
    if not displays:
        raise RuntimeError("no active displays found")
    if display_index is None:
        return next((d for d in displays if d.is_main), displays[0])
    if display_index < 0 or display_index >= len(displays):
        raise ValueError(f"display index {display_index} out of range (0..{len(displays) - 1})")
    return displays[display_index]


def _encode(raw: bytes, max_long_edge: int) -> tuple[bytes, int, int, bool]:
    """Open PNG bytes, optionally downscale to max_long_edge, re-encode PNG."""
    from PIL import Image

    with Image.open(BytesIO(raw)) as img:
        width, height = img.size
        new_w, new_h, downscaled = compute_downscale(width, height, max_long_edge)
        out = img.resize((new_w, new_h), Image.Resampling.LANCZOS) if downscaled else img
        buffer = BytesIO()
        out.save(buffer, format="PNG")
        return buffer.getvalue(), new_w, new_h, downscaled


def capture(
    *,
    display_index: int | None = None,
    region: tuple[int, int, int, int] | None = None,
    max_long_edge: int = DEFAULT_MAX_LONG_EDGE,
) -> ScreenshotResult:
    """Capture a display (or a region of it) and return a ScreenshotResult."""
    from tst_cu_mcp.backends import get_backend
    from tst_cu_mcp.overlay import get_overlay

    displays = list_displays()
    display = select_display(displays, display_index)
    rect = resolve_region(display, region)

    # The real-display glow must never paint into the frame it signals for.
    # grab_hidden orders the panels out and only returns once they are gone
    # (acked by the helper's main thread), so the grab below is clean.
    with get_overlay().grab_hidden():
        raw = get_backend().capture_png(rect)
    png_bytes, image_w, image_h, downscaled = _encode(raw, max_long_edge)
    result = ScreenshotResult(
        png_bytes=png_bytes,
        image_px_width=image_w,
        image_px_height=image_h,
        region_points=rect,
        display=display,
        downscaled=downscaled,
    )
    from tst_cu_mcp.hit_test import remember_capture

    remember_capture(result)
    return result
