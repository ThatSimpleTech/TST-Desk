"""The ``health`` tool: liveness plus a small environment summary.

Intentionally dependency-free (no pyobjc, no ctypes, no capture) so it can answer
even when permissions are missing or the platform is unsupported. Detailed
permission state is reported by the separate ``check_permissions`` tool.
"""

from __future__ import annotations

import platform
import sys
from typing import Any

from tst_cu_mcp._version import __version__
from tst_cu_mcp.backends import SUPPORTED_PLATFORMS, backend_name
from tst_cu_mcp.backends.linux import linux_session_kind, linux_session_usable
from tst_cu_mcp.backends.wayland import wayland_supported

SERVER_NAME = "tst-cu-mcp"


def health_report() -> dict[str, Any]:
    """Return a small liveness/report payload.

    Pure and safe to call in any environment, including one this server does not
    support: ``supported`` is reported rather than raised, so an unsupported host
    is visible instead of failing at the first real call. ``backend`` is the
    implementation that would handle capture and input, or ``None``.

    On Linux, ``supported`` is true for a native X11 session, or for a
    Wayland session only when *both* ScreenCast and RemoteDesktop
    strategies are available (TD-4901). Capture-only compositors stay
    unsupported. An XWayland ``DISPLAY`` never flips the bit.
    """
    if sys.platform.startswith("linux"):
        session = linux_session_kind()
        if session == "wayland":
            usable = wayland_supported()
            backend = "wayland" if usable else None
        else:
            usable = linux_session_usable()
            backend = backend_name() if usable else None
        return {
            "name": SERVER_NAME,
            "version": __version__,
            "platform": sys.platform,
            "system": platform.system(),
            "session_type": session,
            "supported": usable,
            "supported_platforms": list(SUPPORTED_PLATFORMS),
            "backend": backend,
            "permissions": "call check_permissions for this platform's capture/input status",
        }
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "platform": sys.platform,
        "system": platform.system(),
        "supported": sys.platform in SUPPORTED_PLATFORMS,
        "supported_platforms": list(SUPPORTED_PLATFORMS),
        "backend": backend_name(),
        "permissions": "call check_permissions for this platform's capture/input status",
    }
