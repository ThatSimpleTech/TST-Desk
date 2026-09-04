"""Which window macOS reports as the foreground one.

The old implementation asked ``NSWorkspace.frontmostApplication()`` for a pid
and then searched the window list for that pid's first window. That question is
answered relative to the *caller's* activation context, and a sidecar spawned by
the host app does not reliably share the user's: in a live session it named the
host app on all seven calls while Spotlight, then Finder, then a remote-desktop
window actually had the screen. Every ``expect_window`` guard and
``wait_for_window`` poll is built on this call, so all three were silently dead.

The window server's own z-order has no caller context to get wrong. These tests
pin the selection policy against recorded listings — the shapes below are real,
captured from a live desktop — and need neither macOS nor a desktop to run.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, cast

import pytest

from tst_cu_mcp.backends.darwin import DarwinBackend, frontmost_entry, window_from_entry
from tst_cu_mcp.focus import window_matches

# Window levels as macOS assigns them, named so the tests read as intent.
NORMAL = 0
FLOATING_PANEL = 3
MODAL_PANEL = 8
DOCK = 20
MENU_BAR = 24
STATUS_ITEM = 25
SCREEN_SAVER = 1000  # where the session ring paints


def win(
    owner: str,
    *,
    layer: int = NORMAL,
    pid: int = 4242,
    name: str | None = None,
    width: float = 1200,
    height: float = 800,
    x: float = 0,
    y: float = 25,
    alpha: float | None = None,
) -> dict[str, Any]:
    """One ``CGWindowListCopyWindowInfo`` entry, keyed as CoreGraphics keys it."""
    entry: dict[str, Any] = {
        "kCGWindowOwnerName": owner,
        "kCGWindowOwnerPID": pid,
        "kCGWindowLayer": layer,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": width, "Height": height},
    }
    if name is not None:
        entry["kCGWindowName"] = name
    if alpha is not None:
        entry["kCGWindowAlpha"] = alpha
    return entry


class TestDesktopChromeIsNeverTheAnswer:
    """Status items come first in the list. Taking entry [0] reports a 34x33
    menu-bar glyph as the window the user is typing into."""

    def test_status_items_menu_bar_and_dock_are_skipped(self) -> None:
        listing = [
            win("Control Center", layer=STATUS_ITEM, pid=1420, width=34, height=33),
            win("Control Center", layer=STATUS_ITEM, pid=1420, width=146, height=33),
            win("Window Server", layer=MENU_BAR, pid=171, name="Menubar", width=1512, height=33),
            win("Dock", layer=DOCK, pid=50558, width=1512, height=982),
            win("Finder", layer=NORMAL, pid=1423, width=920, height=436),
        ]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerName"] == "Finder"

    def test_the_session_ring_does_not_report_itself(self) -> None:
        """The overlay paints at the screen-saver level, above everything. A
        foreground window of "our own ring" would fail every guard."""
        listing = [
            win("tst-cu-mcp", layer=SCREEN_SAVER, pid=9001, width=1512, height=982),
            win("Finder", layer=NORMAL, pid=1423),
        ]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerName"] == "Finder"


class TestFrontToBackOrderDecides:
    def test_first_qualifying_entry_wins(self) -> None:
        """The list is front to back, so "first" is "frontmost"."""
        listing = [
            win("Chrome Remote Desktop", pid=46375),
            win("grok-desktop", pid=93930),
            win("TST Desk", pid=19415),
        ]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerPID"] == 46375

    @pytest.mark.parametrize("panel_layer", [FLOATING_PANEL, MODAL_PANEL])
    def test_a_panel_in_front_of_a_window_is_the_foreground_window(self, panel_layer: int) -> None:
        """Sheets, dialogs and torn-off panels take keystrokes. Reporting the
        window behind one is how text lands in the wrong place."""
        listing = [
            win("Preview", layer=panel_layer, pid=555, width=420, height=200),
            win("Preview", layer=NORMAL, pid=555),
        ]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowBounds"]["Width"] == 420


class TestWindowsNothingCanBeAimedAt:
    def test_fully_transparent_windows_are_skipped(self) -> None:
        listing = [win("Ghost", pid=1, alpha=0.0), win("Finder", pid=1423)]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerName"] == "Finder"

    def test_a_partially_transparent_window_still_counts(self) -> None:
        listing = [win("Fading", pid=1, alpha=0.4), win("Finder", pid=1423)]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerName"] == "Fading"

    def test_one_pixel_state_holders_are_skipped(self) -> None:
        listing = [win("Helper", pid=1, width=1, height=1), win("Finder", pid=1423)]
        entry = frontmost_entry(listing)
        assert entry is not None
        assert entry["kCGWindowOwnerName"] == "Finder"

    def test_an_empty_desktop_reports_nothing_rather_than_guessing(self) -> None:
        assert frontmost_entry([]) is None
        assert frontmost_entry([win("Dock", layer=DOCK, pid=50558)]) is None


class TestReadingAnEntry:
    def test_identity_and_bounds_come_from_the_chosen_window(self) -> None:
        window = window_from_entry(
            win("Finder", pid=1423, name="Downloads", x=116, y=53, width=920, height=436)
        )
        assert window.process == "Finder"
        assert window.title == "Downloads"
        assert window.pid == 1423
        assert (window.x, window.y, window.width, window.height) == (116, 53, 920, 436)

    def test_a_missing_title_still_identifies_the_application(self) -> None:
        """``kCGWindowName`` needs Screen Recording; without it every window in
        the list is nameless. The process name carries the guard on its own."""
        window = window_from_entry(win("Chrome Remote Desktop", pid=46375))
        assert window.title == ""
        assert window_matches("chrome remote", window)

    def test_nothing_in_front_reads_as_an_empty_window(self) -> None:
        window = window_from_entry(None)
        assert (window.process, window.title, window.pid) == ("", "", 0)
        assert window.describe()  # never raises, whatever it says
        assert not window_matches("Finder", window)


class FakeQuartz:
    """A window server that reports exactly the listing a test hands it."""

    def __init__(self, listing: list[dict[str, Any]] | None) -> None:
        self.kCGWindowListOptionOnScreenOnly = 1
        self.kCGWindowListExcludeDesktopElements = 16
        self.kCGNullWindowID = 0
        self._listing = listing
        self.options: list[int] = []

    def CGWindowListCopyWindowInfo(self, options: int, _relative: int) -> Any:
        self.options.append(options)
        return self._listing


def test_the_backend_reads_the_window_list_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression guard. These pids belong to no running application, so a
    result that matches them can only have come from the listing — reintroduce
    the ``frontmostApplication`` filter and this fails."""
    fake = FakeQuartz(
        [
            win("Control Center", layer=STATUS_ITEM, pid=1420, width=34, height=33),
            win("Chrome Remote Desktop", pid=46375, name="", width=1512, height=911),
            win("TST Desk", pid=19415),
        ]
    )
    monkeypatch.setitem(sys.modules, "Quartz", cast(ModuleType, fake))

    window = DarwinBackend().foreground_window()

    assert (window.process, window.pid) == ("Chrome Remote Desktop", 46375)
    assert (window.width, window.height) == (1512, 911)
    # On-screen only, desktop elements excluded — a full listing would put
    # wallpaper and off-screen windows ahead of the real answer.
    assert fake.options == [17]


def test_a_desktop_with_no_windows_does_not_crash_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "Quartz", cast(ModuleType, FakeQuartz(None)))
    assert DarwinBackend().foreground_window().pid == 0
