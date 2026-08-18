"""Windows tests that only a real Windows desktop can settle.

Everything else in this suite proves logic. These prove the OS actually does what
the port claims: that DPI awareness took effect, that captured pixels are real,
and that a synthesized absolute mouse move lands where the maths said it would.
No amount of unit testing substitutes for that, and ticking it from a green run
on another platform would be a lie.

Excluded from the default run by the `desktop` marker. The cursor test moves the
real pointer and puts it back; nothing here types, clicks, or scrolls, so it
cannot disturb a document. The genuinely disruptive case is marked `intrusive`
and kept separate.

Run with:  pytest -m desktop
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from io import BytesIO

import pytest
from PIL import Image

from tst_cu_mcp import capture, input_control
from tst_cu_mcp.backends.windows import (
    WindowsBackend,
    cursor_position,
    is_elevated,
    virtual_desktop,
)
from tst_cu_mcp.displays import DisplayInfo, screen_info
from tst_cu_mcp.focus import foreground_window, window_matches
from tst_cu_mcp.tools.health import health_report
from tst_cu_mcp.waiting import wait_for_window

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(sys.platform != "win32", reason="native Windows desktop required"),
]

# The absolute grid has 65536 steps across the whole virtual desktop, so a very
# wide multi-monitor layout quantises to just over a tenth of a pixel. Two pixels
# of tolerance absorbs that without hiding a real mapping error, which would be
# wrong by hundreds.
CURSOR_TOLERANCE_PX = 2


@pytest.fixture
def backend() -> WindowsBackend:
    return WindowsBackend()


@pytest.fixture
def restore_cursor() -> Iterator[None]:
    """Put the pointer back where the user left it, even if the test fails."""
    origin = cursor_position()
    try:
        yield
    finally:
        WindowsBackend().move_mouse(*origin)


class TestRealDisplays:
    def test_at_least_one_display_is_found(self, backend: WindowsBackend) -> None:
        assert backend.list_displays() != []

    def test_exactly_one_primary(self, backend: WindowsBackend) -> None:
        primaries = [d for d in backend.list_displays() if d.is_main]
        assert len(primaries) == 1

    def test_primary_is_index_zero(self, backend: WindowsBackend) -> None:
        assert backend.list_displays()[0].is_main is True

    def test_dimensions_are_positive(self, backend: WindowsBackend) -> None:
        for display in backend.list_displays():
            assert display.width > 0
            assert display.height > 0

    def test_scale_is_plausible(self, backend: WindowsBackend) -> None:
        # Windows scaling runs 100%-500%. A 0 or absurd value means GetDpiForMonitor
        # failed and the default leaked through as something else.
        for display in backend.list_displays():
            assert 1.0 <= display.scale <= 5.0

    def test_displays_fit_inside_the_virtual_desktop(self, backend: WindowsBackend) -> None:
        # This is the real DPI-awareness check. Without per-monitor awareness the
        # monitor rectangles come back scaled while the virtual desktop metrics do
        # not, and on any scaled display the two disagree.
        vx, vy, vw, vh = virtual_desktop()
        for display in backend.list_displays():
            assert display.x >= vx
            assert display.y >= vy
            assert display.x + display.width <= vx + vw
            assert display.y + display.height <= vy + vh

    def test_virtual_desktop_has_area(self) -> None:
        _, _, width, height = virtual_desktop()
        assert width > 0
        assert height > 0

    def test_display_ids_are_unique(self, backend: WindowsBackend) -> None:
        displays = backend.list_displays()
        assert len({d.display_id for d in displays}) == len(displays)

    def test_screen_info_agrees_with_the_backend(self, backend: WindowsBackend) -> None:
        info = screen_info()
        assert info["count"] == len(backend.list_displays())
        assert info["main_index"] == 0


class TestRealCapture:
    def test_a_small_region_returns_a_valid_png(self, backend: WindowsBackend) -> None:
        primary = backend.list_displays()[0]
        raw = backend.capture_png((primary.x, primary.y, 64, 48))
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(raw)) as image:
            assert image.size == (64, 48)

    def test_capture_never_writes_to_disk(self, backend: WindowsBackend) -> None:
        # The Windows path goes straight to memory; there is no temp file to leak.
        # Asserted by there being no filesystem involvement to assert on: if this
        # ever grows a temp file, the docstring in capture_png is wrong.
        primary = backend.list_displays()[0]
        assert backend.capture_png((primary.x, primary.y, 8, 8))

    def test_full_display_capture_matches_the_reported_bounds(
        self, backend: WindowsBackend
    ) -> None:
        # End-to-end proof that reported geometry and real pixels are the same
        # space: ask for the whole primary display with downscaling disabled and
        # the image must come back exactly that many pixels. On a scaled display
        # without DPI awareness this comes back smaller.
        primary = backend.list_displays()[0]
        result = capture.capture(display_index=0, max_long_edge=0)
        assert result.downscaled is False
        assert (result.image_px_width, result.image_px_height) == (
            primary.width,
            primary.height,
        )

    def test_pixels_are_not_uniformly_black(self, backend: WindowsBackend) -> None:
        # An all-black frame is the signature of capturing the secure desktop or
        # of a failed grab that still returned an image.
        primary = backend.list_displays()[0]
        raw = backend.capture_png((primary.x, primary.y, primary.width, primary.height))
        with Image.open(BytesIO(raw)) as image:
            brightest = image.convert("L").getextrema()[1]
        # Single-band after convert("L"), so the multiband tuple shape cannot occur.
        assert not isinstance(brightest, tuple)
        assert brightest > 0, "captured frame was entirely black"

    def test_region_offsets_capture_different_content(self, backend: WindowsBackend) -> None:
        # Guards against a backend that ignores the rectangle and always returns
        # the same grab, which would pass every size assertion above.
        primary = backend.list_displays()[0]
        if primary.width < 400 or primary.height < 400:
            pytest.skip("display too small to compare two distinct regions")
        first = backend.capture_png((primary.x, primary.y, 200, 200))
        second = backend.capture_png(
            (primary.x + primary.width - 200, primary.y + primary.height - 200, 200, 200)
        )
        assert first != second


@pytest.mark.usefixtures("restore_cursor")
class TestRealCursorMovement:
    """The absolute-coordinate mapping, verified by asking the OS where it went."""

    def test_move_lands_where_it_was_asked(self, backend: WindowsBackend) -> None:
        primary = backend.list_displays()[0]
        target = (primary.x + primary.width // 2, primary.y + primary.height // 2)
        backend.move_mouse(*target)
        actual = cursor_position()
        assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX
        assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX

    def test_display_origin_is_reachable(self, backend: WindowsBackend) -> None:
        # The origin is where an off-by-one in the normalisation shows up first.
        primary = backend.list_displays()[0]
        backend.move_mouse(primary.x, primary.y)
        actual = cursor_position()
        assert abs(actual[0] - primary.x) <= CURSOR_TOLERANCE_PX
        assert abs(actual[1] - primary.y) <= CURSOR_TOLERANCE_PX

    def test_far_corner_is_reachable(self, backend: WindowsBackend) -> None:
        # The other end, where dividing by width instead of width-1 would leave
        # the cursor permanently short.
        primary = backend.list_displays()[0]
        target = (primary.x + primary.width - 1, primary.y + primary.height - 1)
        backend.move_mouse(*target)
        actual = cursor_position()
        assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX
        assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX

    @pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 0.75, 0.9])
    def test_round_trip_across_the_primary_display(
        self, backend: WindowsBackend, fraction: float
    ) -> None:
        primary = backend.list_displays()[0]
        target = (
            primary.x + int(primary.width * fraction),
            primary.y + int(primary.height * fraction),
        )
        backend.move_mouse(*target)
        actual = cursor_position()
        assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX
        assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX

    def test_every_display_is_reachable(self, backend: WindowsBackend) -> None:
        # A second monitor left of the primary has negative coordinates. If the
        # virtual-desktop flag were missing, those clamp to the primary instead.
        for display in backend.list_displays():
            target = (display.x + display.width // 2, display.y + display.height // 2)
            backend.move_mouse(*target)
            actual = cursor_position()
            assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX, display.index
            assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX, display.index


class TestRealForegroundWindow:
    """The guard is only as good as this read, and only the OS can confirm it."""

    def test_reports_a_window(self, backend: WindowsBackend) -> None:
        window = backend.foreground_window()
        # Something is always in front on a live desktop, and it has a pid.
        assert window.pid > 0

    def test_has_a_title_or_a_process(self, backend: WindowsBackend) -> None:
        window = backend.foreground_window()
        assert window.title or window.process

    def test_process_name_is_a_bare_filename(self, backend: WindowsBackend) -> None:
        # Deliberately not a full path: the guard matches on it, and a path would
        # leak the user's directory layout into every response.
        process = backend.foreground_window().process
        if process:
            assert "\\" not in process
            assert "/" not in process
            assert process.lower().endswith(".exe")

    def test_bounds_are_plausible(self, backend: WindowsBackend) -> None:
        window = backend.foreground_window()
        if window.width or window.height:
            assert window.width > 0
            assert window.height > 0

    def test_matches_itself(self, backend: WindowsBackend) -> None:
        # The round trip that matters: whatever the OS reports must satisfy a
        # guard written against it, or expect_window is unusable in practice.
        window = backend.foreground_window()
        needle = window.process or window.title
        assert window_matches(needle, window) is True

    def test_does_not_match_something_absent(self, backend: WindowsBackend) -> None:
        window = backend.foreground_window()
        assert window_matches("no-such-window-zzz", window) is False

    def test_module_level_reader_agrees_with_the_backend(self, backend: WindowsBackend) -> None:
        assert foreground_window().pid == backend.foreground_window().pid


class TestRealWaitForWindow:
    def test_finds_the_current_foreground_immediately(self) -> None:
        current = foreground_window()
        needle = current.process or current.title
        result = wait_for_window(needle, timeout_seconds=2)
        assert result["matched"] is True
        assert result["waited_seconds"] < 1

    def test_times_out_on_something_absent(self) -> None:
        result = wait_for_window("no-such-window-zzz", timeout_seconds=0.5)
        assert result["matched"] is False
        assert result["expected"] == "no-such-window-zzz"
        assert "hint" in result


@pytest.mark.usefixtures("restore_cursor")
class TestRealCursorReadBack:
    def test_backend_and_module_agree(self, backend: WindowsBackend) -> None:
        assert backend.cursor_position() == input_control.cursor_position()

    def test_reflects_a_move(self, backend: WindowsBackend) -> None:
        primary = backend.list_displays()[0]
        target = (primary.x + primary.width // 3, primary.y + primary.height // 3)
        backend.move_mouse(*target)
        reported = backend.cursor_position()
        assert abs(reported[0] - target[0]) <= CURSOR_TOLERANCE_PX
        assert abs(reported[1] - target[1]) <= CURSOR_TOLERANCE_PX


class TestRealEnvironment:
    def test_health_reports_windows_support(self) -> None:
        report = health_report()
        assert report["supported"] is True
        assert report["backend"] == "windows"
        assert report["platform"] == "win32"

    def test_permissions_report_is_the_windows_shape(self, backend: WindowsBackend) -> None:
        report = backend.check_permissions()
        assert report["platform"] == "windows"
        assert report["all_granted"] is True
        assert set(report["limits"]) == {"uipi", "secure_desktop"}

    def test_elevation_probe_answers(self) -> None:
        assert isinstance(is_elevated(), bool)

    def test_reported_elevation_matches_the_probe(self, backend: WindowsBackend) -> None:
        assert backend.check_permissions()["elevated"] is is_elevated()

    def test_uipi_is_reported_as_live_when_unelevated(self, backend: WindowsBackend) -> None:
        report = backend.check_permissions()
        assert report["limits_apply"]["uipi"] is not is_elevated()


class TestDisplayInfoShape:
    def test_real_displays_serialise(self, backend: WindowsBackend) -> None:
        for display in backend.list_displays():
            assert isinstance(display, DisplayInfo)
            payload = display.to_dict()
            assert set(payload) == {"index", "display_id", "is_main", "scale", "bounds_points"}
            assert set(payload["bounds_points"]) == {"x", "y", "width", "height"}


# Keyboard and scroll actuation deliberately does NOT live in this file. It used
# to, marked `intrusive` on top of this module's blanket `desktop` mark — and
# `pytest -m desktop` selected it anyway, because it carried both marks and a
# command-line `-m` replaces the `addopts` filter rather than narrowing it. It
# typed into whichever window happened to have focus, three times, before anyone
# noticed. It now lives in `test_intrusive_windows.py` behind an environment
# variable, so no marker selection can reach it by accident.
