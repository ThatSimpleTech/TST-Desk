"""The image-pixel to global-coordinate mapper.

Unchanged by the port, and that is the point worth recording: because the mapping
is proportional over the captured region, it is already correct for Windows
per-monitor DPI without knowing anything about DPI. These tests pin that property
so a future "optimisation" that reintroduces a scale factor fails here.
"""

from __future__ import annotations

import pytest

from tst_cu_mcp.coordinates import image_px_to_global_point, resolve_point

REGION = (100, 200, 800, 600)


class TestImagePxToGlobalPoint:
    def test_top_left_maps_to_the_region_origin(self) -> None:
        assert image_px_to_global_point(REGION, 800, 600, 0, 0) == (100, 200)

    def test_bottom_right_maps_to_the_region_far_corner(self) -> None:
        assert image_px_to_global_point(REGION, 800, 600, 800, 600) == (900, 800)

    def test_centre_maps_to_the_region_centre(self) -> None:
        assert image_px_to_global_point(REGION, 800, 600, 400, 300) == (500, 500)

    def test_a_downscaled_image_still_maps_correctly(self) -> None:
        # Half-size image, same region: each image pixel is worth two region units.
        assert image_px_to_global_point(REGION, 400, 300, 200, 150) == (500, 500)

    def test_a_retina_style_upscale_maps_correctly(self) -> None:
        # Image twice the region's size — the 2x case, handled by the same maths
        # with no scale factor anywhere.
        assert image_px_to_global_point(REGION, 1600, 1200, 800, 600) == (500, 500)

    def test_negative_region_origin(self) -> None:
        assert image_px_to_global_point((-1600, -200, 800, 600), 800, 600, 0, 0) == (
            -1600,
            -200,
        )

    @pytest.mark.parametrize(("width", "height"), [(0, 600), (800, 0), (-1, 600)])
    def test_degenerate_image_size_is_refused(self, width: int, height: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            image_px_to_global_point(REGION, width, height, 0, 0)

    @pytest.mark.parametrize(("x", "y"), [(-1, 0), (801, 0), (0, -1), (0, 601)])
    def test_out_of_image_coordinates_are_refused(self, x: float, y: float) -> None:
        # Refusing beats clamping here: an out-of-image coordinate means the model
        # misread the metadata, and silently moving the click hides that.
        with pytest.raises(ValueError, match="out of range"):
            image_px_to_global_point(REGION, 800, 600, x, y)


class TestResolvePoint:
    def test_points_space_passes_through(self) -> None:
        assert resolve_point(
            x=42, y=43, coordinate_space="points", image_width=None, image_height=None, region=None
        ) == (42.0, 43.0)

    def test_image_space_maps_through_the_region(self) -> None:
        assert resolve_point(
            x=400,
            y=300,
            coordinate_space="image",
            image_width=800,
            image_height=600,
            region={"x": 100, "y": 200, "width": 800, "height": 600},
        ) == (500, 500)

    @pytest.mark.parametrize(
        ("image_width", "image_height", "region"),
        [
            (None, 600, {"x": 0, "y": 0, "width": 8, "height": 6}),
            (800, None, {"x": 0, "y": 0, "width": 8, "height": 6}),
            (800, 600, None),
        ],
    )
    def test_image_space_requires_the_screenshot_metadata(
        self,
        image_width: int | None,
        image_height: int | None,
        region: dict[str, int] | None,
    ) -> None:
        with pytest.raises(ValueError, match="requires image_width"):
            resolve_point(
                x=1,
                y=1,
                coordinate_space="image",
                image_width=image_width,
                image_height=image_height,
                region=region,
            )

    def test_error_message_offers_the_points_escape_hatch(self) -> None:
        with pytest.raises(ValueError, match="coordinate_space='points'"):
            resolve_point(
                x=1,
                y=1,
                coordinate_space="image",
                image_width=None,
                image_height=None,
                region=None,
            )

    def test_unknown_space_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown coordinate_space"):
            resolve_point(
                x=1,
                y=1,
                coordinate_space="inches",
                image_width=None,
                image_height=None,
                region=None,
            )

    @pytest.mark.parametrize("missing", ["x", "y", "width", "height"])
    def test_incomplete_region_names_the_missing_key(self, missing: str) -> None:
        region = {"x": 0, "y": 0, "width": 800, "height": 600}
        del region[missing]
        with pytest.raises(ValueError, match=missing):
            resolve_point(
                x=1,
                y=1,
                coordinate_space="image",
                image_width=800,
                image_height=600,
                region=region,
            )
