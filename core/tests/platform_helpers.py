"""Platform-specific test expectations (see docs/windows.md)."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest

from tstd.config import cached_config
from tstd.daemon import Daemon

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


def isolate_user_data_env(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Keep user config and data dir off the developer machine (docs/windows.md).

    ``user_data_dir()`` on Windows follows ``%APPDATA%``, not ``HOME``; tests
    that only repoint ``HOME`` still read and write the real profile unless
    these variables move with it.
    """
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if sys.platform == "win32":
        profile = root / "win-profile"
        roaming = profile / "AppData" / "Roaming"
        local = profile / "AppData" / "Local"
        roaming.mkdir(parents=True, exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("USERPROFILE", str(profile))
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.setenv("LOCALAPPDATA", str(local))
    cached_config.cache_clear()


def path_endswith(path: str, suffix: str) -> bool:
    """Compare path suffixes portably (forward slashes in test literals)."""
    return Path(path).as_posix().endswith(suffix.replace("\\", "/"))


_SCHED_FIXTURE_ROOT = Path(__file__).resolve().parent / "_sched_fixture_ws"


def scheduler_fixture_ws(name: str = "ws") -> Path:
    """Absolute workspace path for scheduler store/runner tests on every OS."""
    path = (_SCHED_FIXTURE_ROOT / name).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


async def stop_daemon_gracefully(daemon: Daemon, task: asyncio.Task[Any]) -> None:
    """Shut down an in-process daemon without leaving audit.db open on Windows."""
    daemon._shutdown_event.set()
    timeout = 15.0 if sys.platform == "win32" else 5.0
    if task.done():
        if daemon._audit_writer is not None:
            await daemon._audit_writer.close()
            daemon._audit_writer = None
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        if daemon._audit_writer is not None:
            await daemon._audit_writer.close()
            daemon._audit_writer = None
