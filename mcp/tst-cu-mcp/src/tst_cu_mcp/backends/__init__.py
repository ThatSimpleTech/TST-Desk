"""Backend selection.

``get_backend()`` resolves the platform implementation from ``sys.platform``.
The lookup is deliberately uncached and takes an explicit override, so a test
can ask for either platform's backend on one host — constructing a backend
performs no OS calls, which is what makes that safe.
"""

from __future__ import annotations

import sys

from tst_cu_mcp.backends.base import Backend

#: ``sys.platform`` values with a real implementation.
#: Linux is X11; a Wayland session still selects this backend but ``health``
#: reports ``supported: false`` (TD-2001 / TD-2002).
SUPPORTED_PLATFORMS = ("darwin", "win32", "linux")


class UnsupportedPlatformError(RuntimeError):
    """Raised when no backend exists for the running platform."""


def get_backend(platform: str | None = None) -> Backend:
    """Return the backend for *platform* (default: the running platform).

    Raises :class:`UnsupportedPlatformError` on a platform with no
    implementation, naming what is supported — a clearer failure than an
    ``ImportError`` for a framework that was never going to be there.
    """
    target = sys.platform if platform is None else platform

    if target == "darwin":
        from tst_cu_mcp.backends.darwin import DarwinBackend

        return DarwinBackend()

    if target == "win32":
        from tst_cu_mcp.backends.windows import WindowsBackend

        return WindowsBackend()

    if target.startswith("linux"):
        from tst_cu_mcp.backends.linux import LinuxBackend

        return LinuxBackend()

    raise UnsupportedPlatformError(
        f"no computer-use backend for platform {target!r}; "
        f"supported: {', '.join(SUPPORTED_PLATFORMS)}"
    )


def backend_name(platform: str | None = None) -> str | None:
    """Backend name for *platform*, or ``None`` if unsupported.

    Used by ``health``, which must answer on any platform rather than raise.
    """
    try:
        return get_backend(platform).name
    except UnsupportedPlatformError:
        return None


__all__ = [
    "SUPPORTED_PLATFORMS",
    "Backend",
    "UnsupportedPlatformError",
    "backend_name",
    "get_backend",
]
