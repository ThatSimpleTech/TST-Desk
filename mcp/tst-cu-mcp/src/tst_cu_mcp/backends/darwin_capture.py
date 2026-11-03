"""Screen capture that leaves out agent-desktop's cursor windows.

agent-desktop draws each agent's cursor in ordinary, capturable windows. A
plain on-screen capture would show those cursors to the model, and one
agent's cursor in another agent's screenshot. When such a window is on
screen, the image is composed from the on-screen window list without them;
otherwise the plain capture runs unchanged. Mirrors the host's
``cu_capture.rs``. Function-local pyobjc so the module imports anywhere.

A window is agent-desktop's when its owner name is ``agent-desktop`` or its
owning process runs an executable of that name: the copy bundled inside an
app (TST Desk ships one) reports the app's name as the owner, so the name
alone misses it (TD-5231).
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable
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


def process_name(pid: int) -> str:
    """The file name of *pid*'s executable, or "" when it cannot be read."""
    try:
        libproc = ctypes.CDLL("libproc.dylib")
        buffer = ctypes.create_string_buffer(4096)
        length = libproc.proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(4096))
    except (OSError, AttributeError):
        return ""
    if length <= 0:
        return ""
    return os.path.basename(buffer.value.decode("utf-8", "replace"))


def _owners(
    ids: Iterable[Any],
    described: Iterable[Any],
    process_name: Callable[[int], str] = process_name,
) -> list[tuple[int, str]]:
    """``(id, owner)`` for each id, in the ids' order; the owner reads
    ``agent-desktop`` when the owning process runs that executable. A window
    with no description (it closed meanwhile) gets an empty owner, so it is
    kept."""
    names: dict[int, str] = {}
    executables: dict[int, str] = {}
    for entry in described:
        getter = getattr(entry, "get", None)
        if getter is None:
            continue
        try:
            window_id = int(getter("kCGWindowNumber"))
            owner = str(getter("kCGWindowOwnerName") or "")
            raw_pid = getter("kCGWindowOwnerPID")
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            continue
        if pid is not None and pid > 0:
            if pid not in executables:
                executables[pid] = process_name(pid)
            if executables[pid] == EXCLUDED_OWNER:
                owner = EXCLUDED_OWNER
        names[window_id] = owner
    windows: list[tuple[int, str]] = []
    for raw in ids:
        try:
            window_id = int(raw)
        except (TypeError, ValueError):
            continue
        windows.append((window_id, names.get(window_id, "")))
    return windows
