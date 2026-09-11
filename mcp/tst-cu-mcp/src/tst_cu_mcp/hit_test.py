"""Observe-only accessibility hit-test (TD-3406).

Maps a Design-mode click (usually last-screenshot image pixels) to a
global point, asks the backend which AX / UIA / AT-SPI node is there,
and remaps that node's box back into the frozen frame. Never moves the
pointer and never calls the kill-switch.
"""

from __future__ import annotations

from typing import Any

from tst_cu_mcp.capture import ScreenshotResult

_last_capture: ScreenshotResult | None = None


def remember_capture(result: ScreenshotResult) -> None:
    """Keep the last grab so image-space Design clicks can be mapped."""
    global _last_capture
    _last_capture = result


def last_capture() -> ScreenshotResult | None:
    return _last_capture


def clear_capture() -> None:
    """Drop the last grab. Tests use this so one case cannot leak into the next."""
    global _last_capture
    _last_capture = None


def empty_node(x: float, y: float) -> dict[str, Any]:
    """A miss: no role, no attributes, a 1x1 box at the click."""
    return {
        "xpath": None,
        "role": None,
        "attributes": {},
        "box": {"x": x, "y": y, "width": 1.0, "height": 1.0},
        "styles": {},
    }


def global_box_to_image(
    region_points: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
    box: dict[str, float],
) -> dict[str, float]:
    """Map a global-points box onto last-screenshot image pixels."""
    ox, oy, rw, rh = region_points
    if rw <= 0 or rh <= 0 or image_width <= 0 or image_height <= 0:
        return box
    return {
        "x": (box["x"] - ox) / rw * image_width,
        "y": (box["y"] - oy) / rh * image_height,
        "width": box["width"] / rw * image_width,
        "height": box["height"] / rh * image_height,
    }


def observe_at(
    x: float,
    y: float,
    *,
    coordinate_space: str = "image",
    image_width: int | None = None,
    image_height: int | None = None,
    region: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Hit-test without actuating. ``coordinate_space`` matches click/move."""
    from tst_cu_mcp.backends import UnsupportedPlatformError, get_backend
    from tst_cu_mcp.coordinates import resolve_point

    last = last_capture()
    space = coordinate_space
    width = image_width
    height = image_height
    region_dict = region
    if space == "image" and last is not None and width is None:
        width = last.image_px_width
        height = last.image_px_height
        ox, oy, rw, rh = last.region_points
        region_dict = {"x": ox, "y": oy, "width": rw, "height": rh}

    if space == "image" and (width is None or height is None or region_dict is None):
        # No last grab and no metadata: treat the click as global points
        # rather than inventing a display.
        space = "points"

    try:
        point_x, point_y = resolve_point(
            x=x,
            y=y,
            coordinate_space=space,
            image_width=width,
            image_height=height,
            region=region_dict,
        )
        raw = get_backend().hit_test(point_x, point_y)
    except (UnsupportedPlatformError, RuntimeError, OSError, TypeError, ValueError):
        return empty_node(x, y)
    node = _normalize(raw, x, y)
    if last is not None and isinstance(node.get("box"), dict):
        node["box"] = global_box_to_image(
            last.region_points,
            last.image_px_width,
            last.image_px_height,
            {
                "x": float(node["box"]["x"]),
                "y": float(node["box"]["y"]),
                "width": float(node["box"]["width"]),
                "height": float(node["box"]["height"]),
            },
        )
    return node


def _normalize(raw: object, x: float, y: float) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    box_raw = data.get("box")
    if isinstance(box_raw, dict):
        box = {
            "x": _as_float(box_raw.get("x"), x),
            "y": _as_float(box_raw.get("y"), y),
            "width": max(0.0, _as_float(box_raw.get("width"), 0.0)),
            "height": max(0.0, _as_float(box_raw.get("height"), 0.0)),
        }
    else:
        box = {"x": x, "y": y, "width": 1.0, "height": 1.0}
    role = data.get("role")
    xpath = data.get("xpath")
    attrs = data.get("attributes")
    styles = data.get("styles")
    return {
        "xpath": xpath if isinstance(xpath, str) else None,
        "role": role if isinstance(role, str) else None,
        "attributes": _str_map(attrs),
        "box": box,
        "styles": _str_map(styles),
    }


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(key, str) and item is not None:
            out[key] = str(item)
    return out
