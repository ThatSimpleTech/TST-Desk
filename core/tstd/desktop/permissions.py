"""Computer-use permission / integrity onboarding (TD-3302, TD-3303).

macOS first-run is a TCC explanation. Windows first-run is honesty: there
is no grant dialog, and two silent failure modes (UIPI, secure desktop).
Linux X11 is the same kind of honesty; Wayland / missing XTEST are named
limits (TD-2001).

The first-run flag lives in the user data dir, not the workspace — same
shape as ``close-is-not-quit.yaml``. Probes never raise a TCC prompt
(``request=True`` is forbidden here). Windows and X11 have nothing to
prompt.

Windows and Linux copy match the sidecar report builders so the live
path and the mock stay on one wording. The daemon parses that report; it
does not grow a second backend.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml

from ..protocol import CuPermissions
from .permissions_linux import LINUX_FLAG_NAME, build_linux_cu_permissions

CuPlatform = Literal["macos", "windows", "linux"]

# Current macOS 15+ / Tahoe deep links (``x-apple.systemsettings``).
# Older ``x-apple.systempreferences:com.apple.preference.security?Privacy_*``
# aliases still resolve on some builds; these are the current pane IDs.
SCREEN_RECORDING_URL = "x-apple.systemsettings:com.apple.preferences.privacy-security.ScreenCapture"
ACCESSIBILITY_URL = "x-apple.systemsettings:com.apple.preferences.privacy-security.accessibility"

FLAG_NAME = "cu-macos-permissions.yaml"
WINDOWS_FLAG_NAME = "cu-windows-permissions.yaml"

_FLAG_FILES: dict[CuPlatform, str] = {
    "macos": FLAG_NAME,
    "windows": WINDOWS_FLAG_NAME,
    "linux": LINUX_FLAG_NAME,
}
_FLAG_PREFIXES: dict[CuPlatform, str] = {
    "macos": ".cu-macos-permissions.",
    "windows": ".cu-windows-permissions.",
    "linux": ".cu-linux-permissions.",
}

_PERMISSION_MARKERS = (
    "screen recording",
    "accessibility",
    "tcc",
)
_UIPI_MARKERS = (
    "uipi",
    "higher integrity",
    "integrity level",
)
_SECURE_DESKTOP_MARKERS = (
    "secure desktop",
    "uac consent",
)

# Same wording as ``tst_cu_mcp.permissions`` — the sidecar report is the
# live source; these are the mock / missing-probe fallback.
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


def normalize_cu_platform(raw: str | None) -> CuPlatform:
    """Map ``sys.platform`` / report labels onto the protocol platform."""
    if raw in ("win32", "windows"):
        return "windows"
    if raw is not None and (raw == "linux" or raw.startswith("linux")):
        return "linux"
    return "macos"


def driver_cu_platform(driver: Any) -> CuPlatform:
    """Platform the driver will report. Mock defaults to macOS on every host."""
    raw = getattr(driver, "platform", None)
    if raw is None:
        raw = getattr(driver, "_platform", None)
    return normalize_cu_platform(raw if isinstance(raw, str) else None)


def flag_name(platform: str = "macos") -> str:
    """User-data filename for this platform's first-run stamp."""
    return _FLAG_FILES[normalize_cu_platform(platform)]


def flag_path(data_dir: str | Path, platform: str = "macos") -> Path:
    """Path of the user-data file that records the first-run explanation."""
    return Path(data_dir) / flag_name(platform)


def load_shown(data_dir: str | Path, platform: str = "macos") -> bool:
    """True when the first-run explanation has already been emitted."""
    path = flag_path(data_dir, platform)
    if not path.exists():
        return False
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and data.get("shown") is True


