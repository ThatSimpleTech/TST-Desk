"""Linux tests that only a real X11 desktop can settle.

Excluded from the default run by the `desktop` marker. The cursor test moves the
real pointer and puts it back; nothing here types, clicks, or scrolls.

Run with:  pytest -m desktop
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from io import BytesIO

import pytest
from PIL import Image

from tst_cu_mcp import capture
from tst_cu_mcp.backends.linux import LinuxBackend
from tst_cu_mcp.backends.linux_x11 import cursor_position, virtual_desktop
from tst_cu_mcp.displays import screen_info
from tst_cu_mcp.focus import window_matches
from tst_cu_mcp.tools.health import health_report

pytestmark = [
    pytest.mark.desktop,
    pytest.mark.skipif(sys.platform != "linux", reason="native Linux desktop required"),
]

CURSOR_TOLERANCE_PX = 2


@pytest.fixture
def backend() -> LinuxBackend:
    return LinuxBackend()


@pytest.fixture
def restore_cursor() -> Iterator[None]:
    origin = cursor_position()
    try:
        yield
    finally:
        LinuxBackend().move_mouse(*origin)


class TestRealDisplays:
    def test_at_least_one_display_is_found(self, backend: LinuxBackend) -> None:
        assert backend.list_displays() != []

    def test_exactly_one_primary(self, backend: LinuxBackend) -> None:
        primaries = [d for d in backend.list_displays() if d.is_main]
        assert len(primaries) == 1

    def test_primary_is_index_zero(self, backend: LinuxBackend) -> None:
        assert backend.list_displays()[0].is_main is True

    def test_dimensions_are_positive(self, backend: LinuxBackend) -> None:
        for display in backend.list_displays():
            assert display.width > 0
            assert display.height > 0

    def test_displays_fit_inside_the_virtual_desktop(self, backend: LinuxBackend) -> None:
        vx, vy, vw, vh = virtual_desktop()
        for display in backend.list_displays():
            assert display.x >= vx
            assert display.y >= vy
            assert display.x + display.width <= vx + vw
            assert display.y + display.height <= vy + vh

    def test_screen_info_agrees_with_the_backend(self, backend: LinuxBackend) -> None:
        info = screen_info()
        assert info["count"] == len(backend.list_displays())
        assert info["main_index"] == 0


class TestRealCapture:
    def test_a_small_region_returns_a_valid_png(self, backend: LinuxBackend) -> None:
        primary = backend.list_displays()[0]
        raw = backend.capture_png((primary.x, primary.y, 64, 48))
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(BytesIO(raw)) as image:
            assert image.size == (64, 48)

    def test_full_display_capture_matches_the_reported_bounds(self, backend: LinuxBackend) -> None:
        primary = backend.list_displays()[0]
        result = capture.capture(display_index=0, max_long_edge=0)
        assert result.downscaled is False
        assert (result.image_px_width, result.image_px_height) == (
            primary.width,
            primary.height,
        )


@pytest.mark.usefixtures("restore_cursor")
class TestRealCursorMovement:
    def test_move_lands_where_it_was_asked(self, backend: LinuxBackend) -> None:
        primary = backend.list_displays()[0]
        target = (primary.x + primary.width // 2, primary.y + primary.height // 2)
        backend.move_mouse(*target)
        actual = cursor_position()
        assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX
        assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX

    @pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 0.75, 0.9])
    def test_round_trip_across_the_primary_display(
        self, backend: LinuxBackend, fraction: float
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

    def test_every_display_is_reachable(self, backend: LinuxBackend) -> None:
        for display in backend.list_displays():
            target = (display.x + display.width // 2, display.y + display.height // 2)
            backend.move_mouse(*target)
            actual = cursor_position()
            assert abs(actual[0] - target[0]) <= CURSOR_TOLERANCE_PX, display.index
            assert abs(actual[1] - target[1]) <= CURSOR_TOLERANCE_PX, display.index


class TestRealForegroundWindow:
    def test_reports_a_window(self, backend: LinuxBackend) -> None:
        window = backend.foreground_window()
        assert window.pid > 0

    def test_has_a_title_or_a_process(self, backend: LinuxBackend) -> None:
        window = backend.foreground_window()
        assert window.title or window.process

    def test_process_name_is_a_bare_filename(self, backend: LinuxBackend) -> None:
        process = backend.foreground_window().process
        if process:
            assert "/" not in process

    def test_matches_itself(self, backend: LinuxBackend) -> None:
        window = backend.foreground_window()
        needle = window.process or window.title
        assert window_matches(needle, window) is True


class TestHealthOnX11:
    def test_health_reports_supported(self) -> None:
        report = health_report()
        assert report["supported"] is True
        assert report["backend"] == "linux"
        assert report["session_type"] == "x11"
