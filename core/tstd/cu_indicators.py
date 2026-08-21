"""Computer-use glow / cursor prefs and the screenshot hide flag (TD-3402).

Machine-wide, not a workspace file: a clone must not carry someone
else's overlay choice. Same shape as coworker (TD-2905): user data dir,
YAML bools, atomic replace.

Glow and the agent cursor are drawn on the Screen pane. ``show_on_real_display``
is the contract for a later host/sidecar software overlay — this module
never moves the hardware pointer. When that toggle is on, screenshot
tools raise ``real_display_overlay_hidden`` for the duration of the
capture so an overlay cannot paint into the frame.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CuIndicatorPrefs:
    """The three Settings bits. Glow and cursor default on; real display off."""

    glow: bool = True
    agent_cursor: bool = True
    show_on_real_display: bool = False


_DEFAULTS = CuIndicatorPrefs()
_current = _DEFAULTS
_real_display_hidden = False


def cu_indicators_path(data_dir: str | Path) -> Path:
    """Path of the user-data file that holds the three indicator bits."""
    return Path(data_dir) / "cu-indicators.yaml"


def _as_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    if key not in data:
        return default
    return data[key] is True


def load_cu_indicators(data_dir: str | Path) -> CuIndicatorPrefs:
    """Load prefs. Absent, empty, or unreadable is the defaults."""
    path = cu_indicators_path(data_dir)
    if not path.exists():
        return _DEFAULTS
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _DEFAULTS
    if raw is None or not isinstance(raw, dict):
        return _DEFAULTS
    return CuIndicatorPrefs(
        glow=_as_bool(raw, "glow", True),
        agent_cursor=_as_bool(raw, "agent_cursor", True),
        show_on_real_display=_as_bool(raw, "show_on_real_display", False),
    )


def save_cu_indicators(data_dir: str | Path, prefs: CuIndicatorPrefs) -> None:
    """Persist prefs atomically in the user data dir."""
    path = cu_indicators_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cu-indicators.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {
                    "glow": prefs.glow,
                    "agent_cursor": prefs.agent_cursor,
                    "show_on_real_display": prefs.show_on_real_display,
                },
                f,
                sort_keys=False,
            )
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def current_prefs() -> CuIndicatorPrefs:
    """Process-wide prefs the screenshot hide path reads."""
    return _current


def set_current_prefs(prefs: CuIndicatorPrefs) -> None:
    """Install prefs so screenshot tools see the latest toggle."""
    global _current
    _current = prefs


def real_display_overlay_hidden() -> bool:
    """True while a screenshot is capturing and the real-display overlay is on.

    The Tauri host path is a no-op in TD-3402 (see
    ``notify_host_real_display_overlay``). A later overlay must hide when
    this is True. This never moves the OS pointer.
    """
    return _real_display_hidden


def notify_host_real_display_overlay(*, hidden: bool) -> None:
    """No-op host hook (TD-3402).

    Glow and the agent cursor live on the Screen pane. A future host
    overlay on the real display should honor this notify — or
    ``real_display_overlay_hidden`` — and hide for the duration of
    ``desktop_screenshot`` / ``browser_screenshot``.
    """
    del hidden


def reset_cu_indicators() -> None:
    """Restore defaults. Tests only."""
    global _current, _real_display_hidden
    _current = _DEFAULTS
    _real_display_hidden = False


@contextmanager
def hide_real_display_for_screenshot() -> Iterator[None]:
    """Hide the real-display overlay for one screenshot, then restore it.

    No-op when ``show_on_real_display`` is off — there is nothing to hide.
    """
    global _real_display_hidden
    if not current_prefs().show_on_real_display:
        yield
        return
    _real_display_hidden = True
    notify_host_real_display_overlay(hidden=True)
    try:
        yield
    finally:
        _real_display_hidden = False
        notify_host_real_display_overlay(hidden=False)
