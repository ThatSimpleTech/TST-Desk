"""Coordinate mapping between screenshot pixels and global logical points.

The model reasons in the pixel space of the screenshot it was given. Mouse
events, however, are posted in the global **top-left origin, logical points**
space (the same space as ``CGDisplayBounds``). The mapping is purely
proportional over the captured region, so it is correct regardless of the
Retina scale factor or any downscaling applied to the returned image.
"""

from __future__ import annotations

_REGION_KEYS = ("x", "y", "width", "height")


def image_px_to_global_point(
    region_points: tuple[int, int, int, int],
    image_width: int,
    image_height: int,
    img_x: float,
    img_y: float,
) -> tuple[float, float]:
    """Map an image-pixel coordinate to a global logical point.

    ``region_points`` is the global ``(x, y, width, height)`` (points) the image
    covers — i.e. the ``captured_region_points`` from a screenshot's metadata.
    ``image_width``/``image_height`` are that screenshot's pixel dimensions.
    Raises ``ValueError`` if the pixel coordinate falls outside the image.
    """
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image_width and image_height must be positive")
    if not 0 <= img_x <= image_width:
        raise ValueError(f"img_x {img_x} out of range [0, {image_width}]")
    if not 0 <= img_y <= image_height:
        raise ValueError(f"img_y {img_y} out of range [0, {image_height}]")

    origin_x, origin_y, region_w, region_h = region_points
    global_x = origin_x + (img_x / image_width) * region_w
    global_y = origin_y + (img_y / image_height) * region_h
    return (global_x, global_y)


def resolve_point(
    *,
    x: float,
    y: float,
    coordinate_space: str,
    image_width: int | None,
    image_height: int | None,
    region: dict[str, int] | None,
) -> tuple[float, float]:
    """Resolve tool input coordinates to a global logical point.

    ``coordinate_space="image"`` (default for tools) maps from the returned
    screenshot's pixel space and requires ``image_width``, ``image_height`` and
    ``region`` (the screenshot metadata's ``image_px`` and
    ``captured_region_points``). ``coordinate_space="points"`` treats ``x``/``y``
    as global logical points directly.
    """
    if coordinate_space == "points":
        return (float(x), float(y))
    if coordinate_space != "image":
        raise ValueError(f"unknown coordinate_space {coordinate_space!r}; use 'image' or 'points'")

    if image_width is None or image_height is None or region is None:
        raise ValueError(
            "coordinate_space='image' requires image_width, image_height, and region — "
            "pass the screenshot metadata's image_px (width/height) and "
            "captured_region_points. Or use coordinate_space='points' with global points."
        )

    missing = [k for k in _REGION_KEYS if k not in region]
    if missing:
        raise ValueError(f"region requires keys {_REGION_KEYS}; missing {missing}")

    region_points = (
        int(region["x"]),
        int(region["y"]),
        int(region["width"]),
        int(region["height"]),
    )
    return image_px_to_global_point(region_points, image_width, image_height, float(x), float(y))
