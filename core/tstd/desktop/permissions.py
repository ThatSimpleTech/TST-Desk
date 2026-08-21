"""macOS computer-use permission onboarding (TD-3302).

The first-run flag lives in the user data dir, not the workspace — same
shape as ``close-is-not-quit.yaml``. Probes never raise a TCC prompt
(``request=True`` is forbidden here).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..protocol import CuPermissions

# Current macOS 15+ / Tahoe deep links (``x-apple.systemsettings``).
# Older ``x-apple.systempreferences:com.apple.preference.security?Privacy_*``
# aliases still resolve on some builds; these are the current pane IDs.
SCREEN_RECORDING_URL = "x-apple.systemsettings:com.apple.preferences.privacy-security.ScreenCapture"
ACCESSIBILITY_URL = "x-apple.systemsettings:com.apple.preferences.privacy-security.accessibility"

FLAG_NAME = "cu-macos-permissions.yaml"

_PERMISSION_MARKERS = (
    "screen recording",
    "accessibility",
    "tcc",
)


def flag_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that records the first-run explanation."""
    return Path(data_dir) / FLAG_NAME


def load_shown(data_dir: str | Path) -> bool:
    """True when the first-run explanation has already been emitted."""
    path = flag_path(data_dir)
    if not path.exists():
        return False
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and data.get("shown") is True


def mark_shown(data_dir: str | Path) -> None:
    """Stamp the first-run flag atomically in the user data dir."""
    path = flag_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cu-macos-permissions.", suffix=".tmp")
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


def build_cu_permissions(
    *,
    screen_recording: bool,
    accessibility: bool,
    first_run: bool,
) -> CuPermissions:
    """Connection-scoped report. macOS copy is used on darwin and the mock."""
    return CuPermissions(
        granted=screen_recording and accessibility,
        screen_recording=screen_recording,
        accessibility=accessibility,
        screen_recording_url=SCREEN_RECORDING_URL,
        accessibility_url=ACCESSIBILITY_URL,
        first_run=first_run,
        platform="macos",
    )


def parse_mcp_permissions_result(result: Any) -> dict[str, Any]:
    """Pull a ``check_permissions`` report out of an MCP ``tools/call`` result."""
    if isinstance(result, dict) and (
        "screen_recording" in result or "all_granted" in result or "accessibility" in result
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


async def probe_driver(driver: Any) -> tuple[bool, bool]:
    """Ask *driver* for TCC status. Never prompts. Missing probe is denied."""
    check = getattr(driver, "check_permissions", None)
    if check is None:
        return False, False
    result = await check()
    return parse_probe(result)
