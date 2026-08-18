"""Windows coordinate maths and display ordering — pure, no desktop required.

These are the calculations that decide *where* a click lands. They are the most
testable part of the port and the least forgiving: an off-by-one in the absolute
grid puts the cursor a pixel short on every action, and an unstable display order
silently repoints `display: 1` at a different monitor between calls.
"""

from __future__ import annotations

import pytest

from tst_cu_mcp.backends.windows import (
    ABSOLUTE_RANGE,
    DEFAULT_DPI,
    normalize_to_virtual_desktop,
    order_displays,
    utf16_units,
)

SINGLE_1080P = (0, 0, 1920, 1080)
DUAL_LEFT_SECOND = (-1920, 0, 3840, 1080)


class TestNormalizeToVirtualDesktop:
    def test_origin_maps_to_zero(self) -> None:
        assert normalize_to_virtual_desktop(0, 0, SINGLE_1080P) == (0, 0)

    def test_far_corner_reaches_the_top_of_the_range(self) -> None:
        # The reason the divisor is width-1: with width, the last pixel would map
        # to 65500-ish and the rightmost column would be unreachable forever.
        assert normalize_to_virtual_desktop(1919, 1079, SINGLE_1080P) == (
            ABSOLUTE_RANGE,
            ABSOLUTE_RANGE,
        )

    def test_centre_is_mid_range(self) -> None:
        nx, ny = normalize_to_virtual_desktop(959.5, 539.5, SINGLE_1080P)
        assert nx == pytest.approx(ABSOLUTE_RANGE // 2, abs=2)
        assert ny == pytest.approx(ABSOLUTE_RANGE // 2, abs=2)

    def test_negative_origin_is_handled(self) -> None:
        # A monitor left of the primary has negative coordinates on Windows just
        # as on macOS. Its own top-left must map to 0, not to a clamped value.
        assert normalize_to_virtual_desktop(-1920, 0, DUAL_LEFT_SECOND) == (0, 0)

    def test_far_corner_of_a_negative_origin_desktop(self) -> None:
        assert normalize_to_virtual_desktop(1919, 1079, DUAL_LEFT_SECOND) == (
            ABSOLUTE_RANGE,
            ABSOLUTE_RANGE,
        )

    def test_primary_origin_on_a_dual_desktop_is_midway(self) -> None:
        nx, _ = normalize_to_virtual_desktop(0, 0, DUAL_LEFT_SECOND)
        assert nx == pytest.approx(ABSOLUTE_RANGE // 2, abs=20)

    @pytest.mark.parametrize(
        ("x", "y"),
        [(-5000, 0), (99999, 0), (0, -5000), (0, 99999)],
    )
    def test_out_of_range_is_clamped_not_wrapped(self, x: float, y: float) -> None:
        # Clamping keeps a bad coordinate on-screen. Wrapping would put it on the
        # opposite edge, which is a far more confusing misfire.
        nx, ny = normalize_to_virtual_desktop(x, y, SINGLE_1080P)
        assert 0 <= nx <= ABSOLUTE_RANGE
        assert 0 <= ny <= ABSOLUTE_RANGE

    def test_fractional_coordinates_yield_integers(self) -> None:
        nx, ny = normalize_to_virtual_desktop(10.6, 20.4, SINGLE_1080P)
        assert isinstance(nx, int)
        assert isinstance(ny, int)

    def test_sub_pixel_offsets_survive_rather_than_truncating(self) -> None:
        # The absolute grid is finer than the pixel raster — 65536 steps across
        # 1920 px — so fractional input is real information, not noise to round
        # away. A half-pixel offset must not collapse onto its neighbour.
        assert (
            normalize_to_virtual_desktop(10.6, 0, SINGLE_1080P)[0]
            != normalize_to_virtual_desktop(10.0, 0, SINGLE_1080P)[0]
        )

    def test_monotonic_in_x(self) -> None:
        # Whatever the rounding, moving right must never move the cursor left.
        previous = -1
        for x in range(0, 1920, 97):
            nx = normalize_to_virtual_desktop(x, 0, SINGLE_1080P)[0]
            assert nx >= previous
            previous = nx

    @pytest.mark.parametrize("virtual", [(0, 0, 0, 1080), (0, 0, 1920, 0)])
    def test_empty_desktop_raises(self, virtual: tuple[int, int, int, int]) -> None:
        with pytest.raises(ValueError, match="no area"):
            normalize_to_virtual_desktop(0, 0, virtual)

    def test_single_pixel_desktop_does_not_divide_by_zero(self) -> None:
        assert normalize_to_virtual_desktop(0, 0, (0, 0, 1, 1)) == (0, 0)


class TestUtf16Units:
    def test_ascii_is_one_unit_per_character(self) -> None:
        assert utf16_units("Hi") == [0x48, 0x69]

    def test_latin1_accent(self) -> None:
        assert utf16_units("é") == [0xE9]

    def test_bmp_character(self) -> None:
        assert utf16_units("€") == [0x20AC]

    def test_astral_character_becomes_a_surrogate_pair(self) -> None:
        # SendInput carries one 16-bit unit per event, so an emoji has to go as
        # its two surrogate halves in order or it arrives as garbage.
        assert utf16_units("\U0001f600") == [0xD83D, 0xDE00]

    def test_empty_string(self) -> None:
        assert utf16_units("") == []

    def test_unit_count_matches_utf16_length(self) -> None:
        text = "aé€\U0001f600"
        assert len(utf16_units(text)) == len(text.encode("utf-16-le")) // 2

    def test_newline_survives(self) -> None:
        assert utf16_units("\n") == [0x0A]


def raw(
    handle: int,
    left: int,
    top: int,
    width: int,
    height: int,
    primary: bool,
    dpi: int = DEFAULT_DPI,
) -> tuple[int, int, int, int, int, bool, int]:
    return (handle, left, top, width, height, primary, dpi)


class TestOrderDisplays:
    def test_primary_is_always_index_zero(self) -> None:
        # EnumDisplayMonitors gives no ordering guarantee, so the primary can
        # arrive second. The model addresses displays by index, so it must not.
        monitors = [
            raw(1, -1920, 0, 1920, 1080, False),
            raw(2, 0, 0, 2560, 1440, True),
        ]
        ordered = order_displays(monitors)
        assert ordered[0].display_id == 2
        assert ordered[0].is_main is True
        assert ordered[0].index == 0

    def test_secondaries_sort_top_to_bottom_then_left_to_right(self) -> None:
        monitors = [
            raw(3, 100, 500, 800, 600, False),
            raw(2, -800, 0, 800, 600, False),
            raw(1, 0, 0, 1920, 1080, True),
            raw(4, 900, 0, 800, 600, False),
        ]
        ordered = order_displays(monitors)
        assert [d.display_id for d in ordered] == [1, 2, 4, 3]

    def test_indices_are_contiguous_from_zero(self) -> None:
        monitors = [raw(i, i * 100, 0, 100, 100, i == 2) for i in range(1, 5)]
        ordered = order_displays(monitors)
        assert [d.index for d in ordered] == [0, 1, 2, 3]

    def test_ordering_is_stable_regardless_of_input_order(self) -> None:
        monitors = [
            raw(1, 0, 0, 1920, 1080, True),
            raw(2, 1920, 0, 1920, 1080, False),
        ]
        assert [d.display_id for d in order_displays(monitors)] == [
            d.display_id for d in order_displays(list(reversed(monitors)))
        ]

    def test_scale_is_dpi_over_96(self) -> None:
        ordered = order_displays([raw(1, 0, 0, 2880, 1800, True, dpi=144)])
        assert ordered[0].scale == 1.5

    @pytest.mark.parametrize(
        ("dpi", "expected"),
        [(96, 1.0), (120, 1.25), (144, 1.5), (192, 2.0), (240, 2.5)],
    )
    def test_common_windows_scaling_factors(self, dpi: int, expected: float) -> None:
        ordered = order_displays([raw(1, 0, 0, 100, 100, True, dpi=dpi)])
        assert ordered[0].scale == expected

    def test_bounds_are_carried_through_unchanged(self) -> None:
        ordered = order_displays([raw(7, -1920, -200, 1920, 1080, True)])
        display = ordered[0]
        assert (display.x, display.y, display.width, display.height) == (
            -1920,
            -200,
            1920,
            1080,
        )

    def test_no_monitors_yields_no_displays(self) -> None:
        assert order_displays([]) == []

    def test_handle_is_reported_as_display_id(self) -> None:
        assert order_displays([raw(4242, 0, 0, 100, 100, True)])[0].display_id == 4242
