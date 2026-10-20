"""Physical memory, and the shell memory cap it defaults to (TD-5116).

``caps.shell_memory_mb`` defaults to half of the machine's RAM. It lives
apart from the shell tool so the workspace config and the trust gate can
read the default without importing the tool package.
"""

from __future__ import annotations

import ctypes
import os
import sys

MIB = 1024 * 1024
# Below this a shell cannot start its own interpreter under an
# address-space limit, so every command would fail with a confusing error.
MIN_SHELL_MEMORY_MB = 64


class _MemoryStatusEx(ctypes.Structure):
    """``MEMORYSTATUSEX`` for ``GlobalMemoryStatusEx`` on Windows."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_total_bytes() -> int | None:
    # ``ctypes.windll`` exists only on Windows and is absent from mypy's
    # Linux stubs, so it is looked up rather than imported.
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def _posix_total_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return pages * page_size


def physical_memory_mb() -> int | None:
    """Installed RAM in MiB, or None when the platform will not say."""
    total = _windows_total_bytes() if sys.platform == "win32" else _posix_total_bytes()
    return None if total is None else total // MIB


def default_shell_memory_mb() -> int | None:
    """Half of RAM, never under the floor; None when RAM is unknown."""
    total = physical_memory_mb()
    if total is None:
        return None
    return max(MIN_SHELL_MEMORY_MB, total // 2)


def shell_memory_cap_mb(configured: int | None) -> int | None:
    """The cap in force: the configured value, else the default."""
    return configured if configured is not None else default_shell_memory_mb()
