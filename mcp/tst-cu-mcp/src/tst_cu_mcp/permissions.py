"""Permission and capability reporting, per platform.

The platforms differ in kind, not degree:

* **macOS** gates capture and input behind TCC permissions the user must grant,
  and attaches the grant to the *host* process rather than to this server.
* **Windows** gates nothing — but has two conditions under which actuation
  succeeds and does nothing at all. Reporting an unqualified "granted" there
  would be technically true and practically a lie, so the Windows report names
  both limits explicitly.
* **Linux / X11** gates nothing either. A Wayland session cannot be driven
  (TD-2002); that is reported rather than silently using XWayland.

The report builders are pure functions taking the probed state as arguments, so
every wording and branch is testable on any host. Probing itself lives in the
backends.
"""

from __future__ import annotations

from typing import Any

SCREEN_RECORDING = "screen_recording"
ACCESSIBILITY = "accessibility"

HOST_CAVEAT = (
    "macOS grants these permissions to the application that launches this server "
    "(Kiro, Claude Desktop, Goose, or your terminal) — not to the server itself. "
    "After enabling a permission, fully quit and reopen that host app: macOS caches "
    "the grant per-binary, so reconnecting alone will not pick it up."
)

_FIX = {
    SCREEN_RECORDING: (
        "Open System Settings > Privacy & Security > Screen Recording, enable the "
        "host app, then quit and reopen it."
    ),
    ACCESSIBILITY: (
        "Open System Settings > Privacy & Security > Accessibility, enable the host "
        "app, then quit and reopen it."
    ),
}

_REQUIRED_FOR = {
    SCREEN_RECORDING: "capturing the screen (screenshot / vision)",
    ACCESSIBILITY: "controlling the mouse and keyboard",
}

# --- Windows ----------------------------------------------------------------

UIPI_LIMIT = (
    "User Interface Privilege Isolation: a process cannot send synthetic input to "
    "a window running at a higher integrity level. Clicks and keystrokes aimed at "
    "an elevated (Run as administrator) window are discarded by the OS. The server "
    "detects the short SendInput result and raises rather than reporting success, "
    "but it cannot deliver the input. Fix: launch the host app elevated too."
)

SECURE_DESKTOP_LIMIT = (
    "The secure desktop — UAC consent prompts, the lock screen, Ctrl+Alt+Del — runs "
    "in a separate session that cannot be captured or driven at all. Screenshots of "
    "it come back black and input never arrives. There is no workaround; this is the "
    "boundary that makes UAC meaningful."
)

WINDOWS_NO_GATE = (
    "Windows has no equivalent of macOS TCC for screen capture or input synthesis, "
    "so there is nothing to grant and no prompt to raise."
)


def build_report(*, screen_recording: bool, accessibility: bool) -> dict[str, Any]:
    """Assemble the macOS permission report. Pure and side-effect free."""
    missing = [
        name
        for name, granted in ((SCREEN_RECORDING, screen_recording), (ACCESSIBILITY, accessibility))
        if not granted
    ]
    report: dict[str, Any] = {
        "platform": "macos",
        SCREEN_RECORDING: {
            "granted": screen_recording,
            "required_for": _REQUIRED_FOR[SCREEN_RECORDING],
        },
        ACCESSIBILITY: {
            "granted": accessibility,
            "required_for": _REQUIRED_FOR[ACCESSIBILITY],
        },
        "all_granted": not missing,
        "host_caveat": HOST_CAVEAT,
    }
    if missing:
        report["fix"] = {name: _FIX[name] for name in missing}
    return report


def build_windows_report(*, elevated: bool) -> dict[str, Any]:
    """Assemble the Windows capability report. Pure and side-effect free.

    Nothing is gated, so both capabilities report granted — qualified by the two
    limits that make actuation silently ineffective. ``elevated`` decides whether
    UIPI is a live constraint for this process or merely context.
    """
    report: dict[str, Any] = {
        "platform": "windows",
        "screen_capture": {
            "granted": True,
            "gate": "none",
            "required_for": _REQUIRED_FOR[SCREEN_RECORDING],
        },
        "input_control": {
            "granted": True,
            "gate": "none",
            "required_for": _REQUIRED_FOR[ACCESSIBILITY],
        },
        "all_granted": True,
        "no_gate": WINDOWS_NO_GATE,
        "elevated": elevated,
        "limits": {
            "uipi": UIPI_LIMIT,
            "secure_desktop": SECURE_DESKTOP_LIMIT,
        },
    }
    report["limits_apply"] = {
        "uipi": not elevated,
        "secure_desktop": True,
    }
    return report


# --- Linux -----------------------------------------------------------------

LINUX_NO_GATE = (
    "X11 has no equivalent of macOS TCC for screen capture or input synthesis, "
    "so there is nothing to grant and no prompt to raise. Any client of this "
    "X session can see the screen and inject input."
)

# Extended by TD-4901: capture and input are separate strategies; see
# docs/wayland-computer-use.md. This string is the product copy.
WAYLAND_LIMIT = (
    "This is a Wayland session. Capture is portal ScreenCast + PipeWire; "
    "input is portal RemoteDesktop / libei (TD-2002, TD-4901a/b). Both must "
    "work before health.supported is true. expect_window never degrades "
    "(TD-4901c). An XWayland DISPLAY is not enough — it would only drive "
    "X11 clients, not native Wayland apps. See docs/wayland-computer-use.md."
)

XTEST_LIMIT = (
    "The XTEST extension is not present on this display. Capture may still "
    "work; mouse and keyboard synthesis will not. A nested or hardened X "
    "server sometimes omits it."
)

NO_DISPLAY_LIMIT = (
    "No X11 display is open (DISPLAY unset or the server refused the "
    "connection). Capture and input are unavailable."
)


def build_linux_report(
    *,
    session: str,
    display: bool,
    xtest: bool,
) -> dict[str, Any]:
    """Assemble the Linux capability report. Pure and side-effect free."""
    wayland = session == "wayland"
    usable = session == "x11" and display and xtest
    limits: dict[str, str] = {}
    limits_apply: dict[str, bool] = {}
    if wayland:
        limits["wayland"] = WAYLAND_LIMIT
        limits_apply["wayland"] = True
    if session == "x11" and not display:
        limits["no_display"] = NO_DISPLAY_LIMIT
        limits_apply["no_display"] = True
    if session == "x11" and display and not xtest:
        limits["xtest"] = XTEST_LIMIT
        limits_apply["xtest"] = True
    return {
        "platform": "linux",
        "session_type": session,
        "screen_capture": {
            "granted": display and not wayland,
            "gate": "none" if session == "x11" else "wayland",
            "required_for": _REQUIRED_FOR[SCREEN_RECORDING],
        },
        "input_control": {
            "granted": usable,
            "gate": "none" if usable else ("wayland" if wayland else "xtest"),
            "required_for": _REQUIRED_FOR[ACCESSIBILITY],
        },
        "all_granted": usable,
        "no_gate": LINUX_NO_GATE,
        "limits": limits,
        "limits_apply": limits_apply,
    }


def check_permissions(*, request: bool = False) -> dict[str, Any]:
    """Report this platform's capture/input situation.

    If ``request`` is true and the platform has prompts to raise, trigger them
    for anything missing and re-probe. On platforms with no gate the flag is
    accepted and ignored.
    """
    from tst_cu_mcp.backends import get_backend

    return get_backend().check_permissions(request=request)
