"""Platform-specific test expectations (see docs/windows.md)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

OWNER_ONLY_MODE = 0o666 if sys.platform == "win32" else 0o600
OWNER_ONLY_OCT_SUFFIX = "666" if sys.platform == "win32" else "600"

skip_posix_file_modes = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows chmod is a no-op; directory ACL is the boundary (docs/windows.md)",
)


def assert_owner_only_mode(mode: int) -> None:
    assert mode == OWNER_ONLY_MODE, f"expected {oct(OWNER_ONLY_MODE)}, got {oct(mode)}"


def assert_owner_only_path(path: Path) -> None:
    assert_owner_only_mode(path.stat().st_mode & 0o777)


def assert_owner_only_oct_suffix(path: Path) -> None:
    assert oct(path.stat().st_mode)[-3:] == OWNER_ONLY_OCT_SUFFIX


def outside_workspace_path() -> str:
    """A path the session wall must refuse — platform-specific."""
    if sys.platform == "win32":
        return r"C:\Windows\System32\drivers\etc\hosts"
    return "/etc/passwd"