def mark_shown(data_dir: str | Path, platform: str = "macos") -> None:
    """Stamp the first-run flag atomically in the user data dir."""
    path = flag_path(data_dir, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = _FLAG_PREFIXES[normalize_cu_platform(platform)]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump({"shown": True}, f, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def is_permission_failure(message: str) -> bool:
    """True when a sidecar error names a TCC gate (Screen Recording / Accessibility)."""
    lower = message.casefold()
    return any(needle in lower for needle in _PERMISSION_MARKERS)


def is_uipi_failure(message: str) -> bool:
    """True when a sidecar error names UIPI / a higher-integrity target."""
    lower = message.casefold()
    return any(needle in lower for needle in _UIPI_MARKERS)


def is_secure_desktop_failure(message: str) -> bool:
    """True when a sidecar error names the secure desktop / UAC consent surface."""
    lower = message.casefold()
    return any(needle in lower for needle in _SECURE_DESKTOP_MARKERS)


def is_desktop_tool(name: str) -> bool:
    """True for the five desktop computer-use tools."""
    return name.startswith("desktop_")


def parse_probe(raw: Any) -> tuple[bool, bool]:
    """Extract ``(screen_recording, accessibility)`` from a sidecar or mock report."""
    if not isinstance(raw, dict):
        return False, False

    def _granted(*keys: str) -> bool | None:
        for key in keys:
            val = raw.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, dict) and isinstance(val.get("granted"), bool):
                return bool(val["granted"])
        return None

    screen = _granted("screen_recording", "screen_capture")
    access = _granted("accessibility", "input_control")
    if screen is None and access is None:
        all_granted = raw.get("all_granted")
        if isinstance(all_granted, bool):
            return all_granted, all_granted
        return False, False
    return bool(screen), bool(access)


def cu_platform_from_report(raw: dict[str, Any]) -> CuPlatform:
    """Read the report's platform.

    Linux and Windows both send ``no_gate`` / ``limits``. The platform
    label wins; a ``session_type`` is Linux; otherwise those keys are the
    Windows shape. Never map a Linux report onto Windows UIPI copy.
    """
    raw_platform = raw.get("platform")
    if isinstance(raw_platform, str):
        if raw_platform in ("win32", "windows"):
            return "windows"
        if raw_platform == "linux" or raw_platform.startswith("linux"):
            return "linux"
        if raw_platform in ("darwin", "macos"):
            return "macos"
    session = raw.get("session_type")
    if isinstance(session, str) and session:
        return "linux"
    if "no_gate" in raw or "limits" in raw:
        return "windows"
    return "macos"


def windows_report(*, elevated: bool) -> dict[str, Any]:
    """``build_windows_report`` shape for the mock. Live path uses the sidecar."""
    return {
        "platform": "windows",
        "screen_capture": {
            "granted": True,
            "gate": "none",
            "required_for": "capturing the screen (screenshot / vision)",
        },
        "input_control": {
            "granted": True,
            "gate": "none",
            "required_for": "controlling the mouse and keyboard",
        },
        "all_granted": True,
        "no_gate": WINDOWS_NO_GATE,
        "elevated": elevated,
        "limits": {
            "uipi": UIPI_LIMIT,
            "secure_desktop": SECURE_DESKTOP_LIMIT,
        },
        "limits_apply": {
            "uipi": not elevated,
            "secure_desktop": True,
        },
    }


_ACTUATION_PATHS = ("host", "daemon", "mock", "none")
_SIGNINGS = ("identity", "adhoc", "unsigned")


def host_diagnosis(raw: dict[str, Any]) -> dict[str, Any]:
    """The TD-4823 fields of a host / sidecar macOS report, as event kwargs.

    Only the host (``cu-agent.sock``) fills these in; a mock or an older
    sidecar report leaves every one at its default, so the event is
    unchanged for them. Unknown values normalize to the empty default —
    the event's ``Literal`` fields must never reject a live report.
    """
    identity = raw.get("identity")
    ident = identity if isinstance(identity, dict) else {}
    stale = raw.get("stale_grant_suspected")
    stale_map = stale if isinstance(stale, dict) else {}
    fix = raw.get("fix")
    fix_map = fix if isinstance(fix, dict) else {}
    path = raw.get("actuation_path")
    signing = ident.get("signing")

    def _text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    return {
        "actuation_path": path if path in _ACTUATION_PATHS else "",
        "signing": signing if signing in _SIGNINGS else "",
        "bundle_path": _text(ident.get("bundle_path")),
        "stale_screen_recording": stale_map.get("screen_recording") is True,
        "stale_accessibility": stale_map.get("accessibility") is True,
        "unbundled_dev_binary": raw.get("unbundled_dev_binary") is True,
        "fix_screen_recording": _text(fix_map.get("screen_recording")),
        "fix_accessibility": _text(fix_map.get("accessibility")),
        "reset_supported": raw.get("reset_supported") is True,
        "reset_error": _text(raw.get("reset_error")),
    }


def build_cu_permissions(
    *,
    screen_recording: bool,
    accessibility: bool,
    first_run: bool,
    **host: Any,
) -> CuPermissions:
    """Connection-scoped macOS report. Used on darwin and the default mock.

    ``host`` is :func:`host_diagnosis` output; omitted for the mock.
    """
    return CuPermissions(
        granted=screen_recording and accessibility,
        screen_recording=screen_recording,
        accessibility=accessibility,
        screen_recording_url=SCREEN_RECORDING_URL,
        accessibility_url=ACCESSIBILITY_URL,
        first_run=first_run,
        platform="macos",
        **host,
    )


def build_windows_cu_permissions(
    raw: dict[str, Any],
    *,
    first_run: bool,
) -> CuPermissions:
    """Map a ``build_windows_report`` payload onto the protocol event."""
    limits = raw.get("limits")
    apply = raw.get("limits_apply")
    limits_map = limits if isinstance(limits, dict) else {}
    apply_map = apply if isinstance(apply, dict) else {}
    elevated = bool(raw.get("elevated", False))
    no_gate = raw.get("no_gate")
    uipi = limits_map.get("uipi")
    secure = limits_map.get("secure_desktop")
    return CuPermissions(
        granted=True,
        screen_recording=True,
        accessibility=True,
        screen_recording_url="",
        accessibility_url="",
        first_run=first_run,
        platform="windows",
        no_gate=no_gate if isinstance(no_gate, str) and no_gate else WINDOWS_NO_GATE,
        uipi=uipi if isinstance(uipi, str) and uipi else UIPI_LIMIT,
        secure_desktop=secure if isinstance(secure, str) and secure else SECURE_DESKTOP_LIMIT,
        elevated=elevated,
        uipi_applies=(
            bool(apply_map["uipi"]) if isinstance(apply_map.get("uipi"), bool) else not elevated
        ),
        secure_desktop_applies=(
            bool(apply_map["secure_desktop"])
            if isinstance(apply_map.get("secure_desktop"), bool)
            else True
        ),
    )


def cu_permissions_from_report(raw: dict[str, Any], *, first_run: bool) -> CuPermissions:
    """Build the event the UI already knows, for the report's platform."""
    platform = cu_platform_from_report(raw)
    if platform == "windows":
        return build_windows_cu_permissions(raw, first_run=first_run)
    if platform == "linux":
        return build_linux_cu_permissions(raw, first_run=first_run)
    screen, access = parse_probe(raw)
    return build_cu_permissions(
        screen_recording=screen,
        accessibility=access,
        first_run=first_run,
        **host_diagnosis(raw),
    )


def parse_mcp_permissions_result(result: Any) -> dict[str, Any]:
    """Pull a ``check_permissions`` report out of an MCP ``tools/call`` result."""
    if isinstance(result, dict) and (
        "screen_recording" in result
        or "all_granted" in result
        or "accessibility" in result
        or result.get("platform") in ("windows", "linux")
        or "limits" in result
        or "no_gate" in result
        or "session_type" in result
    ):
        return result
    content: list[Any] = []
    if isinstance(result, dict):
        raw = result.get("content")
        if isinstance(raw, list):
            content = raw
    elif isinstance(result, list):
        content = result
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text", ""))
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"all_granted": False}


async def probe_report(driver: Any) -> dict[str, Any]:
    """Ask *driver* for capture/input status. Never prompts. Missing probe is denied."""
    check = getattr(driver, "check_permissions", None)
    if check is None:
        return {"all_granted": False}
    result = await check()
    if isinstance(result, dict):
        return result
    return parse_mcp_permissions_result(result)


async def probe_driver(driver: Any) -> tuple[bool, bool]:
    """Ask *driver* for TCC status. Never prompts. Missing probe is denied."""
    return parse_probe(await probe_report(driver))
