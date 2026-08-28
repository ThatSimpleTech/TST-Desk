"""Linux computer-use capability report (TD-2001).

X11 has no TCC analog. Wayland is a named refusal (TD-2002). Copy matches
``tst_cu_mcp.permissions.build_linux_report`` so the live sidecar and the
mock stay on one wording.
"""

from __future__ import annotations

from typing import Any

from ..protocol import CuPermissions

LINUX_FLAG_NAME = "cu-linux-permissions.yaml"

LINUX_NO_GATE = (
    "X11 has no equivalent of macOS TCC for screen capture or input synthesis, "
    "so there is nothing to grant and no prompt to raise. Any client of this "
    "X session can see the screen and inject input."
)

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


def linux_report(
    *,
    session: str = "x11",
    display: bool = True,
    xtest: bool = True,
) -> dict[str, Any]:
    """``build_linux_report`` shape for the mock. Live path uses the sidecar."""
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
            "required_for": "capturing the screen (screenshot / vision)",
        },
        "input_control": {
            "granted": usable,
            "gate": "none" if usable else ("wayland" if wayland else "xtest"),
            "required_for": "controlling the mouse and keyboard",
        },
        "all_granted": usable,
        "no_gate": LINUX_NO_GATE,
        "limits": limits,
        "limits_apply": limits_apply,
    }


def build_linux_cu_permissions(
    raw: dict[str, Any],
    *,
    first_run: bool,
) -> CuPermissions:
    """Map a ``build_linux_report`` payload onto the protocol event."""
    from .permissions import parse_probe

    screen, access = parse_probe(raw)
    all_granted = raw.get("all_granted")
    granted = all_granted if isinstance(all_granted, bool) else screen and access
    limits = raw.get("limits")
    apply = raw.get("limits_apply")
    limits_map = limits if isinstance(limits, dict) else {}
    apply_map = apply if isinstance(apply, dict) else {}
    session = raw.get("session_type")
    no_gate = raw.get("no_gate")
    wayland = limits_map.get("wayland")
    xtest = limits_map.get("xtest")
    no_display = limits_map.get("no_display")
    return CuPermissions(
        granted=granted,
        screen_recording=screen,
        accessibility=access,
        screen_recording_url="",
        accessibility_url="",
        first_run=first_run,
        platform="linux",
        no_gate=no_gate if isinstance(no_gate, str) and no_gate else LINUX_NO_GATE,
        session_type=session if isinstance(session, str) else "",
        wayland=wayland if isinstance(wayland, str) else "",
        wayland_applies=(
            bool(apply_map["wayland"]) if isinstance(apply_map.get("wayland"), bool) else False
        ),
        xtest=xtest if isinstance(xtest, str) else "",
        xtest_applies=(
            bool(apply_map["xtest"]) if isinstance(apply_map.get("xtest"), bool) else False
        ),
        no_display=no_display if isinstance(no_display, str) else "",
        no_display_applies=(
            bool(apply_map["no_display"])
            if isinstance(apply_map.get("no_display"), bool)
            else False
        ),
    )
