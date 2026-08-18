"""Which window is in front, and whether it is the one the caller meant.

This exists because of two real misfires during live use, both silent:

* Text typed into a window that had just opened was discarded — the window had
  focus but had not finished re-homing focus onto its own text field. The
  keystrokes were genuinely delivered, so the tool reported success.
* A click aimed at a menu item landed in the application *behind* it, because
  the menu closed between the screenshot and the click. A click targets a point,
  not a thing, and the point had come to mean something else.

Neither is preventable inside the input layer. Both are *detectable* by asking
the OS what is actually in front before acting, which is what
:func:`foreground_window` is for and what the ``expect_window`` guard on every
input tool does with it.

:func:`window_matches` is deliberately a pure function so the whole matching
policy is testable without a desktop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class WindowFocusError(RuntimeError):
    """Raised when the foreground window is not the one the caller expected.

    Carries both sides of the comparison: a model that gets this back needs to
    know what it is looking at, not just that it guessed wrong.
    """


@dataclass(frozen=True)
class WindowInfo:
    """The foreground window, as the OS reports it.

    ``title`` and ``process`` are both best-effort — a platform may know one and
    not the other, and an empty string is normal rather than an error (a window
    with no title bar, or a process we may not query). Bounds are in the
    backend's global coordinate space, the same one clicks use.
    """

    title: str
    process: str
    pid: int
    x: int
    y: int
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "process": self.process,
            "pid": self.pid,
            "bounds_points": {
                "x": self.x,
                "y": self.y,
                "width": self.width,
                "height": self.height,
            },
        }

    def describe(self) -> str:
        """Short human/model-readable identity, for error messages."""
        if self.title and self.process:
            return f"{self.title!r} ({self.process})"
        if self.title:
            return repr(self.title)
        if self.process:
            return f"untitled window in {self.process}"
        return f"untitled window (pid {self.pid})"


def window_matches(expected: str, window: WindowInfo) -> bool:
    """True if *expected* identifies *window*.

    Case-insensitive substring against **either** the window title or the
    process name. Substring rather than equality because real titles carry
    volatile detail — a browser tab is
    ``"2026_Engineer Report - Google Docs - Google Chrome"``, and requiring the
    whole string would make the guard unusable. Matching the process name too
    means ``"chrome"`` works when the caller only cares which application has
    focus.

    An empty or whitespace-only *expected* matches nothing: it almost certainly
    means the caller passed a variable that was never set, and silently allowing
    everything is the opposite of what the guard is for.
    """
    needle = expected.strip().casefold()
    if not needle:
        return False
    return needle in window.title.casefold() or needle in window.process.casefold()


def assert_foreground(expected: str | None) -> WindowInfo | None:
    """Check the foreground window against *expected*, or do nothing if None.

    Returns the observed window on success so callers can report what they acted
    on. Raises :class:`WindowFocusError` on mismatch, naming both sides.
    """
    if expected is None:
        return None

    window = foreground_window()
    if not window_matches(expected, window):
        raise WindowFocusError(
            f"expected the foreground window to match {expected!r}, but it is "
            f"{window.describe()}. Nothing was sent. Take a fresh screenshot: the "
            "UI has moved since the one this call was aimed at, or the window has "
            "not finished taking focus."
        )
    return window


def foreground_window() -> WindowInfo:
    """The window currently in front, via the active backend."""
    from tst_cu_mcp.backends import get_backend

    return get_backend().foreground_window()
