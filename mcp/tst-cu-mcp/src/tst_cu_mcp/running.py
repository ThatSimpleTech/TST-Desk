"""List, launch, hide, and unhide applications.

Listing is a read: it does not take the pointer and does not need the
kill-switch. Launch/hide/unhide are actuation. Platform imports stay
function-local so this module loads on every host.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

from tst_cu_mcp.apps import AppInfo

# Bundle ids we never hide: the host the user is watching, and the system
# bits hiding them would strand the session.
_KEEP_BUNDLE_PREFIXES = (
    "com.thatsimpletech.tstdesk",
    "com.apple.finder",  # hiding Finder also hides the desktop
)


_hidden: list[int] = []


def list_running() -> list[AppInfo]:
    """Regular (user-facing) applications currently running."""
    if sys.platform == "darwin":
        return _darwin_running()
    if sys.platform == "win32":
        return _win_running()
    if sys.platform.startswith("linux"):
        return _linux_running()
    return []


def launch_named(name: str) -> dict[str, Any]:
    """Open *name* (display name or bundle id) without taking the pointer."""
    target = name.strip()
    if not target:
        raise ValueError("app name is empty")
    if sys.platform == "darwin":
        argv = ["open", "-b", target] if _looks_like_bundle_id(target) else ["open", "-a", target]
        proc = subprocess.run(argv, capture_output=True, timeout=15, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace")[:200]
            raise RuntimeError(f"could not launch {target!r}: {detail or proc.returncode}")
        return {"launched": target, "argv": argv}
    if sys.platform == "win32":
        # os.startfile is the shell association; it does not take the pointer.
        import os

        os.startfile(target)
        return {"launched": target, "argv": ["startfile", target]}
    binary = shutil.which(target)
    if binary is None:
        raise RuntimeError(f"could not launch {target!r}: not on PATH")
    subprocess.Popen(
        [binary],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"launched": target, "argv": [binary]}


def hide_except(keep: AppInfo) -> dict[str, Any]:
    """Hide every regular app except *keep* and TST Desk. Darwin only."""
    if sys.platform != "darwin":
        raise RuntimeError("hide_other_apps is macOS-only")
    hidden: list[int] = []
    for app in _darwin_running_ns():
        bundle = str(app.get("bundle_id") or "")
        pid = int(app["pid"])
        if pid == keep.pid:
            continue
        if any(bundle.startswith(prefix) for prefix in _KEEP_BUNDLE_PREFIXES):
            continue
        if _darwin_hide_pid(pid):
            hidden.append(pid)
    _hidden.clear()
    _hidden.extend(hidden)
    return {"hidden_pids": hidden, "kept": keep.to_dict()}


def unhide_hidden() -> dict[str, Any]:
    """Restore apps this process hid. Darwin only."""
    if sys.platform != "darwin":
        raise RuntimeError("unhide_apps is macOS-only")
    restored: list[int] = []
    remaining: list[int] = []
    for pid in list(_hidden):
        if _darwin_unhide_pid(pid):
            restored.append(pid)
        else:
            remaining.append(pid)
    _hidden.clear()
    _hidden.extend(remaining)
    return {"restored_pids": restored, "still_hidden": remaining}


def _looks_like_bundle_id(name: str) -> bool:
    return "." in name and not name.startswith(".") and " " not in name


def _darwin_running() -> list[AppInfo]:
    rows = _darwin_running_ns()
    windows = _darwin_window_titles()
    apps: list[AppInfo] = []
    for row in rows:
        pid = int(row["pid"])
        apps.append(
            AppInfo(
                name=str(row.get("name") or ""),
                pid=pid,
                bundle_id=str(row.get("bundle_id") or ""),
                hidden=bool(row.get("hidden")),
                active=bool(row.get("active")),
                windows=tuple(windows.get(pid, ())),
            )
        )
    return apps


def _darwin_running_ns() -> list[dict[str, Any]]:
    """NSWorkspace regular apps. Function-local pyobjc so Windows can import."""
    try:
        from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace
    except (ImportError, AttributeError):
        return []
    workspace = NSWorkspace.sharedWorkspace()
    rows: list[dict[str, Any]] = []
    for app in workspace.runningApplications():
        try:
            if int(app.activationPolicy()) != int(NSApplicationActivationPolicyRegular):
                continue
            pid = int(app.processIdentifier())
        except (AttributeError, TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        rows.append(
            {
                "name": str(app.localizedName() or ""),
                "pid": pid,
                "bundle_id": str(app.bundleIdentifier() or ""),
                "hidden": bool(app.isHidden()),
                "active": bool(app.isActive()),
            }
        )
    return rows


def _darwin_window_titles() -> dict[int, list[str]]:
    """Best-effort window titles per pid. Empty titles without Screen Recording."""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except (ImportError, AttributeError):
        return {}
    listing = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
    titles: dict[int, list[str]] = {}
    for entry in listing:
        try:
            pid = int(entry.get("kCGWindowOwnerPID") or 0)
        except (TypeError, ValueError):
            continue
        name = str(entry.get("kCGWindowName") or "").strip()
        if pid > 0 and name:
            titles.setdefault(pid, [])
            if name not in titles[pid]:
                titles[pid].append(name)
    return titles


def _darwin_hide_pid(pid: int) -> bool:
    app = _ns_running(pid)
    if app is None:
        return False
    try:
        return bool(app.hide())
    except Exception:
        return False


def _darwin_unhide_pid(pid: int) -> bool:
    app = _ns_running(pid)
    if app is None:
        return False
    try:
        return bool(app.unhide())
    except Exception:
        return False


def _ns_running(pid: int) -> Any | None:
    try:
        from AppKit import NSRunningApplication
    except (ImportError, AttributeError):
        return None
    try:
        return NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    except Exception:
        return None


def _win_running() -> list[AppInfo]:
    """Top-level windows grouped by pid. Best-effort names; no bundle id."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return []
    user32 = ctypes.windll.user32
    pid_of: dict[int, list[str]] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        value = int(pid.value)
        if value:
            pid_of.setdefault(value, [])
            if title not in pid_of[value]:
                pid_of[value].append(title)
        return True

    user32.EnumWindows(_enum, 0)
    apps: list[AppInfo] = []
    for pid, titles in pid_of.items():
        apps.append(AppInfo(name=titles[0] if titles else "", pid=pid, windows=tuple(titles)))
    return apps


def _linux_running() -> list[AppInfo]:
    """Background app listing is macOS-first. Linux uses screenshot/click."""
    return []
