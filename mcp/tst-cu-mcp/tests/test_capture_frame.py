"""The platform-free frame around capture: region maths, display choice, encoding.

None of this needs a screen. It does need to survive negative display origins,
which is where a second monitor placed left of the primary lives on both
platforms and where naive clamping quietly captures the wrong rectangle.
"""

from __future__ import annotations

import sys
from io import BytesIO

import pytest
from PIL import Image

from tst_cu_mcp import capture
from tst_cu_mcp.backends.windows import WindowsBackend
from tst_cu_mcp.displays import DisplayInfo

PRIMARY = DisplayInfo(
    display_id=1, index=0, x=0, y=0, width=1920, height=1080, scale=1.0, is_main=True
)
LEFT_OF_PRIMARY = DisplayInfo(
    display_id=2, index=1, x=-1600, y=-200, width=1600, height=900, scale=1.0, is_main=False
)


def png_bytes(width: int, height: int, colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class TestComputeDownscale:
    def test_small_image_is_untouched(self) -> None:
        assert capture.compute_downscale(800, 600, 1568) == (800, 600, False)

    def test_exactly_at_the_limit_is_untouched(self) -> None:
        assert capture.compute_downscale(1568, 900, 1568) == (1568, 900, False)

    def test_wide_image_scales_by_its_long_edge(self) -> None:
        width, height, scaled = capture.compute_downscale(3840, 2160, 1568)
        assert scaled is True
        assert width == 1568
        assert height == 882

    def test_tall_image_scales_by_its_long_edge(self) -> None:
        width, height, scaled = capture.compute_downscale(1000, 4000, 2000)
        assert (width, height, scaled) == (500, 2000, True)

    def test_aspect_ratio_is_preserved(self) -> None:
        width, height, _ = capture.compute_downscale(2560, 1440, 1000)
        assert width / height == pytest.approx(2560 / 1440, rel=0.01)

    def test_zero_limit_disables_downscaling(self) -> None:
        assert capture.compute_downscale(4000, 4000, 0) == (4000, 4000, False)

    def test_never_collapses_to_zero(self) -> None:
        width, height, _ = capture.compute_downscale(10000, 1, 10)
        assert width >= 1
        assert height >= 1


class TestParseRegionDict:
    def test_none_stays_none(self) -> None:
        assert capture.parse_region_dict(None) is None

    def test_full_region(self) -> None:
        assert capture.parse_region_dict({"x": 1, "y": 2, "width": 3, "height": 4}) == (1, 2, 3, 4)

    @pytest.mark.parametrize("missing", ["x", "y", "width", "height"])
    def test_missing_key_is_named(self, missing: str) -> None:
        region = {"x": 0, "y": 0, "width": 10, "height": 10}
        del region[missing]
        with pytest.raises(ValueError, match=missing):
            capture.parse_region_dict(region)


class TestResolveRegion:
    def test_no_region_is_the_whole_display(self) -> None:
        assert capture.resolve_region(PRIMARY, None) == (0, 0, 1920, 1080)

    def test_region_is_offset_into_display_coordinates(self) -> None:
        assert capture.resolve_region(PRIMARY, (100, 50, 400, 300)) == (100, 50, 400, 300)

    def test_region_on_a_negative_origin_display(self) -> None:
        # The display-local origin is 0,0 even though the display sits at
        # -1600,-200 globally. Getting this wrong captures another monitor.
        assert capture.resolve_region(LEFT_OF_PRIMARY, (0, 0, 100, 100)) == (
            -1600,
            -200,
            100,
            100,
        )

    def test_whole_negative_origin_display(self) -> None:
        assert capture.resolve_region(LEFT_OF_PRIMARY, None) == (-1600, -200, 1600, 900)

    def test_oversized_region_is_clamped_to_the_display(self) -> None:
        assert capture.resolve_region(PRIMARY, (1800, 1000, 500, 500)) == (
            1800,
            1000,
            120,
            80,
        )

    def test_offset_beyond_the_display_is_pulled_inside(self) -> None:
        x, y, width, height = capture.resolve_region(PRIMARY, (5000, 5000, 100, 100))
        assert x < 1920
        assert y < 1080
        assert width >= 1
        assert height >= 1

    @pytest.mark.parametrize("region", [(0, 0, 0, 100), (0, 0, 100, 0), (0, 0, -5, 10)])
    def test_empty_region_is_refused(self, region: tuple[int, int, int, int]) -> None:
        with pytest.raises(ValueError, match="positive"):
            capture.resolve_region(PRIMARY, region)


class TestSelectDisplay:
    def test_default_is_the_main_display(self) -> None:
        assert capture.select_display([LEFT_OF_PRIMARY, PRIMARY], None) is PRIMARY

    def test_explicit_index(self) -> None:
        assert capture.select_display([PRIMARY, LEFT_OF_PRIMARY], 1) is LEFT_OF_PRIMARY

    def test_first_display_when_none_is_main(self) -> None:
        secondary = DisplayInfo(
            display_id=9, index=0, x=0, y=0, width=100, height=100, scale=1.0, is_main=False
        )
        assert capture.select_display([secondary], None) is secondary

    def test_no_displays_is_an_error(self) -> None:
        with pytest.raises(RuntimeError, match="no active displays"):
            capture.select_display([], None)

    @pytest.mark.parametrize("index", [-1, 2, 99])
    def test_out_of_range_index_states_the_range(self, index: int) -> None:
        with pytest.raises(ValueError, match="out of range"):
            capture.select_display([PRIMARY, LEFT_OF_PRIMARY], index)


class TestCaptureEndToEndWithoutAScreen:
    """The frame, wired to a stubbed backend — no pixels from a real display."""

    @pytest.fixture(autouse=True)
    def windows_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("tst_cu_mcp.capture.list_displays", lambda: [PRIMARY])

    def test_metadata_describes_the_region_actually_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(WindowsBackend, "capture_png", lambda _s, _rect: png_bytes(400, 300))
        result = capture.capture(region=(100, 50, 400, 300))
        metadata = result.metadata()
        assert metadata["captured_region_points"] == {
            "x": 100,
            "y": 50,
            "width": 400,
            "height": 300,
        }
        assert metadata["image_px"] == {"width": 400, "height": 300}
        assert metadata["downscaled"] is False

    def test_backend_receives_the_resolved_global_rectangle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[int, int, int, int]] = []

        def spy(_self: object, rect: tuple[int, int, int, int]) -> bytes:
            seen.append(rect)
            return png_bytes(10, 10)

        monkeypatch.setattr(WindowsBackend, "capture_png", spy)
        capture.capture(region=(5, 6, 10, 10))
        assert seen == [(5, 6, 10, 10)]

    def test_downscale_is_reflected_in_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WindowsBackend, "capture_png", lambda _s, _rect: png_bytes(3840, 2160))
        result = capture.capture(max_long_edge=1000)
        metadata = result.metadata()
        assert metadata["downscaled"] is True
        assert metadata["image_px"]["width"] == 1000
        # The region is unchanged by downscaling — that is what keeps the
        # proportional mapping correct after the image shrinks.
        assert metadata["captured_region_points"]["width"] == 1920

    def test_result_is_a_real_png(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WindowsBackend, "capture_png", lambda _s, _rect: png_bytes(64, 48))
        result = capture.capture()
        assert result.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(result.png_bytes)) as image:
            assert image.size == (64, 48)

    def test_coordinate_note_states_the_image_pixel_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(WindowsBackend, "capture_png", lambda _s, _rect: png_bytes(64, 48))
        note = capture.capture().metadata()["coordinate_note"]
        assert "[0,64]" in note
        assert "[0,48]" in note
