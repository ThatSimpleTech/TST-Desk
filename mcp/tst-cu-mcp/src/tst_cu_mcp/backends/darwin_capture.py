"""Screen capture that leaves out agent-desktop's cursor windows.

agent-desktop draws each agent's cursor in ordinary, capturable windows. A
plain on-screen capture would show those cursors to the model, and one
agent's cursor in another agent's screenshot. When such a window is on
screen, the image is composed from the on-screen window list without them;
otherwise the plain capture runs unchanged. Mirrors the host's
``cu_capture.rs``. Function-local pyobjc so the module imports anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

EXCLUDED_OWNER = "agent-desktop"


def kept_window_ids(windows: Iterable[tuple[int, str]]) -> list[int]:
    """Window ids a capture draws: every ``(id, owner name)`` pair, front to
    back, except agent-desktop's, in the same order."""
    return [window_id for window_id, owner in windows if owner != EXCLUDED_OWNER]


def screen_image(rect: Any) -> Any:
    """The on-screen ``CGImage`` of *rect* (a ``CGRect`` in global points)
    without agent-desktop's windows. Raises what the plain call raises."""
    from Quartz import (
        CGWindowListCreateImage,
        kCGNullWindowID,
        kCGWindowImageDefault,
        kCGWindowListOptionOnScreenOnly,
    )

    kept = _kept_on_screen()
    if kept is not None:
        image = _image_of(rect, kept)
        if image is not None:
            return image
    return CGWindowListCreateImage(
        rect, kCGWindowListOptionOnScreenOnly, kCGNullWindowID, kCGWindowImageDefault
    )


def _image_of(rect: Any, window_ids: list[int]) -> Any:
    try:
        from Quartz import CGWindowListCreateImageFromArray, kCGWindowImageDefault

        return CGWindowListCreateImageFromArray(rect, window_ids, kCGWindowImageDefault)
    except Exception:
        return None


def _kept_on_screen() -> list[int] | None:
    """Window ids to draw, front to back, or ``None`` when no agent-desktop
    window is on screen (the plain capture is then the same picture, and
    cheaper) or the window list cannot be read."""
    try:
        from Quartz import (
            CGWindowListCreate,
            CGWindowListCreateDescriptionFromArray,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )

        ids = CGWindowListCreate(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        described = CGWindowListCreateDescriptionFromArray(ids) if ids else None
    except Exception:
        return None
    windows = _owners(ids or (), described or ())
    kept = kept_window_ids(windows)
    return kept if len(kept) < len(windows) else None


def _owners(ids: Iterable[Any], described: Iterable[Any]) -> list[tuple[int, str]]:
    """``(id, owner name)`` for each id, in the ids' order. A window with no
    description (it closed meanwhile) gets an empty owner, so it is kept."""
    names: dict[int, str] = {}
    for entry in described:
        getter = getattr(entry, "get", None)
        if getter is None:
            continue
        try:
            names[int(getter("kCGWindowNumber"))] = str(getter("kCGWindowOwnerName") or "")
        except (TypeError, ValueError):
            continue
    windows: list[tuple[int, str]] = []
    for raw in ids:
        try:
            window_id = int(raw)
        except (TypeError, ValueError):
            continue
        windows.append((window_id, names.get(window_id, "")))
    return windows
