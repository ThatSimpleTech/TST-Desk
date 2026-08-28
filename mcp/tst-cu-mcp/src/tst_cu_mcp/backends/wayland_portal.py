"""xdg-desktop-portal probe for Wayland computer-use (TD-4901).

Capture (ScreenCast) and input (RemoteDesktop) are *different* portal
interfaces. A compositor can ship one without the other (wlr ScreenCast
is common; RemoteDesktop is not). This module only *names* what is
there — it never CreateSession, never raises a picker, never hangs.

Live PipeWire frames and libei events are not in this probe. See
``docs/wayland-computer-use.md``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from typing import Final

from tst_cu_mcp.backends.linux import linux_session_kind

SCREENCAST_IFACE: Final = "org.freedesktop.portal.ScreenCast"
REMOTE_DESKTOP_IFACE: Final = "org.freedesktop.portal.RemoteDesktop"
_PORTAL_DEST: Final = "org.freedesktop.portal.Desktop"
_PORTAL_PATH: Final = "/org/freedesktop/portal/desktop"
_INTROSPECT_TIMEOUT_SECS: Final = 2.0

IntrospectFn = Callable[[], str]


def _busctl_introspect() -> str:
    """User-bus introspect of the desktop portal, or empty on any failure."""
    busctl = shutil.which("busctl")
    if busctl is None:
        return ""
    try:
        proc = subprocess.run(
            [busctl, "--user", "introspect", _PORTAL_DEST, _PORTAL_PATH],
            capture_output=True,
            text=True,
            timeout=_INTROSPECT_TIMEOUT_SECS,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "DISPLAY"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def portal_interfaces(*, introspect: IntrospectFn | None = None) -> frozenset[str]:
    """Portal interfaces visible on this session.

    Empty on X11, on a missing busctl, or when introspect fails. Never
    treats an XWayland ``DISPLAY`` as evidence of a portal grant.
    """
    if linux_session_kind() != "wayland":
        return frozenset()
    text = (introspect if introspect is not None else _busctl_introspect)()
    found: set[str] = set()
    if SCREENCAST_IFACE in text:
        found.add(SCREENCAST_IFACE)
    if REMOTE_DESKTOP_IFACE in text:
        found.add(REMOTE_DESKTOP_IFACE)
    return frozenset(found)


def screencast_listed(*, introspect: IntrospectFn | None = None) -> bool:
    """True when the ScreenCast interface is on the portal (TD-4901a)."""
    return SCREENCAST_IFACE in portal_interfaces(introspect=introspect)


def remotedesktop_listed(*, introspect: IntrospectFn | None = None) -> bool:
    """True when the RemoteDesktop interface is on the portal (TD-4901b)."""
    return REMOTE_DESKTOP_IFACE in portal_interfaces(introspect=introspect)
